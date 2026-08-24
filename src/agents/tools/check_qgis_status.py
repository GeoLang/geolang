from pydantic import BaseModel

from src.core.qgis_session import QgisUnavailable, qgis_session


class CheckQgisStatusArgs(BaseModel):
    pass  # no arguments needed


def check_qgis_status() -> str:
    """Diagnostic tool — run this FIRST."""
    try:
        session = qgis_session()
    except QgisUnavailable as e:
        return f"❌ QGIS failed: {e}"

    algorithm_count = len(session.algorithms())
    if session.processing is not None:
        return f"✅ QGIS is FULLY WORKING!\nTotal algorithms: {algorithm_count}"
    return (
        f"⚠️ QGIS core works ({algorithm_count} algorithms) but processing module is NOT "
        f"available ({session.processing_error}). run_qgis_algorithm will fail. Use "
        "GeoPandas-based tools instead."
    )


# Required for auto-registration
TOOL_FUNCTION = check_qgis_status
TOOL_SCHEMA = CheckQgisStatusArgs
