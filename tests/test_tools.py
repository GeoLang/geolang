# Tests for tools
"""Area tools must render the analysed AREA, not a summary point over it."""

import json
import pathlib
import sys
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import Point, box
from shapely.geometry import mapping as shapely_mapping

from src.agents.tools.assess_environmental_risk import (
    assess_environmental_risk,
    flood_score_from,
)
from src.agents.tools.calculate_isochrones import calculate_isochrones
from src.agents.tools.download_population_grid import download_population_grid
from src.agents.tools.score_sites import score_sites
from src.core import utils

LAT, LON = 52.6369, -1.1398  # Leicester

# two equally-ranked Nominatim hits, so only the tie-break makes the pick stable
HITS = [
    {"lat": LAT, "lon": LON, "importance": 0.75, "osm_type": "relation", "osm_id": 100},
    {
        "lat": LAT + 0.5,
        "lon": LON + 0.5,
        "importance": 0.75,
        "osm_type": "relation",
        "osm_id": 200,
    },
]

# canned WorldPop task payload: wpgpas answers per age class and sex, never a total
PYRAMID = [
    {"class": "0", "age": "0 to 1", "male": 2158.31, "female": 2057.88},
    {"class": "1", "age": "1 to 5", "male": 8665.65, "female": 8255.97},
    {"class": "5", "age": "5 to 10", "male": 11046.78, "female": 10611.40},
]
PYRAMID_TOTAL = 42796  # 2158.31 + 2057.88 + 8665.65 + 8255.97 + 11046.78 + 10611.40


class _FakeOsmnx:
    """Geocoder that answers, OSM feature queries that don't (the tools degrade)."""

    def geocode(self, place_name):
        return LAT, LON

    def features_from_point(self, *args, **kwargs):
        raise RuntimeError("no OSM in tests")


class _DriftingOsmnx(_FakeOsmnx):
    """Geocoder whose answer moves between calls, as Nominatim's top hit can."""

    def __init__(self):
        self.calls = 0

    def geocode(self, place_name):
        self.calls += 1
        return LAT + 0.5 * self.calls, LON


def _fake_requests(payload, status=200, geocode_hits=None):
    hits = HITS if geocode_hits is None else geocode_hits

    def get(url, params=None, headers=None, timeout=None):
        if "nominatim" in url:
            return SimpleNamespace(status_code=200, json=lambda: hits)
        return SimpleNamespace(status_code=status, json=lambda: payload)

    return SimpleNamespace(get=get)


def _fake_worldpop(pyramid=None, pending_polls=0, submits=None):
    """WorldPop stand-in: async submit, then a finished age-sex pyramid."""
    polls = {"n": 0}

    def get(url, params=None, headers=None, timeout=None):
        if "/v1/tasks/" in url:
            polls["n"] += 1
            if polls["n"] <= pending_polls:
                return SimpleNamespace(status_code=200, json=lambda: {"status": "created"})
            body = {
                "status": "finished",
                "data": {"agesexpyramid": PYRAMID if pyramid is None else pyramid},
            }
            return SimpleNamespace(status_code=200, json=lambda: body)
        if "worldpop" in url:
            if submits is not None:
                submits.append(params)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "created", "taskid": "task-1"},
            )
        raise AssertionError(f"unexpected request: {url}")

    return SimpleNamespace(get=get)


class _SettingsOsmnx(_FakeOsmnx):
    """osmnx carrying the settings object score_sites writes its timeout to."""

    def __init__(self):
        self.settings = SimpleNamespace(timeout=180, overpass_rate_limit=True)


def _fake_valhalla(posts):
    """One request answers every contour, ascending, which is not the asked order."""

    def post(url, json=None, timeout=None):
        posts.append(json)
        features = [
            {
                "type": "Feature",
                "properties": {"contour": float(contour["time"])},
                "geometry": shapely_mapping(
                    Point(LON, LAT).buffer(contour["time"] / 1000.0)
                ),
            }
            for contour in sorted(json["contours"], key=lambda c: c["time"])
        ]
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"type": "FeatureCollection", "features": features},
        )

    return SimpleNamespace(post=post)


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


def _no_http():
    def get(url, params=None, headers=None, timeout=None):
        raise AssertionError(f"no HTTP expected, got {url}")

    return SimpleNamespace(get=get)


