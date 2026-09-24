import sys

import geopandas as gpd
from shapely.geometry import Point

import src.agents.tools.geocode_place as geocode_module
from src.agents.tools.geocode_place import _geokode_answer, geocode_place
from tests.geokode_fakes import fake_platform

JASPER_AVENUE = {
    "kind": "address",
    "match_type": "prefix",
    "lon": -79.48301,
    "lat": 43.6835,
    "address": {"full": "2, Jasper Avenue"},
}
QUEEN_STREET_WEST = {
    "kind": "address",
    "match_type": "exact",
    "lon": -79.38034,
    "lat": 43.65219,
    "address": {"full": "Queen Street West, Toronto"},
}
JASPER_TOWN = {
    "kind": "place",
    "lon": -118.08243,
    "lat": 52.87523,
    "address": {"full": "Jasper, Alberta"},
}


def test_a_place_answers_a_query_with_no_house_number():
    assert _geokode_answer([JASPER_AVENUE, JASPER_TOWN], "Jasper") is JASPER_TOWN


def test_a_house_number_takes_the_best_hit_as_it_stands():
    assert _geokode_answer([JASPER_AVENUE, JASPER_TOWN], "2 Jasper Avenue") is JASPER_AVENUE


def test_only_streets_answer_nothing_for_a_place_query():
    assert _geokode_answer([JASPER_AVENUE], "Jasper") is None


def test_a_whole_street_name_answers_its_own_query():
    answer = _geokode_answer([QUEEN_STREET_WEST], "Queen Street West")
    assert answer is QUEEN_STREET_WEST


def _natural_earth_with_jasper(directory):
    path = directory / "ne_populated_places.shp"
    gpd.GeoDataFrame(
        {"NAME": ["Jasper"], "SOV0NAME": ["Canada"]},
        geometry=[Point(-118.08, 52.88)],
        crs="EPSG:4326",
    ).to_file(path)
    return str(path)


def test_a_street_only_match_falls_through_to_the_place_sources(
    monkeypatch, tmp_path, geokode_env
):
    natural_earth = _natural_earth_with_jasper(tmp_path)
    monkeypatch.setattr(geocode_module, "natural_earth_dataset_paths", lambda _: [natural_earth])
    monkeypatch.setitem(sys.modules, "requests", fake_platform(lambda query: [JASPER_AVENUE]))

    answer = geocode_place("Jasper")

    assert "Jasper, Canada" in answer
    assert "Jasper Avenue" not in answer


def test_the_street_still_answers_when_no_place_source_can(monkeypatch, geokode_env):
    monkeypatch.setattr(geocode_module, "natural_earth_dataset_paths", lambda _: [])
    monkeypatch.setitem(sys.modules, "requests", fake_platform(lambda query: [JASPER_AVENUE]))

    assert "Jasper Avenue" in geocode_place("Jasper")
