"""Run every advertised tool through the HTTP API and report what broke.

  python -m tool_sweep.runner                                   # localhost:8080
  python -m tool_sweep.runner --base-url http://localhost:5174/agent
  python -m tool_sweep.runner --skip-external --skip-crashing   # deterministic
  python -m tool_sweep.runner --only clip_layer,voronoi

`PLATFORM_TOKEN` is the bearer for every request. Without it the sweep runs
unauthenticated, which only reaches a stack started with the auth gate off.

Each result is appended to the JSONL file as its tool finishes, so a run killed
half way still says how far it got and which tool it died on.

Exit status is 1 when anything failed, tool and external alike. A tool in the
manifest with no entry in the arguments table is one of those failures.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from tool_sweep.arguments import STAGED_LAYERS, SWEEP_ARGUMENTS

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_RESULTS = "outputs/tool_sweep.jsonl"
TOKEN_ENV = "PLATFORM_TOKEN"
MESSAGE_CHARS = 300
# the error marker every tool returns its failures behind, and the one thing a
# 200 body has to be read for: the route answers a tool exception with one
ERROR_MARKER = "❌"


def _headers() -> dict:
    token = os.environ.get(TOKEN_ENV, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _selected(names: list[str] | None) -> list[str]:
    """Table order, filtered, with each entry's `after` pulled in ahead of it."""
    if not names:
        return list(SWEEP_ARGUMENTS)
    unknown = sorted(set(names) - set(SWEEP_ARGUMENTS))
    if unknown:
        raise SystemExit(f"--only names no tool in the arguments table: {unknown}")
    wanted = set(names)
    for name in names:
        sample = SWEEP_ARGUMENTS.get(name)
        while sample and sample.after:
            wanted.add(sample.after)
            sample = SWEEP_ARGUMENTS.get(sample.after)
    return [name for name in SWEEP_ARGUMENTS if name in wanted]


def stage_layers(client: httpx.Client) -> None:
    """Upload the sample layers the path-taking tools name."""
    for filename, geojson in STAGED_LAYERS.items():
        response = client.post(
            "/upload",
            files={
                "file": (filename, json.dumps(geojson).encode(), "application/geo+json")
            },
        )
        response.raise_for_status()


def run_tool(
    client: httpx.Client, name: str, args: dict
) -> tuple[bool, str, float]:
    started = time.monotonic()
    try:
        response = client.post(f"/tools/{name}", json={"args": args})
    except httpx.HTTPError as e:
        return False, f"request failed: {e}", time.monotonic() - started
    seconds = time.monotonic() - started
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}: {response.text}", seconds
    result = str(response.json().get("result") or "")
    if not result:
        return False, "empty result", seconds
    if ERROR_MARKER in result:
        return False, result, seconds
    return True, result, seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--results", default=DEFAULT_RESULTS)
    parser.add_argument(
        "--skip-external",
        action="store_true",
        help="leave out the tools that can reach a third-party host",
    )
    parser.add_argument(
        "--skip-crashing",
        action="store_true",
        help="leave out the tools that segfault the executor (the QGIS four)",
    )
    parser.add_argument("--only", help="comma-separated tool names")
    parser.add_argument("--timeout", type=float, default=300.0)
    options = parser.parse_args()

    results_path = Path(options.results)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text("")

    client = httpx.Client(
        base_url=options.base_url.rstrip("/"),
        headers=_headers(),
        timeout=options.timeout,
    )

    listing = client.get("/tools")
    listing.raise_for_status()
    manifest = {tool["name"] for tool in listing.json()["tools"]}
    unswept = sorted(manifest - set(SWEEP_ARGUMENTS))
    stale = sorted(set(SWEEP_ARGUMENTS) - manifest)

    names = options.only.split(",") if options.only else None
    selected = [name for name in _selected(names) if name in manifest]
    if options.skip_external:
        selected = [n for n in selected if not SWEEP_ARGUMENTS[n].external]
    if options.skip_crashing:
        selected = [n for n in selected if not SWEEP_ARGUMENTS[n].crashes_executor]

    stage_layers(client)

    records = []
    for name in unswept:
        records.append(
            {
                "name": name,
                "ok": False,
                "external": False,
                "seconds": 0.0,
                "message": "no sweep arguments: a new tool shipped unswept",
            }
        )
    for name in stale:
        records.append(
            {
                "name": name,
                "ok": False,
                "external": False,
                "seconds": 0.0,
                "message": "sweep arguments for a tool the manifest no longer has",
            }
        )

    with results_path.open("a") as results_file:
        for record in records:
            results_file.write(json.dumps(record) + "\n")
        results_file.flush()

        for name in selected:
            sample = SWEEP_ARGUMENTS[name]
            ok, message, seconds = run_tool(client, name, sample.args)
            record = {
                "name": name,
                "ok": ok,
                "external": sample.external,
                "seconds": round(seconds, 2),
                "message": message[:MESSAGE_CHARS].replace("\n", " "),
            }
            records.append(record)
            results_file.write(json.dumps(record) + "\n")
            results_file.flush()
            print(f"{'ok  ' if ok else 'FAIL'} {seconds:7.2f}s  {name}", flush=True)

    broken = [r for r in records if not r["ok"] and not r["external"]]
    external_down = [r for r in records if not r["ok"] and r["external"]]
    print(f"\n{len(records) - len(broken) - len(external_down)} passed, "
          f"{len(broken)} broken, {len(external_down)} external failures")
    for heading, group in (("broken", broken), ("external", external_down)):
        for record in group:
            print(f"  {heading}: {record['name']}: {record['message']}")
    print(f"results: {results_path}")

    return 1 if broken or external_down else 0


if __name__ == "__main__":
    sys.exit(main())
