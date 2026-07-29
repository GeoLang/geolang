"""Execute a geodukt manifest through the server's synchronous /run endpoint."""

from pydantic import BaseModel, Field

from src.core.user_token import service_headers

from ._geodukt import error_detail, geodukt_url, manifest_steps, parse_manifest


class RunWorkflowArgs(BaseModel):
    manifest_toml: str = Field(
        ...,
        description=(
            "The same TOML manifest that plan_workflow validated and the user "
            "approved. Pass it back unchanged unless the user asked for an edit."
        ),
    )


def run_workflow(manifest_toml: str) -> str:
    """Execute a geodukt pipeline manifest and report per-step feature counts and
    the files it wrote. Only call this after plan_workflow and after the user has
    approved that plan. If the user asked for a change, revise the manifest and
    call plan_workflow again instead of running it."""
    manifest, error = parse_manifest(manifest_toml)
    if error:
        return f"ERROR: {error}"

    url = geodukt_url()
    try:
        import requests

        # a pipeline can chew through large inputs, so allow a long read.
        # /run is the gated route: without a token geodukt answers 401 unless it
        # is running without a platform secret
        resp = requests.post(
            f"{url}/run",
            json={"manifest": manifest_toml},
            headers=service_headers(),
            timeout=(10, 600),
        )
    except Exception as e:
        return f"ERROR: geodukt is unreachable at {url}: {e}"

    if resp.status_code >= 400:
        return f"ERROR: geodukt failed to run the workflow: {error_detail(resp)}"

    try:
        record = resp.json()
    except Exception:
        return f"ERROR: geodukt returned a non-JSON response: {resp.text[:300]}"

    # RunStatus serializes as "Completed" or {"Failed": "reason"}
    status = record.get("status")
    if isinstance(status, dict):
        reason = next(iter(status.values()), "unknown reason")
        return f"ERROR: workflow run {record.get('id')} failed: {reason}"

    name = record.get("manifest_name") or (manifest.get("project") or {}).get(
        "name", ""
    )
    lines = [f'Workflow "{name}" run {record.get("id")} {str(status).lower()}.']
    for step in record.get("steps") or []:
        lines.append(f"  {step.get('name')}: {step.get('feature_count')} features")

    sinks = [s for s in manifest_steps(manifest) if s["kind"] == "sink" and s["path"]]
    for sink in sinks:
        lines.append(f"  wrote {sink['path']} ({sink['format']})")
    if sinks:
        lines.append(
            "Call emit_ui_spec with ui_type='map' to show the output on the map."
        )
    return "\n".join(lines)


TOOL_FUNCTION = run_workflow
TOOL_SCHEMA = RunWorkflowArgs
