"""download_osm_data: whole-feature fetch, sub-query dedupe, search radius."""

import pathlib
import sys
import warnings
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from src.agents.tools.download_osm_data import download_osm_data
from src.core import utils

LAT, LON = 51.5, -0.12

# the wording osmnx uses when a place is too big for one Overpass call
SUBDIVIDE_WARNING = (
    "This area is 11 times your configured Overpass max query area size. "
    "It will automatically be divided up into multiple sub-queries accordingly."
)


def _features(ids):
    """Feature frame indexed the way osmnx indexes one, so ids can repeat."""
    return gpd.GeoDataFrame(
        {"name": [f"way {i}" for i in ids]},
        geometry=[LineString([(LON, LAT), (LON + 0.01 * i, LAT)]) for i in ids],
        index=pd.MultiIndex.from_tuples(
            [("way", i) for i in ids], names=["element", "id"]
        ),
        crs="EPSG:4326",
    )


class _RecordingOsmnx:
    """Records what it was asked for and answers with the frame it was given."""

    def __init__(self, frame, warn=False):
        self.frame = frame
        self.warn = warn
        self.point_calls = []
        self.place_calls = []

    def geocode(self, place_name):
        return LAT, LON

    def features_from_place(self, place_name, tags=None):
        self.place_calls.append((place_name, tags))
        if self.warn:
            warnings.warn(SUBDIVIDE_WARNING, UserWarning, stacklevel=2)
        return self.frame

    def features_from_point(self, point, tags=None, dist=None):
        self.point_calls.append({"point": point, "tags": tags, "dist": dist})
        return self.frame


class _RoadsOsmnx:
    """Answers geocode_to_gdf with a fixed boundary and records graph requests."""

    def __init__(self, boundary, frame):
        self.boundary = boundary
        self.frame = frame
        self.graph_calls = []

    def geocode_to_gdf(self, place_name):
        return self.boundary

    def graph_from_place(self, place_name, network_type=None):
        self.graph_calls.append((place_name, network_type))
        return "graph"

    def graph_to_gdfs(self, G, nodes=False):
        return self.frame


def _boundary(degrees):
    return gpd.GeoDataFrame(
        geometry=[box(LON, LAT, LON + degrees, LAT + degrees)], crs="EPSG:4326"
    )


def _nominatim(hits, calls=None):
    def get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append(params)
        return SimpleNamespace(
            status_code=200, raise_for_status=lambda: None, json=lambda: hits
        )

    return SimpleNamespace(get=get)


@pytest.fixture
def outputs(monkeypatch, tmp_path):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    return tmp_path


def _read_output(name):
    path = pathlib.Path(utils.caller_outputs_dir()) / f"{name}.gpkg"
    assert path.exists(), f"{path} not written"
    return gpd.read_file(path)


def test_overlapping_sub_queries_do_not_inflate_the_feature_count(monkeypatch, outputs):
    # osmnx concatenates every sub-query response before indexing, so a way that
    # falls in four sub-polygons arrives four times
    repeated = _features([1, 1, 2, 2, 2, 3])
    fake = _RecordingOsmnx(repeated)
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    result = download_osm_data(
        data_type="waterway=river", place_name="South East England", output_filename="rivers"
    )

    assert len(_read_output("rivers")) == 3
    assert "Downloaded 3 waterway=river features" in result
    assert "Dropped 3 duplicate features" in result


def test_a_subdivided_query_says_so_instead_of_going_quiet(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]), warn=True)
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    result = download_osm_data(
        data_type="waterway=river", place_name="South East England", output_filename="rivers"
    )

    assert "exceeds the Overpass max query area" in result
    assert "cached" in result
    assert "feature_name" in result