@pytest.fixture
def stub_services(monkeypatch, tmp_path):
    """Stub the HTTP boundaries and point tool output at tmp_path."""
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    # the tree dirs are read once at import, so the env var alone misses them
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    monkeypatch.setitem(sys.modules, "osmnx", _FakeOsmnx())
    return tmp_path


def _read_output(name):
    path = pathlib.Path(utils.caller_outputs_dir()) / f"{name}.gpkg"
    assert path.exists(), f"{path} not written"
    return gpd.read_file(path)


def _circle(radius_m):
    return gpd.GeoDataFrame(
        geometry=gpd.GeoSeries(gpd.points_from_xy([LON], [LAT]), crs="EPSG:4326")
        .to_crs("EPSG:32630")
        .buffer(radius_m)
        .to_crs("EPSG:4326"),
        crs="EPSG:4326",
    )


def _fake_requests_elev_by_coord(geocode_hits):
    """Elevation mirrors the sampled latitude, so a moved anchor moves the scores."""

    def get(url, params=None, headers=None, timeout=None):
        if "nominatim" in url:
            return SimpleNamespace(status_code=200, json=lambda: geocode_hits)
        locs = url.split("locations=")[1].split("|")
        results = [
            {"elevation": round((float(p.split(",")[0]) - 52.0) * 100, 3)} for p in locs
        ]
        return SimpleNamespace(
            status_code=200, json=lambda: {"status": "OK", "results": results}
        )

    return SimpleNamespace(get=get)


def _write_pop_raster(path, value=10.0, size=200, res=0.002):
    """GHS-POP stand-in centred on Leicester: every cell holds `value` people."""
    transform = rasterio.transform.from_origin(
        LON - size * res / 2, LAT + size * res / 2, res, res
    )
    with rasterio.open(
        str(path),
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float64",
        crs="EPSG:4326",
        transform=transform,
        nodata=-200.0,
    ) as dst:
        dst.write(np.full((size, size), value), 1)
    return transform


def _cells_inside(transform, geom, value=10.0, size=200):
    """Independent zonal sum: every cell centre falling inside the polygon."""
    total = 0.0
    for row in range(size):
        for col in range(size):
            x, y = transform * (col + 0.5, row + 0.5)
            if geom.contains(Point(x, y)):
                total += value
    return total


# San Francisco shape: 20% waterfront at 2m, a little at 8m, the rest hills.
# Mean is 45m, which the old mean-only score called LOW.
SF_ELEVATIONS = [2.0] * 20 + [8.0] * 5 + [60.0] * 75


def test_flood_score_catches_low_lying_waterfront_under_a_high_mean():
    assert np.mean(SF_ELEVATIONS) > 20  # a mean-based score would say LOW
    score, label = flood_score_from(SF_ELEVATIONS, water_dist_m=120)
    assert score >= 8
    assert label == "VERY HIGH"


def test_flood_score_is_very_low_on_an_inland_plateau():
    score, label = flood_score_from([120.0] * 50, water_dist_m=None)
    assert score == 1
    assert label == "VERY LOW"


def test_flood_score_is_very_high_on_a_floodplain():
    score, label = flood_score_from([2.0] * 50, water_dist_m=None)
    assert score >= 8
    assert label == "VERY HIGH"


def test_flood_score_stays_low_when_water_is_near_but_ground_is_high():
    score, label = flood_score_from([40.0] * 50, water_dist_m=50)
    assert score == 1
    assert label == "VERY LOW"


def test_flood_score_falls_back_to_elevation_only_without_water_distance():
    elevation_only = flood_score_from(SF_ELEVATIONS, water_dist_m=None)
    assert elevation_only == (7, "HIGH")
    # far water is not an amplifier, so it scores the same as no water data
    assert flood_score_from(SF_ELEVATIONS, water_dist_m=5000) == elevation_only
    # near water is worse than either signal alone
    assert flood_score_from(SF_ELEVATIONS, water_dist_m=120)[0] > elevation_only[0]


def test_flood_score_is_unknown_without_elevations():
    assert flood_score_from([], water_dist_m=100) == (None, "UNKNOWN")


