"""With no input_path the tool has to save the buffer polygon itself: asked for
"3 km around Athens" it used to clip the geocoded point to its own buffer and
present one Point as the polygon.

Both branches name the file the way every reader of a tool result expects,
"Saved to outputs/<name>". The absolute container path it used to print left
the model with no layer name to draw.

The radius has to be true on the ground, so the extents are measured with a
geodesic rather than read off the projected bounds."""

import json
import pathlib

import geopandas as gpd
import pyproj
import pytest
from shapely.geometry import Point

from src.agents.tools.buffer_clip_dissolve import (
    METRES_PER_KILOMETRE,
    WGS84,
    buffer_clip_dissolve,
    metric_crs_centred_on,
)
from src.core import utils
from tool_sweep.runner import output_names

CENTER_LON = 23.7275
CENTER_LAT = 37.9838  # Athens, where a web mercator buffer is a fifth short
BUFFER_KM = 3.0
EXTENT_TOLERANCE = 0.01
ROUND_TRIP_TOLERANCE_METRES = 0.001

POINTS_LAYER = "athens_points.geojson"
NEAR_CENTER = (23.7280, 37.9840)
FAR_AWAY = (24.5000, 38.5000)

GEODESIC = pyproj.Geod(ellps="WGS84")


def ground_extents_metres(bounds) -> tuple[float, float]:
    """How wide and how tall the bounding box is on the ground, through the centre."""
    min_lon, min_lat, max_lon, max_lat = bounds
    _, _, east_west = GEODESIC.inv(min_lon, CENTER_LAT, max_lon, CENTER_LAT)
    _, _, north_south = GEODESIC.inv(CENTER_LON, min_lat, CENTER_LON, max_lat)
    return east_west, north_south


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


def test_the_installed_proj_reads_the_projection_the_tool_builds():
    crs = metric_crs_centred_on(CENTER_LON, CENTER_LAT)
    into = pyproj.Transformer.from_crs(WGS84, crs, always_xy=True)
    out_of = pyproj.Transformer.from_crs(crs, WGS84, always_xy=True)

    # the centre is the origin, which is what makes the metres true distances
    assert into.transform(CENTER_LON, CENTER_LAT) == pytest.approx(
        (0.0, 0.0), abs=ROUND_TRIP_TOLERANCE_METRES
    )

    radius = BUFFER_KM * METRES_PER_KILOMETRE
    due_east = out_of.transform(radius, 0)
    _, _, distance = GEODESIC.inv(CENTER_LON, CENTER_LAT, *due_east)
    assert distance == pytest.approx(radius, rel=EXTENT_TOLERANCE)


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

    assert saved.geometry.iloc[0].contains(Point(CENTER_LON, CENTER_LAT))

    diameter = 2 * BUFFER_KM * METRES_PER_KILOMETRE
    east_west, north_south = ground_extents_metres(saved.total_bounds)
    assert east_west == pytest.approx(diameter, rel=EXTENT_TOLERANCE)
    assert north_south == pytest.approx(diameter, rel=EXTENT_TOLERANCE)


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
