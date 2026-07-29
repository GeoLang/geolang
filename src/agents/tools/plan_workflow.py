"""Validate a geodukt manifest and emit the execution plan for approval.

Returns a readable summary for the model plus a ``__PLAN__:{json}`` marker that
server.py's agent_event_stream turns into a "plan" event for the viewer.
Nothing runs here: run_workflow executes the same manifest after the user agrees.
"""

from pydantic import BaseModel, Field
from typing import Optional

from src.core.user_token import service_headers

from ._geodukt import (
    SUPPORTED_FORMATS,
    direct_tool_advice,
    error_detail,
    geodukt_url,
    manifest_steps,
    order_steps,
    parse_manifest,
    plan_payload,
    plan_summary,
    validated_order,
)


class PlanWorkflowArgs(BaseModel):
    manifest_toml: str = Field(
        ...,
        description=(
            "The whole geodukt pipeline as TOML. Tables: [project] (name), "
            "[[source]] (name, format, path, optional crs/layer), [[transform]] "
            "(name, input, operation, plus that operation's own parameters), "
            "[[sink]] (name, input, format, path). 'input' names an earlier step. "
            f"Formats: {SUPPORTED_FORMATS}. Call list_workflow_operations for the "
            "operation names and their parameters. Example:\n"
            '[project]\nname = "depot-catchment"\n\n'
            '[[source]]\nname = "depots"\nformat = "geojson"\n'
            'path = "outputs/depots.geojson"\n\n'
            '[[transform]]\nname = "catchment"\ninput = "depots"\n'
            'operation = "buffer"\ndistance = 500.0\n\n'
            '[[sink]]\nname = "out"\ninput = "catchment"\n'
            'format = "gpkg"\npath = "outputs/depot_catchment.gpkg"'
        ),
    )
    title: Optional[str] = Field(
        None,
        description=(
            "Short human title for the plan, e.g. 'Depot catchment areas'. "
            "Defaults to the manifest's project name."
        ),
    )


def plan_workflow(manifest_toml: str, title: str = None) -> str:
    """Validate a multi-step geoprocessing workflow and show the user the plan
    before anything runs. Compose the pipeline as a geodukt TOML manifest, call
    this, present the returned steps in plain language, and only call
    run_workflow with the same manifest once the user approves. Use this instead
    of chaining buffer_clip_dissolve, clip_layer and friends by hand whenever a
    request needs several chained operations over files. A manifest cannot run
    spatial_join (transforms take one input): call that tool directly."""
    import json

    manifest, error = parse_manifest(manifest_toml)
    if error:
        return f"ERROR: {error} Fix the TOML and call plan_workflow again."

    url = geodukt_url()
    try:
        import requests

        resp = requests.post(
            f"{url}/validate",
            json={"manifest": manifest_toml},
            headers=service_headers(),
            timeout=30,
        )
    except Exception as e:
        return f"ERROR: geodukt is unreachable at {url}: {e}"

    # axum answers an unmounted route with a bodyless 404: older geodukt builds
    # have /run but not /validate, so plan on the manifest alone and say so
    route_missing = resp.status_code == 404 and not (resp.text or "").strip()

    if resp.status_code >= 400 and not route_missing:
        detail = error_detail(resp)
        advice = direct_tool_advice(detail)
        if advice:
            return f"ERROR: geodukt rejected the manifest: {detail}\n{advice}"
        return (
            f"ERROR: geodukt rejected the manifest: {detail}\n"
            f"Fix it and call plan_workflow again. Formats: {SUPPORTED_FORMATS}. "
            "Call list_workflow_operations for valid operation names."
        )

    order = []
    if not route_missing:
        try:
            order = validated_order(resp.json())
        except Exception:
            order = []

    steps = order_steps(manifest_steps(manifest), order)
    if not steps:
        return (
            "ERROR: the manifest has no steps. It needs at least one [[source]] "
            "and one [[sink]] table."
        )

    plan = plan_payload(title, manifest, steps, manifest_toml, not route_missing)
    checked = (
        "not validated (this geodukt build has no /validate endpoint)"
        if route_missing
        else "validated by geodukt"
    )
    lines = [
        f'Plan "{plan["title"]}": {len(steps)} steps, {checked}.',
        plan_summary(plan),
    ]
    if plan["outputs"]:
        lines.append(f"Writes: {', '.join(plan['outputs'])}")
    lines.append(
        "Nothing has run yet. Describe these steps to the user, then call "
        "run_workflow with the same manifest once they approve."
    )
    return "\n".join(lines) + f"\n__PLAN__:{json.dumps(plan, default=str)}"


TOOL_FUNCTION = plan_workflow
TOOL_SCHEMA = PlanWorkflowArgs
