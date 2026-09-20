"""trade_area and the modules it shares with the tools it was built from."""

import pathlib
import sys
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon, box
from shapely.geometry import mapping as shapely_mapping

from src.agents.tools._osm_tags import merge_tags, tags_for_category
from src.agents.tools._population import population_inside
from src.agents.tools._sites import resolve_sites
from src.agents.tools.score_sites import score_sites
from src.agents.tools.trade_area import trade_area
from src.core import utils
from tests.test_tools import _cells_inside, _write_pop_raster

LAT, LON = 52.6369, -1.1398  # Leicester

BAND_MINUTES = 10
# the fake Valhalla answers a contour of N minutes with this many degrees of reach
DEGREES_PER_MINUTE = 0.001

SITE_A = (LAT, LON)
SITE_B = (LAT + 0.05, LON + 0.05)


def _circle_band(lon, lat, minutes):
    return Point(lon, lat).buffer(minutes * DEGREES_PER_MINUTE)


def _square_band(lon, lat, minutes):
    reach = minutes * DEGREES_PER_MINUTE
    return box(lon - reach, lat - reach, lon + reach, lat + reach)


def _fake_valhalla(posts, band=_circle_band):
    def post(url, json=None, timeout=None):
        posts.append(json)
        location = json["locations"][0]
        features = [
            {
                "type": "Feature",
                "properties": {"contour": float(contour["time"])},
                "geometry": shapely_mapping(
                    band(location["lon"], location["lat"], contour["time"])
                ),
            }
            for contour in sorted(json["contours"], key=lambda c: c["time"])
        ]
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"type": "FeatureCollection", "features": features},
        )

    def get(url, params=None, headers=None, timeout=None):
        raise AssertionError(f"no HTTP GET expected, got {url}")

    return SimpleNamespace(post=post, get=get)


def _fake_opentopodata(gets):
    """Elevation rises with position in the batch, so a shifted list shows up."""

    def get(url, params=None, headers=None, timeout=None):
        gets.append(url)
        locations = url.split("locations=")[1].split("|")
        results = [{"elevation": 10.0 * (i + 1)} for i in range(len(locations))]
        return SimpleNamespace(
            status_code=200, json=lambda: {"status": "OK", "results": results}
        )

    return SimpleNamespace(get=get)


class _RecordingOsmnx:
    """Records every query and answers with the feature frame it was given."""

    def __init__(self, features=None):
        self.features = features
        self.polygon_calls = []
        self.point_calls = []
        self.geocode_calls = []
        self.settings = SimpleNamespace(timeout=180, overpass_rate_limit=True)

    def geocode(self, place_name):
        self.geocode_calls.append(place_name)
        return LAT, LON

    def features_from_polygon(self, polygon, tags=None):
        self.polygon_calls.append({"polygon": polygon, "tags": tags})
        if self.features is None:
            raise RuntimeError("no OSM in tests")
        return self.features

    def features_from_point(self, point, tags=None, dist=None):
        self.point_calls.append({"point": point, "tags": tags, "dist": dist})
        if self.features is None:
            raise RuntimeError("no OSM in tests")
        return self.features


@pytest.fixture
def stub_services(monkeypatch, tmp_path):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    return tmp_path


def _write_layer(frame, name):
    path = pathlib.Path(utils.caller_outputs_dir()) / f"{name}.gpkg"
    frame.to_file(path, driver="GPKG")
    return f"{name}.gpkg"


def _read_output(name):
    path = pathlib.Path(utils.caller_outputs_dir()) / f"{name}.gpkg"
    assert path.exists(), f"{path} not written"
    return gpd.read_file(path)


def _points(records):
    return gpd.GeoDataFrame(
        [{k: v for k, v in record.items() if k != "at"} for record in records],
        geometry=[Point(record["at"]) for record in records],
        crs="EPSG:4326",
    )


