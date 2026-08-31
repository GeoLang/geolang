from pydantic import BaseModel, Field
from src.core.utils import tool_input_path, tool_output_path


class ClipLayerArgs(BaseModel):
    input_path: str = Field(
        ...,
        description="Filename of the layer to clip, e.g. 'restaurants.gpkg'. Not a path.",
    )
    clip_path: str = Field(
        ...,
        description="Filename of the polygon layer to clip by, e.g. an isochrone.",
    )
    output_filename: str = Field(
        ...,
        description="Output name, no extension.",
    )


def clip_layer(input_path: str, clip_path: str, output_filename: str) -> str:
    """
    Clip one spatial layer by a polygon layer, keeping the features inside the
    boundary: restaurants within an isochrone, points within an admin area.
    """
    import traceback

    try:
        import geopandas as gpd

        input_full = tool_input_path("input_path", input_path)
        clip_full = tool_input_path("clip_path", clip_path)

        gdf = gpd.read_file(input_full)
        clip_gdf = gpd.read_file(clip_full)

        if gdf.empty:
            return "Input layer is empty."

        # Align CRS
        if clip_gdf.crs != gdf.crs:
            clip_gdf = clip_gdf.to_crs(gdf.crs)

        # Dissolve clip layer to a single geometry
        clip_geom = clip_gdf.geometry.unary_union

        clipped = gpd.clip(gdf, clip_geom)

        if clipped.empty:
            return f"No features from {input_path} fall within {clip_path}."

        # Strip .gpkg if already present to avoid double extension
        if output_filename.lower().endswith(".gpkg"):
            output_filename = output_filename[:-5]
        output_path = tool_output_path("output_filename", f"{output_filename}.gpkg")
        clipped.to_file(output_path, driver="GPKG")

        return (
            f"Clipped {len(clipped)} of {len(gdf)} features. "
            f"Saved to outputs/{output_filename}.gpkg"
        )

    except Exception as e:
        return f"Clip failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = clip_layer
TOOL_SCHEMA = ClipLayerArgs