def test_env_risk_renders_the_buffer_polygon(monkeypatch, stub_services):
    elevations = {"status": "OK", "results": [{"elevation": 8.0}] * 100}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(elevations))

    out = assess_environmental_risk("Leicester", radius_km=2.0, output_filename="risk")
    assert "OVERALL:" in out, out

    gdf = _read_output("risk")
    assert len(gdf) == 1
    assert gdf.geometry.iloc[0].geom_type == "Polygon"

    # the polygon is the assessment area, not a degenerate point
    minx, miny, maxx, maxy = gdf.total_bounds
    assert maxx > minx and maxy > miny
    # 2km radius in true metres: UTM area within 2% of pi*r^2
    assert gdf["area_km2"].iloc[0] == pytest.approx(12.57, rel=0.02)

    # scores survive on the polygon, centroid survives as properties
    row = gdf.iloc[0]
    assert row["flood_score"] == 7  # every sample below 10m -> HIGH
    assert row["overall_risk"] > 0
    assert row["overall_label"].endswith("RISK")
    assert row["radius_km"] == 2.0
    assert row["center_lon"] == pytest.approx(LON, abs=1e-4)
    assert row["center_lat"] == pytest.approx(LAT, abs=1e-4)


def test_env_risk_uses_a_supplied_polygon_as_the_area(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "requests", _fake_requests({"status": "OK"}))

    iso_path = pathlib.Path(utils.caller_outputs_dir()) / "iso.gpkg"
    _circle(5000).to_file(iso_path, driver="GPKG")

    assess_environmental_risk(
        "Leicester", radius_km=2.0, polygon_path="iso.gpkg", output_filename="risk_iso"
    )

    gdf = _read_output("risk_iso")
    assert gdf.geometry.iloc[0].geom_type == "Polygon"
    # the isochrone won, not the 2km buffer
    assert gdf["area_km2"].iloc[0] == pytest.approx(78.5, rel=0.02)


def test_env_risk_is_deterministic_across_geocoder_ordering(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "osmnx", _DriftingOsmnx())
    monkeypatch.setitem(sys.modules, "requests", _fake_requests_elev_by_coord(HITS))
    first = assess_environmental_risk(
        "Leicester", radius_km=2.0, output_filename="risk_det"
    )
    assert "OVERALL:" in first, first
    row = _read_output("risk_det").iloc[0]
    assert row["elev_mean_m"] is not None
    # the tie between equally-ranked hits goes to the lower OSM id, not to hit order
    assert row["center_lat"] == pytest.approx(LAT, abs=1e-4)

    monkeypatch.setitem(
        sys.modules, "requests", _fake_requests_elev_by_coord(list(reversed(HITS)))
    )
    second = assess_environmental_risk(
        "Leicester", radius_km=2.0, output_filename="risk_det"
    )
    assert first == second


def test_population_grid_renders_the_queried_bbox(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "requests", _fake_worldpop())

    out = download_population_grid("Leicester", radius_km=10.0, output_filename="pop")
    assert f"{PYRAMID_TOTAL:,}" in out, out

    gdf = _read_output("pop")
    row = gdf.iloc[0]
    assert gdf.geometry.iloc[0].geom_type == "Polygon"
    assert row["population"] == PYRAMID_TOTAL
    assert row["area_source"] == "radius_bbox"
    assert row["area_km2"] > 0
    assert row["lon"] == pytest.approx(LON, abs=1e-4)
    assert row["lat"] == pytest.approx(LAT, abs=1e-4)

    # bbox spans the requested radius in both directions
    minx, miny, maxx, maxy = gdf.total_bounds
    assert maxy - miny == pytest.approx(2 * 10.0 / 111.0, rel=0.01)


def test_population_grid_renders_the_clip_polygon(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "requests", _fake_worldpop())

    clip_path = pathlib.Path(utils.caller_outputs_dir()) / "clip.gpkg"
    _circle(1000).to_file(clip_path, driver="GPKG")

    download_population_grid(
        "Leicester",
        radius_km=10.0,
        clip_layer_path="clip.gpkg",
        output_filename="pop_clip",
    )

    gdf = _read_output("pop_clip")
    row = gdf.iloc[0]
    assert gdf.geometry.iloc[0].geom_type == "Polygon"
    assert row["area_source"] == "clip_polygon"
    assert row["area_km2"] == pytest.approx(3.14, rel=0.02)


