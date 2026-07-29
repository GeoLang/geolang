"""The geodukt workflow tools: plan first, run only after the user approves.

geodukt-server's /validate and /operations routes are newer than its /run route,
so they are stubbed here the way the other external services are stubbed.
"""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
import respx
from pydantic import ValidationError

from src.agents.tools.list_workflow_operations import (
    ListWorkflowOperationsArgs,
    list_workflow_operations,
)
from src.agents.tools.plan_workflow import PlanWorkflowArgs, plan_workflow
from src.agents.tools.run_workflow import RunWorkflowArgs, run_workflow
from src.api import server

MANIFEST = """
[project]
name = "depot-catchment"

[[source]]
name = "depots"
format = "geojson"
path = "outputs/depots.geojson"

[[transform]]
name = "catchment"
input = "depots"
operation = "buffer"
distance = 500.0

[[sink]]
name = "out"
input = "catchment"
format = "gpkg"
path = "outputs/depot_catchment.gpkg"
"""

# "wide" is declared before the step it consumes, so only geodukt's reported
# order puts the plan in execution order
OUT_OF_ORDER_MANIFEST = """
[project]
name = "two-step"

[[source]]
name = "depots"
format = "geojson"
path = "outputs/depots.geojson"

[[transform]]
name = "wide"
input = "narrow"
operation = "buffer"
distance = 900.0

[[transform]]
name = "narrow"
input = "depots"
operation = "buffer"
distance = 100.0

[[sink]]
name = "out"
input = "wide"
format = "gpkg"
path = "outputs/two_step.gpkg"
"""

RUN_RECORD = {
    "id": 7,
    "status": "Completed",
    "manifest_name": "depot-catchment",
    "steps": [
        {"name": "depots", "feature_count": 12},
        {"name": "catchment", "feature_count": 12},
        {"name": "out", "feature_count": 12},
    ],
}

GP_CATALOG = [
    {
        "name": "buffer",
        "description": "Buffer geometries by a distance",
        "parameters": [
            {
                "name": "distance",
                "param_type": "f64",
                "required": True,
                "description": "Buffer distance in CRS units",
            }
        ],
    },
    {"name": "centroid", "description": "Compute centroids", "parameters": []},
]

# as GET /operations answers it
OPERATIONS = {
    "operations": [
        {
            "name": "buffer",
            "description": "Expand or shrink geometries by a distance in meters",
            "parameters": [
                {
                    "name": "distance",
                    "param_type": "float",
                    "required": False,
                    "default": "1.0",
                    "description": "Buffer distance in meters",
                }
            ],
        }
    ]
}

# as POST /validate answers it: full per-step detail in execution order
VALIDATED = {
    "project": "depot-catchment",
    "version": "0.1.0",
    "steps": [
        {
            "name": "depots",
            "kind": "source",
            "format": "geojson",
            "path": "outputs/depots.geojson",
        },
        {
            "name": "catchment",
            "kind": "transform",
            "operation": "buffer",
            "input": "depots",
            "params": {"distance": 500.0},
        },
        {
            "name": "out",
            "kind": "sink",
            "input": "catchment",
            "format": "gpkg",
            "path": "outputs/depot_catchment.gpkg",
        },
    ],
}


