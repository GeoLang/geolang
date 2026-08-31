from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import (
    caller_outputs_dir,
    population_raster_path,
    tool_input_path_or_none,
    tool_output_path,
)


class DownloadPopulationGridArgs(BaseModel):
    place_name: str = Field(
        ...,
        description="Place or address to centre the query on, e.g. 'Kegworth, UK'.",
    )
    radius_km: float = Field(
        10.0,
        description="Radius in km around the location to count population within.",
    )
    clip_layer_path: Optional[str] = Field(
        None,
        description=(
            "Polygon GPKG, e.g. an isochrone, to count population inside instead of "
            "the radius. A filename in outputs/, not a path."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def download_population_grid(
    place_name: str,
    radius_km: float = 10.0,
    clip_layer_path: str = None,
    output_filename: str = None,
) -> str:
    """
    Estimate population within a radius or polygon, as a zonal sum of the local
    GHS-POP raster over the queried area, so the count matches the area drawn.
    Falls back to the WorldPop API when no local raster is present. Returns the
    total and saves the queried area as a polygon GPKG carrying the estimate.
    Use this for population catchment, how many people live within a drive or
    walk time, or the demographic coverage of a service area.
    """
    import os
    import json
    import time
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = caller_outputs_dir()

    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        from shapely.geometry import box

        lat, lon = ox.geocode(place_name)

        # Convert radius to degrees (approximate)
        deg = radius_km / 111.0
        bbox = box(lon - deg, lat - deg, lon + deg, lat + deg)

        # Use WorldPop API — free, no key, takes a geojson FeatureCollection and
        # answers asynchronously: submit, then poll the task until it finishes
        # API: https://www.worldpop.org/rest/data
        def _worldpop_polygon(poly):
            geojson = json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            "geometry": poly.__geo_interface__,
                        }
                    ],
                }
            )
            try:
                resp = requests.get(
                    "https://api.worldpop.org/v1/services/stats",
                    params={"dataset": "wpgpas", "year": 2020, "geojson": geojson},
                    timeout=30,
                )
                if resp.status_code != 200:
                    return None
                taskid = resp.json().get("taskid")
                if not taskid:
                    return None
                deadline = time.time() + 30
                while True:
                    task = requests.get(
                        f"https://api.worldpop.org/v1/tasks/{taskid}", timeout=30
                    )
                    tj = task.json() if task.status_code == 200 else {}
                    if tj.get("status") == "finished":
                        # wpgpas reports people per age class and sex, never a total
                        pyramid = (tj.get("data") or {}).get("agesexpyramid") or []
                        if not pyramid:
                            return None
                        return sum(
                            float(c.get("male") or 0) + float(c.get("female") or 0)
                            for c in pyramid
                        )
                    if tj.get("status") in ("failed", "error"):
                        return None
                    if time.time() >= deadline:
                        return None
                    time.sleep(1.5)
            except Exception:
                pass
            return None

        def _ghsl_sum(poly):
            """Sum GHS-POP cells inside the polygon, or None with no local raster."""
            raster_path = population_raster_path()
            if not raster_path:
                return None
            import numpy as np
            import rasterio
            from rasterio.mask import mask as rio_mask
            from shapely.geometry import mapping

            with rasterio.open(raster_path) as src:
                geom = gpd.GeoSeries([poly], crs="EPSG:4326").to_crs(src.crs).iloc[0]
                nodata = src.nodata if src.nodata is not None else -9999
                out_image, _ = rio_mask(src, [mapping(geom)], crop=True, nodata=nodata)
                data = out_image[0].astype(float)
                data[data == nodata] = np.nan
                data[data < 0] = np.nan
                return float(np.nansum(data))

        # Resolve the clip polygon first: when given, it (not the radius bbox) is the
        # area the population count has to describe
        clip_gdf = None
        clip_missing = False
        if clip_layer_path:
            clip_path = tool_input_path_or_none("clip_layer_path", clip_layer_path)
            if clip_path:
                clip_gdf = gpd.read_file(clip_path)
                if clip_gdf.crs and clip_gdf.crs.to_epsg() != 4326:
                    clip_gdf = clip_gdf.to_crs("EPSG:4326")
            else:
                clip_missing = True

        # union the clip features: overlapping rings (e.g. nested isochrones) must not
        # count their shared cells twice
        query_poly = clip_gdf.union_all() if clip_gdf is not None else bbox

        # The local GHS-POP raster is the primary source for both paths, so the count
        # always covers exactly the area that gets rendered
        pop_total = None
        source = "unavailable"

        raster_pop = _ghsl_sum(query_poly)
        if raster_pop is not None:
            pop_total = int(round(raster_pop))
            source = "GHSL GHS-POP 2020 (zonal sum)"
        else:
            worldpop_pop = _worldpop_polygon(query_poly)
            if worldpop_pop is not None:
                pop_total = int(round(worldpop_pop))
                source = "WorldPop wpgpas 2020 (age-sex pyramid sum)"

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
        output_path = tool_output_path(
            "output_filename", f"{output_filename}.gpkg"
        )
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
                f"No local GHS-POP raster was found (save one as ghsl_pop.tif in the "
                f"project directory) and the WorldPop API returned nothing. "
                f"Try using Natural Earth populated places via download_osm_data instead."
            )

    except Exception as e:
        return f"Population query failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = download_population_grid
TOOL_SCHEMA = DownloadPopulationGridArgs
