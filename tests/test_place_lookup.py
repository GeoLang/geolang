import pathlib
import sys
from types import SimpleNamespace

import geopandas as gpd
import pytest

from src.agents.tools.batch_geocode import batch_geocode
from src.agents.tools.get_admin_boundary import get_admin_boundary
from src.core import utils
from src.core.place_lookup import (
    GEOKODE_URL_ENV,
    GeocoderUnavailable,
    geocode,
    geocode_batch,
    osm_geometry,
)
from tests.geokode_fakes import fake_platform, geokode_hit, overpass_way


# a 10 by 10 square whose outer ring is split over two ways, a lake from 2 to 8
# inside it and an island from 4 to 6 inside the lake
LAKE_WITH_ISLAND = {
    "type": "relation",
    "id": 42,
    "tags": {"type": "multipolygon", "natural": "wood"},
    "members": [
        overpass_way("outer", (0, 0), (10, 0), (10, 10)),
        overpass_way("outer", (10, 10), (0, 10), (0, 0)),
        overpass_way("inner", (2, 2), (8, 2), (8, 8)),
        overpass_way("inner", (8, 8), (2, 8), (2, 2)),
        overpass_way("outer", (4, 4), (6, 4), (6, 6), (4, 6), (4, 4)),
        {"type": "node", "ref": 7, "role": "label", "lat": 1.0, "lon": 1.0},
    ],
}

# three ways of one stream end to end, and a side channel touching none of them
RIVER = {
    "type": "relation",
    "id": 2263653,
    "tags": {"type": "waterway", "waterway": "river"},
    "members": [
        overpass_way("main_stream", (-2.03, 51.69), (-1.26, 51.75)),
        overpass_way("main_stream", (-1.26, 51.75), (-0.12, 51.50)),
        overpass_way("main_stream", (-0.12, 51.50), (0.68, 51.52)),
        overpass_way("side_stream", (-1.0, 51.0), (-0.9, 51.1)),
    ],
}


@pytest.fixture
def overpass(monkeypatch, geokode_env):
    def answer(element):
        fake = fake_platform(lambda query: [], overpass_elements=[element])
        monkeypatch.setitem(sys.modules, "requests", fake)
        return fake

    return answer


def test_a_multipolygon_stitches_split_rings_and_keeps_the_island_in_the_lake(overpass):
    fake = overpass(LAKE_WITH_ISLAND)

    shape = osm_geometry("relation", 42)

    assert shape.geom_type == "MultiPolygon"
    assert shape.is_valid
    assert len(shape.geoms) == 2
    assert shape.area == pytest.approx(100 - 36 + 4)
    wood = max(shape.geoms, key=lambda polygon: polygon.area)
    assert len(wood.interiors) == 1
    assert fake.seen.overpass == ["[out:json][timeout:90];relation(42);out geom;"]
    # overpass-api.de refuses the python-requests default with 406
    assert "python-requests" not in fake.seen.overpass_headers[0].get("User-Agent", "python-requests")


def test_a_river_relation_comes_back_as_its_merged_lines(overpass):
    overpass(RIVER)

    shape = osm_geometry("relation", 2263653)

    assert shape.geom_type == "MultiLineString"
    main_stream = max(shape.geoms, key=lambda line: line.length)
    assert len(shape.geoms) == 2
    assert main_stream.coords[0] == (-2.03, 51.69)
    assert main_stream.coords[-1] == (0.68, 51.52)


def test_every_lookup_refuses_without_a_geocoder(monkeypatch):
    monkeypatch.delenv(GEOKODE_URL_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "requests", fake_platform(lambda query: []))

    with pytest.raises(GeocoderUnavailable, match=GEOKODE_URL_ENV):
        geocode("Leicester")
    with pytest.raises(GeocoderUnavailable, match=GEOKODE_URL_ENV):
        geocode_batch(["Leicester"])


def test_an_unreachable_geocoder_says_so(monkeypatch, geokode_env):
    def refuse(url, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=refuse))

    with pytest.raises(GeocoderUnavailable, match="unreachable"):
        geocode("Leicester")


def test_a_long_batch_goes_out_in_hundreds_and_answers_in_order(monkeypatch, geokode_env):
    fake = fake_platform(lambda query: [geokode_hit(float(query), 0.0)])
    posts = []
    post = fake.post

    def counting_post(url, **kwargs):
        posts.append(len(kwargs["json"]["queries"]))
        return post(url, **kwargs)

    fake.post = counting_post
    monkeypatch.setitem(sys.modules, "requests", fake)
    queries = [str(number) for number in range(250)]

    answers = geocode_batch(queries)

    assert posts == [100, 100, 50]
    assert [hits[0]["lat"] for hits in answers] == [float(q) for q in queries]


@pytest.fixture
def outputs(monkeypatch, tmp_path, geokode_env):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")


def _read_output(name):
    return gpd.read_file(pathlib.Path(utils.caller_outputs_dir()) / f"{name}.gpkg")


def test_an_admin_boundary_is_the_outline_of_the_hit_at_the_asked_level(monkeypatch, outputs):
    city = geokode_hit(52.63, -1.13, name="Leicester", kind="boundary", osm_type="relation", osm_id=1, admin_level=8)
    county = geokode_hit(52.66, -1.10, name="Leicestershire", kind="boundary", osm_type="relation", osm_id=2, admin_level=6)
    square = [(-1.2, 52.6), (-1.0, 52.6), (-1.0, 52.7), (-1.2, 52.7), (-1.2, 52.6)]
    relation = {
        "type": "relation",
        "id": 2,
        "tags": {"type": "boundary", "boundary": "administrative"},
        "members": [overpass_way("outer", *square)],
    }
    fake = fake_platform(lambda query: [city, county], overpass_elements=[relation])
    monkeypatch.setitem(sys.modules, "requests", fake)

    result = get_admin_boundary("Leicester", admin_level=6, output_filename="county")

    assert "retrieved" in result, result
    assert fake.seen.overpass == ["[out:json][timeout:90];relation(2);out geom;"]
    row = _read_output("county").iloc[0]
    assert row["name"] == "Leicestershire"
    assert row["admin_level"] == 6
    assert row.geometry.geom_type == "MultiPolygon"


def test_batch_geocode_saves_the_hits_and_names_the_misses(monkeypatch, outputs):
    known = {"Leicester": [geokode_hit(52.63, -1.13, display_name="Leicester, England")]}
    fake = fake_platform(lambda query: known.get(query, []))
    monkeypatch.setitem(sys.modules, "requests", fake)

    result = batch_geocode(addresses="Leicester; Nowhereville", output_filename="points")

    assert "Geocoded 1 of 2" in result, result
    assert "Nowhereville" in result
    row = _read_output("points").iloc[0]
    assert row["geocoded_name"] == "Leicester, England"
    assert (row.geometry.x, row.geometry.y) == (-1.13, 52.63)
