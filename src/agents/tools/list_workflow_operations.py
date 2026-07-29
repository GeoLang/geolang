"""List the transforms a geodukt manifest can use, straight from the server."""

from pydantic import BaseModel

from src.core.user_token import service_headers

from ._geodukt import SUPPORTED_FORMATS, geodukt_url


class ListWorkflowOperationsArgs(BaseModel):
    pass  # no arguments needed


def _format_catalog(payload) -> list:
    """Render an operation catalog: a list of entries, or {"operations": [...]}."""
    if isinstance(payload, dict):
        entries = payload.get("operations") or payload.get("tools") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []

    lines = []
    for op in entries:
        if not isinstance(op, dict):
            continue
        lines.append(f"  • {op.get('name')}: {op.get('description', '')}")
        for param in op.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            ptype = param.get("param_type") or param.get("type") or "value"
            need = "required" if param.get("required") else "optional"
            default = param.get("default")
            if default not in (None, ""):
                need += f", default {default}"
            lines.append(
                f"      - {param.get('name')} ({ptype}, {need}): "
                f"{param.get('description', '')}"
            )
    return lines


def list_workflow_operations() -> str:
    """List the geoprocessing operations and file formats a workflow manifest can
    use, with their parameters. Call this before plan_workflow whenever you are
    not certain an operation name or parameter exists, rather than guessing."""
    url = geodukt_url()
    try:
        import requests

        headers = service_headers()
        resp = requests.get(f"{url}/operations", headers=headers, timeout=15)
        source = "geodukt transform catalog"
        if resp.status_code == 404:
            # /operations is newer than the GP tool routes
            resp = requests.get(f"{url}/gp/catalog", headers=headers, timeout=15)
            source = "geodukt GP tool catalog (partial: /operations unavailable)"
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"
            return f"ERROR: geodukt catalog request failed: {detail}"
        payload = resp.json()
    except Exception as e:
        return f"ERROR: geodukt is unreachable at {url}: {e}"

    lines = _format_catalog(payload)
    if not lines:
        return f"ERROR: {source} is empty, so no operations can be planned."
    return (
        f"Workflow operations ({source}):\n"
        + "\n".join(lines)
        + f"\nSource and sink formats: {SUPPORTED_FORMATS}."
        + "\nUse these names as 'operation' in a [[transform]] table, then call "
        "plan_workflow."
    )


TOOL_FUNCTION = list_workflow_operations
TOOL_SCHEMA = ListWorkflowOperationsArgs
