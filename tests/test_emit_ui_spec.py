"""emit_ui_spec must never return a success-shaped result for a blank map:
models retry-loop on silent no-ops (observed with grok), so bad layer input
and missing files have to come back as actionable errors."""

import json
import pathlib

import pytest

from src.agents.tools.a2ui import emit_ui_spec
from src.core import utils


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    # the tree dirs are read once at import, so the env var alone misses them
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_DIR", tmp_path / "user_data")
    # the caller's own directory, which is where a layer of theirs is looked up
    out = pathlib.Path(utils.caller_outputs_dir())
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


def test_shade_by_rides_on_a_fourth_part(outputs):
    res = emit_ui_spec("map", layers="Gaps|outputs/buffer.gpkg|#ff6b35|gap_score")
    layer = spec_of(res)["layers"][0]
    assert layer["shade_by"] == "gap_score"
    assert layer["color"] == "#ff6b35"


def test_shade_by_survives_an_empty_color(outputs):
    res = emit_ui_spec("map", layers="Gaps|outputs/buffer.gpkg||gap_score")
    layer = spec_of(res)["layers"][0]
    assert layer["shade_by"] == "gap_score"
    assert layer["color"] == "#3388ff"


def test_shade_by_accepted_in_json_array(outputs):
    res = emit_ui_spec(
        "map",
        layers='[{"name": "Gaps", "file": "outputs/buffer.gpkg", "shade_by": "gap_score"}]',
    )
    assert spec_of(res)["layers"][0]["shade_by"] == "gap_score"


def test_prose_instead_of_a_column_is_an_error(outputs):
    res = emit_ui_spec("map", layers="Gaps|outputs/buffer.gpkg|#ff6b35|the gap score column")
    assert res.startswith("ERROR")
    assert "the gap score column" in res
    assert "gap_score" in res


def test_missing_file_is_an_error(outputs):
    res = emit_ui_spec("map", layers="Ghost|outputs/nope.gpkg")
    assert res.startswith("ERROR")
    assert "nope.gpkg" in res
    assert "list_outputs" in res
