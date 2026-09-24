"""Which geokode hit anchors an environmental risk assessment."""

from src.agents.tools.assess_environmental_risk import pinned_geocode_hit
from tests.geokode_fakes import geokode_hit

AMSTERDAM_HITS = [
    geokode_hit(52.3730, 4.8924, osm_type="relation", osm_id=271110, confidence=0.97, display_name="Amsterdam, Noord-Holland, Nederland"),
    geokode_hit(-37.836, 77.554, osm_type="relation", osm_id=2226734, confidence=0.81, display_name="Île Amsterdam"),
    geokode_hit(42.9362, -74.1905, osm_type="relation", osm_id=174846, confidence=0.74, display_name="City of Amsterdam, New York"),
]


def test_the_first_hit_wins_over_the_ones_ranked_below_it():
    assert pinned_geocode_hit(AMSTERDAM_HITS)["display_name"].startswith("Amsterdam, Noord-Holland")


def test_equally_ranked_hits_pick_the_same_one_whatever_their_order():
    twins = [
        geokode_hit(1.0, 1.0, osm_type="node", osm_id=20, confidence=0.5),
        geokode_hit(2.0, 2.0, osm_type="node", osm_id=10, confidence=0.5),
        geokode_hit(3.0, 3.0, osm_type="node", osm_id=30, confidence=0.4),
    ]

    assert pinned_geocode_hit(twins)["osm_id"] == 10
    assert pinned_geocode_hit(list(reversed(twins[:2])) + twins[2:])["osm_id"] == 10
