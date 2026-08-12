from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import (
    population_raster_path,
    tool_input_path,
    tool_input_path_or_none,
    tool_output_path,
)


class QueryZonalPopulationArgs(BaseModel):
    polygon_path: str = Field(
        ...,
        description=(
            "Path to a polygon GPKG to sum population within — typically an isochrone. "
            "A filename in outputs/, not a path. E.g. 'leicester_driv_isochrones.gpkg'."
        ),
    )
    place_name: str = Field(
        ...,
        description="Human-readable label for the location (used in output text and filename).",
    )
    ghsl_raster_path: Optional[str] = Field(
        None,
        description=(
            "Path to a local GHSL GHS-POP GeoTIFF raster. "
            "If omitted the tool looks for 'ghsl_pop.tif' in your own files or at the "
            "project root. "
            "Download from: https://ghsl.jrc.ec.europa.eu/ghs_pop2023.php "
            "(R2023A epoch 2020, resolution 1km, Mollweide projection)."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def query_zonal_population(
    polygon_path: str,
    place_name: str,
    ghsl_raster_path: str = None,
    output_filename: str = None,
) -> str:
    """
    Compute true grid-based population within a polygon (e.g. an isochrone) using
    the GHSL GHS-POP raster dataset. Returns total population count and per-feature
    breakdown. Requires a local GHS-POP GeoTIFF — download once from the GHSL portal
    (free). Falls back to WorldPop API bounding-box estimate if no raster is found.

    Use this when the user asks how many people live within a drive/walk time,
    or wants accurate population catchment for a service area or depot.
    """
    import traceback

    try:
        import geopandas as gpd
        import numpy as np

        poly_path = tool_input_path("polygon_path", polygon_path)

        gdf = gpd.read_file(poly_path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        raster_path = tool_input_path_or_none(
            "ghsl_raster_path", ghsl_raster_path
        ) or population_raster_path()

        if not raster_path:
            # Fallback: WorldPop bounding-box API
            import requests

            total_bbox = gdf.total_bounds  # minx, miny, maxx, maxy
            west, south, east, north = total_bbox

            url = (
                "https://api.worldpop.org/v1/services/stats"
                f"?dataset=wpgpas&year=2020&iso3=GBR"
                f"&bbox={west:.4f},{south:.4f},{east:.4f},{north:.4f}"
            )
            pop_total = None
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "success" and "data" in data:
                        pop_total = data["data"].get("total_population")
            except Exception:
                pass

            if pop_total is not None:
                return (
                    f"No local GHSL raster found. WorldPop bounding-box estimate for "
                    f"{place_name}: ~{int(pop_total):,} people (approximate — includes "
                    f"area outside the polygon). "
                    f"For precise zonal stats, download GHS-POP from "
                    f"https://ghsl.jrc.ec.europa.eu/ghs_pop2023.php and save as "
                    f"ghsl_pop.tif in the project directory."
                )
            else:
                return (
                    "No local GHSL raster found and WorldPop API returned no data. "
                    "To get accurate population figures, download the GHS-POP raster "
                    "(free) from https://ghsl.jrc.ec.europa.eu/ghs_pop2023.php "
                    "(epoch 2020, 1km resolution) and save as ghsl_pop.tif in the "
                    "project root or outputs/ directory."
                )

        # --- Zonal stats using rasterstats ---
        try:
            from rasterstats import zonal_stats
        except ImportError:
            # Manual rasterio fallback
            import rasterio
            from rasterio.mask import mask as rio_mask
            from shapely.geometry import mapping

            results = []
            with rasterio.open(raster_path) as src:
                # Reproject polygon to raster CRS if needed
                raster_crs = src.crs
                gdf_proj = gdf.to_crs(raster_crs) if gdf.crs != raster_crs else gdf

                total_pop = 0.0
                for idx, row in gdf_proj.iterrows():
                    geom = [mapping(row.geometry)]
                    try:
                        out_image, _ = rio_mask(
                            src, geom, crop=True, nodata=src.nodata or -9999
                        )
                        data = out_image[0].astype(float)
                        nodata = src.nodata if src.nodata is not None else -9999
                        data[data == nodata] = np.nan
                        data[data < 0] = np.nan
                        cell_pop = float(np.nansum(data))
                        total_pop += cell_pop
                        results.append(
                            {
                                "geometry": row.geometry,
                                "minutes": row.get("minutes", idx),
                                "population": int(round(cell_pop)),
                            }
                        )
                    except Exception:
                        results.append(
                            {
                                "geometry": row.geometry,
                                "minutes": row.get("minutes", idx),
                                "population": 0,
                            }
                        )

            out_gdf = gpd.GeoDataFrame(results, crs=gdf.crs)
            if not output_filename:
                safe = (
                    place_name.lower()
                    .replace(" ", "_")
                    .replace(",", "")[:18]
                    .strip("_")
                )
                output_filename = f"{safe}_zonal_pop"
            # Strip .gpkg if already present to avoid double extension
            if output_filename.lower().endswith(".gpkg"):
                output_filename = output_filename[:-5]
            output_path = tool_output_path(
                "output_filename", f"{output_filename}.gpkg"
            )
            out_gdf.to_file(output_path, driver="GPKG")

            time_str = ", ".join(
                f"{r['minutes']}min={r['population']:,}" for r in results
            )
            return (
                f"Population within {place_name} isochrone (GHSL 2020): "
                f"total {int(round(total_pop)):,} people. "
                f"Breakdown: {time_str}. "
                f"Saved to outputs/{output_filename}.gpkg."
            )

        # rasterstats path
        stats_list = zonal_stats(
            gdf,
            raster_path,
            stats=["sum"],
            all_touched=False,
        )

        results = []
        total_pop = 0.0
        for i, (row_idx, row) in enumerate(gdf.iterrows()):
            cell_pop = stats_list[i].get("sum") or 0.0
            if cell_pop < 0:
                cell_pop = 0.0
            total_pop += cell_pop
            results.append(
                {
                    "geometry": row.geometry,
                    "minutes": row.get("minutes", row_idx),
                    "population": int(round(cell_pop)),
                }
            )

        out_gdf = gpd.GeoDataFrame(results, crs=gdf.crs)

        if not output_filename:
            safe = place_name.lower().replace(" ", "_").replace(",", "")[:18].strip("_")
            output_filename = f"{safe}_zonal_pop"

        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = tool_output_path(
            "output_filename", f"{output_filename}.gpkg"
        )
        out_gdf.to_file(output_path, driver="GPKG")

        time_str = ", ".join(f"{r['minutes']}min={r['population']:,}" for r in results)
        return (
            f"Population within {place_name} isochrone (GHSL 2020): "
            f"total {int(round(total_pop)):,} people. "
            f"Breakdown by zone: {time_str}. "
            f"Saved to outputs/{output_filename}.gpkg."
        )

    except Exception as e:
        return f"Zonal population query failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = query_zonal_population
TOOL_SCHEMA = QueryZonalPopulationArgs
