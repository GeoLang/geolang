"""The workflow eval scorer: same manifest in, same score out, no prose grading.

The golden tasks and their reference answers are fixtures in evals/, so these
tests also guard that every shipped task is satisfiable by a real manifest.
"""

import json
from pathlib import Path

import pytest
import respx

from evals import runner
from evals.runner import (
    captured_manifest,
    ensure_fixtures,
    markdown_report,
    score_from_directory,
    stack_skip_reason,
)
from evals.scoring import (
    Check,
    Result,
    Task,
    TaskSamples,
    aggregate,
    canon_format,
    load_tasks,
    score_manifest,
    topological_transforms,
)

TASKS_DIR = Path(__file__).resolve().parent.parent / "evals" / "tasks"
REFERENCE_DIR = Path(__file__).resolve().parent.parent / "evals" / "reference"

BUFFER_TASK = Task(
    {
        "id": "buffer",
        "prompt": "buffer it",
        "expect": {
            "source": [{"format": "geojson"}],
            "transform": [
                {
                    "operation": "buffer",
                    "params": {"distance": 500.0},
                    "tolerance": {"distance": 1.0},
                }
            ],
            "sink": [{"format": "geopackage"}],
        },
    }
)

CHAIN_TASK = Task(
    {
        "id": "chain",
        "prompt": "reproject then buffer",
        "expect": {
            "source": [{"format": "geojson"}],
            "transform": [
                {"operation": "reproject", "params": {"to_crs": "EPSG:3857"}},
                {"operation": "buffer", "params": {"distance": 250.0}},
            ],
            "sink": [{"format": "geopackage"}],
        },
    }
)

NEGATIVE_TASK = Task(
    {"id": "nope", "prompt": "join them", "unavailable": "spatial_join"}
)


def manifest(transforms: str, source_format="geojson", sink_format="gpkg") -> str:
    return f"""
[project]
name = "t"

[[source]]
name = "src"
format = "{source_format}"
path = "outputs/in.geojson"
{transforms}
[[sink]]
name = "out"
input = "last"
format = "{sink_format}"
path = "outputs/out.file"
"""


BUFFER_STEP = """
[[transform]]
name = "last"
input = "src"
operation = "buffer"
distance = 500.0
"""


def test_a_correct_manifest_scores_one():
    res = score_manifest(BUFFER_TASK, manifest(BUFFER_STEP))
    assert res.score == 1.0
    assert res.failures == []
    # parse, source format, operation, parameter, sink format, no extras
    assert res.total == 6


def test_a_wrong_operation_loses_only_its_own_checks():
    res = score_manifest(
        BUFFER_TASK,
        manifest(
            """
[[transform]]
name = "last"
input = "src"
operation = "simplify"
epsilon = 500.0
"""
        ),
    )
    assert 0.0 < res.score < 1.0
    failed = {c.name for c in res.failures}
    assert "operation buffer" in failed
    assert "buffer.distance = 500.0" in failed
    assert "no unexpected operations" in failed
    # the formats were still right, so this is partial credit, not zero
    assert res.passed == 3


def test_a_missing_step_fails_that_step_and_the_order():
    res = score_manifest(CHAIN_TASK, manifest(BUFFER_STEP))
    failed = {c.name for c in res.failures}
    assert "operation reproject" in failed
    assert "operation order" in failed
    assert "operation buffer" not in failed
    assert 0.0 < res.score < 1.0


def test_a_wrong_parameter_value_fails_only_that_parameter():
    res = score_manifest(
        BUFFER_TASK,
        manifest(
            """
[[transform]]
name = "last"
input = "src"
operation = "buffer"
distance = 5000.0
"""
        ),
    )
    assert [c.name for c in res.failures] == ["buffer.distance = 500.0"]
    assert res.failures[0].detail == "got 5000.0"
    # scores are rounded to 4 decimals so reports stay comparable
    assert res.score == pytest.approx(5 / 6, abs=1e-4)


def test_a_missing_parameter_fails_that_parameter():
    res = score_manifest(
        BUFFER_TASK,
        manifest(
            """
[[transform]]
name = "last"
input = "src"
operation = "buffer"
"""
        ),
    )
    assert [c.name for c in res.failures] == ["buffer.distance = 500.0"]
    assert res.failures[0].detail == "got None"