def _two_site_layer():
    return _points(
        [
            {"name": "town centre", "at": (SITE_A[1], SITE_A[0])},
            {"name": "north depot", "at": (SITE_B[1], SITE_B[0])},
        ]
    )


# two cafes and three supermarkets inside the town centre band, one cafe inside
# the north depot band, one cafe between the two bands and inside neither
OSM_FEATURES = _points(
    [
        {"amenity": "cafe", "shop": None, "at": (LON + 0.002, LAT)},
        {"amenity": "cafe", "shop": None, "at": (LON, LAT + 0.003)},
        {"amenity": "cafe", "shop": None, "at": (LON + 0.05, LAT + 0.05)},
        {"amenity": "cafe", "shop": None, "at": (LON + 0.025, LAT + 0.025)},
        {"amenity": None, "shop": "supermarket", "at": (LON - 0.002, LAT)},
        {"amenity": None, "shop": "supermarket", "at": (LON, LAT - 0.003)},
        {"amenity": None, "shop": "supermarket", "at": (LON + 0.004, LAT + 0.004)},
    ]
)


def test_trade_area_asks_each_service_once_and_counts_per_band(
    monkeypatch, stub_services
):
    osmnx = _RecordingOsmnx(OSM_FEATURES)
    monkeypatch.setitem(sys.modules, "osmnx", osmnx)
    posts = []
    monkeypatch.setitem(sys.modules, "requests", _fake_valhalla(posts))
    transform = _write_pop_raster(stub_services / "ghsl_pop.tif")
    sites_layer = _write_layer(_two_site_layer(), "candidates")

    out = trade_area(
        sites_path=sites_layer,
        travel_mode="driving",
        minutes=BAND_MINUTES,
        competitors="cafes",
        anchors="supermarkets",
        output_filename="bands",
    )
    assert "Trade areas for 2 sites" in out, out

    # one Valhalla request per site, one contour each
    assert len(posts) == 2
    assert [len(post["contours"]) for post in posts] == [1, 1]
    assert [post["contours"][0]["time"] for post in posts] == [BAND_MINUTES] * 2

    # one Overpass query covers both bands, carrying every tag either needs
    assert len(osmnx.polygon_calls) == 1
    assert osmnx.polygon_calls[0]["tags"] == {
        "shop": "supermarket",
        "amenity": "cafe",
    }
    assert osmnx.geocode_calls == []

    rows = _read_output("bands").set_index("site")
    assert sorted(rows.index) == ["north depot", "town centre"]
    assert rows.loc["town centre", "competitors"] == 2
    assert rows.loc["north depot", "competitors"] == 1
    assert rows.loc["town centre", "anchor_supermarkets"] == 3
    assert rows.loc["north depot", "anchor_supermarkets"] == 0

    for site, row in rows.iterrows():
        expected = _cells_inside(transform, row.geometry)
        assert expected > 0
        assert row["population"] == pytest.approx(expected, rel=0.01), site
        assert "GHS-POP" in row["population_source"]
        assert row["minutes"] == BAND_MINUTES
        assert row["mode"] == "driving"
        assert row["area_km2"] > 0


