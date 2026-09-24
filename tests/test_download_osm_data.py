"""download_osm_data: whole-feature fetch, sub-query dedupe, search radius."""

import pathlib
import sys
import warnings

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from src.agents.tools.download_osm_data import download_osm_data
from src.core import utils
from tests.geokode_fakes import fake_platform, geokode_hit, overpass_way

LAT, LON = 51.5, -0.12
DISTRICT_ID = 51805

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
        self.polygon_calls = []

    def features_from_polygon(self, polygon, tags=None):
        self.polygon_calls.append((polygon, tags))
        if self.warn:
            warnings.warn(SUBDIVIDE_WARNING, UserWarning, stacklevel=2)
        return self.frame

    def features_from_point(self, point, tags=None, dist=None):
        self.point_calls.append({"point": point, "tags": tags, "dist": dist})
        return self.frame


class _RoadsOsmnx:
    def __init__(self, frame):
        self.frame = frame
        self.graph_calls = []

    def graph_from_polygon(self, polygon, network_type=None):
        self.graph_calls.append((polygon, network_type))
        return "graph"

    def graph_to_gdfs(self, G, nodes=False):
        return self.frame


def _district(degrees):
    corners = [
        (LON, LAT),
        (LON + degrees, LAT),
        (LON + degrees, LAT + degrees),
        (LON, LAT + degrees),
        (LON, LAT),
    ]
    hit = geokode_hit(
        LAT + degrees / 2,
        LON + degrees / 2,
        kind="boundary",
        osm_type="relation",
        osm_id=DISTRICT_ID,
        osm_key="boundary",
        osm_value="administrative",
        bbox=[LON, LAT, LON + degrees, LAT + degrees],
    )
    relation = {
        "type": "relation",
        "id": DISTRICT_ID,
        "tags": {"type": "boundary", "boundary": "administrative"},
        "members": [overpass_way("outer", *corners)],
    }
    return hit, relation, box(LON, LAT, LON + degrees, LAT + degrees)


def _platform(monkeypatch, hits, overpass_element=None):
    fake = fake_platform(
        lambda query: hits,
        overpass_elements=[overpass_element] if overpass_element else [],
    )
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


@pytest.fixture
def outputs(monkeypatch, tmp_path, geokode_env):
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
    hit, relation, _ = _district(0.05)
    _platform(monkeypatch, [hit], relation)

    result = download_osm_data(
        data_type="waterway=river", place_name="South East England", output_filename="rivers"
    )

    assert len(_read_output("rivers")) == 3
    assert "Downloaded 3 waterway=river features" in result
    assert "Dropped 3 duplicate features" in result


