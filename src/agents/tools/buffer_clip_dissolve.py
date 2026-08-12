from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path


class BufferClipDissolveArgs(BaseModel):
    input_path: str = Field(
        ...,
        description="Filename of the input vector layer (shapefile or GPKG) to clip.",
    )
    center_lon: float = Field(..., description="Longitude of the buffer center point.")
    center_lat: float = Field(..., description="Latitude of the buffer center point.")
    buffer_km: float = Field(..., description="Buffer radius in kilometres.")
    dissolve_field: Optional[str] = Field(
        None,
        description="Field name to dissolve by after clipping (e.g. 'type', 'type_en'). Omit to skip dissolve.",
    )
    output_filename: str = Field(
        ..., description="Output filename, e.g. 'paris_roads_300km.gpkg'."
    )


def buffer_clip_dissolve(
    input_path: str,
    center_lon: float,
    center_lat: float,
    buffer_km: float,
    output_filename: str,
    dissolve_field: Optional[str] = None,
) -> str:
    """
    Buffer a point, clip a vector layer to that buffer, optionally dissolve by a field,
    and save the result as a GeoPackage. All in one step using GeoPandas.
    """
    import traceback
    import geopandas as gpd
    from shapely.geometry import Point

    # Ensure .gpkg extension
    if not output_filename.lower().endswith(".gpkg"):
        output_filename = output_filename + ".gpkg"
    output_path = tool_output_path("output_filename", output_filename)
    input_full = tool_input_path("input_path", input_path)

    try:
        # Build buffer polygon in metric CRS (EPSG:3857)
        point = gpd.GeoDataFrame(
            geometry=[Point(center_lon, center_lat)], crs="EPSG:4326"
        ).to_crs("EPSG:3857")
        buffer_geom = point.geometry.iloc[0].buffer(buffer_km * 1000)
        buffer_gdf = gpd.GeoDataFrame(geometry=[buffer_geom], crs="EPSG:3857")

        # Load and reproject input layer
        gdf = gpd.read_file(input_full)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        gdf = gdf.to_crs("EPSG:3857")

        # Clip
        clipped = gpd.clip(gdf, buffer_gdf)
        if clipped.empty:
            return f"❌ No features found within {buffer_km}km of [{center_lon}, {center_lat}]"

        # Optional dissolve
        if dissolve_field and dissolve_field in clipped.columns:
            clipped = clipped.dissolve(by=dissolve_field).reset_index()
        elif dissolve_field:
            available = list(clipped.columns)
            return (
                f"⚠️ Dissolve field '{dissolve_field}' not found.\n"
                f"Available fields: {available}\n"
                f"Re-run without dissolve_field or use one of the above."
            )

        # Reproject back to WGS84 and save
        clipped = clipped.to_crs("EPSG:4326")
        clipped.to_file(output_path, driver="GPKG")

        return (
            f"✅ Done!\n"
            f"Features: {len(clipped)}\n"
            f"Output: {output_path}\n"
            f"CRS: EPSG:4326"
        )

    except Exception as e:
        return f"❌ buffer_clip_dissolve failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = buffer_clip_dissolve
TOOL_SCHEMA = BufferClipDissolveArgs