@pytest.mark.parametrize(
    "distance,passes",
    [(500.0, True), (500.9999, True), (501.0, True), (501.0001, False), (499.0, True)],
)
def test_tolerance_is_absolute_and_inclusive(distance, passes):
    """The task pins distance to 500 with a tolerance of 1.0."""
    res = score_manifest(
        BUFFER_TASK,
        manifest(
            f"""
[[transform]]
name = "last"
input = "src"
operation = "buffer"
distance = {distance}
"""
        ),
    )
    assert (res.score == 1.0) is passes


def test_default_tolerance_is_effectively_exact():
    task = Task(
        {
            "id": "exact",
            "prompt": "simplify",
            "expect": {
                "transform": [{"operation": "simplify", "params": {"epsilon": 0.01}}]
            },
        }
    )
    assert (
        score_manifest(
            task,
            manifest("""
[[transform]]
name = "last"
input = "src"
operation = "simplify"
epsilon = 0.01
"""),
        ).score
        == 1.0
    )
    assert (
        score_manifest(
            task,
            manifest("""
[[transform]]
name = "last"
input = "src"
operation = "simplify"
epsilon = 0.011
"""),
        ).score
        < 1.0
    )


def test_order_comes_from_input_references_not_declaration_order():
    # buffer is declared first but consumes the reprojection, so the chain is correct
    out_of_order = """
[[transform]]
name = "last"
input = "webmerc"
operation = "buffer"
distance = 250.0

[[transform]]
name = "webmerc"
input = "src"
operation = "reproject"
to_crs = "EPSG:3857"
"""
    assert score_manifest(CHAIN_TASK, manifest(out_of_order)).score == 1.0


def test_a_reversed_chain_fails_the_order_check():
    reversed_chain = """
[[transform]]
name = "buffered"
input = "src"
operation = "buffer"
distance = 250.0

[[transform]]
name = "last"
input = "buffered"
operation = "reproject"
to_crs = "EPSG:3857"
"""
    res = score_manifest(CHAIN_TASK, manifest(reversed_chain))
    failed = {c.name for c in res.failures}
    assert failed == {"operation order"}
    assert "buffer -> reproject" in res.failures[0].detail


def test_padding_the_manifest_with_extra_operations_is_penalised():
    padded = (
        BUFFER_STEP
        + """
[[transform]]
name = "noise"
input = "last"
operation = "centroid"
"""
    )
    res = score_manifest(BUFFER_TASK, manifest(padded))
    assert [c.name for c in res.failures] == ["no unexpected operations"]
    assert "centroid" in res.failures[0].detail


def test_format_aliases_score_the_same():
    assert canon_format("gpkg") == canon_format("geopackage") == "geopackage"
    assert canon_format("SHP") == "shapefile"
    spelled_out = score_manifest(
        BUFFER_TASK, manifest(BUFFER_STEP, sink_format="geopackage")
    )
    abbreviated = score_manifest(BUFFER_TASK, manifest(BUFFER_STEP, sink_format="gpkg"))
    assert spelled_out.score == abbreviated.score == 1.0


def test_a_wrong_format_fails_only_the_format_check():
    res = score_manifest(BUFFER_TASK, manifest(BUFFER_STEP, sink_format="csv"))
    assert [c.name for c in res.failures] == ["sink format geopackage"]
    assert "csv" in res.failures[0].detail


def test_an_unparseable_manifest_scores_zero():
    res = score_manifest(BUFFER_TASK, "I will buffer the depots by 500 metres")
    assert res.score == 0.0
    assert res.failures[0].name == "manifest parses"


def test_no_manifest_at_all_scores_zero_on_a_positive_task():
    res = score_manifest(BUFFER_TASK, "")
    assert res.score == 0.0
    assert res.failures[0].detail == "no manifest produced"


def test_a_negative_task_passes_when_no_manifest_is_built():
    res = score_manifest(NEGATIVE_TASK, "")
    assert res.score == 1.0
    assert res.total == 1


def test_a_negative_task_fails_when_the_model_fakes_the_operation():
    res = score_manifest(
        NEGATIVE_TASK,
        manifest(
            """
[[transform]]
name = "last"
input = "src"
operation = "spatial_join"
join_type = "intersects"
"""
        ),
    )
    assert res.score == 0.0
    assert "built a manifest using spatial_join" in res.failures[0].detail


