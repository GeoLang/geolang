import os
import json
from pydantic import BaseModel, Field
from typing import Optional
from src.core.qgis_session import QgisUnavailable, qgis_session
from src.core.utils import PathRefused, tool_input_path, tool_output_path

# the QGIS parameter types whose value names a file to read
INPUT_FILE_TYPES = frozenset(
    {"source", "vector", "raster", "layer", "multilayer", "mesh", "pointcloud", "file"}
)
# ... and the ones whose value names a file to write
DESTINATION_TYPES = frozenset(
    {
        "sink",
        "vectorDestination",
        "rasterDestination",
        "fileDestination",
        "folderDestination",
        "pointCloudDestination",
        "vectorTileDestination",
    }
)
# these name layers inside a structure this tool cannot take apart, so a path in
# one of them would reach QGIS unchecked
UNRESOLVABLE_LAYER_TYPES = frozenset(
    {
        "alignrasterlayers",
        "dxflayers",
        "idw_interpolation_data",
        "tininputlayers",
        "vectortilewriterlayers",
    }
)
# a destination named this is written to a temporary file QGIS picks
TEMPORARY_OUTPUT = "TEMPORARY_OUTPUT"

# the gdal algorithms paste these into the command line they build instead of
# reading them as values, and a token starting with "-" is passed on unquoted,
# so a path written into one reaches GDAL as arguments of its own. The same
# names on a native algorithm go to the raster writer and are left alone.
GDAL_PROVIDER = "gdal"
COMMAND_LINE_PARAMETERS = frozenset({"CREATION_OPTIONS", "EXTRA", "OPTIONS"})


def confined_parameter(key, value, parameter_type):
    """One algorithm parameter, with any file it names resolved to a real file.

    The type comes from the algorithm's own definitions, so a value is treated
    as a filename only where QGIS would open or write one. Guessing it from the
    string refused ordinary values instead: a field calculator FORMULA of
    `"population" / "area"` looks exactly like a path.
    """
    if parameter_type in UNRESOLVABLE_LAYER_TYPES:
        raise PathRefused(
            f"parameters.{key} names layers in a form this tool cannot confine "
            f"to your own files, so '{key}' cannot be used here."
        )
    if isinstance(value, list):
        return [confined_parameter(key, item, parameter_type) for item in value]
    if not isinstance(value, str):
        return value
    if parameter_type in DESTINATION_TYPES:
        if value == TEMPORARY_OUTPUT:
            return value
        return tool_output_path(f"parameters.{key}", value)
    if parameter_type not in INPUT_FILE_TYPES:
        return value
    # a layer can carry a suffix that is not part of the name: "roads.gpkg|layername=x"
    name, separator, suffix = value.partition("|")
    return tool_input_path(f"parameters.{key}", name) + separator + suffix


def confined_parameters(params, parameter_types, command_line_names=frozenset()):
    """Every parameter the caller gave, confined by what the algorithm calls it.

    A name the algorithm does not define is passed through untouched: QGIS
    ignores it, so nothing opens it. A name in `command_line_names` is refused
    outright, because no resolving here would confine a value that is pasted
    into a command line whole. Only the gdal provider has any, so the caller
    passes the empty default for every other one.
    """
    pasted = sorted(
        key
        for key, value in params.items()
        if key in command_line_names and value
    )
    if pasted:
        raise PathRefused(
            f"parameters.{pasted[0]} is passed to gdal as command line options, "
            "so a file named in it would not be confined to your own. Use the "
            "algorithm's own parameters instead."
        )
    return {
        key: confined_parameter(key, value, parameter_types.get(key))
        for key, value in params.items()
    }


