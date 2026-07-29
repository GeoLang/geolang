"""Shared pieces of the geodukt pipeline tools (underscore: not a tool module).

geodukt-server runs TOML manifests: POST /validate checks one without running
it, POST /run executes it. The structured plan is derived from the manifest text
rather than from the /validate reply, so the plan the user approves cannot drift
from what /run will execute.
"""

import os

DEFAULT_GEODUKT_URL = "http://geodukt:8080"

# mirrors SUPPORTED_FORMATS in geodukt-io/src/formats.rs
SUPPORTED_FORMATS = "csv, geojson, geopackage (gpkg), shapefile (shp)"


def geodukt_url() -> str:
    return os.environ.get("GEODUKT_URL", DEFAULT_GEODUKT_URL).rstrip("/")


def parse_manifest(manifest_toml: str):
    """(manifest, error): geodukt judges validity, this only needs it to parse."""
    import tomllib

    try:
        return tomllib.loads(manifest_toml or ""), None
    except Exception as e:
        return None, f"manifest is not valid TOML: {e}"


def error_detail(resp) -> str:
    """geodukt rejects a manifest with {"kind", "message"} JSON, older builds text."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
    except Exception:
        pass
    return (resp.text or "").strip()[:600] or f"HTTP {resp.status_code}"


def manifest_steps(manifest: dict) -> list:
    """Flatten a manifest into source, transform and sink steps."""
    steps = []
    for src in manifest.get("source") or []:
        steps.append(
            {
                "kind": "source",
                "name": src.get("name", ""),
                "operation": None,
                "input": None,
                "format": src.get("format"),
                "path": src.get("path"),
                "params": {
                    k: v for k, v in src.items() if k not in ("name", "format", "path")
                },
            }
        )
    for tf in manifest.get("transform") or []:
        steps.append(
            {
                "kind": "transform",
                "name": tf.get("name", ""),
                "operation": tf.get("operation"),
                "input": tf.get("input"),
                "format": None,
                "path": None,
                "params": {
                    k: v
                    for k, v in tf.items()
                    if k not in ("name", "operation", "input")
                },
            }
        )
    for sink in manifest.get("sink") or []:
        steps.append(
            {
                "kind": "sink",
                "name": sink.get("name", ""),
                "operation": None,
                "input": sink.get("input"),
                "format": sink.get("format"),
                "path": sink.get("path"),
                "params": {
                    k: v
                    for k, v in sink.items()
                    if k not in ("name", "format", "path", "input")
                },
            }
        )
    return steps


def validated_order(payload) -> list:
    """Step names in execution order, from whatever shape /validate returns."""
    if isinstance(payload, dict):
        entries = payload.get("steps") or payload.get("order") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    names = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
    return names


def order_steps(steps: list, order: list) -> list:
    """Reorder steps by geodukt's execution order, when it covers every step."""
    if not order:
        return steps
    rank = {name: i for i, name in enumerate(order)}
    if any(step["name"] not in rank for step in steps):
        return steps
    return sorted(steps, key=lambda step: rank[step["name"]])


def plan_payload(title: str, manifest: dict, steps: list, manifest_toml: str) -> dict:
    """Structured plan for the viewer, carrying the manifest run_workflow needs."""
    project = (manifest.get("project") or {}).get("name", "")
    return {
        "title": title or project or "workflow",
        "project": project,
        "steps": [dict(step, index=i + 1) for i, step in enumerate(steps)],
        "datasets": [s["path"] for s in steps if s["kind"] == "source" and s["path"]],
        "outputs": [s["path"] for s in steps if s["kind"] == "sink" and s["path"]],
        "formats": sorted({s["format"] for s in steps if s.get("format")}),
        "manifest": manifest_toml,
    }


def plan_summary(plan: dict) -> str:
    """The same plan as prose, so the model can read it back to the user."""
    lines = []
    for step in plan["steps"]:
        params = ", ".join(f"{k}={v}" for k, v in step["params"].items())
        if step["kind"] == "source":
            text = f"read {step['name']} from {step['path']} ({step['format']})"
        elif step["kind"] == "transform":
            text = f"{step['operation']} {step['input']} -> {step['name']}"
            if params:
                text += f" ({params})"
        else:
            text = f"write {step['input']} to {step['path']} ({step['format']})"
        lines.append(f"  {step['index']}. {text}")
    return "\n".join(lines)