def test_a_negative_task_allows_an_honest_alternative_manifest():
    # saying spatial_join is unavailable and offering something else is not a failure
    assert score_manifest(NEGATIVE_TASK, manifest(BUFFER_STEP)).score == 1.0


def test_scoring_is_deterministic():
    text = manifest(BUFFER_STEP)
    first = score_manifest(BUFFER_TASK, text)
    second = score_manifest(BUFFER_TASK, text)
    assert first.score == second.score
    assert [c.as_dict() for c in first.checks] == [c.as_dict() for c in second.checks]


def test_topological_transforms_survives_a_cycle():
    """A cyclic manifest must not hang or drop steps, and must order the same every time."""
    cyclic = {
        "transform": [
            {"name": "a", "input": "b", "operation": "buffer"},
            {"name": "b", "input": "a", "operation": "centroid"},
        ]
    }
    first = [t["name"] for t in topological_transforms(cyclic)]
    assert sorted(first) == ["a", "b"]
    assert [t["name"] for t in topological_transforms(cyclic)] == first


def test_a_task_must_expect_something():
    with pytest.raises(ValueError):
        Task({"id": "empty", "prompt": "do nothing"})


def test_the_shipped_suite_loads_and_spans_the_pipeline():
    tasks = load_tasks(TASKS_DIR)
    assert len(tasks) == 10
    assert len({t.id for t in tasks}) == 10
    assert all(t.prompt and t.notes for t in tasks)

    operations = {str(tf.get("operation")) for t in tasks for tf in t.transforms}
    # every operation the suite pins must be one geodukt actually registers
    assert operations <= {
        "buffer",
        "centroid",
        "clip",
        "dissolve",
        "expression",
        "filter",
        "reproject",
        "schema_map",
        "simplify",
        "spatial_join",
    }
    formats = {canon_format(f) for t in tasks for f in t.sources + t.sinks}
    assert formats == {"csv", "geojson", "geopackage", "shapefile"}
    assert [t.id for t in tasks if t.unavailable] == []


def test_every_reference_answer_scores_one():
    """A task nobody can satisfy is a broken task, so the goldens must be perfect."""
    tasks = load_tasks(TASKS_DIR)
    results = score_from_directory(tasks, REFERENCE_DIR)
    imperfect = {
        r.task_id: [c.name for c in r.failures] for r in results if r.score < 1.0
    }
    assert imperfect == {}
    assert aggregate(results)["score"] == 1.0
    assert aggregate(results)["perfect"] == 10


def _ndjson(*events):
    return "".join(json.dumps(e) + "\n" for e in events).encode()


def _plan_call(manifest_toml: str) -> dict:
    return {
        "kind": "tool_call",
        "name": "plan_workflow",
        "args": json.dumps({"manifest_toml": manifest_toml, "title": "t"}),
    }


