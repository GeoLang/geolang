"""Execute a geodukt manifest through the server's synchronous /run endpoint.

Returns a readable summary for the model plus a ``__RUN__:{json}`` marker that
server.py's agent_event_stream turns into a "run" event for the viewer, the same
seam plan_workflow uses for the plan itself.

A manifest plan_workflow never validated, and one the user never approved in the
viewer, are both refused before geodukt is called at all: see core's
planned_manifests.
"""

from pydantic import BaseModel, Field

from src.core.errors import PathRefused
from src.core.planned_manifests import manifest_was_approved, manifest_was_planned
from src.core.user_token import service_headers

from ._geodukt import (
    confine_manifest,
    error_detail,
    geodukt_url,
    parse_manifest,
    run_payload,
)

NOT_PLANNED = (
    "ERROR: this manifest was not planned, so it cannot run. Call plan_workflow "
    "with it, describe the steps it returns to the user, and call run_workflow "
    "again with the same manifest once they approve. If you edited the manifest "
    "after planning it, call plan_workflow again with the edited one."
)

# a bare refusal sends the model looking for another way to do the job, and it
# finds sql_query and the raw geopandas tools, which is the opposite of the
# reviewable plan the user is meant to approve
NOT_APPROVED = (
    "ERROR: the user has not approved this plan, so it cannot run. The manifest "
    "is fine and you have done nothing wrong. Approving is theirs alone: they "
    "press Approve on the plan in the viewer, and no tool you can call does it "
    "for them. Do NOT retry this call, and do NOT fall back to sql_query, "
    "geopandas_api or pyqgis_api to do the work another way. Tell the user the "
    "plan is ready and ask them to approve it."
)


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
    the files it wrote. Only call this after plan_workflow and after the user
    approved that plan in the viewer: an unplanned or unapproved manifest is
    refused here. If the user asked for a change, revise the manifest and call
    plan_workflow again instead."""
    import json

    manifest, error = parse_manifest(manifest_toml)
    if error:
        return f"ERROR: {error}"
    try:
        manifest_toml = confine_manifest(manifest_toml, manifest)
    except PathRefused as e:
        return f"ERROR: {e}"
    if not manifest_was_planned(manifest_toml):
        return NOT_PLANNED
    if not manifest_was_approved(manifest_toml):
        return NOT_APPROVED

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

    try:
        record = resp.json()
    except Exception:
        record = None

    # a mid-pipeline failure answers 4xx with the run record itself, which still
    # carries the per-step detail; a rejected manifest answers {"kind","message"}
    # and never ran a step
    is_record = isinstance(record, dict) and "status" in record
    # a session with no credentials cannot execute. Say so as an instruction: a
    # bare 401 sends the model looking for another way to do the job, and it
    # finds sql_query and the raw geopandas tools, which is the opposite of the
    # reviewable plan the user is meant to approve
    if resp.status_code in (401, 403):
        return (
            "ERROR: this session cannot execute workflows, it has no credentials "
            f"for geodukt ({error_detail(resp)}). The plan itself is fine. Do NOT "
            "retry this, and do NOT fall back to sql_query, geopandas_api or "
            "pyqgis_api to do the work another way. Tell the user the plan is "
            "ready and that they approve it in the viewer to run it."
        )
    if resp.status_code >= 400 and not is_record:
        return f"ERROR: geodukt failed to run the workflow: {error_detail(resp)}"
    if not is_record:
        return f"ERROR: geodukt returned a non-JSON response: {resp.text[:300]}"

    # RunStatus serializes as "Completed" or {"Failed": "reason"}
    status = record["status"]
    completed = not isinstance(status, dict)
    reason = (
        "" if completed else str(next(iter(status.values()), "") or "unknown reason")
    )
    report = run_payload(manifest, record, completed, reason)

    if completed:
        lines = [f'Workflow "{report["title"]}" run {report["id"]} completed.']
    else:
        lines = [f"ERROR: workflow run {report['id']} failed: {reason}"]
    for step in report["steps"]:
        if step["outcome"] == "failed":
            detail = "failed"
            if step["message"]:
                detail += f": {step['message']}"
        elif step["outcome"] == "not_run":
            detail = "did not run"
        else:
            detail = f"{step['feature_count']} features"
        lines.append(f"  {step['name']}: {detail}")

    written = [out for out in report["outputs"] if out["written"]]
    for out in written:
        lines.append(f"  wrote {out['path']} ({out['format']})")
    if written:
        lines.append(
            "Call emit_ui_spec with ui_type='map' to show the output on the map."
        )
    return "\n".join(lines) + f"\n__RUN__:{json.dumps(report, default=str)}"


TOOL_FUNCTION = run_workflow
TOOL_SCHEMA = RunWorkflowArgs
# the approval is a click in the user's own viewer, which an agent reaching this
# service from outside does not have, so it could never get past the gate
TOOL_NEEDS_USER_APPROVAL = True
