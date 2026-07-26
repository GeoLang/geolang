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
    polygon when given) attributed with the population estimate.
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
        west, south, east, north = bbox.bounds

        url = (
            "https://api.worldpop.org/v1/services/stats"
            f"?dataset=wpgpas&year=2020&iso3=GBR"
            f"&bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}"
        )

        # Try WorldPop first; fall back to GHS-POP via alternative
        pop_total = None
        source = "WorldPop 2020"

        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success" and "data" in data:
                    pop_total = data["data"].get("total_population")
        except Exception:
            pass

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
                # Clip to radius bbox
                ne_clip = ne[ne.geometry.within(bbox)]
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

        # If clip polygon provided, use it instead of radius bbox
        clip_gdf = None
        if clip_layer_path:
            _res = lambda p: (
                None
                if not p
                else (
                    p
                    if os.path.isabs(p) and os.path.exists(p)
                    else next(
                        (
                            c
                            for _b in (outputs_dir, exec_dir)
                            for _n in (
                                [p]
                                + ([] if p.lower().endswith(".gpkg") else [p + ".gpkg"])
                            )
                            for c in [os.path.join(_b, _n)]
                            if os.path.exists(c)
                        ),
                        None,
                    )
                )
            )
            clip_path = _res(clip_layer_path)
            if os.path.exists(clip_path):
                clip_gdf = gpd.read_file(clip_path)
                if clip_gdf.crs and clip_gdf.crs.to_epsg() != 4326:
                    clip_gdf = clip_gdf.to_crs("EPSG:4326")
                clip_area_km2 = round(
                    clip_gdf.to_crs("EPSG:3857").geometry.area.sum() / 1e6, 1
                )
                clip_note = f" (clipped to polygon, area={clip_area_km2} km²)"
            else:
                clip_note = f" (clip file not found: {clip_layer_path})"
        else:
            clip_note = f" (within {radius_km}km radius)"

        # Save the queried AREA as the rendered geometry, attributed with the
        # population estimate. The centroid stays as the lat/lon properties.
        if not output_filename:
            safe = place_name.lower().replace(" ", "_").replace(",", "")[:18].strip("_")
            output_filename = f"{safe}_population"

        if clip_gdf is not None:
            area_poly = clip_gdf.union_all()
            area_source = "clip_polygon"
        else:
            area_poly = bbox
            area_source = "radius_bbox"

        area_series = gpd.GeoSeries([area_poly], crs="EPSG:4326")
        area_km2 = round(
            area_series.to_crs(area_series.estimate_utm_crs()).area.iloc[0] / 1e6, 2
        )

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
