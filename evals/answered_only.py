"""Score a viewer eval over the runs that answered.

A run the stack cut short before the model emitted any viewer_control call is
unmeasurable: what it would have answered is unknown. A run that finished and
emitted no call is a real failure and stays in. The report cannot tell the two
apart, both being a zero with an empty manifest, so which runs stalled is read
from the sweep log, whose "run cut short" line follows the run it belongs to.

  python -m evals.answered_only <sweep.log>                 # list the stalls
  python -m evals.answered_only <sweep.log> <report.json>   # the score
"""

import json
import re
import sys
from statistics import mean

RUNNING = re.compile(r"^running (?P<label>.+?)…\s*$")
NUMBERED = re.compile(r"^(?P<task>.+) (?P<run>\d+)/(?P<total>\d+)$")
CUT_SHORT = re.compile(r"^\s+run cut short")


def stalled_runs(lines):
    """Every (task id, run number) the stack cut short, and every run started."""
    stalls = set()
    started = []
    current = None
    for line in lines:
        running = RUNNING.match(line)
        if running:
            label = running.group("label")
            numbered = NUMBERED.match(label)
            current = (
                (numbered.group("task"), int(numbered.group("run")))
                if numbered
                else (label, 1)
            )
            started.append(current)
            continue
        if current and CUT_SHORT.match(line):
            stalls.add(current)
    return stalls, started


def summarise(report, stalls):
    """The report's own tasks rescored over the runs that answered."""
    kept, dropped, unmeasured = [], 0, []
    for task in report["tasks"]:
        answered = [
            detail
            for index, detail in enumerate(task["runs_detail"], 1)
            if not ((task["id"], index) in stalls and not detail["manifest"])
        ]
        dropped += len(task["runs_detail"]) - len(answered)
        if not answered:
            unmeasured.append(task["id"])
            continue
        worst = min(answered, key=lambda detail: detail["score"])
        kept.append(
            {
                "id": task["id"],
                "score": round(mean(detail["score"] for detail in answered), 4),
                "runs": len(answered),
                "passed": worst["passed"],
                "total": worst["total"],
            }
        )

    scores = [task["score"] for task in kept]
    return {
        "tasks": kept,
        "dropped": dropped,
        "unmeasured": unmeasured,
        "answered_aggregate": mean(scores) if scores else 0.0,
        "perfect": sum(1 for score in scores if score == 1.0),
        "checks_passed": sum(task["passed"] for task in kept),
        "checks_total": sum(task["total"] for task in kept),
    }


def stall_rate(stalls, started, tasks=None):
    """How often the stack cut a run short, optionally over named tasks only."""
    counted = [run for run in started if tasks is None or run[0] in tasks]
    cut = [run for run in stalls if tasks is None or run[0] in tasks]
    return len(cut), len(counted)


def main(argv):
    with open(argv[1]) as log:
        stalls, started = stalled_runs(log)
    cut, total = stall_rate(stalls, started)
    print(f"runs started: {total}")
    print(f"runs cut short: {cut} ({cut / total:.1%})" if total else "runs cut short: 0")
    if len(argv) < 3:
        for task, run in sorted(stalls):
            print(f"  {task} {run}")
        return 0

    with open(argv[2]) as handle:
        report = json.load(handle)
    summary = summarise(report, stalls)
    print()
    print(
        f"reported aggregate:  {report['aggregate']['score']:.4f} "
        f"over {report['aggregate']['tasks']} tasks"
    )
    print(
        f"answered aggregate:  {summary['answered_aggregate']:.4f} "
        f"over {len(summary['tasks'])} tasks"
    )
    print(f"perfect:             {summary['perfect']}/{len(summary['tasks'])}")
    print(f"checks:              {summary['checks_passed']}/{summary['checks_total']}")
    print(f"runs dropped:        {summary['dropped']}")
    if summary["unmeasured"]:
        print(f"tasks with no answered run: {', '.join(summary['unmeasured'])}")
    print()
    for task in sorted(summary["tasks"], key=lambda t: t["score"]):
        if task["score"] < 1.0:
            print(
                f"  {task['score']:.2f}  {task['id']}  "
                f"({task['passed']}/{task['total']} checks, {task['runs']} runs)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
