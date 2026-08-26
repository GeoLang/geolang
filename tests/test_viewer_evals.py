"""The viewer eval scorer: same tool calls in, same score out.

The shipped tasks are fixtures, so these tests also guard that every one of them
names an action the viewer's catalogue offers.
"""

import json
from pathlib import Path

import pytest
import respx

from evals import viewer_runner
from evals.scoring import aggregate
from evals.viewer_runner import capture_calls, viewer_skip_reason
from evals.viewer_scoring import (
    ViewerTask,
    equivalent_identifiers,
    load_tasks,
    score_calls,
)
from src.agents.tools.viewer_control import ViewerAction

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "evals" / "viewer"
TASKS_DIR = FIXTURE_DIR / "tasks"
SNAPSHOT = json.loads((FIXTURE_DIR / "snapshot.json").read_text())
CATALOGUE = json.loads((FIXTURE_DIR / "catalogue.json").read_text())

HIDE_PARCELS = ViewerTask(
    {
        "id": "hide-parcels",
        "prompt": "hide the parcels layer",
        "expect": {
            "action": "run",
            "name": "layers.set_visible",
            "args": {"layer": "Parcels", "visible": False},
        },
    }
)

FLY_TO_PARIS = ViewerTask(
    {
        "id": "fly-to-paris",
        "prompt": "fly to Paris",
        "expect": {"action": "fly_to", "args": {"lon": 2.35, "lat": 48.85}},
        "tolerance": {"lon": 0.5, "lat": 0.5},
    }
)


def run_call(name, **args):
    return {"action": "run", "name": name, "args": args}


# ── the checks a call earns ──────────────────────────────────────────────


def test_the_expected_call_scores_one():
    result = score_calls(
        HIDE_PARCELS,
        [run_call("layers.set_visible", layer="Parcels", visible=False)],
        SNAPSHOT,
    )

    assert result.score == 1.0
    assert result.failures == []
    # the action and its two arguments
    assert result.total == 3


def test_a_wrong_catalogue_name_fails_the_action_check():
    result = score_calls(
        HIDE_PARCELS,
        [run_call("layers.remove", layer="Parcels", visible=False)],
        SNAPSHOT,
    )

    assert [c.name for c in result.failures] == ["calls run layers.set_visible"]
    assert result.failures[0].detail == "called run layers.remove"
    # the arguments were still right, so this is partial credit
    assert result.passed == 2


def test_the_layer_id_from_the_snapshot_passes_as_well_as_the_name():
    result = score_calls(
        HIDE_PARCELS,
        [run_call("layers.set_visible", layer="lyr_parcels", visible=False)],
        SNAPSHOT,
    )

    assert result.score == 1.0


def test_a_layer_the_snapshot_does_not_name_that_way_fails():
    result = score_calls(
        HIDE_PARCELS,
        [run_call("layers.set_visible", layer="lyr_flood", visible=False)],
        SNAPSHOT,
    )

    assert [c.name for c in result.failures] == ["layer = Parcels"]


def test_arguments_sent_as_json_text_still_score():
    call = {
        "action": "run",
        "name": "layers.set_visible",
        "args": '{"layer": "Parcels", "visible": false}',
    }

    assert score_calls(HIDE_PARCELS, [call], SNAPSHOT).score == 1.0


def test_a_string_that_is_not_an_object_leaves_the_arguments_empty():
    call = {"action": "run", "name": "layers.set_visible", "args": "Parcels"}

    result = score_calls(HIDE_PARCELS, [call], SNAPSHOT)

    assert [c.name for c in result.failures] == ["layer = Parcels", "visible = False"]


def test_a_boolean_the_wrong_way_round_fails_only_that_argument():
    result = score_calls(
        HIDE_PARCELS,
        [run_call("layers.set_visible", layer="Parcels", visible=True)],
        SNAPSHOT,
    )

    assert [c.name for c in result.failures] == ["visible = False"]
    assert result.failures[0].detail == "got True"


def test_no_viewer_control_call_scores_zero():
    result = score_calls(HIDE_PARCELS, [], SNAPSHOT)

    assert result.score == 0.0
    assert result.failures[0].detail == "no viewer_control call"


def test_the_best_matching_call_is_the_one_scored():
    """A model that looked around first and then got it right has answered."""
    result = score_calls(
        HIDE_PARCELS,
        [
            run_call("project.list"),
            run_call("layers.set_visible", layer="Parcels", visible=False),
        ],
        SNAPSHOT,
    )

    assert result.score == 1.0


def test_scoring_is_deterministic():
    calls = [run_call("layers.set_visible", layer="lyr_parcels", visible=False)]
    first = score_calls(HIDE_PARCELS, calls, SNAPSHOT)
    second = score_calls(HIDE_PARCELS, calls, SNAPSHOT)

    assert [c.as_dict() for c in first.checks] == [c.as_dict() for c in second.checks]


# ── the fixed actions, whose arguments are not nested ────────────────────


def test_a_fixed_action_reads_its_arguments_from_the_call_itself():
    result = score_calls(
        FLY_TO_PARIS, [{"action": "fly_to", "lon": 2.3522, "lat": 48.8566}], SNAPSHOT
    )

    assert result.score == 1.0


def test_a_fixed_action_outside_its_tolerance_fails_that_argument():
    result = score_calls(
        FLY_TO_PARIS, [{"action": "fly_to", "lon": 4.9, "lat": 48.8566}], SNAPSHOT
    )

    assert [c.name for c in result.failures] == ["lon = 2.35"]


