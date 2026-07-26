# Tests for tools
"""Area tools must render the analysed AREA, not a summary point over it."""

import sys
from types import SimpleNamespace

import geopandas as gpd
import pytest

from src.agents.tools.assess_environmental_risk import assess_environmental_risk
from src.agents.tools.download_population_grid import download_population_grid

LAT, LON = 52.6369, -1.1398  # Leicester


class _FakeOsmnx:
    """Geocoder that answers, OSM feature queries that don't (the tools degrade)."""

    def geocode(self, place_name):
        return LAT, LON

    def features_from_point(self, *args, **kwargs):
        raise RuntimeError("no OSM in tests")


def _fake_requests(payload, status=200):
    def get(url, timeout=None):
        return SimpleNamespace(status_code=status, json=lambda: payload)

    return SimpleNamespace(get=get)


@pytest.fixture
def stub_services(monkeypatch, tmp_path):
    """Stub the HTTP boundaries and point tool output at tmp_path."""
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "osmnx", _FakeOsmnx())
    return tmp_path


def _read_output(tmp_path, name):
    path = tmp_path / "outputs" / f"{name}.gpkg"
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


def test_env_risk_renders_the_buffer_polygon(monkeypatch, stub_services):
    elevations = {"status": "OK", "results": [{"elevation": 8.0}] * 100}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(elevations))

    out = assess_environmental_risk("Leicester", radius_km=2.0, output_filename="risk")
    assert "OVERALL:" in out, out

    gdf = _read_output(stub_services, "risk")
    assert len(gdf) == 1
    assert gdf.geometry.iloc[0].geom_type == "Polygon"

    # the polygon is the assessment area, not a degenerate point
    minx, miny, maxx, maxy = gdf.total_bounds
    assert maxx > minx and maxy > miny
    # 2km radius in true metres: UTM area within 2% of pi*r^2
    assert gdf["area_km2"].iloc[0] == pytest.approx(12.57, rel=0.02)

    # scores survive on the polygon, centroid survives as properties
    row = gdf.iloc[0]
    assert row["flood_score"] == 7  # 8m mean elevation -> HIGH
    assert row["overall_risk"] > 0
    assert row["overall_label"].endswith("RISK")
    assert row["radius_km"] == 2.0
    assert row["center_lon"] == pytest.approx(LON, abs=1e-4)
    assert row["center_lat"] == pytest.approx(LAT, abs=1e-4)


def test_env_risk_uses_a_supplied_polygon_as_the_area(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "requests", _fake_requests({"status": "OK"}))

    iso_path = stub_services / "outputs" / "iso.gpkg"
    iso_path.parent.mkdir(parents=True, exist_ok=True)
    _circle(5000).to_file(iso_path, driver="GPKG")

    assess_environmental_risk(
        "Leicester", radius_km=2.0, polygon_path="iso.gpkg", output_filename="risk_iso"
    )

    gdf = _read_output(stub_services, "risk_iso")
    assert gdf.geometry.iloc[0].geom_type == "Polygon"
    # the isochrone won, not the 2km buffer
    assert gdf["area_km2"].iloc[0] == pytest.approx(78.5, rel=0.02)


def test_population_grid_renders_the_queried_bbox(monkeypatch, stub_services):
    payload = {"status": "success", "data": {"total_population": 412345}}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(payload))

    out = download_population_grid("Leicester", radius_km=10.0, output_filename="pop")
    assert "412,345" in out, out

    gdf = _read_output(stub_services, "pop")
    row = gdf.iloc[0]
    assert gdf.geometry.iloc[0].geom_type == "Polygon"
    assert row["population"] == 412345
    assert row["area_source"] == "radius_bbox"
    assert row["area_km2"] > 0
    assert row["lon"] == pytest.approx(LON, abs=1e-4)
    assert row["lat"] == pytest.approx(LAT, abs=1e-4)

    # bbox spans the requested radius in both directions
    minx, miny, maxx, maxy = gdf.total_bounds
    assert maxy - miny == pytest.approx(2 * 10.0 / 111.0, rel=0.01)


def test_population_grid_renders_the_clip_polygon(monkeypatch, stub_services):
    payload = {"status": "success", "data": {"total_population": 900}}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(payload))

    clip_path = stub_services / "outputs" / "clip.gpkg"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    _circle(1000).to_file(clip_path, driver="GPKG")

    download_population_grid(
        "Leicester",
        radius_km=10.0,
        clip_layer_path="clip.gpkg",
        output_filename="pop_clip",
    )

    gdf = _read_output(stub_services, "pop_clip")
    row = gdf.iloc[0]
    assert gdf.geometry.iloc[0].geom_type == "Polygon"
    assert row["area_source"] == "clip_polygon"
    assert row["area_km2"] == pytest.approx(3.14, rel=0.02)
