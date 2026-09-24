from pydantic import BaseModel, Field
from typing import Optional
from src.core.place_lookup import first_outlined_hit, geocode, osm_geometry
from src.core.utils import tool_output_path

BOUNDARY_CANDIDATES = 10


class GetAdminBoundaryArgs(BaseModel):
    place_name: str = Field(
        ...,
        description=(
            "Administrative area to fetch, e.g. 'Greater London'. Be specific "
            "enough to avoid ambiguity."
        ),
    )
    admin_level: Optional[int] = Field(
        None,
        description=(
            "OSM admin level 2 to 10, lower being larger: 2 country, 4 state, "
            "6 county, 8 city. Omit to take whatever OSM returns for the name."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output name, no extension. Auto-generated if omitted.",
    )


def get_admin_boundary(
    place_name: str,
    admin_level: int = None,
    output_filename: str = None,
) -> str:
    """
    Administrative boundary polygon for a country, region, city or district,
    from OpenStreetMap. Returns a polygon GPKG usable as a clip mask or analysis
    area. Answers that nothing was found when OSM has no boundary for the place.
    """
    import traceback


    try:
        import geopandas as gpd
        import re

        # Build safe filename stem
        safe_name = re.sub(r"[^\w]", "_", place_name.lower())[:24].strip("_")

        if not output_filename:
            output_filename = f"{safe_name}_boundary"
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]

        output_path = tool_output_path("output_filename", f"{output_filename}.gpkg")

        results = geocode(place_name, limit=BOUNDARY_CANDIDATES)
        boundaries = [hit for hit in results if hit["kind"] == "boundary"]
        place = first_outlined_hit(boundaries or results, admin_level)
        outline = osm_geometry(place["osm_type"], place["osm_id"]) if place else None
        if outline is None or outline.geom_type not in ("Polygon", "MultiPolygon"):
            return (
                f"Could not retrieve an administrative boundary for '{place_name}'. "
                f"Try a more specific name, or check that the place has an OSM boundary relation. "
                f"You can use geocode_place to get a point location instead."
            )
        gdf = gpd.GeoDataFrame(
            [
                {
                    "name": place["name"] or place_name,
                    "admin_level": place["admin_level"],
                    "display_name": place["display_name"],
                }
            ],
            geometry=[outline],
            crs="EPSG:4326",
        )

        # Compute area
        gdf_proj = gdf.to_crs("EPSG:3857")
        area_km2 = round(float(gdf_proj.geometry.area.sum()) / 1e6, 1)

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
