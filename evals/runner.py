"""Score a model on the golden workflow tasks, against the live stack or a capture.

  python -m evals.runner                        # drive the running stack
  python -m evals.runner --manifests evals/reference   # no stack, score files

Stack mode reads the manifest out of the model's plan_workflow tool call, so it
scores what the model composed whether or not geodukt itself is deployed.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from evals.scoring import TaskSamples, aggregate, load_tasks, score_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
GEOLANG = os.environ.get("NL_EVAL_GEOLANG", "http://localhost:8080")
SIBYL = os.environ.get("NL_EVAL_SIBYL", "http://localhost:8090")
RUN_READ_TIMEOUT = 300.0


def service_up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code == 200
    except Exception:
        return False


def active_profile() -> tuple:
    """(profile id, model name, server) from sibyl, ("unknown", "", "") when it cannot say."""
    try:
        body = httpx.get(f"{SIBYL}/models", timeout=5).json()
    except Exception:
        return "unknown", "", ""
    active = body.get("active") or "unknown"
    for profile in body.get("profiles") or []:
        if profile.get("id") == active:
            return active, profile.get("model") or "", profile.get("server") or ""
    return active, "", ""


def geodukt_reachable() -> str:
    """ "" when the tool executor can reach geodukt, else the tool's own error.

    Asked through geolang rather than directly: the executor runs in its own
    container, and only its view of geodukt decides whether a model can plan.
    """
    try:
        body = httpx.post(
            f"{GEOLANG}/tools/list_workflow_operations", json={"args": {}}, timeout=30
        ).json()
    except Exception as e:
        return str(e)
    result = str(body.get("result") or "")
    return "" if not result.startswith("ERROR") else result.split("\n")[0]


def stack_skip_reason(allow_cloud: bool) -> str:
    """Why the stack cannot be evaluated, or "" when it can."""
    if not service_up(f"{GEOLANG}/tools"):
        return f"geolang api not up at {GEOLANG}"
    if not service_up(f"{SIBYL}/health"):
        return f"sibyl not up at {SIBYL}"
    _, _, server = active_profile()
    if server == "cloud" and not allow_cloud:
        return "sibyl is on the cloud profile: pass --allow-cloud to spend credits"
    # without the catalog the model abandons the workflow path and falls back to
    # the single-shot tools, which would score every task zero for the wrong reason
    unreachable = geodukt_reachable()
    if unreachable:
        return f"geodukt not reachable from the geolang tool executor: {unreachable}"
    return ""


GDAL_DRIVERS = {".geojson": "GeoJSON", ".gpkg": "GPKG", ".shp": "ESRI Shapefile"}


def ensure_fixtures(tasks: list) -> list:
    """Create any missing input layer the tasks declare, and return what was created.

    A model that checks whether the file exists is right to refuse to plan over
    one that does not, which would score workflow construction on file lookup.
    The eval never runs the pipeline, so a three-feature stand-in is enough.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    created = []
    wanted = {path for task in tasks for path in task.inputs}
    for relative in sorted(wanted):
        path = REPO_ROOT / relative
        driver = GDAL_DRIVERS.get(path.suffix.lower())
        if path.exists() or driver is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        gpd.GeoDataFrame(
            {"name": ["one", "two", "three"], "region": ["North", "South", "North"]},
            geometry=[Point(-1.1, 52.63), Point(-1.05, 52.66), Point(4.9, 52.35)],
            crs="EPSG:4326",
        ).to_file(path, driver=driver)
        created.append(relative)
    return created


def active_session_id():
    try:
        sessions = httpx.get(f"{SIBYL}/sessions", timeout=5).json()
    except Exception:
        return None
    return next((s["id"] for s in sessions if s.get("active")), None)


def start_eval_session(name: str):
    """Create a session and make it active, so one task cannot bias the next.

    A run always replays sibyl's active session, and a model that refused once
    ("that file does not exist") repeats itself from its own history otherwise.
    """
    created = httpx.post(f"{SIBYL}/sessions", json={"name": name}, timeout=10).json()
    session_id = created["id"]
    httpx.post(f"{SIBYL}/sessions/{session_id}/activate", timeout=10).raise_for_status()
    return session_id


