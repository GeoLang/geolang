from pydantic import BaseModel, Field
from src.core.utils import tool_input_path, tool_output_path


class ExportToGPKGArgs(BaseModel):
    dataset_path: str = Field(
        ..., description="Filename of the input shapefile or layer"
    )
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

    # Ensure .gpkg extension
    if not output_filename.lower().endswith(".gpkg"):
        output_filename = output_filename + ".gpkg"
    output_path = tool_output_path("output_filename", output_filename)
    resolved = tool_input_path("dataset_path", dataset_path)

    try:
        gdf = gpd.read_file(resolved)
        gdf.to_file(output_path, driver="GPKG", layer=layer_name)
        return f"✅ Exported to GPKG successfully!\nPath: {output_path}\nFeatures: {len(gdf)}\nLayer name: {layer_name}"
    except Exception as e:
        return f"ERROR exporting to GPKG: {dataset_path}: {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = export_to_gpkg
TOOL_SCHEMA = ExportToGPKGArgs
