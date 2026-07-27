# Tests for tools
"""Area tools must render the analysed AREA, not a summary point over it."""

import sys
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import Point

from src.agents.tools.assess_environmental_risk import assess_environmental_risk
from src.agents.tools.download_population_grid import download_population_grid

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


def test_env_risk_is_deterministic_across_geocoder_ordering(monkeypatch, stub_services):
    monkeypatch.setitem(sys.modules, "osmnx", _DriftingOsmnx())
    monkeypatch.setitem(sys.modules, "requests", _fake_requests_elev_by_coord(HITS))
    first = assess_environmental_risk(
        "Leicester", radius_km=2.0, output_filename="risk_det"
    )
    assert "OVERALL:" in first, first
    row = _read_output(stub_services, "risk_det").iloc[0]
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


def test_population_grid_counts_only_the_clip_polygon(monkeypatch, stub_services):
    payload = {"status": "success", "data": {"total_population": 412345}}
    monkeypatch.setitem(sys.modules, "requests", _fake_requests(payload))

    clip = _circle(5000)
    clip_path = stub_services / "outputs" / "clip5k.gpkg"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
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

    expected = _cells_inside(transform, clip.geometry.iloc[0])
    assert expected > 0
    row = _read_output(stub_services, "pop_clipped").iloc[0]
    assert row["population"] == pytest.approx(expected, rel=0.01)
    assert "GHS-POP" in row["source"]
    # the radius-bbox estimate must not leak into the clipped answer
    assert row["population"] != 412345
    assert "412,345" in unclipped
    assert f"{int(round(expected)):,}" in clipped
    assert _read_output(stub_services, "pop_unclipped").iloc[0]["population"] == 412345