def test_running_a_catalogue_action_does_not_answer_a_fixed_one():
    result = score_calls(
        FLY_TO_PARIS,
        [run_call("camera.fly_to", lon=2.3522, lat=48.8566)],
        SNAPSHOT,
    )

    assert [c.name for c in result.failures] == ["calls fly_to"]


# ── the snapshot's own names ─────────────────────────────────────────────


def test_the_snapshot_pairs_every_id_with_its_name():
    groups = equivalent_identifiers(SNAPSHOT)

    assert {"lyr_sensors", "sensors"} in groups
    assert {"doc_riverside", "riverside live"} in groups
    assert {"feed_gateway_a", "gateway a"} in groups
    assert {"ds_network", "road network"} in groups


# ── the shipped suite ────────────────────────────────────────────────────


def test_every_task_names_an_action_the_viewer_offers():
    tasks = load_tasks(TASKS_DIR)
    catalogue_names = {entry["name"] for entry in CATALOGUE}

    assert len(tasks) == 40
    assert all(t.prompt and t.notes for t in tasks)
    for task in tasks:
        if task.action == "run":
            assert task.name in catalogue_names, task.id
        else:
            assert task.action in ViewerAction.__args__, task.id


def test_the_suite_covers_every_action_in_the_catalogue():
    tasks = load_tasks(TASKS_DIR)
    covered = {t.name for t in tasks if t.action == "run"}

    assert covered == {entry["name"] for entry in CATALOGUE}


def test_the_suite_exercises_the_fixed_actions_too():
    tasks = load_tasks(TASKS_DIR)

    assert {t.action for t in tasks if t.action != "run"} == {
        "fly_to",
        "add_marker",
        "clear_entities",
    }


def test_a_task_must_expect_an_action():
    with pytest.raises(ValueError):
        ViewerTask({"id": "empty", "prompt": "do nothing"})


def test_a_run_task_must_name_the_action_it_runs():
    with pytest.raises(ValueError):
        ViewerTask({"id": "nameless", "prompt": "do it", "expect": {"action": "run"}})


def test_a_perfect_answer_to_every_task_aggregates_to_one():
    """A task nobody could satisfy is a broken task."""
    tasks = load_tasks(TASKS_DIR)
    results = []
    for task in tasks:
        if task.action == "run":
            call = {"action": "run", "name": task.name, "args": dict(task.args)}
        else:
            call = dict(task.args, action=task.action)
        results.append(score_calls(task, [call], SNAPSHOT))

    assert aggregate(results)["score"] == 1.0
    assert aggregate(results)["perfect"] == len(tasks)


# ── reading the calls out of a run ───────────────────────────────────────


def _ndjson(*events):
    return "".join(json.dumps(e) + "\n" for e in events).encode()


def test_only_viewer_control_calls_are_captured():
    body = _ndjson(
        {"kind": "tool_call", "name": "list_outputs", "args": "{}"},
        {
            "kind": "tool_call",
            "name": "viewer_control",
            "args": json.dumps(
                {
                    "action": "run",
                    "name": "layers.set_visible",
                    "args": {"layer": "Parcels", "visible": False},
                }
            ),
        },
        {"kind": "text", "content": "hidden"},
        {"kind": "done"},
    )
    with respx.mock(base_url=viewer_runner.runner.SIBYL) as sibyl:
        route = sibyl.post("/runs").respond(200, content=body)
        calls = capture_calls("hide the parcels layer", "a system prompt")

    assert score_calls(HIDE_PARCELS, calls, SNAPSHOT).score == 1.0
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"system_prompt": "a system prompt", "message": "hide the parcels layer"}


def test_a_call_whose_arguments_will_not_parse_is_dropped():
    body = _ndjson(
        {"kind": "tool_call", "name": "viewer_control", "args": "not json"},
        {"kind": "done"},
    )
    with respx.mock(base_url=viewer_runner.runner.SIBYL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        assert capture_calls("hide the parcels layer", "a system prompt") == []


# ── when the eval can run at all ─────────────────────────────────────────


def _stack(sibyl_up=True, profile="local"):
    mock = respx.mock(assert_all_called=False)
    mock.get(f"{viewer_runner.runner.GEOLANG}/tools").respond(200, json={"tools": []})
    if sibyl_up:
        mock.get(f"{viewer_runner.runner.SIBYL}/health").respond(200, json={"status": "ok"})
    else:
        mock.get(f"{viewer_runner.runner.SIBYL}/health").respond(503)
    mock.get(f"{viewer_runner.runner.SIBYL}/models").respond(
        200,
        json={"active": profile, "profiles": [{"id": profile, "model": "some-model"}]},
    )
    return mock


def test_a_ready_local_stack_is_not_skipped_without_geodukt():
    """This eval never plans a workflow, so geodukt has nothing to say about it."""
    with _stack():
        assert viewer_skip_reason(allow_cloud=False) == ""


def test_a_down_stack_skips():
    with _stack(sibyl_up=False):
        assert "sibyl not up" in viewer_skip_reason(allow_cloud=False)


def test_the_cloud_profile_needs_opting_in():
    with _stack(profile="cloud"):
        assert "cloud profile" in viewer_skip_reason(allow_cloud=False)
        assert viewer_skip_reason(allow_cloud=True) == ""
