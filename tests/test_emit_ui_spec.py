"""emit_ui_spec must never return a success-shaped result for a blank map:
models retry-loop on silent no-ops (observed with grok), so bad layer input
and missing files have to come back as actionable errors."""

import json

import pytest

from src.agents.tools.a2ui import emit_ui_spec


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "buffer.gpkg").write_bytes(b"stub")
    return out


def spec_of(result: str) -> dict:
    assert result.startswith("__UI_SPEC__:"), result
    return json.loads(result.split("__UI_SPEC__:", 1)[1])


def test_pipe_format_still_works(outputs):
    res = emit_ui_spec("map", center_lon=1.0, center_lat=2.0,
                       layers="Buffer|outputs/buffer.gpkg|#ff6b35")
    spec = spec_of(res)
    assert spec["layers"] == [{"name": "Buffer", "file": "outputs/buffer.gpkg", "color": "#ff6b35"}]


def test_json_array_accepted(outputs):
    res = emit_ui_spec("map", layers='[{"name": "Buffer", "file": "outputs/buffer.gpkg"}]')
    spec = spec_of(res)
    assert spec["layers"][0]["file"] == "outputs/buffer.gpkg"
    assert spec["layers"][0]["color"] == "#3388ff"


def test_unparseable_layers_is_an_error(outputs):
    res = emit_ui_spec("map", layers="buffer.gpkg and also the boundary")
    assert res.startswith("ERROR")
    assert "name|file|color" in res


def test_no_layers_is_an_error(outputs):
    res = emit_ui_spec("map", center_lon=-9.15, center_lat=38.74)
    assert res.startswith("ERROR")
    assert "viewer_control" in res


def test_missing_file_is_an_error(outputs):
    res = emit_ui_spec("map", layers="Ghost|outputs/nope.gpkg")
    assert res.startswith("ERROR")
    assert "nope.gpkg" in res
    assert "list_outputs" in res
