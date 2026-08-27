"""Excluding stalled runs from a viewer eval score.

The distinction the whole thing rests on: a run the stack cut short before any
call is dropped, a run that finished and called nothing is a real zero.
"""

from evals.answered_only import stall_rate, stalled_runs, summarise


def sweep_log(*entries):
    """A sweep log in the runner's own shape: the cut-short line follows its run."""
    lines = []
    for task, run, cut_short in entries:
        lines.append(f"running {task} {run}/3…\n")
        if cut_short:
            lines.append("  run cut short: timed out\n")
    return lines


def report(*tasks):
    return {
        "aggregate": {"score": 0.0, "tasks": len(tasks)},
        "tasks": [
            {"id": task_id, "runs_detail": runs_detail} for task_id, runs_detail in tasks
        ],
    }


def run(score, manifest, passed, total):
    return {"score": score, "manifest": manifest, "passed": passed, "total": total}


def test_a_cut_short_line_belongs_to_the_run_above_it():
    stalls, started = stalled_runs(
        sweep_log(("fly-to", 1, True), ("fly-to", 2, False), ("hide", 1, True))
    )
    assert stalls == {("fly-to", 1), ("hide", 1)}
    assert started == [("fly-to", 1), ("fly-to", 2), ("hide", 1)]


def test_an_unnumbered_run_counts_as_the_first():
    stalls, started = stalled_runs(["running fly-to…\n", "  run cut short: timed out\n"])
    assert stalls == {("fly-to", 1)}
    assert started == [("fly-to", 1)]


def test_a_stalled_run_that_called_nothing_is_dropped():
    summary = summarise(
        report(("fly-to", [run(0.0, [], 0, 2), run(1.0, ["camera.fly_to"], 2, 2)])),
        stalls={("fly-to", 1)},
    )
    assert summary["dropped"] == 1
    assert summary["tasks"][0]["score"] == 1.0
    assert summary["answered_aggregate"] == 1.0


def test_a_finished_run_that_called_nothing_is_a_real_zero():
    summary = summarise(
        report(("fly-to", [run(0.0, [], 0, 2), run(1.0, ["camera.fly_to"], 2, 2)])),
        stalls=set(),
    )
    assert summary["dropped"] == 0
    assert summary["tasks"][0]["score"] == 0.5


def test_a_stalled_run_that_still_called_something_stays_in():
    summary = summarise(
        report(("fly-to", [run(0.4, ["camera.fly_to"], 1, 2), run(1.0, ["camera.fly_to"], 2, 2)])),
        stalls={("fly-to", 1)},
    )
    assert summary["dropped"] == 0
    assert summary["tasks"][0]["score"] == 0.7


def test_a_task_whose_every_run_stalled_is_unmeasured_not_zero():
    summary = summarise(
        report(("fly-to", [run(0.0, [], 0, 2)]), ("hide", [run(1.0, ["layers.set_visible"], 1, 1)])),
        stalls={("fly-to", 1)},
    )
    assert summary["unmeasured"] == ["fly-to"]
    assert [task["id"] for task in summary["tasks"]] == ["hide"]
    assert summary["answered_aggregate"] == 1.0


def test_checks_come_from_the_worst_answered_run():
    summary = summarise(
        report(("fly-to", [run(0.0, [], 0, 3), run(0.5, ["camera.fly_to"], 1, 3)])),
        stalls={("fly-to", 1)},
    )
    assert (summary["checks_passed"], summary["checks_total"]) == (1, 3)


def test_stall_rate_can_be_taken_over_named_tasks_only():
    stalls, started = stalled_runs(
        sweep_log(("fly-to", 1, True), ("fly-to", 2, False), ("hide", 1, True))
    )
    assert stall_rate(stalls, started) == (2, 3)
    assert stall_rate(stalls, started, tasks={"fly-to"}) == (1, 2)
