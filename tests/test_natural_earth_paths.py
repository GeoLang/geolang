"""Natural Earth data uses its writable mount without widening file access."""

import io
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
from shapely.geometry import Point

from src.agents.tools.download_natural_earth import download_natural_earth_dataset
from src.agents.tools.geocode_place import geocode_place
from src.core import utils


def test_natural_earth_discovery_finds_mounted_and_direct_datasets(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
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
