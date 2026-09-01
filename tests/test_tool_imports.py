"""The manifest offers a tool only where its imports resolve."""

import pytest
from fastapi.testclient import TestClient

from src.agents import tool_imports
from src.agents.tool_imports import missing_packages, required_packages
from src.api import server

client = TestClient(server.app)

MODULE_SOURCE = '''
import osmnx
from . import sibling
from src.core.utils import tool_output_path


def run(place):
    import json

    try:
        import rasterstats
    except ImportError:
        import rasterio

    try:
        from scipy.spatial import Voronoi
    except Exception:
        return "no scipy"

    try:
        import networkx
        return networkx.Graph()
    except Exception as error:
        return str(error)
'''


def test_the_scanner_reads_what_a_module_cannot_run_without():
    """networkx sits in the body-wide error wrapper, so it is required.

    rasterstats and its rasterio fallback are guarded by ImportError, scipy by a
    try that does not span the body, json is stdlib and src is this repo's.
    """
    assert required_packages(MODULE_SOURCE) == {"osmnx", "networkx"}


def test_a_package_on_the_bridged_qgis_paths_counts_as_installed(tmp_path, monkeypatch):
    """The platform image holds qgis on the paths qgis_session bridges, not on
    sys.path, so find_spec alone would drop pyqgis_api there."""
    (tmp_path / "faux_qgis").mkdir()
    (tmp_path / "faux_qgis" / "__init__.py").write_text("")
    monkeypatch.setattr(tool_imports, "QGIS_SYSTEM_PATHS", (str(tmp_path),))

    assert missing_packages("import faux_qgis") == []
    assert missing_packages("import faux_absent") == ["faux_absent"]


@pytest.fixture
def networkx_missing(monkeypatch):
    real_find_spec = tool_imports.find_spec
    monkeypatch.setattr(
        tool_imports,
        "find_spec",
        lambda name: None if name == "networkx" else real_find_spec(name),
    )


def manifest_names() -> set[str]:
    return {tool["name"] for tool in client.get("/tools").json()["tools"]}


def test_the_manifest_drops_a_tool_whose_package_is_not_installed(networkx_missing):
    names = manifest_names()

    assert "calculate_isochrones" not in names
    assert {"geocode_place", "buffer_clip_dissolve", "spatial_join"} <= names


def test_the_manifest_keeps_that_tool_once_the_package_is_back():
    assert "calculate_isochrones" in manifest_names()
