"""The viewer eval as a test: recorded calls, no model, no network.

A recording holds the calls a model made and the pass or fail each one earned
when it was written, so a change to a task, to the snapshot or to the scorer that
moves any of those scores turns this red.
"""

import json
from pathlib import Path

import pytest

from evals.viewer_runner import replay_recording
from evals.viewer_scoring import load_tasks

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "evals" / "viewer"
RECORDING = json.loads(
    (FIXTURE_DIR / "recordings" / "grok-2026-08-29.json").read_text()
)
SNAPSHOT = json.loads((FIXTURE_DIR / "snapshot.json").read_text())
TASKS = load_tasks(FIXTURE_DIR / "tasks")

# the calls in this recording that answer their prompt outright. change-to-3d
# wraps its scalar in an array, which the scorer unwraps the way the viewer does
PASSING_TASKS = {"renderer-named-by-value", "map-to-2d", "change-to-3d"}

SCORED = replay_recording(RECORDING["tasks"], TASKS, SNAPSHOT)


def test_the_recording_says_where_it_came_from():
    assert RECORDING["source"] == "hand-recorded from the 2026-08-29 Grok session"


@pytest.mark.parametrize("entry,result", SCORED, ids=[e["id"] for e, _ in SCORED])
def test_a_recorded_task_still_scores_the_way_the_recording_claims(entry, result):
    failures = ", ".join(f"{c.name} {c.detail}".strip() for c in result.failures)
    assert (result.score == 1.0) is entry["expected_pass"], (
        f"{entry['id']} scored {result.passed}/{result.total}: {failures or 'no failure'}"
    )


@pytest.mark.parametrize("task_id", sorted(PASSING_TASKS))
def test_the_calls_that_answer_their_prompt_score_every_check(task_id):
    result = next(r for e, r in SCORED if e["id"] == task_id)

    assert result.passed == result.total
