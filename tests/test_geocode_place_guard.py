from types import SimpleNamespace

import src.agents.tools.geocode_place as geocode_module
from src.agents.tools.geocode_place import _geokode_answer, geocode_place

JASPER_AVENUE = {
    "kind": "address",
    "lon": -79.48301,
    "lat": 43.6835,
    "address": {"full": "2, Jasper Avenue"},
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


def fake_geokode(results):
    def get(url, **kwargs):
        if "nominatim" in url:
            return SimpleNamespace(
                ok=True,
                json=lambda: [
                    {
                        "lon": "-118.08243",
                        "lat": "52.87523",
                        "display_name": "Municipality of Jasper, Alberta, Canada",
                    }
                ],
            )
        return SimpleNamespace(ok=True, json=lambda: {"results": results})

    return get


def test_a_street_only_match_falls_through_to_the_place_sources(monkeypatch):
    monkeypatch.setenv("GEOKODE_URL", "http://geokode:3000")
    monkeypatch.setattr(geocode_module, "natural_earth_dataset_paths", lambda _: [])
    import requests

    monkeypatch.setattr(requests, "get", fake_geokode([JASPER_AVENUE]))

    answer = geocode_place("Jasper")

    assert "nominatim" in answer
    assert "52.87523" in answer
    assert "Jasper Avenue" not in answer


def test_the_street_still_answers_when_no_place_source_can(monkeypatch):
    monkeypatch.setenv("GEOKODE_URL", "http://geokode:3000")
    monkeypatch.setattr(geocode_module, "natural_earth_dataset_paths", lambda _: [])
    import requests

    def get(url, **kwargs):
        if "nominatim" in url:
            return SimpleNamespace(ok=True, json=lambda: [])
        return SimpleNamespace(ok=True, json=lambda: {"results": [JASPER_AVENUE]})

    monkeypatch.setattr(requests, "get", get)

    assert "Jasper Avenue" in geocode_place("Jasper")
