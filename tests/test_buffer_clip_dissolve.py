"""With no input_path the tool has to save the buffer polygon itself: asked for
"3 km around Athens" it used to clip the geocoded point to its own buffer and
present one Point as the polygon.

Both branches name the file the way every reader of a tool result expects,
"Saved to outputs/<name>". The absolute container path it used to print left
the model with no layer name to draw."""

import json
import pathlib

import geopandas as gpd
import pytest

from src.agents.tools.buffer_clip_dissolve import (
    METRES_PER_KILOMETRE,
    METRIC_CRS,
    buffer_clip_dissolve,
)
from src.core import utils
from tool_sweep.runner import output_names

CENTER_LON = 23.7275
CENTER_LAT = 37.9838
BUFFER_KM = 3.0
# the buffer is a circle of straight segments, so its bounds meet the circle at
# the four cardinal points
BOUNDS_TOLERANCE_METRES = 1.0

POINTS_LAYER = "athens_points.geojson"
NEAR_CENTER = (23.7280, 37.9840)
FAR_AWAY = (24.5000, 38.5000)


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    # the tree dirs are read once at import, so the env var alone misses them
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    return pathlib.Path(utils.caller_outputs_dir())


def stage_points(outputs):
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(coordinates)},
            "properties": {"name": name},
        }
        for coordinates, name in ((NEAR_CENTER, "inside"), (FAR_AWAY, "outside"))
    ]
    (outputs / POINTS_LAYER).write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )
    return POINTS_LAYER


def test_no_input_saves_the_buffer_polygon(outputs):
    result = buffer_clip_dissolve(
        center_lon=CENTER_LON,
        center_lat=CENTER_LAT,
        buffer_km=BUFFER_KM,
        output_filename="athens_buffer",
    )

    assert "Buffer polygon" in result, result
    assert output_names(result) == ["athens_buffer.gpkg"]
    assert str(outputs) not in result

    saved = gpd.read_file(outputs / "athens_buffer.gpkg")
    assert len(saved) == 1
    assert saved.geometry.iloc[0].geom_type == "Polygon"
    assert saved.crs.to_epsg() == 4326
    assert saved.iloc[0]["center_lon"] == CENTER_LON
    assert saved.iloc[0]["center_lat"] == CENTER_LAT
    assert saved.iloc[0]["buffer_km"] == BUFFER_KM

    min_x, min_y, max_x, max_y = saved.to_crs(METRIC_CRS).total_bounds
    diameter = 2 * BUFFER_KM * METRES_PER_KILOMETRE
    assert max_x - min_x == pytest.approx(diameter, abs=BOUNDS_TOLERANCE_METRES)
    assert max_y - min_y == pytest.approx(diameter, abs=BOUNDS_TOLERANCE_METRES)

    center = gpd.GeoSeries.from_wkt(
        [f"POINT ({CENTER_LON} {CENTER_LAT})"], crs="EPSG:4326"
    ).to_crs(METRIC_CRS)
    assert (min_x + max_x) / 2 == pytest.approx(
        center.x.iloc[0], abs=BOUNDS_TOLERANCE_METRES
    )
    assert (min_y + max_y) / 2 == pytest.approx(
        center.y.iloc[0], abs=BOUNDS_TOLERANCE_METRES
    )


def test_a_dissolve_field_with_no_input_is_ignored(outputs):
    result = buffer_clip_dissolve(
        center_lon=CENTER_LON,
        center_lat=CENTER_LAT,
        buffer_km=BUFFER_KM,
        output_filename="athens_buffer",
        dissolve_field="category",
    )

    assert "Buffer polygon" in result, result
    saved = gpd.read_file(outputs / "athens_buffer.gpkg")
    assert len(saved) == 1
    assert saved.geometry.iloc[0].geom_type == "Polygon"


def test_an_input_layer_is_still_clipped_to_the_buffer(outputs):
    layer = stage_points(outputs)

    result = buffer_clip_dissolve(
        center_lon=CENTER_LON,
        center_lat=CENTER_LAT,
        buffer_km=BUFFER_KM,
        output_filename="athens_points_3km",
        input_path=layer,
    )

    assert "Features: 1" in result, result
    assert output_names(result) == ["athens_points_3km.gpkg"]
    assert str(outputs) not in result

    saved = gpd.read_file(outputs / "athens_points_3km.gpkg")
    assert list(saved["name"]) == ["inside"]
    assert saved.geometry.iloc[0].geom_type == "Point"
    assert saved.crs.to_epsg() == 4326
