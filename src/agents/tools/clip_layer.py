from pydantic import BaseModel, Field
from src.core.utils import caller_outputs_dir


class ClipLayerArgs(BaseModel):
    input_path: str = Field(
        ...,
        description="Path to the layer to clip. Can be relative to TOOL_EXEC_DIR (e.g. 'outputs/restaurants.gpkg') or absolute.",
    )
    clip_path: str = Field(
        ...,
        description="Path to the polygon layer to clip by (e.g. an isochrone or boundary). Relative or absolute.",
    )
    output_filename: str = Field(
        ...,
        description="Output filename without extension, e.g. 'restaurants_within_isochrone'.",
    )


def clip_layer(input_path: str, clip_path: str, output_filename: str) -> str:
    """
    Clip one spatial layer by another polygon layer. Use this to extract features
    that fall within a boundary — e.g. restaurants within an isochrone, buildings
    within a catchment area, or points within an administrative boundary.
    """
    import os
    import traceback

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    outputs_dir = caller_outputs_dir()

    try:
        import geopandas as gpd

        # Resolve relative paths — check exec_dir and outputs_dir
        def _res(p):
            return (
                p
                if (os.path.isabs(p) and os.path.exists(p))
                else next(
                    (
                        os.path.join(_b, p)
                        for _b in (exec_dir, outputs_dir)
                        if os.path.exists(os.path.join(_b, p))
                    ),
                    os.path.join(exec_dir, p),
                )
            )

        input_full = _res(input_path)
        clip_full = _res(clip_path)

        if not os.path.exists(input_full):
            return f"Input file not found: {input_path}"
        if not os.path.exists(clip_full):
            return f"Clip file not found: {clip_path}"

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
        output_path = os.path.join(outputs_dir, f"{output_filename}.gpkg")
        clipped.to_file(output_path, driver="GPKG")

        return (
            f"Clipped {len(clipped)} of {len(gdf)} features. "
            f"Saved to outputs/{output_filename}.gpkg"
        )

    except Exception as e:
        return f"Clip failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = clip_layer
TOOL_SCHEMA = ClipLayerArgs
