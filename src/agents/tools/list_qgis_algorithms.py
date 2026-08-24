from pydantic import BaseModel

from src.core.qgis_session import QgisUnavailable, qgis_session


class ListQgisAlgorithmsArgs(BaseModel):
    pass  # no arguments needed


def list_qgis_algorithms() -> str:
    """List all available QGIS processing algorithms."""
    try:
        session = qgis_session()
    except QgisUnavailable as e:
        return f"❌ QGIS failed: {e}"

    algorithms = session.algorithms()
    lines = ["=== AVAILABLE QGIS ALGORITHMS (server-side) ==="]
    for algorithm in sorted(algorithms, key=lambda a: a.id()):
        lines.append(f"• {algorithm.id()}  →  {algorithm.displayName()}")
    lines.append(f"\nTotal: {len(algorithms)} algorithms ready to use!")
    return "\n".join(lines)


# Required for auto-registration
TOOL_FUNCTION = list_qgis_algorithms
TOOL_SCHEMA = ListQgisAlgorithmsArgs
