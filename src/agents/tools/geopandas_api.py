from pydantic import BaseModel, Field
from typing import Optional, List
from src.core.utils import caller_outputs_dir


class GeopandasArgs(BaseModel):
    function_name: str = Field(
        ...,
        description="One of: read_file, sjoin, buffer, to_file, proximity_analysis, filter",
    )
    filter_query: Optional[str] = Field(
        None,
        description="Pandas query expression for the filter function. Example: CONTINENT == 'Europe'",
    )
    dataset_path: Optional[str] = Field(
        None,
        description="Path to file (e.g., 'natural_earth/ne_110m_populated_places.shp')",
    )
    point_coords: Optional[List[float]] = Field(
        None, description="[lon, lat] for proximity_analysis"
    )
    distance_m: Optional[float] = Field(5000, description="Buffer distance in meters")
    how: Optional[str] = Field(None, description="For sjoin (e.g., 'inner', 'left')")
    predicate: Optional[str] = Field(
        None, description="For sjoin (e.g., 'intersects', 'within')"
    )
    distance: Optional[float] = Field(None, description="For buffer in CRS units")
    output_path: Optional[str] = Field(None, description="For to_file")
    driver: Optional[str] = Field(
        None, description="For to_file (e.g., 'ESRI Shapefile')"
    )


