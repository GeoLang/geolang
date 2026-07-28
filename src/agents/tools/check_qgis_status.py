from pydantic import BaseModel


class CheckQgisStatusArgs(BaseModel):
    pass  # no arguments needed


def check_qgis_status() -> str:
    """Diagnostic tool — run this FIRST."""
    import os
    import sys

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
        from qgis.core import QgsApplication
        from qgis.analysis import QgsNativeAlgorithms

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QGIS_PREFIX_PATH"] = "/usr"
        QgsApplication.setPrefixPath("/usr", True)
        qgs = QgsApplication([], False)
        qgs.initQgis()
        QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
        alg_count = len(QgsApplication.processingRegistry().algorithms())

        processing_ok = False
        try:
            import processing

            processing_ok = True
        except ImportError:
            pass
        qgs.exitQgis()

        if processing_ok:
            return f"✅ QGIS is FULLY WORKING!\nTotal algorithms: {alg_count}"
        else:
            return (
                f"⚠️ QGIS core works ({alg_count} algorithms) but processing module is NOT available. "
                "run_qgis_algorithm will fail. Use GeoPandas-based tools instead."
            )
    except Exception as e:
        return f"❌ QGIS failed: {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = check_qgis_status
TOOL_SCHEMA = CheckQgisStatusArgs
