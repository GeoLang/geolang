from pydantic import BaseModel, Field
from typing import Optional


class DownloadPopulationGridArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Place or address to centre the population query on. "
            "E.g. 'M1 Junction 24, Kegworth, UK' or 'Trinity Bellwoods Park, Toronto'."
        ),
    )
    radius_km: float = Field(
        10.0,
        description="Radius in km around the location to query population for.",
    )
    clip_layer_path: Optional[str] = Field(
        None,
        description=(
            "Optional path to a polygon GPKG to clip population to (e.g. an isochrone). "
            "Relative to outputs/ or absolute. If provided, returns total population inside the polygon."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def download_population_grid(
    place_name: str,
    radius_km: float = 10.0,
    clip_layer_path: str = None,
    output_filename: str = None,
) -> str:
    """
    Estimate population within a radius or polygon using the GHS-POP (Global Human
    Settlement Population) grid via the WorldPop REST API. Returns total population
    count and saves a polygon GPKG of the queried area (the radius bbox, or the clip
    polygon when given) attributed with the population estimate. With a clip polygon
    the count is a zonal sum of the local GHS-POP raster inside that polygon, so it
    matches the rendered area.
    Use this when the user asks about population catchment, how many people live within
    a drive/walk time, or demographic coverage of a service area.
    """
    import os
    import json
    import traceback
    import math

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import box

        lat, lon = ox.geocode(place_name)

        # Convert radius to degrees (approximate)
        deg = radius_km / 111.0
        bbox = box(lon - deg, lat - deg, lon + deg, lat + deg)

        # Use WorldPop API — free, no key, returns population summary for a bounding box
        # API: https://www.worldpop.org/rest/data
        # We use the summary statistics endpoint for GHS population
        def _worldpop_bbox(west, south, east, north):
            url = (
                "https://api.worldpop.org/v1/services/stats"
                f"?dataset=wpgpas&year=2020&iso3=GBR"
                f"&bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}"
            )
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success" and "data" in data:
                        return data["data"].get("total_population")
            except Exception:
                pass
            return None

        def _res(p):
            """Resolve a file name against outputs_dir and exec_dir; None if absent."""
            if not p:
                return None
            if os.path.isabs(p) and os.path.exists(p):
                return p
            names = [p] if p.lower().endswith(".gpkg") else [p, p + ".gpkg"]
            for base in (outputs_dir, exec_dir):
                for name in names:
                    candidate = os.path.join(base, name)
                    if os.path.exists(candidate):
                        return candidate
            return None

        def _ghsl_sum(polys_gdf):
            """Sum GHS-POP cells inside the polygons, or None with no local raster."""
            raster_path = next(
                (
                    r
                    for r in (
                        _res(c)
                        for c in (
                            "ghsl_pop.tif",
                            "GHS_POP.tif",
                            "ghs_pop_2020.tif",
                            "ghsl_pop_2020.tif",
                        )
                    )
                    if r
                ),
                None,
            )
            if not raster_path:
                return None
            import numpy as np
            import rasterio
            from rasterio.mask import mask as rio_mask
            from shapely.geometry import mapping

            with rasterio.open(raster_path) as src:
                # union first: overlapping rings (e.g. nested isochrones) must not
                # count their shared cells twice
                union = polys_gdf.to_crs(src.crs).union_all()
                nodata = src.nodata if src.nodata is not None else -9999
                out_image, _ = rio_mask(
                    src, [mapping(union)], crop=True, nodata=nodata
                )
                data = out_image[0].astype(float)
                data[data == nodata] = np.nan
                data[data < 0] = np.nan
                return float(np.nansum(data))

        # Resolve the clip polygon first: when given, it (not the radius bbox) is the
        # area the population count has to describe
        clip_gdf = None
        clip_missing = False
        if clip_layer_path:
            clip_path = _res(clip_layer_path)
            if clip_path:
                clip_gdf = gpd.read_file(clip_path)
                if clip_gdf.crs and clip_gdf.crs.to_epsg() != 4326:
                    clip_gdf = clip_gdf.to_crs("EPSG:4326")
            else:
                clip_missing = True

        # Clipped: sum the GHS-POP raster in the polygon, WorldPop bbox only as a
        # fallback. Unclipped: WorldPop over the radius bbox
        pop_total = None
        source = "WorldPop 2020"

        if clip_gdf is not None:
            clip_pop = _ghsl_sum(clip_gdf)
            if clip_pop is not None:
                pop_total = int(round(clip_pop))
                source = "GHSL GHS-POP 2020 (zonal sum inside polygon)"
            else:
                pop_total = _worldpop_bbox(*clip_gdf.total_bounds)
                source = "WorldPop 2020 (polygon bounding box, approximate)"
        else:
            pop_total = _worldpop_bbox(*bbox.bounds)

        query_poly = clip_gdf.union_all() if clip_gdf is not None else bbox

        # Fallback: use GeoJSON population estimate from Overture/OSM admin boundaries
        # via a simple approximation from Natural Earth populated places
        if pop_total is None:
            # Approximate from Natural Earth 10m populated places within radius
            ne_path = None
            for fname in ["ne_pop_10m.gpkg", "natural_earth_10m_populated_places.gpkg"]:
                for base in [outputs_dir, exec_dir]:
                    candidate = os.path.join(base, fname)
                    if os.path.exists(candidate):
                        ne_path = candidate
                        break
                if ne_path:
                    break

            if ne_path:
                ne = gpd.read_file(ne_path)
                if ne.crs and ne.crs.to_epsg() != 4326:
                    ne = ne.to_crs("EPSG:4326")
                # Clip to the queried area (clip polygon when given, else radius bbox)
                ne_clip = ne[ne.geometry.within(query_poly)]
                pop_col = next(
                    (
                        c
                        for c in ("pop_max", "POP_MAX", "population", "pop")
                        if c in ne.columns
                    ),
                    None,
                )
                if pop_col and not ne_clip.empty:
                    pop_total = int(ne_clip[pop_col].sum())
                    source = "Natural Earth populated places (approximate)"

        # Save the queried AREA as the rendered geometry, attributed with the
        # population estimate. The centroid stays as the lat/lon properties.
        if not output_filename:
            safe = place_name.lower().replace(" ", "_").replace(",", "")[:18].strip("_")
            output_filename = f"{safe}_population"

        area_poly = query_poly
        area_source = "clip_polygon" if clip_gdf is not None else "radius_bbox"

        area_series = gpd.GeoSeries([area_poly], crs="EPSG:4326")
        area_km2 = round(
            area_series.to_crs(area_series.estimate_utm_crs()).area.iloc[0] / 1e6, 2
        )

        # note the area the count actually covers, using the same area figure as the
        # saved layer
        if clip_gdf is not None:
            clip_note = f" (clipped to polygon, area={area_km2} km2)"
        elif clip_missing:
            clip_note = f" (clip file not found: {clip_layer_path})"
        else:
            clip_note = f" (within {radius_km}km radius)"

        result_data = {
            "place": place_name,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "radius_km": radius_km,
            "area_km2": area_km2,
            "area_source": area_source,
            "population": int(pop_total) if pop_total is not None else -1,
            "source": source,
        }

        gdf = gpd.GeoDataFrame(
            [result_data],
            geometry=[area_poly],
            crs="EPSG:4326",
        )
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        gdf.to_file(output_path, driver="GPKG")

        if pop_total is not None:
            pop_str = f"{int(pop_total):,}"
            return (
                f"Estimated population{clip_note} around {place_name}: "
                f"{pop_str} people (source: {source}). "
                f"Saved to outputs/{output_filename}.gpkg. "
                f"That layer is the {area_km2} km2 queried area polygon attributed "
                f"with the population count. "
                f"Center: lon={lon:.4f}, lat={lat:.4f}"
            )
        else:
            return (
                f"Could not retrieve population data for {place_name}. "
                f"WorldPop API may not cover this region. "
                f"Try using Natural Earth populated places via download_osm_data instead."
            )

    except Exception as e:
        return f"Population query failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = download_population_grid
TOOL_SCHEMA = DownloadPopulationGridArgs
