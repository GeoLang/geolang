"""The offline half of the nightly tool sweep, run on every push.

The nightly runs every advertised tool against the live platform stack
(`python -m tool_sweep.runner`). That needs the whole data plane, so per push
only the tools marked `offline` in the sweep's own arguments table run here,
through the same `POST /tools/{name}` route with the same arguments. One table
feeds both, so a tool that works in this suite and breaks under the nightly
differs by the stack, not by the arguments.

The sample layers are uploaded through `POST /upload` exactly as the nightly
stages them, into a tmp_path tree rather than the checkout.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import server
from src.core import utils
from tool_sweep.arguments import STAGED_LAYERS, SWEEP_ARGUMENTS
from tool_sweep.runner import ERROR_MARKER

client = TestClient(server.app)

REPO_ROOT = Path(__file__).resolve().parent.parent
OFFLINE_TOOLS = [name for name, sample in SWEEP_ARGUMENTS.items() if sample.offline]


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
    response = client.post(f"/tools/{name}", json={"args": SWEEP_ARGUMENTS[name].args})

    assert response.status_code == 200
    result = response.json()["result"]
    assert result and ERROR_MARKER not in result, result[:400]


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
