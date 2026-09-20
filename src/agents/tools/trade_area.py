from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_input_path_or_none, tool_output_path

from ._isochrones import MODE_NETWORKS, isochrone_polygons
from ._osm_tags import (
    OSM_TAG_MAP,
    features_matching_tags,
    known_category,
    merge_tags,
    tags_for_category,
)
from ._population import population_inside
from ._sites import resolve_sites


class TradeAreaArgs(BaseModel):
    sites: Optional[str] = Field(
        None,
        description=(
            "Site names to evaluate, each geocoded, separated by semicolons and not "
            "commas: 'Shoreditch London; Brixton London'. A 'lat, lon' pair is taken "
            "as coordinates. Give this or sites_path, not both."
        ),
    )
    sites_path: Optional[str] = Field(
        None,
        description=(
            "The sites as a layer instead, a filename in outputs/ or user_data/ and "
            "not a path. Points or polygons, one trade area per feature. Use this "
            "when the user uploaded or exported their candidate sites, and call "
            "list_user_datasets to learn the filename."
        ),
    )
    name_column: Optional[str] = Field(
        None,
        description=(
            "Column of sites_path holding each site's name. Tries name, site, label "
            "and address when omitted."
        ),
    )
    travel_mode: str = Field(
        "driving",
        description="'driving', 'walking', or 'cycling'.",
    )
    minutes: int = Field(
        10,
        description="Travel time in minutes, one band per site.",
    )
    competitors: Optional[str] = Field(
        None,
        description=(
            "What counts as a competitor: an OSM category in download_osm_data "
            "syntax such as 'supermarkets' or 'shop=bakery', or a filename in "
            "outputs/ or user_data/ holding the user's own competitor features."
        ),
    )
    anchors: str = Field(
        "supermarkets,transit",
        description=(
            "Comma-separated OSM categories counted as traffic generators, one "
            "anchor_<category> column each. Pass an empty string to count none."
        ),
    )
    demographics_path: Optional[str] = Field(
        None,
        description=(
            "Polygon layer with numeric columns, e.g. census areas, a filename in "
            "outputs/ or user_data/ and not a path."
        ),
    )
    demographics_columns: Optional[str] = Field(
        None,
        description=(
            "Which columns of demographics_path to summarise, as "
            "'population:sum,median_income:mean'. sum is area-weighted by the share "
            "of each area inside the trade area, mean is weighted by the "
            "intersecting area. Required with demographics_path."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


DEMOGRAPHICS_AGGREGATIONS = ("sum", "mean")
SQUARE_METRES_PER_SQUARE_KM = 1e6
TABLE_COLUMN_GAP = "  "


def _count_intersecting(frame, polygon):
    if frame is None:
        return None
    return int(frame.geometry.intersects(polygon).sum())


def _demographic_figures(areas_metric, metric_crs, polygon, wanted):
    import geopandas as gpd
    import pandas as pd

    trade_area_metric = (
        gpd.GeoSeries([polygon], crs="EPSG:4326").to_crs(metric_crs).iloc[0]
    )
    touching = areas_metric[areas_metric.geometry.intersects(trade_area_metric)]
    if touching.empty:
        return {column: None for column, _ in wanted}

    overlap = touching.geometry.intersection(trade_area_metric).area
    source_area = touching.geometry.area
    share = (overlap / source_area).where(source_area > 0, 0.0)
    overlap_total = float(overlap.sum())

    figures = {}
    for column, aggregation in wanted:
        values = pd.to_numeric(touching[column], errors="coerce")
        if aggregation == "sum":
            figures[column] = float((values * share).sum())
        elif overlap_total > 0:
            figures[column] = float((values * overlap).sum() / overlap_total)
        else:
            figures[column] = None
    return figures


def _cell(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and not float(value).is_integer():
        return f"{value:,.2f}"
    return f"{int(value):,}"


def trade_area(
    sites: str = None,
    sites_path: str = None,
    name_column: str = None,
    travel_mode: str = "driving",
    minutes: int = 10,
    competitors: str = None,
    anchors: str = "supermarkets,transit",
    demographics_path: str = None,
    demographics_columns: str = None,
    output_filename: str = None,
) -> str:
    """
    Trade areas around candidate sites: one travel-time catchment per site
    carrying the population, the competitor count and an anchor count for each
    traffic generator inside it, plus figures from a census layer. Use this for
    how many people and competitors fall within N minutes of each site, rather
    than chaining isochrones, population and OSM downloads per site.
    """
    import traceback

    try:
        import geopandas as gpd
        import osmnx as ox
        import pandas as pd

        mode = travel_mode.lower().strip()
        if mode not in MODE_NETWORKS:
            return (
                f"Unknown travel_mode '{travel_mode}'. "
                f"Choose: {', '.join(MODE_NETWORKS)}."
            )

        site_list = resolve_sites(sites, sites_path, name_column)
        if isinstance(site_list, str):
            return site_list

        anchor_categories = [
            part.strip().lower() for part in anchors.split(",") if part.strip()
        ]
        unknown_anchors = [
            category for category in anchor_categories if not known_category(category)
        ]
        if unknown_anchors:
            return (
                f"anchors names no such OSM category: {', '.join(unknown_anchors)}. "
                f"Known categories: {', '.join(sorted(OSM_TAG_MAP))}. "
                "Or use key=value syntax like 'shop=bakery'."
            )

        competitor_frame = None
        competitor_tags = None
        if competitors:
            competitor_file = tool_input_path_or_none("competitors", competitors)
            if competitor_file:
                competitor_frame = gpd.read_file(competitor_file)
                if competitor_frame.crs is None:
                    competitor_frame = competitor_frame.set_crs("EPSG:4326")
                competitor_frame = competitor_frame.to_crs("EPSG:4326")
            elif known_category(competitors):
                competitor_tags = tags_for_category(competitors)
            else:
                return (
                    f"competitors '{competitors}' is neither a file in your outputs "
                    f"or user_data nor a known OSM category. Known categories: "
                    f"{', '.join(sorted(OSM_TAG_MAP))}. Or use key=value syntax like "
                    "'shop=bakery'."
                )

        if demographics_columns and not demographics_path:
            return (
                "demographics_columns needs demographics_path, the layer those "
                "columns are read from."
            )

        wanted_figures = []
        demographics_metric = None
        demographics_crs = None
        if demographics_path:
            if not demographics_columns:
                return (
                    "demographics_columns is required with demographics_path, e.g. "
                    "'population:sum,median_income:mean'."
                )
            for item in (part.strip() for part in demographics_columns.split(",")):
                if not item:
                    continue
                column, _, aggregation = item.partition(":")
                aggregation = aggregation.strip().lower()
                if aggregation not in DEMOGRAPHICS_AGGREGATIONS:
                    return (
                        f"'{item}' names no aggregation. Write column:sum for an "
                        "area-weighted total or column:mean for an area-weighted mean."
                    )
                wanted_figures.append((column.strip(), aggregation))
            if not wanted_figures:
                return "demographics_columns named no columns, e.g. 'population:sum'."

            demographics = gpd.read_file(
                tool_input_path("demographics_path", demographics_path)
            )
            absent = [
                column
                for column, _ in wanted_figures
                if column not in demographics.columns
            ]
            if absent:
                carried = [c for c in demographics.columns if c != "geometry"]
                return (
                    f"demographics_path has no column {', '.join(absent)}. "
                    f"It carries: {', '.join(carried) or 'no attribute columns'}."
                )
            if demographics.crs is None:
                demographics = demographics.set_crs("EPSG:4326")
            demographics_crs = demographics.estimate_utm_crs()
            demographics_metric = demographics.to_crs(demographics_crs)

        bands = []
        skipped = []
        for site in site_list:
            rings = isochrone_polygons(
                site["lat"], site["lon"], mode, [minutes], "auto"
            )
            if isinstance(rings, str):
                return rings
            if not rings:
                skipped.append(site["name"])
                continue
            bands.append((site, rings[0]))

        if not bands:
            return (
                f"No {mode} trade area came back for any site "
                f"({', '.join(skipped)}), so nothing was saved."
            )

        query_tags = merge_tags(
            [tags_for_category(category) for category in anchor_categories]
            + ([competitor_tags] if competitor_tags else [])
        )
        osm_features = None
        if query_tags:
            # one Overpass call for every trade area at once, counted per band below
            search_area = (
                gpd.GeoSeries([band["geometry"] for _, band in bands], crs="EPSG:4326")
                .union_all()
                .convex_hull
            )
            try:
                osm_features = ox.features_from_polygon(search_area, tags=query_tags)
            except Exception:
                osm_features = None

        anchor_subsets = {}
        competitor_subset = None
        if osm_features is not None:
            for category in anchor_categories:
                anchor_subsets[category] = features_matching_tags(
                    osm_features, tags_for_category(category)
                )
            if competitor_tags:
                competitor_subset = features_matching_tags(
                    osm_features, competitor_tags
                )
        competitor_source = (
            competitor_frame if competitor_frame is not None else competitor_subset
        )

        rows = []
        geometries = []
        for site, band in bands:
            polygon = band["geometry"]
            area_series = gpd.GeoSeries([polygon], crs="EPSG:4326")
            metric_area = area_series.to_crs(area_series.estimate_utm_crs()).area.iloc[0]
            population, population_source = population_inside(polygon)

            row = {
                "site": site["name"],
                "lat": round(site["lat"], 6),
                "lon": round(site["lon"], 6),
                "minutes": band["minutes"],
                "mode": band["mode"],
                "area_km2": round(metric_area / SQUARE_METRES_PER_SQUARE_KM, 2),
                "population": population,
                "population_source": population_source,
                "competitors": _count_intersecting(competitor_source, polygon),
            }
            for category in anchor_categories:
                row[f"anchor_{category}"] = _count_intersecting(
                    anchor_subsets.get(category), polygon
                )
            if demographics_metric is not None:
                row.update(
                    _demographic_figures(
                        demographics_metric, demographics_crs, polygon, wanted_figures
                    )
                )
            rows.append(row)
            geometries.append(polygon)

        frame = gpd.GeoDataFrame(rows, geometry=geometries, crs="EPSG:4326")
        figure_columns = (
            ["population", "competitors"]
            + [f"anchor_{category}" for category in anchor_categories]
            + [column for column, _ in wanted_figures]
        )
        for column in figure_columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

        if not output_filename:
            output_filename = "trade_areas"
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = tool_output_path("output_filename", f"{output_filename}.gpkg")
        frame.to_file(output_path, driver="GPKG")

        table_columns = ["minutes", "area_km2"] + figure_columns
        cells = [
            {column: _cell(row[column]) for column in table_columns} for row in rows
        ]
        widths = {
            column: max([len(column)] + [len(cell[column]) for cell in cells])
            for column in table_columns
        }
        name_width = max([len("site")] + [len(str(row["site"])) for row in rows])
        table = [
            "site".ljust(name_width)
            + "".join(
                TABLE_COLUMN_GAP + column.rjust(widths[column])
                for column in table_columns
            )
        ]
        for row, cell in zip(rows, cells):
            table.append(
                str(row["site"]).ljust(name_width)
                + "".join(
                    TABLE_COLUMN_GAP + cell[column].rjust(widths[column])
                    for column in table_columns
                )
            )

        counted = len(rows)
        skipped_note = (
            f" No band came back for {', '.join(skipped)}, left out of the layer."
            if skipped
            else ""
        )
        summary = (
            f"Trade areas for {counted} site{'' if counted == 1 else 's'}: "
            f"{minutes} minute {mode} bands with population, competitors and anchors "
            f"counted inside each. Saved to outputs/{output_filename}.gpkg."
            f"{skipped_note}"
        )
        closing = (
            "Call emit_ui_spec with ui_type='map' and shade it by passing "
            "'population' as the fourth part of the layer entry: "
            f"'Trade areas|outputs/{output_filename}.gpkg|#ff6b35|population'."
        )
        return "\n".join([summary, ""] + table + ["", closing])

    except Exception as e:
        return f"Trade area analysis failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = trade_area
TOOL_SCHEMA = TradeAreaArgs