def test_radius_reaches_the_point_query(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    download_osm_data(
        data_type="cafes",
        place_name=f"{LAT},{LON}",
        radius_m=40000,
        output_filename="cafes",
    )

    assert fake.point_calls[0]["dist"] == 40000


def test_point_query_without_a_radius_keeps_the_1km_default(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    download_osm_data(
        data_type="cafes", place_name=f"{LAT},{LON}", output_filename="cafes"
    )

    assert fake.point_calls[0]["dist"] == 1000


def test_a_named_feature_arrives_whole_and_ignores_place_boundaries(monkeypatch, outputs):
    # a river running well outside any one place: a boundary search would clip it
    whole_river = {
        "osm_type": "relation",
        "osm_id": 2263653,
        "class": "waterway",
        "type": "river",
        "display_name": "River Thames, England, United Kingdom",
        "geojson": {
            "type": "MultiLineString",
            "coordinates": [
                [[-2.03, 51.69], [-1.26, 51.75]],
                [[-1.26, 51.75], [-0.12, 51.50]],
                [[-0.12, 51.50], [0.68, 51.52]],
            ],
        },
    }
    calls = []
    fake = _RecordingOsmnx(_features([1]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    monkeypatch.setitem(sys.modules, "requests", _nominatim([whole_river], calls))

    result = download_osm_data(
        data_type="waterway=river",
        feature_name="River Thames",
        output_filename="thames",
    )

    written = _read_output("thames")
    assert len(written) == 3, "each part of the relation should survive as a feature"
    assert fake.place_calls == [], "a named feature must not fall back to an area search"
    assert calls[0]["polygon_geojson"] == 1
    # the full span, not the piece inside any single place
    assert written.total_bounds[0] == pytest.approx(-2.03)
    assert written.total_bounds[2] == pytest.approx(0.68)
    assert "relation 2263653" in result
    assert "Total length:" in result


def test_a_named_feature_prefers_the_hit_matching_the_requested_tag(monkeypatch, outputs):
    pub = {
        "osm_type": "node",
        "osm_id": 1,
        "class": "amenity",
        "type": "pub",
        "display_name": "The River Thames, London",
        "geojson": {"type": "Point", "coordinates": [-0.12, 51.50]},
    }
    river = {
        "osm_type": "relation",
        "osm_id": 2263653,
        "class": "waterway",
        "type": "river",
        "display_name": "River Thames, England",
        "geojson": {
            "type": "LineString",
            "coordinates": [[-2.03, 51.69], [0.68, 51.52]],
        },
    }
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx(_features([1])))
    # the pub ranks first, so only the tag match keeps this off it
    monkeypatch.setitem(sys.modules, "requests", _nominatim([pub, river]))

    result = download_osm_data(
        data_type="waterway=river", feature_name="River Thames", output_filename="thames"
    )

    assert "relation 2263653" in result
    assert _read_output("thames").geometry.iloc[0].geom_type == "LineString"


def test_roads_for_an_oversized_place_are_refused(monkeypatch, outputs):
    # a 1 degree square, thousands of km2
    fake = _RoadsOsmnx(_boundary(1.0), _features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    result = download_osm_data(
        data_type="roads", place_name="London", output_filename="roads"
    )

    assert "capped at 50 km2" in result
    assert fake.graph_calls == [], "the graph download must not start"


def test_roads_for_a_district_sized_place_download(monkeypatch, outputs):
    # about 20 km2, under the cap
    fake = _RoadsOsmnx(_boundary(0.05), _features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    result = download_osm_data(
        data_type="roads", place_name="Camden", output_filename="roads"
    )

    assert fake.graph_calls == [("Camden", "all")]
    assert len(_read_output("roads")) == 2
    assert "Downloaded 2 roads features" in result


def test_neither_a_place_nor_a_feature_is_refused(monkeypatch, outputs):
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx(_features([1])))

    assert "place_name or feature_name" in download_osm_data(data_type="cafes")


def test_a_kind_of_feature_is_refused_as_a_name(monkeypatch, outputs):
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx(_features([1])))

    result = download_osm_data(data_type="waterway", feature_name="river", place_name="Benghazi")

    assert "names a kind of feature" in result
    assert "data_type='river'" in result


def test_a_named_feature_that_is_not_the_kind_asked_for_is_refused(monkeypatch, outputs):
    # relation 2604751 is a civil parish called River, near Dover
    parish = {
        "osm_type": "relation",
        "osm_id": 2604751,
        "class": "boundary",
        "type": "administrative",
        "display_name": "River, Dover, Kent, England",
        "geojson": {"type": "Point", "coordinates": [1.27, 51.14]},
    }
    fake = _RecordingOsmnx(_features([1]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    monkeypatch.setitem(sys.modules, "requests", _nominatim([parish]))

    result = download_osm_data(
        data_type="waterway=river", feature_name="Riverside Walk", output_filename="x"
    )

    assert "is a waterway=river" in result
    assert "River (boundary=administrative)" in result
    assert "place_name and data_type" in result


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        ("rivers", {"waterway": "river"}),
        ("river", {"waterway": "river"}),
        ("waterway", {"waterway": True}),
        ("railway", {"railway": True}),
        ("cafes", {"amenity": "cafe"}),
        ("gym", {"amenity": "gym"}),
        ("shop=bakery", {"shop": "bakery"}),
    ],
)
def test_a_data_type_resolves_to_a_tag_that_can_match_something(
    monkeypatch, outputs, data_type, expected
):
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)

    download_osm_data(data_type=data_type, place_name=f"{LAT},{LON}", output_filename="x")

    assert fake.point_calls[0]["tags"] == expected
