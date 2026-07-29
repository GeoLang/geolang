"""NL regression evals: real agent runs through sibyl against the local model.

Each test costs a full model run (10-90s on the 35B), so the whole module is
skipped unless the local chain is up: geolang api, sibyl in local-model mode,
and the llama server. Model wording varies between runs: assert on tool
choices and artifact validity, never on exact phrasing.
"""

import json
import os
import re
import subprocess

import httpx
import pytest

from src.agents.agent_manager import PERSONA

GEOLANG = os.environ.get("NL_EVAL_GEOLANG", "http://localhost:8080")
SIBYL = os.environ.get("NL_EVAL_SIBYL", "http://localhost:8090")
MODEL_PROBE = os.environ.get("NL_EVAL_MODEL", "http://172.17.0.1:18200/v1/models")
RUN_READ_TIMEOUT = 240.0


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code == 200
    except Exception:
        return False


def _sibyl_is_local() -> bool:
    # /health doesn't report the backend, so read the container env: running
    # these against cloud mode would silently spend x.ai credits
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", "viewtopia-sibyl-1"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if line.startswith("SIBYL_API_BASE="):
            return "host.docker.internal" in line or "172.17.0.1" in line
    return False


# NL_EVAL_ALLOW_CLOUD=1 runs against whatever backend sibyl has (e.g. grok as
# a reference baseline): it spends cloud credits, so it's opt-in per run
_allow_cloud = os.environ.get("NL_EVAL_ALLOW_CLOUD") == "1"
pytestmark = pytest.mark.skipif(
    not (
        _up(f"{GEOLANG}/tools")
        and _up(f"{SIBYL}/health")
        and (_allow_cloud or (_up(MODEL_PROBE) and _sibyl_is_local()))
    ),
    reason="local model chain not up (geolang api + sibyl in local mode + llama server)",
)


class RunResult:
    def __init__(self):
        self.tool_calls: list[tuple[str, str]] = []
        self.tool_returns: list[tuple[str, str]] = []
        self.texts: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.texts)

    def calls(self, name: str) -> list[str]:
        return [args for n, args in self.tool_calls if n == name]

    @property
    def failures(self) -> list[str]:
        return [c for _, c in self.tool_returns if c.startswith("❌") or c.startswith("ERROR")]

    @property
    def ui_spec(self) -> dict | None:
        for _, content in self.tool_returns:
            if "__UI_SPEC__:" in content:
                try:
                    return json.loads(content.split("__UI_SPEC__:", 1)[1])
                except json.JSONDecodeError:
                    return None
        return None


def run_prompt(message: str) -> RunResult:
    res = RunResult()
    timeout = httpx.Timeout(connect=10.0, read=RUN_READ_TIMEOUT, write=30.0, pool=10.0)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST", f"{SIBYL}/runs", json={"system_prompt": PERSONA, "message": message}
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                kind = ev.get("kind")
                if kind == "tool_call":
                    res.tool_calls.append((str(ev.get("name") or ""), str(ev.get("args") or "")))
                elif kind == "tool_return":
                    res.tool_returns.append((str(ev.get("name") or ""), str(ev.get("content") or "")))
                elif kind == "text":
                    res.texts.append(str(ev.get("content") or ""))
    return res


def fetch_geojson(path: str) -> dict:
    name = path.split("/")[-1]
    r = httpx.get(f"{GEOLANG}/geojson/{name}", timeout=60)
    r.raise_for_status()
    return r.json()


def max_abs_coords(geojson: dict) -> tuple[float, float]:
    worst_lon = worst_lat = 0.0

    def walk(c):
        nonlocal worst_lon, worst_lat
        if not isinstance(c, list) or not c:
            return
        if isinstance(c[0], (int, float)):
            worst_lon = max(worst_lon, abs(c[0]))
            if len(c) > 1 and isinstance(c[1], (int, float)):
                worst_lat = max(worst_lat, abs(c[1]))
        else:
            for x in c:
                walk(x)

    for f in geojson.get("features", []):
        walk((f.get("geometry") or {}).get("coordinates", []))
    return worst_lon, worst_lat


def parse_args(args: str) -> dict:
    try:
        return json.loads(args)
    except json.JSONDecodeError:
        return {}


def test_plain_question_completes():
    res = run_prompt("In one or two sentences, what kinds of geospatial analysis can you do?")
    assert res.text.strip(), "run produced no assistant text"
    assert not res.failures, f"tool failures on a plain question: {res.failures}"


def test_geocode_eiffel_tower():
    # tool named in the prompt: models sometimes answer landmark coordinates
    # from their weights, and this test is about the geocoding tool path
    res = run_prompt("Use the geocode_place tool to find the exact coordinates of the Eiffel Tower.")
    geocodes = res.calls("geocode_place")
    assert geocodes, f"geocode_place not called; called {[n for n, _ in res.tool_calls]}"
    assert any("eiffel" in a.lower() for a in geocodes)
    assert "48.8" in res.text, f"expected Eiffel latitude in reply, got: {res.text[:300]}"


def test_metric_buffer_stays_in_lonlat_range():
    # regression for the degrees-vs-metres buffer: the run may hit the
    # corrective tool error, but the final artifacts must be valid lon/lat
    res = run_prompt("Show me Lisbon's city boundary with an 8 km buffer around it on the map.")
    spec = res.ui_spec
    assert spec and spec.get("layers"), f"no map spec emitted; failures: {res.failures}"
    for layer in spec["layers"]:
        gj = fetch_geojson(layer["file"])
        lon, lat = max_abs_coords(gj)
        assert lon <= 180 and lat <= 90, (
            f"layer '{layer.get('name')}' out of range (|lon| {lon:.0f}, |lat| {lat:.0f})"
        )


def test_marker_at_mount_fuji():
    res = run_prompt("Add a marker on the map at the summit of Mount Fuji.")
    markers = [
        parse_args(a)
        for a in res.calls("viewer_control")
        if parse_args(a).get("action") == "add_marker"
    ]
    assert markers, f"no add_marker viewer_control call; called {[n for n, _ in res.tool_calls]}"
    m = markers[-1]
    assert 35.0 <= float(m.get("lat", 0)) <= 36.0, f"marker lat off Fuji: {m}"
    assert 138.0 <= float(m.get("lon", 0)) <= 139.5, f"marker lon off Fuji: {m}"


def test_elevation_mont_blanc():
    # the tool is named in the prompt: with a soft "check with your tools" the
    # model sometimes answers from its weights, and this test is about the tool path
    res = run_prompt("Use the query_elevation tool to check how high Mont Blanc is.")
    assert res.calls("query_elevation"), (
        f"query_elevation not called; called {[n for n, _ in res.tool_calls]}"
    )
    assert not res.failures, f"tool failures: {res.failures}"
    assert re.search(r"\b4[4-9]\d{2}\b", res.text.replace(",", "")), (
        f"expected ~4800m elevation in reply, got: {res.text[:300]}"
    )
