"""Natural Earth data uses its writable mount without widening file access."""

import io
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
import requests
from shapely.geometry import Point

from src.agents.tools.download_natural_earth import download_natural_earth_dataset
from src.agents.tools.geocode_place import geocode_place
from src.core import utils


def test_natural_earth_discovery_finds_mounted_and_direct_datasets(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    mounted = tmp_path / "natural_earth" / "110m"
    direct = tmp_path / "natural_earth_50m"
    outside = tmp_path / "outside"
    mounted.mkdir(parents=True)
    direct.mkdir()
    outside.mkdir()
    (tmp_path / "natural_earth_10m").symlink_to(outside)

    assert utils.natural_earth_directory("110m") == mounted
    assert set(utils.natural_earth_dirs()) == {str(mounted), str(direct)}
    assert set(utils.natural_earth_dataset_paths("populated_places")) == {
        str(direct / "ne_50m_populated_places.shp"),
        str(mounted / "ne_110m_populated_places.shp"),
    }


def test_geocoder_reads_a_dataset_from_the_mounted_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    path = tmp_path / "natural_earth" / "110m" / "ne_110m_populated_places.shp"
    path.parent.mkdir(parents=True)
    gpd.GeoDataFrame(
        {"NAME": ["Testville"], "SOV0NAME": ["Testland"]},
        geometry=[Point(1.0, 2.0)],
        crs="EPSG:4326",
    ).to_file(path)

    assert "Testville, Testland" in geocode_place("Testville")


def test_downloader_uses_the_writable_mount_when_the_source_root_is_readonly(
    monkeypatch, tmp_path
):
    exec_dir = tmp_path / "app"
    natural_earth = exec_dir / "natural_earth"
    natural_earth.mkdir(parents=True)
    exec_dir.chmod(0o555)
    monkeypatch.setattr(utils, "EXEC_DIR", str(exec_dir))

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("ne_110m_populated_places.shp", "shape")

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        iter_content=lambda chunk_size: [archive.getvalue()],
    )
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=lambda *args, **kwargs: response))

    try:
        result = download_natural_earth_dataset()
    finally:
        exec_dir.chmod(0o755)

    target = natural_earth / "110m" / "ne_110m_populated_places.shp"
    assert target.read_text() == "shape"
    assert str(target) in result
    assert not (exec_dir / "natural_earth_110m").exists()


def test_image_creates_uid_1000_runtime_mounts_before_copying_source():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    recipe = dockerfile.read_text()

    mount_targets = (
        "/app/geolang/outputs /app/geolang/user_data /app/geolang/live_data "
        "/app/geolang/natural_earth"
    )
    assert f"mkdir -p {mount_targets}" in recipe
    assert f"chown 1000:1000 {mount_targets}" in recipe
    assert recipe.index("chown 1000:1000") < recipe.index("COPY src/ ./src/")


def test_a_filtered_download_named_without_an_extension_is_saved_as_gpkg(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    shapefile_dir = tmp_path / "shapefile"
    shapefile_dir.mkdir()
    gpd.GeoDataFrame(
        {"CONTINENT": ["Europe", "Asia"]},
        geometry=[Point(8.0, 47.0), Point(100.0, 30.0)],
        crs="EPSG:4326",
    ).to_file(shapefile_dir / "ne_50m_admin_0_countries.shp")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        for part in shapefile_dir.iterdir():
            zip_file.write(part, part.name)
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        iter_content=lambda chunk_size: [archive.getvalue()],
    )
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=lambda *args, **kwargs: response))

    result = download_natural_earth_dataset(
        scale="50m",
        dataset="admin_0_countries",
        filter_query="CONTINENT == 'Europe'",
        output_filename="europe_countries",
    )

    assert "outputs/europe_countries.gpkg" in result
    saved = Path(utils.caller_outputs_dir()) / "europe_countries.gpkg"
    assert len(gpd.read_file(saved)) == 1


@pytest.fixture
def natural_earth_server(monkeypatch, tmp_path):
    exec_dir = tmp_path / "app"
    exec_dir.mkdir()
    monkeypatch.setattr(utils, "EXEC_DIR", str(exec_dir))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(exec_dir / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", exec_dir / "user_data")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("ne_110m_populated_places.shp", "shape")
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            iter_content=lambda chunk_size: [archive.getvalue()],
        )

    monkeypatch.setattr(requests, "get", get)
    return SimpleNamespace(exec_dir=exec_dir, requested=requested)


def caller_copy(exec_dir: Path) -> Path:
    return exec_dir / "outputs" / "anonymous" / "natural_earth" / "110m"


def test_a_dataset_already_in_the_shared_directory_is_not_downloaded(
    natural_earth_server,
):
    shared = natural_earth_server.exec_dir / "natural_earth" / "110m"
    shared.mkdir(parents=True)
    (shared / "ne_110m_populated_places.shp").write_text("present")

    result = download_natural_earth_dataset()

    assert natural_earth_server.requested == []
    assert str(shared / "ne_110m_populated_places.shp") in result
    assert not caller_copy(natural_earth_server.exec_dir).exists()


def test_a_missing_dataset_downloads_into_a_writable_shared_directory(
    natural_earth_server,
):
    shared = natural_earth_server.exec_dir / "natural_earth"
    shared.mkdir()

    result = download_natural_earth_dataset()

    target = shared / "110m" / "ne_110m_populated_places.shp"
    assert target.read_text() == "shape"
    assert str(target) in result
    assert len(natural_earth_server.requested) == 1
    assert not caller_copy(natural_earth_server.exec_dir).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only chmod")
def test_a_read_only_shared_directory_downloads_into_the_callers_own_copy(
    natural_earth_server,
):
    exec_dir = natural_earth_server.exec_dir
    shared = exec_dir / "natural_earth"
    shared.mkdir()
    shared.chmod(0o555)
    try:
        first = download_natural_earth_dataset()
        second = download_natural_earth_dataset()
    finally:
        shared.chmod(0o755)

    target = caller_copy(exec_dir) / "ne_110m_populated_places.shp"
    assert target.read_text() == "shape"
    assert str(target) in first
    assert str(target) in second
    assert len(natural_earth_server.requested) == 1
    assert list(shared.iterdir()) == []
    assert str(target) in utils.natural_earth_dataset_paths("populated_places")
    assert str(target.parent) in utils.allowed_roots()
    assert str(target.parent) in utils.layer_search_dirs()


def test_the_shared_directory_is_listed_before_the_callers_copy(natural_earth_server):
    exec_dir = natural_earth_server.exec_dir
    shared = exec_dir / "natural_earth" / "110m"
    shared.mkdir(parents=True)
    caller_copy(exec_dir).mkdir(parents=True)

    assert utils.natural_earth_dirs("110m") == [str(shared), str(caller_copy(exec_dir))]


def test_a_missing_shared_directory_is_created_by_the_first_download(
    natural_earth_server,
):
    exec_dir = natural_earth_server.exec_dir
    assert not (exec_dir / "natural_earth").exists()

    result = download_natural_earth_dataset()

    target = exec_dir / "natural_earth" / "110m" / "ne_110m_populated_places.shp"
    assert target.read_text() == "shape"
    assert str(target) in result
    assert not caller_copy(exec_dir).exists()
