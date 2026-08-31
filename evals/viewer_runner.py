"""Score a model on mapping a chat prompt onto one of the viewer's own actions.

  python -m evals.viewer_runner
  python -m evals.viewer_runner --only layers-hide-parcels --repeat 3
  python -m evals.viewer_runner --replay evals/viewer/recordings/grok-2026-08-29.json

The viewer state and the action catalogue are fixtures under evals/viewer/, sent
to sibyl exactly the way `/chat/agui` sends the live ones, so what the model is
told here is what it is told in the viewer. geodukt is not involved: this eval
only reads viewer_control calls, so an unreachable geodukt does not skip it.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


from evals import runner
from evals.runner import (
    active_profile,
    active_session_id,
    delete_sessions,
    markdown_report,
    restore_session,
    run_events,
    service_up,
    sibyl_refuses_the_token,
    start_eval_session,
)
from evals.scoring import TaskSamples, aggregate
from evals.viewer_scoring import VIEWER_CONTROL_TOOL, load_tasks, score_calls

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "evals" / "viewer"
REPORT_TITLE = "Viewer eval"


def viewer_skip_reason(allow_cloud: bool) -> str:
    """Why the stack cannot be evaluated, or "" when it can."""
    if not service_up(f"{runner.GEOLANG}/tools"):
        return f"geolang api not up at {runner.GEOLANG}"
    if not service_up(f"{runner.SIBYL}/health"):
        return f"sibyl not up at {runner.SIBYL}"
    if sibyl_refuses_the_token():
        return runner.TOKEN_HINT
    _, _, server = active_profile()
    if server == "cloud" and not allow_cloud:
        return "sibyl is on the cloud profile: pass --allow-cloud to spend credits"
    return ""


def capture_calls(prompt: str, system_prompt: str) -> list:
    """Every viewer_control call of one run, arguments decoded.

    A call whose arguments will not parse is dropped: the viewer could not have
    run it either, so it is not an answer. A run cut short keeps the calls it
    made before it went, since a model that answered and then rambled on still
    answered.
    """
    calls = []
    for event in run_events({"system_prompt": system_prompt, "message": prompt}):
        if event.get("kind") != "tool_call":
            continue
        if str(event.get("name") or "") != VIEWER_CONTROL_TOOL:
            continue
        try:
            arguments = json.loads(str(event.get("args") or ""))
        except json.JSONDecodeError:
            continue
        if isinstance(arguments, dict):
            calls.append(arguments)
    return calls


def load_fixture(path):
    with open(path) as fh:
        return json.load(fh)


LIVE_RECORDING_SOURCE = "captured by evals.viewer_runner --record"


def write_recording(path: Path, source: str, profile: str, model: str, captured: dict):
    """The calls each task drew, so a later run can score them with no model.

    `captured` is {task id: (calls, result)}. The result is written as the
    pass/fail the recording claims, which is what makes a replay a fixed point.
    """
    document = {
        "source": source,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": profile,
        "model": model,
        "tasks": [
            {
                "id": task_id,
                "calls": calls,
                "expected_pass": result.score == 1.0,
            }
            for task_id, (calls, result) in captured.items()
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n")


def replay_recording(entries: list, tasks: list, snapshot: dict) -> list:
    """(recorded entry, score) for every recorded task, scored as a live run does."""
    by_id = {task.id: task for task in tasks}
    scored = []
    for entry in entries:
        task = by_id.get(entry["id"])
        if task is None:
            raise ValueError(f"recording names {entry['id']}, which no task defines")
        scored.append((entry, score_calls(task, entry["calls"], snapshot)))
    return scored


def run_against_the_stack(args, tasks: list, snapshot: dict) -> tuple:
    """(one TaskSamples per task, {task id: (calls, first run's score)})."""
    # imported late so loading a task never needs the persona or the tools
    from src.api.viewer_state import system_prompt_for

    catalogue = load_fixture(args.catalogue)
    system_prompt = system_prompt_for({"viewer": snapshot, "actions": catalogue})

    results = []
    captured = {}
    # one line per finished task, so a killed run keeps the scores it earned
    progress = Path(args.out) / "viewer-progress.jsonl"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("")
    user_session = active_session_id()
    eval_sessions = []
    try:
        for task in tasks:
            samples = []
            for run in range(1, args.repeat + 1):
                label = (
                    f"{task.id}…"
                    if args.repeat == 1
                    else f"{task.id} {run}/{args.repeat}…"
                )
                print(f"running {label}", file=sys.stderr)
                # a fresh session per run, so one sample cannot bias the next
                eval_sessions.append(start_eval_session(f"viewer eval {task.id} {run}"))
                calls = capture_calls(task.prompt, system_prompt)
                result = score_calls(task, calls, snapshot)
                if run == 1:
                    captured[task.id] = (calls, result)
                samples.append(result)
            results.append(TaskSamples(task.id, samples))
            with progress.open("a") as fh:
                fh.write(json.dumps(results[-1].as_dict()) + "\n")
    finally:
        restore_session(user_session)
        delete_sessions(eval_sessions)
    return results, captured


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", default=str(FIXTURE_DIR / "tasks"))
    parser.add_argument("--snapshot", default=str(FIXTURE_DIR / "snapshot.json"))
    parser.add_argument("--catalogue", default=str(FIXTURE_DIR / "catalogue.json"))
    parser.add_argument(
        "--only", nargs="+", metavar="ID", help="run just these task ids"
    )
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument(
        "--allow-cloud",
        action="store_true",
        default=os.environ.get("NL_EVAL_ALLOW_CLOUD") == "1",
        help="permit running against a cloud model profile, which costs credits",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="run each task N times and report the mean, the range and which "
        "tasks were flaky. Sampling makes one run a poor estimate of a score",
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        help="after the run, write the calls each task drew to PATH, so they can "
        "be replayed later with no model",
    )
    parser.add_argument(
        "--replay",
        metavar="PATH",
        help="score the calls in the recording at PATH instead of asking a model. "
        "Reaches neither sibyl nor the network",
    )
    args = parser.parse_args(argv)
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    if args.record and args.replay:
        print(
            "--record needs a live run, so it cannot go with --replay",
            file=sys.stderr,
        )
        return 2

    tasks = load_tasks(args.tasks)
    if args.only:
        tasks = [t for t in tasks if t.id in set(args.only)]
        if not tasks:
            print(f"no tasks match {args.only}", file=sys.stderr)
            return 2

    snapshot = load_fixture(args.snapshot)

    if args.replay:
        recording = load_fixture(args.replay)
        wanted = {t.id for t in tasks}
        entries = [e for e in recording["tasks"] if e["id"] in wanted]
        scored = replay_recording(entries, tasks, snapshot)
        results = [TaskSamples(entry["id"], [result]) for entry, result in scored]
        profile = recording.get("profile") or "recording"
        model = recording.get("model") or ""
        mode = "replay"
    else:
        reason = viewer_skip_reason(args.allow_cloud)
        if reason:
            print(f"SKIP: {reason}")
            return 0
        profile, model, _ = active_profile()
        results, captured = run_against_the_stack(args, tasks, snapshot)
        mode = "stack"
        if args.record:
            write_recording(
                Path(args.record), LIVE_RECORDING_SOURCE, profile, model, captured
            )

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "profile": profile,
        "model": model,
        "aggregate": aggregate(results),
    }
    report = dict(meta, tasks=[r.as_dict() for r in results])
    text = markdown_report(meta, results, tasks, title=REPORT_TITLE)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = meta["generated_at"].replace(":", "").replace("-", "")
    base = out_dir / f"{stamp}-viewer-{profile}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    base.with_suffix(".md").write_text(text)

    print(text)
    print(
        f"wrote {base.with_suffix('.json')} and {base.with_suffix('.md')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