def geopandas_api(
    function_name: str,
    dataset_path: Optional[str] = None,
    point_coords: Optional[List[float]] = None,
    distance_m: Optional[float] = 5000,
    how: Optional[str] = None,
    predicate: Optional[str] = None,
    distance: Optional[float] = None,
    output_path: Optional[str] = None,
    driver: Optional[str] = None,
    filter_query: Optional[str] = None,
) -> str:
    """
    Execute GeoPandas operations dynamically.
    """
    import os
    import traceback
    import geopandas as gpd
    from shapely.geometry import Point

    kwargs = {
        k: v for k, v in locals().items() if k != "function_name" and v is not None
    }

    log = []
    log.append("=== GEOPANDAS_API EXECUTION ===")
    log.append(f"Function name: {function_name}")
    log.append(f"Arguments: {kwargs}")
    log.append(f"CWD: {os.getcwd()}")
    log.append(f"TOOL_EXEC_DIR: {os.environ.get('TOOL_EXEC_DIR', '/app/geolang')}")

    try:
        allowed = {"read_file", "sjoin", "buffer", "to_file", "proximity_analysis", "filter"}
        func_name = function_name.strip().split()[0]

        if func_name not in allowed:
            log.append(f"Error: '{func_name}' not supported")
            log.append(f"Allowed: {allowed}")
            log.append("\n=== FAILURE ===")
            return "\n".join(log) + "\n\nRESULT: Error: Unsupported function"

        if func_name in {"read_file", "proximity_analysis"} and not dataset_path:
            dataset_path = os.path.join(
                os.environ.get("TOOL_EXEC_DIR", "/app/geolang"),
                "natural_earth",
                "ne_110m_populated_places.shp",
            )
            log.append(f"Default dataset_path: {dataset_path}")

        # ─── filter: read → .query() → write ─────────────────────────────
        if func_name == "filter":
            if not dataset_path:
                log.append("Error: Missing dataset_path for filter")
                return "\n".join(log) + "\n\nRESULT: Error: Missing dataset_path"
            if not filter_query:
                log.append("Error: Missing filter_query for filter")
                return "\n".join(log) + "\n\nRESULT: Error: Missing filter_query"

            tool_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
            if not os.path.isabs(dataset_path):
                dataset_path = os.path.join(tool_dir, dataset_path)
            if not os.path.exists(dataset_path):
                log.append(f"Error: File not found: {dataset_path}")
                return "\n".join(log) + "\n\nRESULT: Error: Dataset not found"

            gdf = gpd.read_file(dataset_path)
            log.append(f"Loaded {len(gdf)} rows, columns: {list(gdf.columns)}")
            filtered = gdf.query(filter_query)
            log.append(f"After filter '{filter_query}': {len(filtered)} rows")

            if output_path is None:
                output_path = os.path.join(caller_outputs_dir(), "filtered.gpkg")
            elif not os.path.isabs(output_path):
                output_path = os.path.join(
                    caller_outputs_dir(), os.path.basename(output_path)
                )
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            filtered.to_file(output_path, driver="GPKG")
            log.append(f"Saved to {output_path}")
            log.append("\n=== SUCCESS ===")
            return "\n".join(log) + f"\n\nRESULT: ✅ Filtered to {len(filtered)} features → {output_path}"
        # ─────────────────────────────────────────────────────────────────

        if func_name == "proximity_analysis":
            point_coords = point_coords or [0.0, 0.0]
            distance_m = distance_m or 5000.0

            if not dataset_path:
                log.append("Error: Missing dataset_path")
                log.append("\n=== FAILURE ===")
                return "\n".join(log) + "\n\nRESULT: Error: Missing dataset_path"

            if not os.path.exists(dataset_path):
                log.append(f"Error: File not found: {dataset_path}")
                log.append("\n=== FAILURE ===")
                return "\n".join(log) + "\n\nRESULT: Error: Dataset not found"

            point = Point(point_coords[0], point_coords[1])
            gdf_point = gpd.GeoDataFrame(geometry=[point], crs="EPSG:4326")
            cities = gpd.read_file(dataset_path)

            gdf_point = gdf_point.to_crs("EPSG:32610")
            cities = cities.to_crs("EPSG:32610")

            matches = cities[cities.distance(gdf_point.geometry.iloc[0]) < distance_m]
            if not matches.empty:
                city = (
                    matches["NAME"].iloc[0] if "NAME" in matches.columns else "Unknown"
                )
                country = (
                    matches["SOV0NAME"].iloc[0]
                    if "SOV0NAME" in matches.columns
                    else "Unknown"
                )
                log.append(f"Found: {city}, {country}")
                log.append("\n=== SUCCESS ===")
                return (
                    "\n".join(log)
                    + f"\n\nRESULT: city={city},country={country},context=Found within {distance_m}m"
                )
            else:
                log.append("No match found")
                log.append("\n=== SUCCESS ===")
                return (
                    "\n".join(log)
                    + "\n\nRESULT: city=Unknown,country=Unknown,context=No match"
                )

        func = getattr(gpd, func_name, None)
        if not func:
            log.append(f"Error: GeoPandas has no '{func_name}'")
            log.append("\n=== FAILURE ===")
            return "\n".join(log) + "\n\nRESULT: Error: Function not found"

        if func_name in {"read_file", "to_file"} and not dataset_path:
            log.append("Error: Missing dataset_path")
            log.append("\n=== FAILURE ===")
            return "\n".join(log) + "\n\nRESULT: Error: Missing dataset_path"

        if func_name == "read_file" and not os.path.exists(dataset_path):
            log.append(f"Error: File not found: {dataset_path}")
            log.append("\n=== FAILURE ===")
            return "\n".join(log) + "\n\nRESULT: Error: Dataset not found"

        # Map dataset_path → correct parameter name for each function
        call_kwargs = dict(kwargs)
        if func_name == "read_file" and "dataset_path" in call_kwargs:
            call_kwargs["filename"] = call_kwargs.pop("dataset_path")
        elif func_name == "to_file" and "dataset_path" in call_kwargs:
            call_kwargs["filename"] = call_kwargs.pop("dataset_path")

        result = func(**call_kwargs)
        log.append(f"Executed {func_name}")
        log.append("\n=== SUCCESS ===")
        return "\n".join(log) + f"\n\nRESULT: {str(result)}"

    except Exception as e:
        log.append(f"Error: {type(e).__name__}: {str(e)}")
        log.append(f"Traceback:\n{traceback.format_exc()}")
        log.append("\n=== FAILURE ===")
        return "\n".join(log) + f"\n\nRESULT: Error: {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = geopandas_api
TOOL_SCHEMA = GeopandasArgs
