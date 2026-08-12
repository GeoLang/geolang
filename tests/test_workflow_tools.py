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

from src.agents.tools._geodukt import operation_runs_caller_code
from src.agents.tools.list_workflow_operations import (
    ListWorkflowOperationsArgs,
    list_workflow_operations,
)
from src.agents.tools.plan_workflow import PlanWorkflowArgs, plan_workflow
from src.agents.tools.run_workflow import RunWorkflowArgs, run_workflow
from src.api import server
from src.core.user_token import user_token_scope

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

# geodukt has no sql_query operation, this package has a sql_query tool that runs
# the model's SQL in the user's own browser
ESCAPE_HATCH_MANIFEST = """
[project]
name = "ad-hoc"

[[source]]
name = "places"
format = "geojson"
path = "outputs/places.geojson"

[[transform]]
name = "big"
input = "places"
operation = "sql_query"
sql = "SELECT * FROM places WHERE pop > 1000"

[[sink]]
name = "out"
input = "big"
format = "gpkg"
path = "outputs/big_places.gpkg"
"""

# an older geodukt build: a run status but no per-step one
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

RUN_RECORD_WITH_STEP_STATUS = {
    **RUN_RECORD,
    "steps": [dict(s, status="Completed") for s in RUN_RECORD["steps"]],
}

