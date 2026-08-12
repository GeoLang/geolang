import os
import json
from pydantic import BaseModel, Field
from typing import Optional
from src.core.utils import PathRefused, tool_input_path, tool_output_path

# what a parameter value has to end in to be read as a layer to open
LAYER_EXTENSIONS = (
    ".gpkg",
    ".shp",
    ".geojson",
    ".json",
    ".tif",
    ".tiff",
    ".csv",
    ".gml",
    ".kml",
)


def confined_parameter(key, value):
    """One algorithm parameter, with any layer it names resolved to a real file.

    Every other parameter shape QGIS takes is a value rather than a path, so a
    value that still looks like a path is refused: this tool cannot tell which
    of an algorithm's parameters it would open, and a wrong guess here opens
    another caller's file.
    """
    if isinstance(value, list):
        return [confined_parameter(key, item) for item in value]
    if not isinstance(value, str):
        return value
    # a layer can carry a suffix that is not part of the name: "roads.gpkg|layername=x"
    name, separator, suffix = value.partition("|")
    if name.lower().endswith(LAYER_EXTENSIONS):
        return tool_input_path(f"parameters.{key}", name) + separator + suffix
    if any(mark in value for mark in ("/", "\\", "..")):
        raise PathRefused(
            f"parameters.{key} looks like a path. Name a layer of your own by "
            f"its filename alone, e.g. 'roads.gpkg': '{value}'"
        )
    return value


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
    import sys
    import traceback

    # the tool venv is isolated; qgis bindings and the processing plugin live
    # in the system paths, so bridge them onto sys.path
    for p in (
        "/usr/lib/python3/dist-packages",
        "/usr/share/qgis/python",
        "/usr/share/qgis/python/plugins",
    ):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.append(p)

    try:
        params = json.loads(parameters)
    except Exception as e:
        return f"ERROR: Invalid JSON parameters: {str(e)}"

    if not isinstance(params, dict):
        return "ERROR: parameters must be a JSON object of parameter names to values"

    given_output = params.pop("OUTPUT", None)
    params = {key: confined_parameter(key, value) for key, value in params.items()}

    if output_filename:
        params["OUTPUT"] = tool_output_path("output_filename", output_filename)
    elif given_output:
        params["OUTPUT"] = tool_output_path("parameters.OUTPUT", str(given_output))
    else:
        safe_name = algorithm_id.replace(":", "_").replace("/", "_")
        params["OUTPUT"] = tool_output_path(
            "output_filename", f"{safe_name}_output.gpkg"
        )

    try:
        from qgis.core import QgsApplication
        from qgis.analysis import QgsNativeAlgorithms

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QGIS_PREFIX_PATH"] = "/usr"
        QgsApplication.setPrefixPath("/usr", True)
        qgs = QgsApplication([], False)
        qgs.initQgis()
        QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

        try:
            import processing
            from processing.core.Processing import Processing

            Processing.initialize()
        except ImportError:
            qgs.exitQgis()
            return (
                f"❌ '{algorithm_id}' failed: QGIS processing module is not available. "
                "Use GeoPandas-based tools instead (e.g. geopandas_api, spatial_join, clip_layer, buffer_clip_dissolve)."
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

    except Exception as e:
        return f"❌ '{algorithm_id}' failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = run_qgis_algorithm
TOOL_SCHEMA = RunQGISAlgorithmArgs
