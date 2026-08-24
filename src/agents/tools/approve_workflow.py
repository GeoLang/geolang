"""Record that the person at the viewer approved a planned manifest.

`POST /workflow/approve` is the only caller: the viewer posts the plan's own
manifest when the user clicks approve, and run_workflow refuses a manifest that
has no approval. This is a tool module because the record has to land in the
process that runs plan_workflow and run_workflow, which is the executor when one
is configured, and a tool call is the only thing that reaches it.

`TOOL_APPROVAL_ROUTE_ONLY` keeps it off the tool manifest and off
`POST /tools/{name}`, so nothing the model can call records the click that call
is meant to be waiting for.

Nothing runs and nothing is written here. An approval of text nobody planned is
refused rather than kept, so the halves cannot be recorded out of order.
"""

from pydantic import BaseModel, Field

from src.core.errors import PathRefused
from src.core.planned_manifests import record_user_approval

from ._geodukt import confine_manifest, parse_manifest

APPROVED = "Approved. run_workflow may now execute this manifest."
NOT_PLANNED = (
    "ERROR: this manifest was never planned, so there is nothing to approve. "
    "Call plan_workflow with it first."
)


class ApproveWorkflowArgs(BaseModel):
    manifest_toml: str = Field(
        ...,
        description=(
            "The manifest the plan carries, exactly as plan_workflow returned it."
        ),
    )


def approve_workflow(manifest_toml: str) -> str:
    """Record the user's approval of a manifest plan_workflow validated, so that
    run_workflow may execute it."""
    manifest, error = parse_manifest(manifest_toml)
    if error:
        return f"ERROR: {error}"
    try:
        manifest_toml = confine_manifest(manifest_toml, manifest)
    except PathRefused as e:
        return f"ERROR: {e}"
    if not record_user_approval(manifest_toml):
        return NOT_PLANNED
    return APPROVED


TOOL_FUNCTION = approve_workflow
TOOL_SCHEMA = ApproveWorkflowArgs
# this is the record of a human clicking, so a caller that could ask for it is a
# caller that never had to click
TOOL_APPROVAL_ROUTE_ONLY = True