def test_a_subdivided_query_says_so_instead_of_going_quiet(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]), warn=True)
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    hit, relation, _ = _district(0.05)
    _platform(monkeypatch, [hit], relation)

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
    river = geokode_hit(
        51.5,
        -0.12,
        name="River Thames",
        display_name="River Thames, England, United Kingdom",
        osm_type="relation",
        osm_id=2263653,
        osm_key="waterway",
        osm_value="river",
    )
    # two stretches end to end, and one the relation carries apart from them
    whole_river = {
        "type": "relation",
        "id": 2263653,
        "tags": {"type": "waterway", "waterway": "river"},
        "members": [
            overpass_way("main_stream", (-2.03, 51.69), (-1.26, 51.75)),
            overpass_way("main_stream", (-1.26, 51.75), (-0.12, 51.50)),
            overpass_way("side_stream", (0.40, 51.40), (0.68, 51.52)),
        ],
    }
    fake = _RecordingOsmnx(_features([1]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    platform = _platform(monkeypatch, [river], whole_river)

    result = download_osm_data(
        data_type="waterway=river",
        feature_name="River Thames",
        output_filename="thames",
    )

    written = _read_output("thames")
    assert len(written) == 2, "each separate stretch should survive as a feature"
    assert fake.polygon_calls == [], "a named feature must not fall back to an area search"
    assert platform.seen.overpass == ["[out:json][timeout:90];relation(2263653);out geom;"]
    # the full span, not the piece inside any single place
    assert written.total_bounds[0] == pytest.approx(-2.03)
    assert written.total_bounds[2] == pytest.approx(0.68)
    assert "relation 2263653" in result
    assert "Total length:" in result


def test_a_named_feature_prefers_the_hit_matching_the_requested_tag(monkeypatch, outputs):
    pub = geokode_hit(
        51.50,
        -0.12,
        display_name="The River Thames, London",
        kind="poi",
        osm_type="node",
        osm_id=1,
        osm_key="amenity",
        osm_value="pub",
    )
    river = geokode_hit(
        51.6,
        -1.0,
        display_name="River Thames, England",
        osm_type="relation",
        osm_id=2263653,
        osm_key="waterway",
        osm_value="river",
    )
    relation = {
        "type": "relation",
        "id": 2263653,
        "tags": {"type": "waterway", "waterway": "river"},
        "members": [overpass_way("main_stream", (-2.03, 51.69), (0.68, 51.52))],
    }
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx(_features([1])))
    # the pub ranks first, so only the tag match keeps this off it
    platform = _platform(monkeypatch, [pub, river], relation)

    result = download_osm_data(
        data_type="waterway=river", feature_name="River Thames", output_filename="thames"
    )

    assert "relation 2263653" in result
    assert platform.seen.overpass == ["[out:json][timeout:90];relation(2263653);out geom;"]
    assert _read_output("thames").geometry.iloc[0].geom_type == "LineString"


def test_roads_for_an_oversized_place_are_refused(monkeypatch, outputs):
    # a 1 degree square, thousands of km2
    fake = _RoadsOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    hit, relation, _ = _district(1.0)
    platform = _platform(monkeypatch, [hit], relation)

    result = download_osm_data(
        data_type="roads", place_name="London", output_filename="roads"
    )

    assert "capped at 50 km2" in result
    assert fake.graph_calls == [], "the graph download must not start"
    assert platform.seen.overpass == [], "the outline must not be fetched either"


def test_roads_for_a_district_sized_place_download_inside_its_outline(monkeypatch, outputs):
    # about 20 km2, under the cap
    fake = _RoadsOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    hit, relation, square = _district(0.05)
    platform = _platform(monkeypatch, [hit], relation)

    result = download_osm_data(
        data_type="roads", place_name="Camden", output_filename="roads"
    )

    assert platform.seen.overpass == [f"[out:json][timeout:90];relation({DISTRICT_ID});out geom;"]
    [(outline, network_type)] = fake.graph_calls
    assert network_type == "all"
    assert outline.equals(square)
    assert len(_read_output("roads")) == 2
    assert "Downloaded 2 roads features" in result


def test_buildings_for_an_oversized_place_are_refused(monkeypatch, outputs):
    # the City of Toronto, whose buildings killed the 4 GiB executor
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    hit, relation, _ = _district(1.0)
    _platform(monkeypatch, [hit], relation)

    result = download_osm_data(
        data_type="buildings", place_name="Toronto", output_filename="buildings"
    )

    assert "capped at 50 km2" in result
    assert "radius_m" in result
    assert fake.polygon_calls == []


def test_a_place_without_an_outline_searches_around_its_point(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    address = geokode_hit(LAT, LON, kind="address", osm_type="node", osm_id=9)
    platform = _platform(monkeypatch, [address])

    download_osm_data(
        data_type="cafes", place_name="221B Baker Street, London", output_filename="x"
    )

    assert platform.seen.overpass == []
    assert fake.polygon_calls == []
    assert fake.point_calls[0]["point"] == (LAT, LON)
    assert fake.point_calls[0]["dist"] == 2000


def test_a_place_the_geocoder_does_not_know_is_a_miss(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    _platform(monkeypatch, [])

    result = download_osm_data(data_type="cafes", place_name="Nowhereville")

    assert "found no place named 'Nowhereville'" in result
    assert fake.point_calls == [] and fake.polygon_calls == []


def test_a_radius_with_a_place_name_searches_around_its_centre(monkeypatch, outputs):
    fake = _RecordingOsmnx(_features([1, 2]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    _platform(monkeypatch, [geokode_hit(LAT, LON)])

    download_osm_data(
        data_type="buildings", place_name="Toronto", radius_m=2000, output_filename="x"
    )

    assert fake.polygon_calls == [], "a radius must skip the place boundary"
    assert fake.point_calls[0]["point"] == (LAT, LON)
    assert fake.point_calls[0]["dist"] == 2000


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
    parish = geokode_hit(
        51.14,
        1.27,
        name="River",
        display_name="River, Dover, Kent, England",
        kind="boundary",
        osm_type="relation",
        osm_id=2604751,
        osm_key="boundary",
        osm_value="administrative",
    )
    fake = _RecordingOsmnx(_features([1]))
    monkeypatch.setitem(sys.modules, "osmnx", fake)
    platform = _platform(monkeypatch, [parish])

    result = download_osm_data(
        data_type="waterway=river", feature_name="Riverside Walk", output_filename="x"
    )

    assert "is a waterway=river" in result
    assert "River (boundary=administrative)" in result
    assert "place_name and data_type" in result
    assert platform.seen.overpass == [], "a refused hit must not be fetched"


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
