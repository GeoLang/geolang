"""What the eval sends back to the model after a viewer_control call.

The viewer answers a call it refused and a [reads] action's result, so a model
that reads before it acts is scored on the same conversation it would have in
the viewer. These are the messages it gets, against the shipped fixtures and
with no service running.
"""

import json
import re
from pathlib import Path

import pytest

from evals.viewer_replies import pending_reply, reply_for_call

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "evals" / "viewer"
CATALOGUE = json.loads((FIXTURE_DIR / "catalogue.json").read_text())
READS_RESULTS = json.loads((FIXTURE_DIR / "reads_results.json").read_text())


def run_call(name, args=None):
    call = {"action": "run", "name": name}
    if args is not None:
        call["args"] = args
    return call


def reply(call):
    return reply_for_call(call, CATALOGUE, READS_RESULTS)


# ── calls the viewer refuses ─────────────────────────────────────────────


def test_a_missing_required_parameter_comes_back_the_way_the_registry_says_it():
    assert reply(run_call("find_feature", {})) == (
        "find_feature failed: find_feature: query is required"
    )


def test_every_missing_parameter_is_named_at_once():
    assert reply(run_call("stac.search", {"limit": 5})) == (
        "stac.search failed: stac.search: catalog is required, collection is required"
    )


def test_a_required_parameter_sent_as_null_is_missing():
    assert "query is required" in reply(run_call("find_feature", {"query": None}))


def test_arguments_that_will_not_read_as_an_object_fail():
    assert reply(run_call("find_feature", "Kingsway substation")) == (
        "find_feature failed: find_feature: its arguments did not read as an object."
    )


def test_arguments_as_json_text_are_read():
    answer = reply(run_call("find_feature", '{"query": "Kingsway substation"}'))

    assert answer.startswith("Result of find_feature: ")


def test_an_action_with_no_required_parameters_takes_no_arguments_at_all():
    assert reply(run_call("project.list")).startswith("Result of project.list: ")


def test_an_action_the_catalogue_does_not_list_fails():
    assert reply(run_call("layers.vanish", {"layer": "Parcels"})) == (
        "layers.vanish failed: There is no viewer action named layers.vanish."
    )


# ── calls the viewer runs ────────────────────────────────────────────────


def test_a_reads_action_comes_back_with_its_result():
    answer = reply(run_call("find_feature", {"query": "Kingsway substation"}))

    assert answer.startswith("Result of find_feature: ")
    assert "Kingsway substation" in answer


def test_a_change_the_model_made_is_not_answered():
    assert reply(run_call("layers.set_visible", {"layer": "Parcels", "visible": False})) is None


def test_a_destructive_action_waits_for_the_user_rather_than_answering():
    """It is pending a confirming reply in the chat, which the model never sees."""
    assert reply(run_call("live.remove_feed", {"feed": "Gateway A"})) is None


def test_a_fixed_action_is_not_a_run_call():
    assert reply({"action": "fly_to", "lon": 2.35, "lat": 48.85}) is None


def test_a_reads_action_with_no_result_text_is_a_fixture_gap():
    with pytest.raises(ValueError, match="dataset.list"):
        reply_for_call(run_call("dataset.list"), CATALOGUE, {})


# ── one pending reply per turn ───────────────────────────────────────────


def test_a_turn_that_changed_something_leaves_no_reply():
    calls = [
        run_call("layers.set_visible", {"layer": "Parcels", "visible": False}),
        run_call("camera.fly_to", {"lon": 2.35, "lat": 48.85}),
    ]

    assert pending_reply(calls, CATALOGUE, READS_RESULTS) is None


def test_the_last_reply_of_a_turn_is_the_one_waiting():
    """The viewer holds one follow-up, so a second queued reply overwrites it."""
    calls = [
        run_call("find_feature", {}),
        run_call("dataset.list"),
    ]

    assert pending_reply(calls, CATALOGUE, READS_RESULTS).startswith(
        "Result of dataset.list: "
    )


def test_a_failure_after_a_result_is_what_the_model_hears_about():
    calls = [
        run_call("dataset.list"),
        run_call("dataset.draw_branch", {"branch": "widening"}),
    ]

    assert pending_reply(calls, CATALOGUE, READS_RESULTS) == (
        "dataset.draw_branch failed: dataset.draw_branch: dataset is required"
    )


# ── the shipped fixture ──────────────────────────────────────────────────


def test_every_reads_action_in_the_catalogue_has_a_result_text():
    reads = {entry["name"] for entry in CATALOGUE if entry.get("reads")}

    assert reads == set(READS_RESULTS)
    assert all(READS_RESULTS[name].strip() for name in reads)


def test_the_result_texts_answer_the_tasks_that_read_before_acting():
    """A follow-up nobody could act on would score the model on nothing."""
    assert "Road Network" in READS_RESULTS["dataset.list"]
    assert "main" in READS_RESULTS["dataset.list"]
    assert "widening" in READS_RESULTS["dataset.list"]
    assert "Kingsway substation" in READS_RESULTS["find_feature"]
    assert "S2B_31UDQ_20260801_0_L2A" in READS_RESULTS["stac.search"]
    assert "visual" in READS_RESULTS["stac.search"]


def test_the_feature_find_answers_with_a_place_near_the_snapshot_camera():
    camera = json.loads((FIXTURE_DIR / "snapshot.json").read_text())["camera"]
    found = re.search(r" at (-?\d+\.\d+), (-?\d+\.\d+)", READS_RESULTS["find_feature"])
    longitude, latitude = (float(part) for part in found.groups())

    assert abs(longitude - camera["longitude"]) < 0.01
    assert abs(latitude - camera["latitude"]) < 0.01
