"""Shared pieces of the geodukt pipeline tools (underscore: not a tool module).

geodukt-server runs TOML manifests: POST /validate checks one without running
it, POST /run executes it. The structured plan is derived from the manifest text
rather than from the /validate reply, so the plan the user approves cannot drift
from what /run will execute.
"""

import os
import re
from pathlib import Path

from src.core import utils
from src.core.errors import PathRefused

DEFAULT_GEODUKT_URL = "http://geodukt:8080"

_PATH_ASSIGNMENT = re.compile(r'^(\s*path\s*=\s*)(["\'])(.*)\2(.*)$')

# geodukt names the operation it refused: "transform 'x' uses operation 'y'
# which cannot run: <why>"
_REFUSED_OPERATION = re.compile(r"operation '([a-z0-9_]+)' which cannot run")

# mirrors SUPPORTED_FORMATS in geodukt-io/src/formats.rs
SUPPORTED_FORMATS = "csv, geojson, geopackage (gpkg), shapefile (shp)"

# the charset geodukt allows in an operation name, so a manifest cannot name a
# module outside this package
_TOOL_NAME = re.compile(r"^[a-z0-9_]+$")


def geodukt_url() -> str:
    return os.environ.get("GEODUKT_URL", DEFAULT_GEODUKT_URL).rstrip("/")


def direct_tool_advice(detail: str) -> str:
    """Guidance for an operation no manifest can run, or "" when it does not apply.

    A rejection that only says "fix it and try again" sends the model round the
    same loop, because there is no manifest that works. Tool names are module
    names in this package, so a same-named module means the operation exists as a
    single-shot tool and that is what the model should call instead.
    """
    match = _REFUSED_OPERATION.search(detail or "")
    if not match:
        return ""
    operation = match.group(1)
    from pathlib import Path

    if not (Path(__file__).parent / f"{operation}.py").exists():
        return ""
    return (
        f"No manifest can run '{operation}', so do NOT call plan_workflow again "
        f"for this. Call the {operation} tool directly instead, and tell the user "
        "you ran it as a single step rather than a workflow."
    )


def operation_runs_caller_code(operation) -> bool:
    """Whether a step's operation names a tool that runs caller-written code.

    An operation name is a module name in this package, the same convention
    direct_tool_advice relies on, so the tool's own TOOL_RUNS_CALLER_CODE
    declaration decides and there is no second list to drift from it.
    """
    if not operation or not _TOOL_NAME.match(str(operation)):
        return False
    from pathlib import Path

    if not (Path(__file__).parent / f"{operation}.py").exists():
        return False

    import importlib

    from src.agents.agent_manager import runs_caller_code

    module = importlib.import_module(f".{operation}", __package__)
    return runs_caller_code(getattr(module, "TOOL_FUNCTION", None))


def parse_manifest(manifest_toml: str):
    """(manifest, error): geodukt judges validity, this only needs it to parse."""
    import tomllib

    try:
        return tomllib.loads(manifest_toml or ""), None
    except Exception as e:
        return None, f"manifest is not valid TOML: {e}"


def _relative_to_exec_dir(path: str | Path) -> str:
    resolved = Path(path).resolve()
    root = Path(utils.EXEC_DIR).resolve()
    if not resolved.is_relative_to(root):
        raise PathRefused(
            f"path names '{path}', which points out of your own directory. "
            "Pick another name."
        )
    return resolved.relative_to(root).as_posix()


def _prefix_into_caller(argument: str, value: str, root_name: str) -> str:
    caller = utils.current_caller_directory()
    rest = value[len(root_name) :].lstrip("/\\")
    rest_parts = Path(rest).parts if rest else ()
    if rest_parts and rest_parts[0] == caller:
        rewritten = f"{root_name}/{rest}" if rest else root_name
    else:
        rewritten = f"{root_name}/{caller}/{rest}" if rest else f"{root_name}/{caller}"
    target = (Path(utils.EXEC_DIR) / rewritten).resolve()
    allowed = Path(
        utils.caller_outputs_dir()
        if root_name == "outputs"
        else utils.caller_user_data_dir()
    ).resolve()
    if not target.is_relative_to(allowed):
        raise PathRefused(
            f"{argument} names '{value}', which points out of your own "
            "directory. Pick another name."
        )
    return _relative_to_exec_dir(target)


def confine_workflow_path(argument: str, value: str, *, sink: bool) -> str:
    """geodukt's cwd is EXEC_DIR and it has no confinement of its own, so
    outputs/foo.gpkg would land in the shared parent."""
    if not value:
        raise PathRefused(f"{argument} has no path")
    if ".." in Path(value).parts:
        raise PathRefused(
            f"{argument} names '{value}', which points out of your own "
            "directory. Pick another name."
        )
    if os.path.isabs(value):
        resolved = Path(value).resolve()
        for directory in (utils.caller_outputs_dir(), utils.caller_user_data_dir()):
            if resolved.is_relative_to(Path(directory).resolve()):
                return _relative_to_exec_dir(resolved)
        raise PathRefused(
            f"{argument} must be a filename in your own outputs, in user_data, "
            f"or in a natural earth set, not an absolute path: '{value}'"
        )
    parts = Path(value).parts
    if parts and parts[0] in ("outputs", "user_data"):
        return _prefix_into_caller(argument, value, parts[0])
    if sink:
        return _relative_to_exec_dir(utils.tool_output_path(argument, value))
    return _relative_to_exec_dir(utils.tool_input_path(argument, value))


