"""The offline half of the nightly tool sweep, run on every push.

The nightly runs every advertised tool against the live platform stack
(`python -m tool_sweep.runner`). That needs the whole data plane, so per push
only the tools marked `offline` in the sweep's own arguments table run here,
through the same `POST /tools/{name}` route with the same arguments. One table
feeds both, so a tool that works in this suite and breaks under the nightly
differs by the stack, not by the arguments.

The sample layers are uploaded through `POST /upload` exactly as the nightly
stages them, into a tmp_path tree rather than the checkout.

The QGIS tools are offline too, and run here wherever the QGIS bindings are
importable: the platform image, which is where the README runs this suite. On a
checkout without them the test skips rather than passes.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import server
from src.core import utils
from src.core.qgis_session import QgisUnavailable, qgis_session
from tool_sweep.arguments import STAGED_LAYERS, SWEEP_ARGUMENTS, SWEEP_MANIFEST_TOML
from tool_sweep.runner import ERROR_MARKER, approve_manifest

client = TestClient(server.app)

REPO_ROOT = Path(__file__).resolve().parent.parent
OFFLINE_TOOLS = [name for name, sample in SWEEP_ARGUMENTS.items() if sample.offline]


@lru_cache(maxsize=1)
def qgis_starts_here() -> bool:
    """Whether QGIS runs in this process, asked the way the tools ask."""
    try:
        qgis_session()
    except QgisUnavailable:
        return False
    return True


@pytest.fixture
def staged_layers(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    # the upload route took its own copy of EXEC_DIR at import
    monkeypatch.setattr(server, "EXEC_DIR", str(tmp_path))
    for filename, geojson in STAGED_LAYERS.items():
        response = client.post(
            "/upload",
            files={
                "file": (filename, json.dumps(geojson).encode(), "application/geo+json")
            },
        )
        assert response.status_code == 200, response.text
    return tmp_path


def manifest_names() -> set[str]:
    return {tool["name"] for tool in client.get("/tools").json()["tools"]}


def test_every_manifest_tool_has_sweep_arguments():
    assert manifest_names() == set(SWEEP_ARGUMENTS)


@pytest.mark.parametrize("name", OFFLINE_TOOLS)
def test_offline_tool_runs_through_the_route(staged_layers, name):
    if SWEEP_ARGUMENTS[name].needs_qgis and not qgis_starts_here():
        pytest.skip("no QGIS bindings here; the nightly runs this in the platform image")

    response = client.post(f"/tools/{name}", json={"args": SWEEP_ARGUMENTS[name].args})

    assert response.status_code == 200
    result = response.json()["result"]
    assert result and ERROR_MARKER not in result, result[:400]


def test_the_sweeps_approve_step_reaches_the_route(staged_layers):
    """run_workflow needs geodukt, so only the nightly runs it. The call standing
    between it and the approval gate is checked here rather than at night.

    The layers are staged first for the same reason the nightly stages them
    before any tool runs: confining a manifest path looks the file up.
    """
    assert SWEEP_ARGUMENTS["run_workflow"].needs_approval
    # nothing planned this manifest in this process, so the route says exactly that
    assert "never planned" in approve_manifest(client, SWEEP_MANIFEST_TOML)


def test_api_reference_lists_every_tool_and_counts_them():
    """The tool catalogue in the docs, checked against the loaded manifest."""
    text = (REPO_ROOT / "docs" / "api_reference.md").read_text()
    catalogue = text.split("## Tool catalogue")[1].split("Adding a tool")[0]
    names = manifest_names()

    stated = re.search(r"(\d+) tools live under", catalogue)
    assert stated and int(stated.group(1)) == len(names)
    for name in names:
        assert f"`{name}`" in catalogue, f"{name} is not in the docs tool catalogue"


def test_readme_counts_the_tools():
    readme = (REPO_ROOT / "README.md").read_text()

    stated = re.search(r"(\d+) geospatial tools", readme)
    assert stated and int(stated.group(1)) == len(manifest_names())