def test_population_grid_zonal_sums_the_local_raster(monkeypatch, stub_services):
    # the local raster answers both paths, so no HTTP call may be needed
    monkeypatch.setitem(sys.modules, "requests", _no_http())

    clip = _circle(5000)
    clip_path = pathlib.Path(utils.caller_outputs_dir()) / "clip5k.gpkg"
    clip.to_file(clip_path, driver="GPKG")
    transform = _write_pop_raster(stub_services / "ghsl_pop.tif")

    unclipped = download_population_grid(
        "Leicester", radius_km=10.0, output_filename="pop_unclipped"
    )
    clipped = download_population_grid(
        "Leicester",
        radius_km=10.0,
        clip_layer_path="clip5k.gpkg",
        output_filename="pop_clipped",
    )

    deg = 10.0 / 111.0
    expected_bbox = _cells_inside(
        transform, box(LON - deg, LAT - deg, LON + deg, LAT + deg)
    )
    expected_clip = _cells_inside(transform, clip.geometry.iloc[0])
    assert 0 < expected_clip < expected_bbox

    row_u = _read_output("pop_unclipped").iloc[0]
    row_c = _read_output("pop_clipped").iloc[0]
    assert row_u["population"] == pytest.approx(expected_bbox, rel=0.01)
    assert row_c["population"] == pytest.approx(expected_clip, rel=0.01)
    assert "GHS-POP" in row_u["source"]
    assert "GHS-POP" in row_c["source"]
    assert f"{int(round(expected_bbox)):,}" in unclipped
    assert f"{int(round(expected_clip)):,}" in clipped


def test_population_grid_sums_the_worldpop_pyramid(monkeypatch, stub_services):
    submits = []
    monkeypatch.setitem(
        sys.modules, "requests", _fake_worldpop(pending_polls=1, submits=submits)
    )

    out = download_population_grid("Leicester", radius_km=2.0, output_filename="pop_wp")
    assert f"{PYRAMID_TOTAL:,}" in out, out

    row = _read_output("pop_wp").iloc[0]
    assert row["population"] == PYRAMID_TOTAL
    assert "WorldPop" in row["source"]

    # submitted as a geojson FeatureCollection of the queried area, no iso3 guess
    assert len(submits) == 1
    params = submits[0]
    assert "iso3" not in params
    sent = json.loads(params["geojson"])
    assert sent["type"] == "FeatureCollection"
    assert sent["features"][0]["geometry"]["type"] == "Polygon"


def test_driving_isochrones_ask_valhalla_once_for_every_contour(
    monkeypatch, stub_services
):
    posts = []
    monkeypatch.setitem(sys.modules, "requests", _fake_valhalla(posts))

    out = calculate_isochrones(
        "Leicester",
        travel_mode="driving",
        time_minutes="5,10,15",
        output_filename="drive_iso",
    )
    assert "Computed driving isochrones" in out, out

    assert len(posts) == 1
    assert [c["time"] for c in posts[0]["contours"]] == [15, 10, 5]
    assert posts[0]["polygons"] is True
    assert posts[0]["denoise"] == 0.5
    assert posts[0]["generalize"] == 150

    gdf = _read_output("drive_iso")
    assert sorted(gdf["minutes"]) == [5, 10, 15]

    # each ring is labelled from properties.contour, so extent ranks with the label
    bounds = gdf.geometry.bounds
    widths = dict(zip(gdf["minutes"], bounds["maxx"] - bounds["minx"]))
    assert widths[15] > widths[10] > widths[5]


def test_score_sites_asks_opentopodata_once_for_every_site(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "osmnx", _SettingsOsmnx())
    gets = []
    monkeypatch.setitem(sys.modules, "requests", _fake_opentopodata(gets))

    names = ["Shoreditch London", "Kings Cross London", "Brixton London"]
    out = score_sites(
        "; ".join(names), criteria="flood_risk", output_filename="site_flood"
    )
    assert "Site scoring results" in out, out

    assert len(gets) == 1
    locations = gets[0].split("locations=")[1].split("|")
    assert len(locations) == len(names)

    # elevations land back on the site that asked for them, not shifted along
    ranked = _read_output("site_flood").sort_values("rank")
    assert list(ranked["name"]) == list(reversed(names))
    assert list(ranked["flood_risk_raw"]) == [30.0, 20.0, 10.0]
