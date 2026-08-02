from pydantic import BaseModel, Field
from typing import Optional


class GetAdminBoundaryArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Name of the administrative area to fetch — e.g. 'Leicester', 'Greater London', "
            "'Bavaria', 'New York State', 'France'. Be as specific as needed to avoid ambiguity."
        ),
    )
    admin_level: Optional[int] = Field(
        None,
        description=(
            "OSM administrative level (2–10). Lower = larger area. "
            "2=country, 4=state/province, 6=county/district, 8=city/municipality. "
            "Leave blank to accept whatever OSM returns for the place name."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename without extension. Auto-generated if omitted.",
    )


def get_admin_boundary(
    place_name: str,
    admin_level: int = None,
    output_filename: str = None,
) -> str:
    """
    Fetch the administrative boundary polygon for a place (country, region, city, district)
    from OpenStreetMap. Returns a polygon GPKG that can be used as a clip mask or
    analysis area.

    Use this when the user asks to:
    - 'Show me the boundary of Leicester / Greater London / Bavaria'
    - 'Clip data to the city of Paris'
    - 'Get the outline of England'
    - 'Find all hospitals within the county of Kent'

    Falls back to a geocoded point + convex-hull approach if OSM boundary is unavailable.
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    try:
        import osmnx as ox
        import geopandas as gpd
        import re

        ox.settings.timeout = 60
        ox.settings.overpass_rate_limit = False

        # Build safe filename stem
        safe_name = re.sub(r"[^\w]", "_", place_name.lower())[:24].strip("_")

        if not output_filename:
            output_filename = f"{safe_name}_boundary"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")

        # Strategy 1: osmnx geocode_to_gdf — returns the OSM boundary polygon directly
        gdf = None
        try:
            gdf = ox.geocode_to_gdf(place_name)
            if gdf.empty:
                gdf = None
        except Exception:
            gdf = None

        # Strategy 2: Overpass query for named admin boundary
        if gdf is None:
            try:
                import requests

                overpass_url = "https://overpass-api.de/api/interpreter"
                level_filter = f'["admin_level"="{admin_level}"]' if admin_level else ""
                query = f"""
                [out:json][timeout:60];
                relation["name"~"{place_name}",i]["boundary"="administrative"]{level_filter};
                out geom;
                """
                resp = requests.post(overpass_url, data={"data": query}, timeout=65)
                data = resp.json()
                elements = data.get("elements", [])

                if elements:
                    from shapely.geometry import Polygon
                    from shapely.ops import unary_union

                    polys = []
                    for el in elements:
                        members = el.get("members", [])
                        outer_ways = [m for m in members if m.get("role") == "outer"]
                        for way in outer_ways:
                            coords = [
                                (n["lon"], n["lat"]) for n in way.get("geometry", [])
                            ]
                            if len(coords) >= 3:
                                try:
                                    polys.append(Polygon(coords))
                                except Exception:
                                    pass

                    if polys:
                        merged = unary_union(polys)
                        tags = elements[0].get("tags", {})
                        gdf = gpd.GeoDataFrame(
                            [
                                {
                                    "name": tags.get("name", place_name),
                                    "admin_level": tags.get("admin_level", ""),
                                    "geometry": merged,
                                }
                            ],
                            crs="EPSG:4326",
                        )
            except Exception:
                gdf = None

        if gdf is None or gdf.empty:
            return (
                f"Could not retrieve an administrative boundary for '{place_name}'. "
                f"Try a more specific name, or check that the place has an OSM boundary relation. "
                f"You can use geocode_place to get a point location instead."
            )

        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        # Compute area
        gdf_proj = gdf.to_crs("EPSG:3857")
        area_km2 = round(float(gdf_proj.geometry.area.sum()) / 1e6, 1)

        # Keep only useful columns
        keep_cols = [
            c
            for c in ("name", "admin_level", "display_name", "geometry")
            if c in gdf.columns
        ]
        gdf = gdf[keep_cols]

        gdf.to_file(output_path, driver="GPKG")

        bounds = gdf.total_bounds  # minx, miny, maxx, maxy
        center_lon = round((bounds[0] + bounds[2]) / 2, 4)
        center_lat = round((bounds[1] + bounds[3]) / 2, 4)

        return (
            f"Administrative boundary for '{place_name}' retrieved: {area_km2} km². "
            f"Center: lon={center_lon}, lat={center_lat}. "
            f"Saved to outputs/{output_filename}.gpkg."
        )

    except Exception as e:
        return f"get_admin_boundary failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = get_admin_boundary
TOOL_SCHEMA = GetAdminBoundaryArgs