def restore_session(session_id) -> None:
    if session_id:
        try:
            httpx.post(f"{SIBYL}/sessions/{session_id}/activate", timeout=10)
        except Exception as e:
            print(f"could not reactivate session {session_id}: {e}", file=sys.stderr)


def delete_sessions(session_ids) -> None:
    for session_id in session_ids:
        try:
            httpx.delete(f"{SIBYL}/sessions/{session_id}", timeout=10)
        except Exception as e:
            print(f"could not delete session {session_id}: {e}", file=sys.stderr)


PLANNING_TOOLS = ("plan_workflow", "run_workflow")


def captured_manifest(message: str) -> str:
    """The manifest the model landed on. Kept for callers that ignore tool use."""
    return capture_answer(message)[0]


def capture_answer(message: str):
    """Run one prompt and return (manifest, tools called, last rejection).

    A manifest the tool rejected is not an answer: the user never saw a plan and
    the model went on to do something else, so scoring the rejected attempt would
    mark a model wrong for recovering correctly. Only a manifest whose own call
    came back without an error counts, and the last of those is the final answer.

    The last rejection comes back too. Without it a run that produced nothing is
    an unexplained zero, and the interesting question is always whether the model
    never tried or tried and failed to recover.
    """
    accepted = []
    tools = []
    rejection = ""
    # a turn can carry several calls before any result, so results are matched to
    # calls by tool name in order rather than assuming they interleave
    pending = {}
    timeout = httpx.Timeout(connect=10.0, read=RUN_READ_TIMEOUT, write=30.0, pool=10.0)
    with httpx.Client(timeout=timeout) as client:
        # imported late so scoring a capture needs neither the tools nor the stack
        from src.agents.agent_manager import PERSONA

        with client.stream(
            "POST", f"{SIBYL}/runs", json={"system_prompt": PERSONA, "message": message}
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                kind = event.get("kind")
                name = str(event.get("name") or "")
                if kind == "tool_call":
                    tools.append(name)
                    if name not in PLANNING_TOOLS:
                        continue
                    try:
                        args = json.loads(str(event.get("args") or ""))
                    except json.JSONDecodeError:
                        continue
                    toml_text = args.get("manifest_toml")
                    if toml_text:
                        pending.setdefault(name, []).append(str(toml_text))
                elif kind == "tool_return" and pending.get(name):
                    manifest = pending[name].pop(0)
                    content = str(event.get("content") or "").strip()
                    if content.upper().startswith("ERROR"):
                        rejection = content
                    else:
                        accepted.append(manifest)
    # a call whose result never arrived stays an answer, so a truncated run fails
    # loudly instead of scoring as though the model planned nothing
    for leftover in pending.values():
        accepted.extend(leftover)
    return (accepted[-1] if accepted else ""), tools, rejection


def score_from_directory(tasks: list, directory: Path) -> list:
    """Score <task id>.toml files. A missing file means the model built nothing."""
    results = []
    for task in tasks:
        path = directory / f"{task.id}.toml"
        text = path.read_text() if path.exists() else ""
        results.append(TaskSamples(task.id, [score_manifest(task, text)]))
    return results


def markdown_report(meta: dict, results: list, tasks: list, title="Workflow eval") -> str:
    by_id = {t.id: t for t in tasks}
    repeated = meta["aggregate"].get("runs_per_task", 1) > 1
    flaky = meta["aggregate"].get("flaky") or []
    lines = [
        f"# {title}: {meta['profile']} / {meta['model'] or 'unknown model'}",
        "",
        f"{meta['mode']} mode, {meta['generated_at']}",
        "",
        f"**Aggregate {meta['aggregate']['score']:.2f}** over "
        f"{meta['aggregate']['tasks']} tasks "
        f"({meta['aggregate']['perfect']} perfect, "
        f"{meta['aggregate']['checks_passed']}/{meta['aggregate']['checks_total']} checks).",
    ]
    if repeated:
        runs = meta["aggregate"]["runs_per_task"]
        note = (
            f"Flaky: {', '.join(f'`{t}`' for t in flaky)}. Their score depends on "
            "sampling, so quote the range rather than one run."
            if flaky
            else "No task varied between runs."
        )
        preamble = (
            f"{runs} runs per task. Scores are means, checks come from each "
            f"task's worst run. {note}"
        )
        lines += ["", preamble]
    lines += [
        "",
        "| Task | Score | Checks | First failure |"
        if not repeated
        else "| Task | Mean | Range | Checks | First failure |",
        "|---|---|---|---|" if not repeated else "|---|---|---|---|---|",
    ]
    for res in results:
        failures = res.failures
        first = "none"
        if failures:
            detail = f": {failures[0].detail}" if failures[0].detail else ""
            first = f"{failures[0].name}{detail}"
        if repeated:
            span = f"{res.low:.2f} to {res.high:.2f}" if res.flaky else "stable"
            lines.append(
                f"| `{res.task_id}` | {res.score:.2f} | {span} | "
                f"{res.passed}/{res.total} | {first} |"
            )
        else:
            lines.append(
                f"| `{res.task_id}` | {res.score:.2f} | {res.passed}/{res.total} | {first} |"
            )
    lines += ["", "## Tasks", ""]
    for res in results:
        task = by_id.get(res.task_id)
        lines.append(f"- `{res.task_id}`: {task.prompt if task else ''}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", default=str(REPO_ROOT / "evals" / "tasks"))
    parser.add_argument(
        "--manifests",
        help="score <task id>.toml files from this directory instead of running the stack",
    )
    parser.add_argument(
        "--only", nargs="+", metavar="ID", help="run just these task ids"
    )
    parser.add_argument(
        "--no-fixtures",
        action="store_true",
        help="do not create the input layers the tasks read",
    )
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument(
        "--capture", help="write each model manifest here for re-scoring"
    )
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
    args = parser.parse_args(argv)
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    if args.repeat > 1 and args.manifests:
        print("--repeat needs the stack: a captured manifest scores the same every time",
              file=sys.stderr)
        return 2

    tasks = load_tasks(args.tasks)
    if args.only:
        tasks = [t for t in tasks if t.id in set(args.only)]
        if not tasks:
            print(f"no tasks match {args.only}", file=sys.stderr)
            return 2

    if args.manifests:
        results = score_from_directory(tasks, Path(args.manifests))
        profile, model = "captured", Path(args.manifests).name
        mode = "captured"
    else:
        reason = stack_skip_reason(args.allow_cloud)
        if reason:
            print(f"SKIP: {reason}")
            return 0
        profile, model, _ = active_profile()
        mode = "stack"
        if not args.no_fixtures:
            created = ensure_fixtures(tasks)
            if created:
                print(f"created input layers: {', '.join(created)}", file=sys.stderr)
        capture_dir = Path(args.capture) if args.capture else None
        if capture_dir:
            capture_dir.mkdir(parents=True, exist_ok=True)
        results = []
        user_session = active_session_id()
        eval_sessions = []
        try:
            for task in tasks:
                samples = []
                for run in range(1, args.repeat + 1):
                    label = f"{task.id}…" if args.repeat == 1 else f"{task.id} {run}/{args.repeat}…"
                    print(f"running {label}", file=sys.stderr)
                    # a fresh session per run, so one sample cannot bias the next
                    eval_sessions.append(start_eval_session(f"eval {task.id} {run}"))
                    manifest, tools, rejection = capture_answer(task.prompt)
                    if capture_dir and manifest:
                        name = (
                            f"{task.id}.toml"
                            if args.repeat == 1
                            else f"{task.id}.run{run}.toml"
                        )
                        (capture_dir / name).write_text(manifest)
                    samples.append(score_manifest(task, manifest, tools, rejection))
                results.append(TaskSamples(task.id, samples))
        finally:
            restore_session(user_session)
            delete_sessions(eval_sessions)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "profile": profile,
        "model": model,
        "aggregate": aggregate(results),
    }
    report = dict(meta, tasks=[r.as_dict() for r in results])
    text = markdown_report(meta, results, tasks)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = meta["generated_at"].replace(":", "").replace("-", "")
    base = out_dir / f"{stamp}-{profile}"
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
