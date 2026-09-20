from src.core.utils import population_raster_path

GHSL_SOURCE = "GHSL GHS-POP 2020 (zonal sum)"
WORLDPOP_SOURCE = "WorldPop wpgpas 2020 (age-sex pyramid sum)"
NO_SOURCE = "unavailable"

WORLDPOP_STATS_URL = "https://api.worldpop.org/v1/services/stats"
WORLDPOP_TASK_URL = "https://api.worldpop.org/v1/tasks"
WORLDPOP_DATASET = "wpgpas"
WORLDPOP_YEAR = 2020
WORLDPOP_POLL_SECONDS = 30
WORLDPOP_POLL_INTERVAL_SECONDS = 1.5


def population_inside(polygon) -> tuple[int | None, str]:
    raster_total = ghsl_sum(polygon)
    if raster_total is not None:
        return int(round(raster_total)), GHSL_SOURCE
    worldpop_total = worldpop_polygon(polygon)
    if worldpop_total is not None:
        return int(round(worldpop_total)), WORLDPOP_SOURCE
    return None, NO_SOURCE


def ghsl_sum(polygon) -> float | None:
    raster_path = population_raster_path()
    if not raster_path:
        return None
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.mask import mask as rio_mask
    from shapely.geometry import mapping

    with rasterio.open(raster_path) as src:
        geom = gpd.GeoSeries([polygon], crs="EPSG:4326").to_crs(src.crs).iloc[0]
        nodata = src.nodata if src.nodata is not None else -9999
        out_image, _ = rio_mask(src, [mapping(geom)], crop=True, nodata=nodata)
        data = out_image[0].astype(float)
        data[data == nodata] = np.nan
        data[data < 0] = np.nan
        return float(np.nansum(data))


def worldpop_polygon(polygon) -> float | None:
    import json
    import time

    import requests

    geojson = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": polygon.__geo_interface__,
                }
            ],
        }
    )
    try:
        resp = requests.get(
            WORLDPOP_STATS_URL,
            params={
                "dataset": WORLDPOP_DATASET,
                "year": WORLDPOP_YEAR,
                "geojson": geojson,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        taskid = resp.json().get("taskid")
        if not taskid:
            return None
        deadline = time.time() + WORLDPOP_POLL_SECONDS
        while True:
            task = requests.get(f"{WORLDPOP_TASK_URL}/{taskid}", timeout=30)
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
            time.sleep(WORLDPOP_POLL_INTERVAL_SECONDS)
    except Exception:
        pass
    return None
