import os
from pydantic import BaseModel, Field


class ExportToGPKGArgs(BaseModel):
    dataset_path: str = Field(..., description="Path to input shapefile or layer")
    output_filename: str = Field(
        ..., description="Name of output file (e.g. 'output.gpkg')"
    )
    layer_name: str = Field("layer", description="Name of the layer inside the GPKG")


def export_to_gpkg(
    dataset_path: str,
    output_filename: str,
    layer_name: str = "layer",
) -> str:
    """Reliable GPKG export using GeoPandas."""
    import geopandas as gpd

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    # Ensure .gpkg extension
    if not output_filename.lower().endswith(".gpkg"):
        output_filename = output_filename + ".gpkg"
    output_path = os.path.join(exec_dir, "outputs", output_filename)

    # Resolve dataset_path — check given path, then search known directories
    resolved = dataset_path
    if not os.path.exists(resolved):
        basename = os.path.basename(dataset_path)
        search_dirs = [
            os.path.join(exec_dir, "outputs"),
            os.path.join(exec_dir, "user_data"),
            os.path.join(exec_dir, "natural_earth_110m"),
            os.path.join(exec_dir, "natural_earth_50m"),
            os.path.join(exec_dir, "natural_earth_10m"),
            os.path.join(exec_dir, "natural_earth"),
        ]
        for d in search_dirs:
            candidate = os.path.join(d, basename)
            if os.path.exists(candidate):
                resolved = candidate
                break

    try:
        gdf = gpd.read_file(resolved)
        gdf.to_file(output_path, driver="GPKG", layer=layer_name)
        return f"✅ Exported to GPKG successfully!\nPath: {output_path}\nFeatures: {len(gdf)}\nLayer name: {layer_name}"
    except Exception as e:
        return f"ERROR exporting to GPKG: {dataset_path}: {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = export_to_gpkg
TOOL_SCHEMA = ExportToGPKGArgs