# a run that died in the middle: the failing step names the reason and the steps
# after it never started
FAILED_RUN_RECORD = {
    "id": 8,
    "status": {"Failed": "transform error for 'catchment': no overlap"},
    "manifest_name": "depot-catchment",
    "manifest": MANIFEST,
    "steps": [
        {"name": "depots", "feature_count": 12, "status": "Completed"},
        {
            "name": "catchment",
            "feature_count": 0,
            "status": {"Failed": "no overlap"},
        },
        {"name": "out", "feature_count": 0, "status": "NotRun"},
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


SPATIAL_JOIN_MANIFEST = """
[project]
name = "sj"

[[source]]
name = "pts"
format = "geojson"
path = "outputs/pts.geojson"

[[transform]]
name = "tagged"
input = "pts"
operation = "spatial_join"

[[sink]]
name = "out"
input = "tagged"
format = "gpkg"
path = "outputs/sj.gpkg"
"""


class _FakeGeodukt:
    """geodukt-server stand-in. Responses are (status_code, body) per path."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []
        # kept apart from calls so the call assertions stay about the payload
        self.headers = []

    def _respond(self, url, payload=None, headers=None):
        path = url.rsplit("8080", 1)[-1] if "8080" in url else url
        key = path.strip("/").replace("/", "_")
        self.calls.append((path, payload))
        self.headers.append(headers or {})
        if key not in self.responses:
            raise AssertionError(f"unexpected request to {path}")
        status, body = self.responses[key]
        text = body if isinstance(body, str) else json.dumps(body)

        def as_json():
            if isinstance(body, str):
                raise ValueError("not json")
            return body

        return SimpleNamespace(status_code=status, text=text, json=as_json)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._respond(url, json, headers)

    def get(self, url, headers=None, timeout=None):
        return self._respond(url, headers=headers)


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


def report_of(result: str) -> dict:
    assert "__RUN__:" in result, result
    return json.loads(result.split("__RUN__:", 1)[1])


def prose_of(result: str) -> str:
    """What the model and the panel show: everything above the marker."""
    return result.split("__RUN__:", 1)[0]


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
    assert plan["validated"] is True
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


def test_a_mid_pipeline_failure_reports_the_status_reason(geodukt):
    # geodukt answers a failed run with the run record, which echoes the whole
    # manifest: the reason lives in status, and dumping the body would bury it
    geodukt(
        run=(
            422,
            {
                "id": 0,
                "status": {"Failed": "transform error for 'clip': no overlap"},
                "manifest_name": "depot-catchment",
                "manifest": MANIFEST,
                "steps": [],
            },
        )
    )

    res = run_workflow(MANIFEST)

    assert res.startswith("ERROR")
    assert "transform error for 'clip': no overlap" in res
    assert "[[source]]" not in res


def test_plan_still_works_without_the_validate_route(geodukt):
    geodukt(validate=(404, ""))

    res = plan_workflow(MANIFEST)

    assert "not validated" in res
    plan = plan_of(res)
    assert plan["outputs"] == ["outputs/depot_catchment.gpkg"]
    # the panel needs this as a flag, not as prose in the summary
    assert plan["validated"] is False


def test_a_step_that_runs_caller_code_is_labelled_in_the_plan(geodukt):
    # a geodukt with /validate rejects an operation it does not have, so the
    # label is what the panel has to go on when the build cannot check at all
    geodukt(validate=(404, ""))

    res = plan_workflow(ESCAPE_HATCH_MANIFEST)

    plan = plan_of(res)
    assert [s["runs_caller_code"] for s in plan["steps"]] == [False, True, False]
    # and in the prose the model reads back, per step and once for the plan
    assert "[escape hatch: runs caller-written code]" in res
    assert "Escape hatch: big runs code you wrote" in res


def test_an_ordinary_plan_carries_no_escape_hatch_label(geodukt):
    geodukt(validate=(200, VALIDATED))

    res = plan_workflow(MANIFEST)

    assert [s["runs_caller_code"] for s in plan_of(res)["steps"]] == [
        False,
        False,
        False,
    ]
    assert "escape hatch" not in res.lower()


def test_the_label_follows_the_tools_own_declaration():
    # not a list kept here: sql_query.py sets TOOL_RUNS_CALLER_CODE, spatial_join
    # is a tool module too and does not
    assert operation_runs_caller_code("sql_query") is True
    assert operation_runs_caller_code("spatial_join") is False
    assert operation_runs_caller_code("buffer") is False
    assert operation_runs_caller_code(None) is False


def test_run_workflow_reports_counts_and_outputs(geodukt):
    fake = geodukt(run=(200, RUN_RECORD))

    res = run_workflow(MANIFEST)

    assert fake.calls == [("/run", {"manifest": MANIFEST})]
    assert 'Workflow "depot-catchment" run 7 completed.' in res
    assert "catchment: 12 features" in res
    assert "wrote outputs/depot_catchment.gpkg (gpkg)" in res
    assert "emit_ui_spec" in res
    assert not res.startswith("ERROR")
    # no per-step status from this build, but the run completed, so every step did
    report = report_of(res)
    assert {s["outcome"] for s in report["steps"]} == {"completed"}
    assert report["outputs"] == [
        {
            "name": "out",
            "path": "outputs/depot_catchment.gpkg",
            "format": "gpkg",
            "written": True,
        }
    ]


def test_a_successful_run_emits_the_structured_report(geodukt):
    geodukt(run=(200, RUN_RECORD_WITH_STEP_STATUS))

    res = run_workflow(MANIFEST)

    report = report_of(res)
    assert report["id"] == 7
    assert report["title"] == "depot-catchment"
    assert report["status"] == "completed"
    assert report["message"] == ""
    assert [(s["name"], s["outcome"], s["feature_count"]) for s in report["steps"]] == [
        ("depots", "completed", 12),
        ("catchment", "completed", 12),
        ("out", "completed", 12),
    ]
    # the panel only offers a download for a file that was actually written
    assert [(o["path"], o["written"]) for o in report["outputs"]] == [
        ("outputs/depot_catchment.gpkg", True)
    ]
    # the marker stays on one line so the stream parser can split on it
    assert "\n" not in res.split("__RUN__:", 1)[1]
    assert "__RUN__" not in prose_of(res)


def test_a_mid_pipeline_failure_reports_every_step(geodukt):
    # geodukt answers a failed run 4xx with the record, so the per-step detail is
    # in the error body rather than in a successful reply
    geodukt(run=(422, FAILED_RUN_RECORD))

    res = run_workflow(MANIFEST)

    assert res.startswith("ERROR")
    assert "transform error for 'catchment': no overlap" in prose_of(res)
    assert "catchment: failed: no overlap" in prose_of(res)
    assert "out: did not run" in prose_of(res)
    # a failed run wrote nothing, so it must not advertise the output
    assert "wrote outputs" not in prose_of(res)
    assert "emit_ui_spec" not in prose_of(res)
    # the record echoes the whole manifest and neither the prose nor the report
    # may drag it along
    assert "[[source]]" not in res

    report = report_of(res)
    assert report["status"] == "failed"
    assert report["message"] == "transform error for 'catchment': no overlap"
    assert [(s["name"], s["outcome"], s["message"]) for s in report["steps"]] == [
        ("depots", "completed", ""),
        ("catchment", "failed", "no overlap"),
        ("out", "not_run", ""),
    ]
    assert [(o["path"], o["written"]) for o in report["outputs"]] == [
        ("outputs/depot_catchment.gpkg", False)
    ]


def test_the_run_goes_out_as_the_user_who_approved_it(geodukt):
    """geodukt gates /run on an editor or admin token, so the approval has to
    reach it as the person who gave it, not as geolang."""
    fake = geodukt(run=(200, RUN_RECORD))

    with user_token_scope("header.payload.signature"):
        run_workflow(MANIFEST)

    assert fake.headers == [{"Authorization": "Bearer header.payload.signature"}]


def test_a_headless_run_carries_no_token(geodukt):
    # the eval harness runs with nobody signed in: no header, and geodukt
    # answers 401 unless it is running without a platform secret
    fake = geodukt(run=(200, RUN_RECORD))

    run_workflow(MANIFEST)

    assert fake.headers == [{}]


def test_planning_and_the_catalog_travel_as_the_caller_too(geodukt):
    fake = geodukt(validate=(200, VALIDATED), operations=(200, OPERATIONS))

    with user_token_scope("header.payload.signature"):
        plan_workflow(MANIFEST)
        list_workflow_operations()

    assert fake.headers == [
        {"Authorization": "Bearer header.payload.signature"},
        {"Authorization": "Bearer header.payload.signature"},
    ]


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


def test_run_marker_becomes_a_run_event():
    report = {
        "id": 7,
        "title": "depot-catchment",
        "status": "completed",
        "steps": [{"name": "out", "outcome": "completed", "feature_count": 12}],
        "outputs": [{"path": "outputs/depot_catchment.gpkg", "written": True}],
    }
    body = _ndjson(
        {"kind": "tool_call", "name": "run_workflow", "args": "{}"},
        {
            "kind": "tool_return",
            "name": "run_workflow",
            "content": "Workflow ran.\n__RUN__:" + json.dumps(report),
        },
        {"kind": "text", "content": "Done, it wrote the catchment layer."},
        {"kind": "done"},
    )
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        events = _collect()

    assert ("run", report) in events
    # the marker is for the panel: nothing the user reads may carry it
    assert not [p for kind, p in events if kind == "text" and "__RUN__" in str(p)]


def test_a_failed_run_streams_its_per_step_outcome():
    report = {
        "id": 8,
        "status": "failed",
        "message": "no overlap",
        "steps": [
            {"name": "depots", "outcome": "completed", "feature_count": 12},
            {"name": "catchment", "outcome": "failed", "message": "no overlap"},
            {"name": "out", "outcome": "not_run"},
        ],
        "outputs": [{"path": "outputs/depot_catchment.gpkg", "written": False}],
    }
    body = _ndjson(
        {
            "kind": "tool_return",
            "name": "run_workflow",
            "content": "ERROR: workflow run 8 failed: no overlap\n__RUN__:"
            + json.dumps(report),
        },
        {"kind": "done"},
    )
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        events = _collect()

    # the tool error surfaces as progress, and the marker line is not part of it
    assert ("progress", "ERROR: workflow run 8 failed: no overlap") in events
    assert ("run", report) in events


def test_a_viewer_run_is_reported_back_into_the_session(geodukt, monkeypatch):
    geodukt(run=(200, RUN_RECORD))
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(server, "notify_agent", fake_notify)

    body = server.run_tool(
        "run_workflow",
        server.ToolCallRequest(args={"manifest_toml": MANIFEST}, notify=True),
    )

    assert "catchment: 12 features" in body["result"]
    assert len(sent) == 1
    assert sent[0].startswith("[run_workflow run from the viewer]")
    # the counts have to survive into the session or a follow-up question cannot use them
    assert "catchment: 12 features" in sent[0]
    # the viewer parses the marker itself: truncating it into the session would
    # leave the model half a JSON blob
    assert "__RUN__" not in sent[0]


def test_an_approved_run_reaches_geodukt_as_the_approving_user(geodukt):
    """The viewer's approve button posts the tool call itself, so its bearer is
    what the executor must run the tool under."""
    fake = geodukt(run=(200, RUN_RECORD))

    body = server.run_tool(
        "run_workflow",
        server.ToolCallRequest(args={"manifest_toml": MANIFEST}),
        authorization="Bearer header.payload.signature",
    )

    assert "catchment: 12 features" in body["result"]
    assert fake.headers == [{"Authorization": "Bearer header.payload.signature"}]


def test_the_models_own_run_is_not_reported_twice(geodukt, monkeypatch):
    geodukt(run=(200, RUN_RECORD))
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(server, "notify_agent", fake_notify)

    # exactly the body sibyl sends: no notify field at all
    body = server.run_tool(
        "run_workflow", server.ToolCallRequest(**{"args": {"manifest_toml": MANIFEST}})
    )

    assert "catchment: 12 features" in body["result"]
    assert sent == []


def test_a_failed_viewer_run_is_still_reported(geodukt, monkeypatch):
    geodukt(run=(422, {"kind": "sink", "message": "sink 'out' has no path"}))
    sent = []

    async def fake_notify(text):
        sent.append(text)

    monkeypatch.setattr(server, "notify_agent", fake_notify)

    server.run_tool(
        "run_workflow",
        server.ToolCallRequest(args={"manifest_toml": MANIFEST}, notify=True),
    )

    assert len(sent) == 1
    assert "sink 'out' has no path" in sent[0]


def test_plan_event_renders_as_an_agui_custom_event():
    from ag_ui.encoder import EventEncoder

    frame = server.render_agui_event(EventEncoder(), "plan", {"title": "x"})

    assert '"name":"plan"' in frame.replace(" ", "")


def test_an_impossible_operation_sends_the_model_to_the_direct_tool(geodukt):
    """Retrying the manifest cannot work, so the rejection must not ask for it."""
    geodukt(
        validate=(
            422,
            {
                "kind": "invalid",
                "message": (
                    "transform 'tagged' uses operation 'spatial_join' which cannot "
                    "run: a manifest cannot supply the second dataset to join against"
                ),
            },
        )
    )
    result = plan_workflow(SPATIAL_JOIN_MANIFEST)

    assert "call the spatial_join tool directly" in result.lower()
    assert "call plan_workflow again" not in result.replace("do NOT call plan_workflow again", "")
    assert "__PLAN__" not in result


def test_an_ordinary_rejection_still_asks_for_a_fix(geodukt):
    geodukt(validate=(422, {"kind": "invalid", "message": "sink 'out' has no input"}))
    result = plan_workflow(SPATIAL_JOIN_MANIFEST)

    assert "Fix it and call plan_workflow again" in result
    assert "directly" not in result


def test_an_unauthorized_run_tells_the_model_to_stop_and_ask(geodukt):
    """A bare 401 sent the model looking for another way and it found raw tools."""
    geodukt(run=(401, {"error": "missing bearer token"}))
    result = run_workflow(MANIFEST)

    assert "cannot execute workflows" in result
    assert "approve it in the viewer" in result
    # the exact fallbacks it reached for when the error said nothing useful
    for tool in ("sql_query", "geopandas_api", "pyqgis_api"):
        assert tool in result
    assert "__RUN__" not in result


def test_a_forbidden_run_is_treated_the_same(geodukt):
    geodukt(run=(403, {"error": "editor or admin role required"}))
    result = run_workflow(MANIFEST)

    assert "cannot execute workflows" in result
    assert "editor or admin role required" in result
