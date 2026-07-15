import os
import json
from pydantic import BaseModel, Field
from typing import Optional


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
            "INPUT (path to input layer), OUTPUT (output path), DISTANCE (for buffer), "
            "OVERLAY (for clip/intersection). Example: "
            '{"INPUT": "/app/geolang/natural_earth/ne_110m_populated_places.shp", "DISTANCE": 1000}'
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description="Output filename (e.g. 'result.gpkg'). Saved to outputs directory. Auto-generated if omitted.",
    )


def run_qgis_algorithm(
    algorithm_id: str,
    parameters: str,
    output_filename: Optional[str] = None,
) -> str:
    """Run any QGIS processing algorithm by ID with JSON parameters."""
    import json
    import os
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

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    out_dir = os.path.join(exec_dir, "outputs")
    os.makedirs(out_dir, exist_ok=True)

    if "OUTPUT" not in params:
        safe_name = algorithm_id.replace(":", "_").replace("/", "_")
        fname = output_filename or f"{safe_name}_output.gpkg"
        params["OUTPUT"] = os.path.join(out_dir, fname)
    elif output_filename:
        params["OUTPUT"] = os.path.join(out_dir, output_filename)

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

        output_lines = [f"  {k}: {v}" for k, v in result.items()]
        return (
            f"✅ '{algorithm_id}' completed successfully!\n"
            f"Outputs:\n" + "\n".join(output_lines)
        )

    except Exception as e:
        return f"❌ '{algorithm_id}' failed: {str(e)}\n{traceback.format_exc()}"


TOOL_FUNCTION = run_qgis_algorithm
TOOL_SCHEMA = RunQGISAlgorithmArgs
