from pydantic import BaseModel


class ListQgisAlgorithmsArgs(BaseModel):
    pass  # no arguments needed


def list_qgis_algorithms() -> str:
    """List all available QGIS processing algorithms."""
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
        algs = QgsApplication.processingRegistry().algorithms()
        result = ["=== AVAILABLE QGIS ALGORITHMS (server-side) ==="]
        for alg in sorted(algs, key=lambda a: a.id()):
            result.append(f"• {alg.id()}  →  {alg.displayName()}")
        result.append(f"\nTotal: {len(algs)} algorithms ready to use!")
        return "\n".join(result)
    except Exception as e:
        return f"❌ QGIS failed: {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = list_qgis_algorithms
TOOL_SCHEMA = ListQgisAlgorithmsArgs