def confine_manifest(manifest_toml: str, parsed: dict) -> str:
    for kind, sink in (("source", False), ("sink", True)):
        for entry in parsed.get(kind) or []:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            entry["path"] = confine_workflow_path("path", entry["path"], sink=sink)

    kind = None
    indexes = {"source": -1, "sink": -1}
    current = None
    lines = []
    for line in manifest_toml.splitlines(keepends=True):
        stripped = line.strip()
        if stripped in ("[[source]]", "[[sink]]"):
            kind = stripped[2:-2]
            indexes[kind] += 1
            entries = parsed.get(kind) or []
            current = entries[indexes[kind]] if indexes[kind] < len(entries) else None
        elif stripped.startswith("["):
            kind = None
            current = None
        if current is not None and "path" in current:
            ending = ""
            body = line
            if body.endswith("\r\n"):
                body, ending = body[:-2], "\r\n"
            elif body.endswith("\n"):
                body, ending = body[:-1], "\n"
            match = _PATH_ASSIGNMENT.match(body)
            if match:
                line = (
                    f"{match.group(1)}{match.group(2)}{current['path']}"
                    f"{match.group(2)}{match.group(4)}{ending}"
                )
        lines.append(line)
    return "".join(lines)


def error_detail(resp) -> str:
    """geodukt rejects a manifest with {"kind", "message"} JSON, older builds text."""
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        if body.get("message"):
            return str(body["message"])
        # a run that fails mid-pipeline answers with the run record itself, and
        # the reason is inside its status rather than in a message field
        status = body.get("status")
        if isinstance(status, dict) and status:
            return str(next(iter(status.values())))
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


def plan_payload(
    title: str, manifest: dict, steps: list, manifest_toml: str, validated: bool
) -> dict:
    """Structured plan for the viewer, carrying the manifest run_workflow needs.

    `validated` is False when geodukt has no /validate route, so the panel can
    say the plan was only parsed rather than checked. A step's
    `runs_caller_code` says the same about that step: approving it hands
    something the model wrote to whatever executes it, so the panel marks it
    rather than leaving the user to trust the prose around the plan.
    """
    project = (manifest.get("project") or {}).get("name", "")
    return {
        "title": title or project or "workflow",
        "project": project,
        "validated": validated,
        "steps": [
            dict(
                step,
                index=i + 1,
                runs_caller_code=operation_runs_caller_code(step["operation"]),
            )
            for i, step in enumerate(steps)
        ],
        "datasets": [s["path"] for s in steps if s["kind"] == "source" and s["path"]],
        "outputs": [s["path"] for s in steps if s["kind"] == "sink" and s["path"]],
        "formats": sorted({s["format"] for s in steps if s.get("format")}),
        "manifest": manifest_toml,
    }


_STEP_OUTCOMES = {"completed": "completed", "notrun": "not_run"}


def step_outcome(status, run_completed: bool):
    """(outcome, message) for one step of a run record.

    Older geodukt builds report no per-step status: every step of a completed
    run did run, and for a failed one there is nothing to claim.
    """
    if isinstance(status, dict):
        return "failed", str(next(iter(status.values()), "") or "")
    if not status:
        return ("completed" if run_completed else "unknown"), ""
    return _STEP_OUTCOMES.get(str(status).lower().replace("_", ""), "unknown"), ""


def run_payload(manifest: dict, record: dict, completed: bool, message: str) -> dict:
    """Structured run report for the viewer: per-step outcome and written outputs.

    `written` is what the panel offers as a download, so it stays false for any
    sink whose step did not complete: that file is not there to fetch.
    """
    steps = []
    for step in record.get("steps") or []:
        outcome, detail = step_outcome(step.get("status"), completed)
        steps.append(
            {
                "name": step.get("name", ""),
                "outcome": outcome,
                "feature_count": step.get("feature_count"),
                "message": detail,
            }
        )
    ran = {s["name"] for s in steps if s["outcome"] == "completed"}
    project = (manifest.get("project") or {}).get("name", "")
    return {
        "id": record.get("id"),
        "title": record.get("manifest_name") or project,
        "status": "completed" if completed else "failed",
        "message": message,
        "steps": steps,
        "outputs": [
            {
                "name": sink["name"],
                "path": sink["path"],
                "format": sink["format"],
                "written": completed and (not steps or sink["name"] in ran),
            }
            for sink in manifest_steps(manifest)
            if sink["kind"] == "sink" and sink["path"]
        ],
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
        if step.get("runs_caller_code"):
            text += " [escape hatch: runs caller-written code]"
        lines.append(f"  {step['index']}. {text}")
    return "\n".join(lines)


def escape_hatch_steps(plan: dict) -> list:
    """Names of the plan's steps that run caller-written code."""
    return [step["name"] for step in plan["steps"] if step.get("runs_caller_code")]