def test_trade_area_weights_demographics_by_the_share_inside_the_band(
    monkeypatch, stub_services
):
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx())
    posts = []
    monkeypatch.setitem(
        sys.modules, "requests", _fake_valhalla(posts, band=_square_band)
    )

    reach = BAND_MINUTES * DEGREES_PER_MINUTE
    fully_inside = box(LON - 0.008, LAT - 0.008, LON - 0.004, LAT - 0.004)
    # the same size, straddling the eastern edge of the band with half its width in
    half_inside = box(LON + reach - 0.002, LAT - 0.002, LON + reach + 0.002, LAT + 0.002)
    census = gpd.GeoDataFrame(
        [
            {"population": 1000.0, "income": 50000.0},
            {"population": 400.0, "income": 30000.0},
        ],
        geometry=[fully_inside, half_inside],
        crs="EPSG:4326",
    )
    census_layer = _write_layer(census, "census")
    sites_layer = _write_layer(
        _points([{"name": "town centre", "at": (LON, LAT)}]), "one_site"
    )

    out = trade_area(
        sites_path=sites_layer,
        travel_mode="driving",
        minutes=BAND_MINUTES,
        anchors="",
        demographics_path=census_layer,
        demographics_columns="population:sum,income:mean",
        output_filename="census_bands",
    )
    assert "Trade areas for 1 site" in out, out

    row = _read_output("census_bands").iloc[0]
    # 1000 whole plus half of 400, not the 1400 an unweighted sum would give
    assert row["population"] == pytest.approx(1200.0, rel=0.01)
    # weighted by the area inside: (50000 * 1 + 30000 * 0.5) / 1.5
    assert row["income"] == pytest.approx(43333.33, rel=0.01)


def test_trade_area_refuses_both_sites_arguments_and_neither(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx())

    both = trade_area(sites="Leicester", sites_path="candidates.gpkg")
    neither = trade_area()

    for answer in (both, neither):
        assert "Give either sites" in answer, answer
        assert "Traceback" not in answer


def test_trade_area_counts_a_competitor_layer_without_asking_overpass(
    monkeypatch, stub_services
):
    osmnx = _RecordingOsmnx(OSM_FEATURES)
    monkeypatch.setitem(sys.modules, "osmnx", osmnx)
    posts = []
    monkeypatch.setitem(sys.modules, "requests", _fake_valhalla(posts))

    rivals = _points(
        [
            {"name": "rival one", "at": (LON + 0.002, LAT)},
            {"name": "rival two", "at": (LON, LAT + 0.003)},
            {"name": "rival three", "at": (LON + 0.05, LAT + 0.05)},
            {"name": "far rival", "at": (LON + 0.025, LAT + 0.025)},
        ]
    )
    rivals_layer = _write_layer(rivals, "rivals")
    sites_layer = _write_layer(_two_site_layer(), "candidates")

    out = trade_area(
        sites_path=sites_layer,
        travel_mode="driving",
        minutes=BAND_MINUTES,
        competitors=rivals_layer,
        anchors="",
        output_filename="rival_bands",
    )
    assert "Trade areas for 2 sites" in out, out
    assert osmnx.polygon_calls == []

    rows = _read_output("rival_bands").set_index("site")
    assert rows.loc["town centre", "competitors"] == 2
    assert rows.loc["north depot", "competitors"] == 1


def test_trade_area_refuses_a_competitor_string_that_is_neither(
    monkeypatch, stub_services
):
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx())

    out = trade_area(sites="0.0, 0.0", competitors="rivals.gpkg")

    assert "neither a file" in out, out
    assert "Traceback" not in out


def test_score_sites_reads_a_layer_without_geocoding_it(monkeypatch, stub_services):
    osmnx = _RecordingOsmnx()
    monkeypatch.setitem(sys.modules, "osmnx", osmnx)
    gets = []
    monkeypatch.setitem(sys.modules, "requests", _fake_opentopodata(gets))

    sites_layer = _write_layer(_two_site_layer(), "candidates")
    from_layer = score_sites(
        sites_path=sites_layer, criteria="flood_risk", output_filename="from_layer"
    )
    from_names = score_sites(
        sites=f"{SITE_A[0]}, {SITE_A[1]}; {SITE_B[0]}, {SITE_B[1]}",
        criteria="flood_risk",
        output_filename="from_names",
    )
    assert "Site scoring results" in from_layer, from_layer
    assert "Site scoring results" in from_names, from_names
    assert osmnx.geocode_calls == []

    compared = ["lat", "lon", "rank", "total_score", "flood_risk_raw"]
    layer_rows = _read_output("from_layer").sort_values("rank")[compared]
    name_rows = _read_output("from_names").sort_values("rank")[compared]
    assert layer_rows.values.tolist() == name_rows.values.tolist()