class _FakeGeodukt:
    """geodukt-server stand-in. Responses are (status_code, body) per path."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def _respond(self, url, payload=None):
        path = url.rsplit("8080", 1)[-1] if "8080" in url else url
        key = path.strip("/").replace("/", "_")
        self.calls.append((path, payload))
        if key not in self.responses:
            raise AssertionError(f"unexpected request to {path}")
        status, body = self.responses[key]
        text = body if isinstance(body, str) else json.dumps(body)

        def as_json():
            if isinstance(body, str):
                raise ValueError("not json")
            return body

        return SimpleNamespace(status_code=status, text=text, json=as_json)

    def post(self, url, json=None, timeout=None):
        return self._respond(url, json)

    def get(self, url, timeout=None):
        return self._respond(url)


class _NoHttp:
    def post(self, url, **kwargs):
        raise AssertionError(f"no HTTP expected, got {url}")

    def get(self, url, **kwargs):
        raise AssertionError(f"no HTTP expected, got {url}")


@pytest.fixture
def geodukt(monkeypatch):
    monkeypatch.setenv("GEODUKT_URL", "http://geodukt:8080")

    def install(**responses):
        fake = _FakeGeodukt(**responses)
        monkeypatch.setitem(sys.modules, "requests", fake)
        return fake

    return install


def plan_of(result: str) -> dict:
    assert "__PLAN__:" in result, result
    return json.loads(result.split("__PLAN__:", 1)[1])


def test_manifest_toml_is_required(monkeypatch):
    with pytest.raises(ValidationError):
        PlanWorkflowArgs()
    with pytest.raises(ValidationError):
        RunWorkflowArgs()
    # title is optional, the project name stands in for it
    assert PlanWorkflowArgs(manifest_toml=MANIFEST).title is None
    ListWorkflowOperationsArgs()


def test_unparseable_toml_never_reaches_geodukt(monkeypatch):
    monkeypatch.setitem(sys.modules, "requests", _NoHttp())
    res = plan_workflow("buffer the depots by 500m please")
    assert res.startswith("ERROR")
    assert "TOML" in res
    assert "__PLAN__" not in res


def test_plan_emits_the_structured_plan(geodukt):
    fake = geodukt(validate=(200, VALIDATED))

    res = plan_workflow(MANIFEST, title="Depot catchment areas")

    assert fake.calls == [("/validate", {"manifest": MANIFEST})]
    assert "Nothing has run yet" in res
    assert "validated by geodukt" in res

    plan = plan_of(res)
    assert plan["title"] == "Depot catchment areas"
    assert plan["project"] == "depot-catchment"
    assert [(s["index"], s["kind"], s["name"]) for s in plan["steps"]] == [
        (1, "source", "depots"),
        (2, "transform", "catchment"),
        (3, "sink", "out"),
    ]
    assert plan["steps"][1]["operation"] == "buffer"
    assert plan["steps"][1]["params"] == {"distance": 500.0}
    assert plan["datasets"] == ["outputs/depots.geojson"]
    assert plan["outputs"] == ["outputs/depot_catchment.gpkg"]
    assert plan["formats"] == ["geojson", "gpkg"]
    # the viewer's approve action re-runs exactly this manifest
    assert plan["manifest"] == MANIFEST
    # the marker stays on one line so the stream parser can split on it
    assert "\n" not in res.split("__PLAN__:", 1)[1]


def test_plan_follows_geodukts_execution_order(geodukt):
    geodukt(
        validate=(
            200,
            {"steps": [{"name": n} for n in ("depots", "narrow", "wide", "out")]},
        )
    )

    plan = plan_of(plan_workflow(OUT_OF_ORDER_MANIFEST))

    assert [s["name"] for s in plan["steps"]] == ["depots", "narrow", "wide", "out"]
    assert [s["index"] for s in plan["steps"]] == [1, 2, 3, 4]
    assert plan["title"] == "two-step"


def test_invalid_manifest_comes_back_as_a_fixable_error(geodukt):
    geodukt(
        validate=(
            422,
            {
                "kind": "operation",
                "message": "transform 'catchment' names unknown operation 'buffalo'",
            },
        )
    )

    res = plan_workflow(MANIFEST)

    assert res.startswith("ERROR")
    # the message is lifted out of the error body, not dumped as raw JSON
    assert "transform 'catchment' names unknown operation 'buffalo'" in res
    assert '{"kind"' not in res
    assert "list_workflow_operations" in res
    # no plan means the viewer never offers an approve button for a broken plan
    assert "__PLAN__" not in res


def test_run_failure_message_is_lifted_out_of_the_error_body(geodukt):
    geodukt(run=(422, {"kind": "source", "message": "source 'depots' has no path"}))

    res = run_workflow(MANIFEST)

    assert res.startswith("ERROR")
    assert "source 'depots' has no path" in res
    assert '{"kind"' not in res


def test_plan_still_works_without_the_validate_route(geodukt):
    geodukt(validate=(404, ""))

    res = plan_workflow(MANIFEST)

    assert "not validated" in res
    assert plan_of(res)["outputs"] == ["outputs/depot_catchment.gpkg"]


def test_run_workflow_reports_counts_and_outputs(geodukt):
    fake = geodukt(run=(200, RUN_RECORD))

    res = run_workflow(MANIFEST)

    assert fake.calls == [("/run", {"manifest": MANIFEST})]
    assert 'Workflow "depot-catchment" run 7 completed.' in res
    assert "catchment: 12 features" in res
    assert "wrote outputs/depot_catchment.gpkg (gpkg)" in res
    assert "emit_ui_spec" in res
    assert not res.startswith("ERROR")


def test_run_workflow_surfaces_a_failed_run(geodukt):
    geodukt(run=(200, {"id": 1, "status": {"Failed": "clip: no overlap"}, "steps": []}))

    res = run_workflow(MANIFEST)

    assert res.startswith("ERROR")
    assert "clip: no overlap" in res


def test_operations_fall_back_to_the_gp_catalog(geodukt):
    fake = geodukt(operations=(404, ""), gp_catalog=(200, GP_CATALOG))

    res = list_workflow_operations()

    assert [path for path, _ in fake.calls] == ["/operations", "/gp/catalog"]
    assert "/operations unavailable" in res
    assert "buffer: Buffer geometries by a distance" in res
    assert "distance (f64, required)" in res
    assert "geopackage (gpkg)" in res


def test_operations_prefer_the_transform_catalog(geodukt):
    fake = geodukt(operations=(200, OPERATIONS))

    res = list_workflow_operations()

    assert [path for path, _ in fake.calls] == ["/operations"]
    assert "buffer: Expand or shrink geometries by a distance in meters" in res
    assert "distance (float, optional, default 1.0)" in res


def _ndjson(*events):
    return "".join(json.dumps(e) + "\n" for e in events).encode()


def _collect(message="buffer the depots"):
    async def run():
        return [event async for event in server.agent_event_stream(message)]

    return asyncio.run(run())


def test_plan_marker_becomes_a_plan_event():
    plan = {
        "title": "Depot catchment areas",
        "steps": [{"index": 1, "kind": "sink", "path": "outputs/depot_catchment.gpkg"}],
        "outputs": ["outputs/depot_catchment.gpkg"],
        "manifest": MANIFEST,
    }
    body = _ndjson(
        {"kind": "tool_call", "name": "plan_workflow", "args": "{}"},
        {
            "kind": "tool_return",
            "name": "plan_workflow",
            "content": "Plan ready.\n__PLAN__:" + json.dumps(plan),
        },
        {"kind": "text", "content": "Here is the plan, shall I run it?"},
        {"kind": "done"},
    )
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        events = _collect()

    # the plan streams as its own event, and the un-run output file must not be
    # inferred into a map spec
    assert events == [
        ("progress", "Running plan_workflow…"),
        ("plan", plan),
        ("text", "Here is the plan, shall I run it?"),
    ]


def test_plan_event_renders_as_an_agui_custom_event():
    from ag_ui.encoder import EventEncoder

    frame = server.render_agui_event(EventEncoder(), "plan", {"title": "x"})

    assert '"name":"plan"' in frame.replace(" ", "")
