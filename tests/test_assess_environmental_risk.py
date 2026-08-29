"""Which Nominatim hit anchors an environmental risk assessment."""

from src.agents.tools.assess_environmental_risk import pinned_geocode_hit

# nominatim's answer to q=Amsterdam on 2026-08-29, in its own order. new york
# is in the list through its old name and carries the highest importance
AMSTERDAM_HITS = [
    {"osm_type": "relation", "osm_id": 271110, "importance": 0.788, "lat": "52.3730", "lon": "4.8924", "display_name": "Amsterdam, Noord-Holland, Nederland"},
    {"osm_type": "node", "osm_id": 8583920, "importance": 0.523, "lat": "52.3745", "lon": "4.8979", "display_name": "Amsterdam, Noord-Holland, Nederland"},
    {"osm_type": "relation", "osm_id": 2226734, "importance": 0.512, "lat": "-37.836", "lon": "77.554", "display_name": "Île Amsterdam"},
    {"osm_type": "relation", "osm_id": 174846, "importance": 0.467, "lat": "42.9362", "lon": "-74.1905", "display_name": "City of Amsterdam, New York"},
    {"osm_type": "relation", "osm_id": 175170, "importance": 0.882, "lat": "40.7127", "lon": "-74.0060", "display_name": "New York, United States"},
]


def test_nominatims_first_hit_wins_over_a_more_important_name_match():
    assert pinned_geocode_hit(AMSTERDAM_HITS)["display_name"].startswith("Amsterdam, Noord-Holland")


def test_equally_ranked_hits_pick_the_same_one_whatever_their_order():
    twins = [
        {"osm_type": "node", "osm_id": 20, "importance": 0.5, "lat": "1", "lon": "1"},
        {"osm_type": "node", "osm_id": 10, "importance": 0.5, "lat": "2", "lon": "2"},
        {"osm_type": "node", "osm_id": 30, "importance": 0.4, "lat": "3", "lon": "3"},
    ]

    assert pinned_geocode_hit(twins)["osm_id"] == 10
    assert pinned_geocode_hit(list(reversed(twins[:2])) + twins[2:])["osm_id"] == 10


def test_a_missing_importance_reads_as_zero():
    hits = [{"osm_type": "node", "osm_id": 5, "lat": "1", "lon": "1"}]

    assert pinned_geocode_hit(hits)["osm_id"] == 5