def test_score_sites_counts_a_competitor_layer_within_a_kilometre(
    monkeypatch, stub_services
):
    osmnx = _RecordingOsmnx()
    monkeypatch.setitem(sys.modules, "osmnx", osmnx)

    # two inside a kilometre of the town centre, one 5km away from both sites
    rivals = _points(
        [
            {"name": "rival one", "at": (LON + 0.002, LAT)},
            {"name": "rival two", "at": (LON, LAT + 0.003)},
            {"name": "distant rival", "at": (LON + 0.08, LAT + 0.08)},
        ]
    )
    rivals_layer = _write_layer(rivals, "rivals")
    sites_layer = _write_layer(_two_site_layer(), "candidates")

    out = score_sites(
        sites_path=sites_layer,
        criteria="competition",
        competitors_path=rivals_layer,
        output_filename="rival_scores",
    )
    assert "Site scoring results" in out, out
    # the competition tags never reached Overpass
    assert osmnx.point_calls == []

    rows = _read_output("rival_scores").set_index("name")
    assert rows.loc["town centre", "competition_raw"] == 2.0
    assert rows.loc["north depot", "competition_raw"] == 0.0
    # fewer competitors ranks higher
    assert rows.loc["north depot", "rank"] == 1


def test_score_sites_refuses_both_competitor_arguments(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "osmnx", _RecordingOsmnx())

    out = score_sites(
        sites="0.0, 0.0; 1.0, 1.0",
        criteria="competition",
        competition_type="cafes",
        competitors_path="rivals.gpkg",
    )

    assert "not both" in out, out


def test_sites_from_a_polygon_layer_take_the_centroid_and_a_numbered_name(
    stub_services,
):
    parcels = gpd.GeoDataFrame(
        {"parcel_reference": ["AB-1", "AB-2"]},
        geometry=[
            Polygon([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]),
            Polygon([(10.0, 10.0), (12.0, 10.0), (12.0, 12.0), (10.0, 12.0)]),
        ],
        crs="EPSG:4326",
    )
    layer = _write_layer(parcels, "parcels")

    resolved = resolve_sites(None, layer, None)

    assert resolved == [
        {"name": "site 1", "lat": 1.0, "lon": 1.0},
        {"name": "site 2", "lat": 11.0, "lon": 11.0},
    ]

    named = resolve_sites(None, layer, "parcel_reference")
    assert [site["name"] for site in named] == ["AB-1", "AB-2"]

    absent = resolve_sites(None, layer, "owner")
    assert "no column 'owner'" in absent


def test_tags_for_category_reads_key_value_a_bare_key_and_a_bare_value():
    assert tags_for_category("shop=bakery") == {"shop": "bakery"}
    assert tags_for_category("supermarkets") == {"shop": "supermarket"}
    # a bare key means everything carrying it, not an amenity named after it
    assert tags_for_category("waterway") == {"waterway": True}
    assert tags_for_category("gym") == {"amenity": "gym"}

    merged = merge_tags(
        [{"amenity": ["cafe", "bar"]}, {"amenity": "cafe"}, {"shop": "supermarket"}]
    )
    assert merged == {"amenity": ["cafe", "bar"], "shop": "supermarket"}
    # a key wanted whole wins over one wanted by value, the query has to cover both
    assert merge_tags([{"shop": "supermarket"}, {"shop": True}]) == {"shop": True}


def test_population_inside_says_unavailable_when_no_source_answers(
    monkeypatch, stub_services
):
    def failing_get(url, params=None, headers=None, timeout=None):
        raise ConnectionError("worldpop is down")

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=failing_get))

    count, source = population_inside(box(LON - 0.01, LAT - 0.01, LON + 0.01, LAT + 0.01))

    assert count is None
    assert source == "unavailable"
