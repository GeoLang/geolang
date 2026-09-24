from pydantic import BaseModel, Field
from typing import Optional
from src.core.place_lookup import (
    first_outlined_hit,
    geocode,
    geocode_point,
    osm_geometry,
    place_not_found,
)
from src.core.utils import tool_output_path

from ._osm_tags import OSM_TAG_KEYS, OSM_TAG_MAP, tags_for_category

NAMED_FEATURE_CANDIDATES = 10
PLACE_CANDIDATES = 5

DEFAULT_RADIUS_M = 1000
# road networks need more reach than point features before the graph connects up
DEFAULT_ROADS_RADIUS_M = 2000
ADDRESS_FALLBACK_RADIUS_M = 2000
# a larger area's Overpass response OOMs the 4 GiB executor
MAX_PLACE_AREA_KM2 = 50


class DownloadOSMDataArgs(BaseModel):
    place_name: Optional[str] = Field(
        None,
        description=(
            "Area to search, best as a neighbourhood or city, e.g. 'Marylebone, "
            "London'. Also accepts 'lat,lon' or a street address, which searches a "
            "radius. Leave blank when using feature_name."
        ),
    )
    feature_name: Optional[str] = Field(
        None,
        description=(
            "One named OSM feature fetched whole, ignoring administrative "
            "boundaries, e.g. 'River Thames', 'M25'. Prefer this whenever the user "
            "names the thing itself rather than an area to search: a place_name "
            "query cuts such a feature off at the edge of the place."
        ),
    )
    data_type: str = Field(
        ...,
        description=(
            "What to download: buildings, roads, schools, hospitals, parks, "
            "restaurants, shops, supermarkets, pharmacies, amenities, parking, "
            "bus_stops, or OSM tag syntax like 'amenity=cafe'. With feature_name it "
            "only picks between same-named features."
        ),
    )
    radius_m: Optional[int] = Field(
        None,
        description=(
            "Search radius in metres around the centre of place_name, default "
            "1000 and 2000 for roads. When given, the place boundary is skipped, "
            "so pass it to stay within one district of a large city."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def _named_feature_gdf(feature_name: str, tags: dict):
    """Fetch one named OSM feature's own geometry by the id the platform geocoder gives.

    Returns (GeoDataFrame, description) or (None, error message). The geometry is
    the feature as OSM defines it, so a river arrives whole instead of clipped to
    whichever place was searched.
    """
    import geopandas as gpd

    hits = [
        hit
        for hit in geocode(feature_name, limit=NAMED_FEATURE_CANDIDATES)
        if hit["osm_type"] and hit["osm_id"] is not None
    ]
    if not hits:
        return None, f"No OSM feature named '{feature_name}' found."

    # prefer a hit matching the requested tag, so "River Thames" as waterway=river
    # cannot land on a pub or a footpath of the same name
    key, value = next(iter(tags.items()), (None, None))
    chosen = next(
        (
            h
            for h in hits
            if h["osm_key"] == key and (value is True or h["osm_value"] == value)
        ),
        None,
    )
    if chosen is None:
        # no fallback to the first hit: geocoding a word that is not really a name
        # lands on whatever happens to carry it, and a wrong layer drawn without
        # complaint is worse than no layer
        found = ", ".join(
            f"{h['display_name'].split(',')[0]} ({h['osm_key']}={h['osm_value']})"
            for h in hits[:3]
        )
        return None, (
            f"No OSM feature named '{feature_name}' is a {key}={value}. "
            f"The platform geocoder offered: {found}. "
            "If you meant every such feature in an area rather than one named "
            "feature, pass place_name and data_type instead of feature_name."
        )

    gdf = gpd.GeoDataFrame(
        [
            {
                "name": chosen["name"] or chosen["display_name"].split(",")[0],
                "osm_type": chosen["osm_type"],
                "osm_id": chosen["osm_id"],
                "osm_key": chosen["osm_key"],
                "osm_value": chosen["osm_value"],
            }
        ],
        geometry=[osm_geometry(chosen["osm_type"], chosen["osm_id"])],
        crs="EPSG:4326",
    ).explode(index_parts=False)

    label = f"{chosen['osm_type']} {chosen['osm_id']}"
    return gdf.reset_index(drop=True), label


def _place_area_km2(bbox):
    import geopandas as gpd
    from shapely.geometry import box

    area = gpd.GeoSeries([box(*bbox)], crs="EPSG:4326")
    return float(area.to_crs(area.estimate_utm_crs()).area.iloc[0]) / 1e6


def _polygon_outline(place):
    if place is None:
        return None
    try:
        outline = osm_geometry(place["osm_type"], place["osm_id"])
    # a boundary relation whose rings do not close
    except LookupError:
        return None
    return outline if outline.geom_type in ("Polygon", "MultiPolygon") else None


def download_osm_data(
    data_type: str,
    place_name: str = None,
    feature_name: str = None,
    radius_m: int = None,
    output_filename: str = None,
) -> str:
    """
    Download OpenStreetMap data for a place and save it as a GeoPackage.
    Returns the output path and feature count. Use this whenever the user asks
    for real-world data like buildings, roads, amenities, parks, or shops.
    """
    import traceback


    try:
        import osmnx as ox

        dt = data_type.lower().strip()
        tags = tags_for_category(dt)

        named_feature_label = None
        duplicates_dropped = 0
        subdivided = False

        if feature_name:
            # "river" is a kind of thing, not the name of one, and geocoding it
            # lands on whatever place happens to be called that
            if feature_name.lower().strip() in OSM_TAG_MAP or feature_name.lower().strip() in OSM_TAG_KEYS:
                return (
                    f"'{feature_name}' names a kind of feature, not one feature. "
                    f"For every {feature_name} in an area, pass data_type='{feature_name}' "
                    "with place_name. Keep feature_name for a proper name like "
                    "'River Thames' or 'M25'."
                )
            gdf, named_feature_label = _named_feature_gdf(feature_name, tags)
            if gdf is None:
                return named_feature_label
            place_name = feature_name
            _coord_download = True  # geometry already in hand, skip the area search
        elif not place_name:
            return "Give either place_name or feature_name."
        else:
            # If place_name looks like "lat,lon" coordinates, use point-based download
            import re as _re

            _coord_m = _re.match(
                r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", place_name.strip()
            )
            if _coord_m:
                _lat, _lon = float(_coord_m.group(1)), float(_coord_m.group(2))
                place_name = f"{_lat},{_lon}"  # normalise for output filename
            elif radius_m:
                point = geocode_point(place_name)
                if point is None:
                    return place_not_found(place_name)
                _lat, _lon = point
            else:
                _lat = _lon = None
            if _lat is not None:
                # Skip place-boundary attempt, go straight to point+radius
                if dt == "roads":
                    radius = radius_m or DEFAULT_ROADS_RADIUS_M
                    G = ox.graph_from_point(
                        (_lat, _lon), dist=radius, network_type="all"
                    )
                    gdf = ox.graph_to_gdfs(G, nodes=False).reset_index(drop=True)
                else:
                    radius = radius_m or DEFAULT_RADIUS_M
                    gdf = ox.features_from_point((_lat, _lon), tags=tags, dist=radius)
                    if gdf.empty:
                        return (
                            f"No '{data_type}' features found within "
                            f"{radius}m of {place_name}."
                        )
                    gdf = gdf.reset_index(drop=True)
                # Jump to post-processing
                _coord_download = True
            else:
                _coord_download = False

        # Build output path
        if not output_filename:
            safe_place = (
                place_name.lower().replace(" ", "_").replace(",", "")[:20].strip("_")
            )
            safe_type = dt.replace("=", "_").replace(" ", "_")[:15]
            output_filename = f"{safe_place}_{safe_type}"
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = tool_output_path("output_filename", f"{output_filename}.gpkg")

        # Download: the place's outline when it has one, point+radius for addresses
        if not _coord_download:
            import warnings

            results = geocode(place_name, limit=PLACE_CANDIDATES)
            if not results:
                return place_not_found(place_name)
            place = first_outlined_hit(results)
            if place is not None and place["bbox"] is not None:
                area_km2 = _place_area_km2(place["bbox"])
                if area_km2 > MAX_PLACE_AREA_KM2:
                    return (
                        f"'{place_name}' covers {area_km2:,.0f} km2 and a full "
                        f"{data_type} download is capped at {MAX_PLACE_AREA_KM2} "
                        "km2. Pass a district-sized place_name, or radius_m to "
                        "search around its centre."
                    )
            outline = _polygon_outline(place)
            centre = (results[0]["lat"], results[0]["lon"])

            # osmnx splits an oversized area into sub-queries and says so only in a
            # warning, which is the difference between a fast call and a slow one
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                if dt == "roads":
                    if outline is None:
                        G = ox.graph_from_point(
                            centre, dist=DEFAULT_ROADS_RADIUS_M, network_type="all"
                        )
                    else:
                        G = ox.graph_from_polygon(outline, network_type="all")
                    gdf = ox.graph_to_gdfs(G, nodes=False).reset_index(drop=True)
                else:
                    if outline is None:
                        gdf = ox.features_from_point(
                            centre, tags=tags, dist=ADDRESS_FALLBACK_RADIUS_M
                        )
                    else:
                        gdf = ox.features_from_polygon(outline, tags=tags)
                    if gdf.empty:
                        return f"No '{data_type}' features found in {place_name}."
                    # sub-queries overlap, so the same way comes back once per tile
                    before = len(gdf)
                    gdf = gdf[~gdf.index.duplicated()]
                    duplicates_dropped = before - len(gdf)
                    gdf = gdf.reset_index(drop=True)

            subdivided = any("sub-queries" in str(w.message) for w in caught)
            for w in caught:
                warnings.warn_explicit(
                    w.message, w.category, w.filename, w.lineno
                )

        if gdf.empty:
            return f"No '{data_type}' features found in {place_name}."

        # Drop columns with unhashable/complex types that break GPKG serialisation
        drop_cols = []
        for col in gdf.columns:
            if col == "geometry":
                continue
            sample = gdf[col].dropna()
            if len(sample) > 0 and isinstance(sample.iloc[0], (list, dict)):
                drop_cols.append(col)
        if drop_cols:
            gdf = gdf.drop(columns=drop_cols)

        # Coerce remaining object columns to string
        for col in gdf.select_dtypes(include="object").columns:
            if col != "geometry":
                gdf[col] = gdf[col].astype(str)

        # Sanitise column names — GPKG/fiona breaks on special chars, reserved words,
        # very long names, or OSM tags like "FIXME", "addr:street", "name:en"
        import re

        rename_map = {}
        seen = set()
        for col in gdf.columns:
            if col == "geometry":
                continue
            clean = re.sub(r"[^a-zA-Z0-9_]", "_", col)  # replace non-alphanumeric
            clean = re.sub(r"_+", "_", clean).strip("_")  # collapse underscores
            clean = clean[:60]  # GPKG max column name length
            if not clean or clean[0].isdigit():
                clean = "col_" + clean
            # Ensure uniqueness
            base, i = clean, 1
            while clean in seen:
                clean = f"{base}_{i}"
                i += 1
            seen.add(clean)
            if clean != col:
                rename_map[col] = clean
        if rename_map:
            gdf = gdf.rename(columns=rename_map)

        # Ensure WGS84
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        # Save — if mixed geometry types, keep only the dominant type
        geom_counts = gdf.geometry.geom_type.value_counts()
        if geom_counts.empty:
            return f"Downloaded data for {place_name} but no valid geometries found."

        dominant = geom_counts.index[0]
        if gdf.geometry.geom_type.nunique() > 1:
            gdf = gdf[gdf.geometry.geom_type == dominant].copy()

        try:
            gdf.to_file(output_path, driver="GPKG")
        except Exception:
            # Last resort: keep only name, geometry and a handful of safe columns
            keep = ["geometry"] + [
                c
                for c in ("name", "amenity", "shop", "building", "leisure")
                if c in gdf.columns
            ]
            gdf[keep].to_file(output_path, driver="GPKG")

        useful_cols = [c for c in gdf.columns if c not in ("geometry",)][:8]
        source = (
            f"{place_name} (OSM {named_feature_label}, whole feature)"
            if named_feature_label
            else place_name
        )
        parts = [
            f"Downloaded {len(gdf)} {data_type} features ({dominant}) for {source}.",
            f"Saved to outputs/{output_filename}.gpkg.",
        ]

        # length tells the caller at a glance whether they got the whole feature or
        # a piece of it, which a feature count cannot
        if "Line" in dominant:
            from pyproj import Geod

            length_km = (
                sum(Geod(ellps="WGS84").geometry_length(g) for g in gdf.geometry) / 1000
            )
            parts.append(f"Total length: {length_km:,.1f} km.")

        if duplicates_dropped:
            parts.append(
                f"Dropped {duplicates_dropped} duplicate features returned by "
                "overlapping sub-queries."
            )
        if subdivided:
            parts.append(
                f"'{place_name}' exceeds the Overpass max query area, so it ran as "
                "several sub-queries and was slow. The response is cached, so the "
                "same request again will be fast. To fetch one named feature whole, "
                "use feature_name instead."
            )
        parts.append(f"Key columns: {', '.join(useful_cols)}")
        return " ".join(parts)

    except Exception as e:
        return f"OSM download failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = download_osm_data
TOOL_SCHEMA = DownloadOSMDataArgs
