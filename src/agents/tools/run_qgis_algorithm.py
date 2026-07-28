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
    """Run any QGIS processing algorithm by ID with JSON parameters.

    DISTANCE and other length parameters are in the INPUT layer's CRS units.
    Layers here are usually EPSG:4326, where units are degrees: to buffer in
    metres, first reproject to EPSG:3857 ('native:reprojectlayer'), run the
    buffer, then reproject back to EPSG:4326 for display."""
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
