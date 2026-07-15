from pydantic import BaseModel, Field
from typing import Optional


class QueryElevationArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Place or address to query elevation for. "
            "E.g. 'M1 Junction 24, Kegworth, UK' or 'Canary Wharf, London'."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension for the point GPKG. Auto-generated if omitted.",
    )


def query_elevation(place_name: str, output_filename: str = None) -> str:
    """
    Get the elevation (metres above sea level) for a location using the
    OpenTopoData API (SRTM 90m dataset, free, no API key required).
    Also returns flood-risk context: locations below 10m are at potential
    flood risk; below 5m are high risk. Saves a point GPKG with elevation
    attribute for use in further spatial analysis.
    """
    import os
    import json
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    try:
        import requests
        import osmnx as ox
        import geopandas as gpd
        from shapely.geometry import Point

        # Geocode — if place_name looks like "lat,lon" or "lat lon", parse directly
        import re as _re

        _coord_m = _re.match(
            r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", place_name.strip()
        )
        if _coord_m:
            lat, lon = float(_coord_m.group(1)), float(_coord_m.group(2))
        else:
            lat, lon = ox.geocode(place_name)

        # Query OpenTopoData SRTM 90m — free, no key needed
        url = f"https://api.opentopodata.org/v1/srtm90m?locations={lat},{lon}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK" or not data.get("results"):
            return f"Elevation query failed for {place_name}: {data.get('status', 'unknown error')}"

        elevation_m = data["results"][0]["elevation"]
        if elevation_m is None:
            return f"No elevation data available for {place_name} (may be offshore or data gap)."

        # Flood risk context
        if elevation_m < 5:
            flood_note = "HIGH flood risk — below 5m elevation."
        elif elevation_m < 10:
            flood_note = "Moderate flood risk — below 10m elevation, check Environment Agency flood maps."
        elif elevation_m < 20:
            flood_note = (
                "Low flood risk — above 10m but worth checking local flood zone maps."
            )
        else:
            flood_note = "Minimal flood risk from sea-level flooding."

        # Save point GPKG
        if not output_filename:
            safe = _re.sub(r"[^\w]", "_", place_name.lower())[:20].strip("_")
            output_filename = f"{safe}_elevation"

        gdf = gpd.GeoDataFrame(
            [
                {
                    "place": place_name,
                    "elevation_m": round(float(elevation_m), 1),
                    "flood_note": flood_note,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }
            ],
            geometry=[Point(lon, lat)],
            crs="EPSG:4326",
        )
        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        gdf.to_file(output_path, driver="GPKG")

        return (
            f"Elevation at {place_name}: {elevation_m:.1f}m above sea level. "
            f"{flood_note} "
            f"Point saved to outputs/{output_filename}.gpkg. "
            f"Center: lon={lon:.4f}, lat={lat:.4f}"
        )

    except Exception as e:
        return f"Elevation query failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = query_elevation
TOOL_SCHEMA = QueryElevationArgs
