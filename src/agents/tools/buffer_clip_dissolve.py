from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import tool_input_path, tool_output_path

WGS84 = "EPSG:4326"
METRIC_CRS = "EPSG:3857"
METRES_PER_KILOMETRE = 1000


class BufferClipDissolveArgs(BaseModel):
    input_path: Optional[str] = Field(
        None,
        description="Filename of a vector layer to clip to the buffer. Omit to save the buffer polygon itself.",
    )
    center_lon: float = Field(..., description="Longitude of the buffer center point.")
    center_lat: float = Field(..., description="Latitude of the buffer center point.")
    buffer_km: float = Field(..., description="Buffer radius in kilometres.")
    dissolve_field: Optional[str] = Field(
        None,
        description="Field name to dissolve by after clipping (e.g. 'type', 'type_en'). Omit to skip dissolve. Ignored when no input_path is given.",
    )
    output_filename: str = Field(
        ..., description="Output filename, e.g. 'paris_roads_300km.gpkg'."
    )


def buffer_clip_dissolve(
    center_lon: float,
    center_lat: float,
    buffer_km: float,
    output_filename: str,
    input_path: Optional[str] = None,
    dissolve_field: Optional[str] = None,
) -> str:
    """
    Buffer a point and save the result as a GeoPackage, in one step using GeoPandas.
    With no input_path the buffer polygon itself is saved, which is what a request
    for a radius around a place asks for. With an input_path that layer is clipped
    to the buffer, optionally dissolved by a field, and the clipped layer is saved.
    """
    import traceback
    import geopandas as gpd
    from shapely.geometry import Point

    # Ensure .gpkg extension
    if not output_filename.lower().endswith(".gpkg"):
        output_filename = output_filename + ".gpkg"
    output_path = tool_output_path("output_filename", output_filename)
    # a refused path is the route's answer to give, not a tool failure string
    input_full = tool_input_path("input_path", input_path) if input_path else None

    try:
        # Build buffer polygon in metric CRS (EPSG:3857)
        point = gpd.GeoDataFrame(
            geometry=[Point(center_lon, center_lat)], crs=WGS84
        ).to_crs(METRIC_CRS)
        buffer_geom = point.geometry.iloc[0].buffer(buffer_km * METRES_PER_KILOMETRE)
        buffer_gdf = gpd.GeoDataFrame(
            {
                "center_lon": [center_lon],
                "center_lat": [center_lat],
                "buffer_km": [buffer_km],
            },
            geometry=[buffer_geom],
            crs=METRIC_CRS,
        )

        if input_full is None:
            buffer_gdf.to_crs(WGS84).to_file(output_path, driver="GPKG")
            return (
                f"✅ Done!\n"
                f"Buffer polygon: {buffer_km}km around "
                f"[{center_lon}, {center_lat}]\n"
                f"Features: 1\n"
                f"Saved to outputs/{output_filename}\n"
                f"CRS: {WGS84}"
            )

        # Load and reproject input layer
        gdf = gpd.read_file(input_full)
        if gdf.crs is None:
            gdf = gdf.set_crs(WGS84)
        gdf = gdf.to_crs(METRIC_CRS)

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
        clipped = clipped.to_crs(WGS84)
        clipped.to_file(output_path, driver="GPKG")

        return (
            f"✅ Done!\n"
            f"Features: {len(clipped)}\n"
            f"Saved to outputs/{output_filename}\n"
            f"CRS: {WGS84}"
        )

    except Exception as e:
        return f"❌ buffer_clip_dissolve failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = buffer_clip_dissolve
TOOL_SCHEMA = BufferClipDissolveArgs