class RunQGISAlgorithmArgs(BaseModel):
    algorithm_id: str = Field(
        ...,
        description=(
            "QGIS processing algorithm ID, e.g. 'native:buffer', 'native:clip', "
            "'native:dissolve', 'qgis:reprojectlayer', 'native:fixgeometries'"
        ),
    )
    parameters: str = Field(
        ...,
        description=(
            "JSON string of algorithm input parameters. Common keys: "
            "INPUT (the layer to read), OUTPUT (the file to write), DISTANCE "
            "(for buffer), OVERLAY (for clip/intersection). A layer is named by "
            "its filename alone, never by a path. Example: "
            '{"INPUT": "ne_110m_populated_places.shp", "DISTANCE": 1000}'
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename (e.g. 'result.gpkg'), with no directory part. Saved to your outputs directory. Auto-generated if omitted.",
    )


def run_qgis_algorithm(
    algorithm_id: str,
    parameters: str,
    output_filename: Optional[str] = None,
) -> str:
    """Run any QGIS processing algorithm by ID with JSON parameters.

    DISTANCE and other length parameters are in the INPUT layer's CRS units.
    Layers here are usually EPSG:4326, where units are degrees: to buffer in
    metres, first reproject to EPSG:3857 ('native:reprojectlayer'), run the
    buffer, then reproject back to EPSG:4326 for display."""
    import traceback

    try:
        params = json.loads(parameters)
    except Exception as e:
        return f"ERROR: Invalid JSON parameters: {str(e)}"

    if not isinstance(params, dict):
        return "ERROR: parameters must be a JSON object of parameter names to values"

    given_output = params.pop("OUTPUT", None)

    try:
        session = qgis_session()
    except QgisUnavailable as e:
        return f"❌ '{algorithm_id}' failed: {e}"

    if session.processing is None:
        return (
            f"❌ '{algorithm_id}' failed: QGIS processing module is not available "
            f"({session.processing_error}). Use GeoPandas-based tools instead "
            "(e.g. geopandas_api, spatial_join, clip_layer, buffer_clip_dissolve)."
        )
    processing = session.processing

    try:
        # confinement needs the algorithm's parameter definitions, so it cannot
        # run before this point
        algorithm = session.algorithm_by_id(algorithm_id)
        if algorithm is None:
            return (
                f"❌ '{algorithm_id}' is not a QGIS algorithm ID. Give a provider "
                "and a name, e.g. 'native:buffer'."
            )

        parameter_types = {
            definition.name(): definition.type()
            for definition in algorithm.parameterDefinitions()
        }
        command_line_names = (
            COMMAND_LINE_PARAMETERS
            if algorithm.provider().id() == GDAL_PROVIDER
            else frozenset()
        )
        params = confined_parameters(params, parameter_types, command_line_names)

        if output_filename:
            params["OUTPUT"] = tool_output_path("output_filename", output_filename)
        elif given_output:
            params["OUTPUT"] = tool_output_path("parameters.OUTPUT", str(given_output))
        else:
            safe_name = algorithm_id.replace(":", "_").replace("/", "_")
            params["OUTPUT"] = tool_output_path(
                "output_filename", f"{safe_name}_output.gpkg"
            )

        result = processing.run(algorithm_id, params)

        # degrees-vs-metres mistakes produce coordinates far outside lon/lat
        # range and crash the viewer; catch them here so the model can retry
        out_path = result.get("OUTPUT")
        if isinstance(out_path, str) and os.path.exists(out_path):
            try:
                from osgeo import ogr

                ds = ogr.Open(out_path)
                lyr = ds.GetLayer() if ds else None
                srs = lyr.GetSpatialRef() if lyr else None
                if lyr is not None and srs is not None and srs.IsGeographic():
                    minx, maxx, miny, maxy = lyr.GetExtent()
                    if abs(minx) > 180 or abs(maxx) > 180 or abs(miny) > 90 or abs(maxy) > 90:
                        return (
                            f"❌ '{algorithm_id}' produced coordinates outside the valid "
                            f"lon/lat range (extent {minx:.0f}..{maxx:.0f}, {miny:.0f}..{maxy:.0f}): "
                            "distance parameters were applied in degrees. Reproject INPUT to "
                            "EPSG:3857 ('native:reprojectlayer'), rerun, then reproject the "
                            "result back to EPSG:4326."
                        )
            except Exception:
                pass  # validation only; never mask a successful run

        output_lines = [f"  {k}: {v}" for k, v in result.items()]
        return (
            f"✅ '{algorithm_id}' completed successfully!\n"
            f"Outputs:\n" + "\n".join(output_lines)
        )

    except PathRefused as e:
        return f"❌ '{algorithm_id}' refused a parameter: {e}"
    except Exception as e:
        return f"❌ '{algorithm_id}' failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = run_qgis_algorithm
TOOL_SCHEMA = RunQGISAlgorithmArgs