def test_the_manifest_is_captured_from_the_plan_call():
    body = _ndjson(
        {"kind": "tool_call", "name": "list_workflow_operations", "args": "{}"},
        _plan_call(manifest(BUFFER_STEP)),
        {"kind": "text", "content": "shall I run it?"},
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured = captured_manifest("buffer the depots")

    assert score_manifest(BUFFER_TASK, captured).score == 1.0


def test_the_last_plan_wins_after_a_correction():
    body = _ndjson(
        _plan_call(manifest(BUFFER_STEP, sink_format="csv")),
        {"kind": "tool_return", "name": "plan_workflow", "content": "ERROR: bad sink"},
        _plan_call(manifest(BUFFER_STEP)),
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured = captured_manifest("buffer the depots")

    # the model's corrected proposal is what it stands behind
    assert score_manifest(BUFFER_TASK, captured).score == 1.0


def test_a_run_with_no_plan_captures_nothing():
    body = _ndjson(
        {"kind": "tool_call", "name": "buffer_clip_dissolve", "args": "{}"},
        {"kind": "text", "content": "done it the old way"},
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        assert captured_manifest("buffer the depots") == ""


def _stack(
    sibyl_up=True, profile="local", tool_result="Workflow operations:\n  • buffer"
):
    """Route every precondition probe the runner makes."""
    mock = respx.mock(assert_all_called=False)
    mock.get(f"{runner.GEOLANG}/tools").respond(200, json={"tools": []})
    mock.post(f"{runner.GEOLANG}/tools/list_workflow_operations").respond(
        200, json={"result": tool_result}
    )
    if sibyl_up:
        mock.get(f"{runner.SIBYL}/health").respond(200, json={"status": "ok"})
    else:
        mock.get(f"{runner.SIBYL}/health").respond(503)
    mock.get(f"{runner.SIBYL}/models").respond(
        200,
        json={
            "active": f"{profile}:some-model",
            "profiles": [
                {
                    "id": f"{profile}:some-model",
                    "model": "some-model",
                    "server": profile,
                }
            ],
        },
    )
    return mock


def test_a_ready_local_stack_is_not_skipped():
    with _stack():
        assert stack_skip_reason(allow_cloud=False) == ""


def test_a_down_stack_skips():
    with _stack(sibyl_up=False):
        assert "sibyl not up" in stack_skip_reason(allow_cloud=False)


def test_the_cloud_profile_needs_opting_in():
    with _stack(profile="cloud"):
        assert "cloud profile" in stack_skip_reason(allow_cloud=False)
        assert stack_skip_reason(allow_cloud=True) == ""


def test_an_unreachable_geodukt_skips_instead_of_scoring_zero():
    # the model abandons the workflow path when the catalog fails, so every task
    # would score zero for a reason that says nothing about the model
    with _stack(tool_result="ERROR: geodukt is unreachable at http://geodukt:8080"):
        reason = stack_skip_reason(allow_cloud=False)
    assert "geodukt not reachable" in reason
    assert "http://geodukt:8080" in reason


def test_fixtures_are_created_once_for_every_declared_input(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    tasks = load_tasks(TASKS_DIR)

    created = ensure_fixtures(tasks)

    declared = {p for t in tasks for p in t.inputs}
    assert set(created) == declared
    for relative in declared:
        assert (tmp_path / relative).exists()
    # a second run is a no-op, so a real layer is never overwritten
    assert ensure_fixtures(tasks) == []


def test_the_markdown_report_tabulates_every_task():
    tasks = load_tasks(TASKS_DIR)
    results = score_from_directory(tasks, REFERENCE_DIR)
    meta = {
        "generated_at": "2026-07-29T00:00:00+00:00",
        "mode": "captured",
        "profile": "captured",
        "model": "reference",
        "aggregate": aggregate(results),
    }
    text = markdown_report(meta, results, tasks)
    assert "| Task | Score | Checks | First failure |" in text
    assert "**Aggregate 1.00**" in text
    for task in tasks:
        assert f"`{task.id}`" in text


def _samples(task_id, scores):
    """TaskSamples whose runs score exactly `scores`, via a one-check stand-in."""
    runs = [
        Result(task_id, [Check("only check", passed=bool(s))]) for s in scores
    ]
    return TaskSamples(task_id, runs)


def test_repeated_runs_report_the_mean_and_the_worst_run():
    """A task that only passes sometimes must not report its lucky run."""
    samples = _samples("flaky-task", [1.0, 0.0, 1.0])

    assert samples.runs == 3
    assert samples.score == pytest.approx(2 / 3, abs=1e-4)
    assert (samples.low, samples.high) == (0.0, 1.0)
    assert samples.flaky is True
    # checks come from the worst run, so the failure stays visible
    assert samples.passed == 0
    assert [c.name for c in samples.failures] == ["only check"]


def test_a_stable_task_is_not_flaky():
    samples = _samples("stable-task", [1.0, 1.0])
    assert samples.flaky is False
    assert samples.score == 1.0
    assert samples.failures == []


def test_aggregate_names_the_flaky_tasks():
    agg = aggregate([_samples("flaky", [1.0, 0.0]), _samples("stable", [1.0, 1.0])])

    assert agg["flaky"] == ["flaky"]
    assert agg["runs_per_task"] == 2
    # the flaky task's mean drags the aggregate below a single good run
    assert agg["score"] == pytest.approx(0.75)
    # only a task perfect in every run counts as perfect
    assert agg["perfect"] == 1


def test_the_repeated_report_shows_the_range_and_flags_flakiness():
    tasks = load_tasks(TASKS_DIR)
    results = score_from_directory(tasks, REFERENCE_DIR)
    # stand in one flaky task so the report has something to flag
    results[0] = _samples(results[0].task_id, [1.0, 0.0])
    meta = {
        "generated_at": "2026-07-29T00:00:00+00:00",
        "mode": "stack",
        "profile": "local",
        "model": "test-model",
        "aggregate": aggregate(results),
    }
    text = markdown_report(meta, results, tasks)

    assert "| Task | Mean | Range | Checks | First failure |" in text
    assert "0.00 to 1.00" in text
    assert "2 runs per task" in text
    assert f"`{results[0].task_id}`" in text
    assert "Flaky:" in text


def test_repeat_needs_the_stack():
    """A captured manifest scores identically every time, so repeating it lies."""
    code = runner.main(["--manifests", str(REFERENCE_DIR), "--repeat", "3"])
    assert code == 2


SPATIAL_JOIN_STEP = """
[[transform]]
name = "last"
input = "src"
operation = "spatial_join"
"""


def test_a_rejected_manifest_is_not_the_models_answer():
    """The user never saw it, so it cannot be what the model stands behind."""
    body = _ndjson(
        _plan_call(manifest(SPATIAL_JOIN_STEP)),
        {
            "kind": "tool_return",
            "name": "plan_workflow",
            "content": "ERROR: geodukt rejected the manifest: spatial_join cannot run",
        },
        {"kind": "tool_call", "name": "spatial_join", "args": "{}"},
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured, tools, _ = runner.capture_answer("join them")

    assert captured == ""
    assert "spatial_join" in tools


def test_recovering_from_a_rejection_passes_the_negative_task():
    body = _ndjson(
        _plan_call(manifest(SPATIAL_JOIN_STEP)),
        {"kind": "tool_return", "name": "plan_workflow", "content": "ERROR: cannot run"},
        {"kind": "tool_call", "name": "spatial_join", "args": "{}"},
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured, tools, _ = runner.capture_answer("join them")

    assert score_manifest(NEGATIVE_TASK, captured, tools).score == 1.0


def test_doing_nothing_fails_the_negative_task():
    """Avoiding the manifest is not enough: the work still has to happen."""
    body = _ndjson({"kind": "text", "content": "sorry, cannot"}, {"kind": "done"})
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured, tools, _ = runner.capture_answer("join them")

    result = score_manifest(NEGATIVE_TASK, captured, tools)
    assert result.score < 1.0
    assert [c.name for c in result.failures] == ["calls spatial_join directly instead"]


def test_an_accepted_bad_manifest_still_fails_the_negative_task():
    body = _ndjson(
        _plan_call(manifest(SPATIAL_JOIN_STEP)),
        {"kind": "tool_return", "name": "plan_workflow", "content": 'Plan "t": 3 steps'},
        {"kind": "tool_call", "name": "spatial_join", "args": "{}"},
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured, tools, _ = runner.capture_answer("join them")

    result = score_manifest(NEGATIVE_TASK, captured, tools)
    assert [c.name for c in result.failures] == ["avoids unavailable operation spatial_join"]


def test_scoring_a_capture_skips_the_tool_check():
    """A captured manifest never recorded tool use, so only the manifest is judged."""
    result = score_manifest(NEGATIVE_TASK, "")
    assert result.total == 1
    assert result.score == 1.0


def test_an_unrecovered_rejection_is_reported_as_the_reason():
    """A zero with no reason is undiagnosable, so the rejection has to survive."""
    body = _ndjson(
        _plan_call(manifest(BUFFER_STEP)),
        {
            "kind": "tool_return",
            "name": "plan_workflow",
            "content": "ERROR: geodukt rejected the manifest: missing required parameter 'distance'\nFix it and call plan_workflow again.",
        },
        {"kind": "done"},
    )
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured, tools, rejection = runner.capture_answer("buffer the depots")

    assert captured == ""
    assert "missing required parameter" in rejection

    result = score_manifest(BUFFER_TASK, captured, tools, rejection)
    detail = result.failures[0].detail
    assert "rejected and not corrected" in detail
    assert "missing required parameter" in detail
    # the retry advice on the second line is noise in a report table
    assert "call plan_workflow again" not in detail


def test_no_attempt_at_all_still_says_so():
    body = _ndjson({"kind": "text", "content": "I would rather not"}, {"kind": "done"})
    with respx.mock(base_url=runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        captured, tools, rejection = runner.capture_answer("buffer the depots")

    assert rejection == ""
    result = score_manifest(BUFFER_TASK, captured, tools, rejection)
    assert result.failures[0].detail == "no manifest produced"
