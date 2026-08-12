"""A tool argument must not name a file the caller could not ask a route for.

The routes were confined first, the tool arguments were not: a tool resolved its
own path arguments against the whole tree, so an absolute path, or one climbing
out of the caller's directory, reached another caller's files. Every named input
now goes through `tool_input_path`, and every output name through
`tool_output_path`, which are the same search dirs and roots `/geojson` uses.

The tree dirs are read once at import, so these tests point the module-level
copies at a tmp_path rather than only setting `TOOL_EXEC_DIR`.
"""

import pathlib

import geopandas as gpd
import numpy as np
import pydantic
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point

from src.agents.tools.download_natural_earth import DownloadNaturalEarthArgs
from src.agents.tools.export_to_gpkg import export_to_gpkg
from src.agents.tools.geocode_place import geocode_place
from src.agents.tools.spatial_join import spatial_join
from src.core import utils
from src.core.utils import caller_directory_scope

ALICE = "alice-0123456789abcdef"
BOB = "bob-fedcba9876543210"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_DIR", tmp_path / "user_data")
    (tmp_path / "user_data").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.gpkg").write_bytes(b"do not read me")
    return tmp_path


def outputs_of(directory):
    with caller_directory_scope(directory):
        return pathlib.Path(utils.caller_outputs_dir())


def layer(path, name="somewhere"):
    """A one-point layer geopandas can really read, written where told."""
    gdf = gpd.GeoDataFrame(
        {"name": [name]}, geometry=[Point(1.0, 2.0)], crs="EPSG:4326"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver="GPKG" if path.suffix == ".gpkg" else "ESRI Shapefile")
    return path


# ── an input argument stays inside the caller's own files ────────────────


def test_a_tool_refuses_another_callers_layer(tree):
    secret = layer(outputs_of(ALICE) / "secret.gpkg", "alice's")

    with caller_directory_scope(BOB):
        for attempt in (
            f"../{ALICE}/secret.gpkg",
            f"outputs/{ALICE}/secret.gpkg",
            f"../../outputs/{ALICE}/secret.gpkg",
            str(secret),
        ):
            with pytest.raises(utils.PathRefused):
                export_to_gpkg(attempt, "copy")

    # nothing of alice's was copied out, under that name or any other
    assert list(outputs_of(BOB).iterdir()) == []


def test_a_tool_refuses_an_absolute_path(tree):
    outside = tree / "outside" / "secret.gpkg"

    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused) as refusal:
            export_to_gpkg(str(outside), "copy")

    # says what it wanted instead of resolving to something else
    assert "absolute path" in str(refusal.value)
    assert list(outputs_of(BOB).iterdir()) == []


def test_a_tool_refuses_a_climb_out_of_the_tree(tree):
    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused):
            export_to_gpkg("../../../etc/passwd", "copy")


def test_a_tool_reads_the_callers_own_layer(tree):
    layer(outputs_of(BOB) / "mine.gpkg", "bob's")

    with caller_directory_scope(BOB):
        result = export_to_gpkg("mine.gpkg", "copied")

    assert "Exported" in result
    assert (outputs_of(BOB) / "copied.gpkg").exists()


def test_a_tool_reads_a_layer_the_result_named(tree):
    """A tool result names a layer `outputs/x.gpkg`, and the model passes it back."""
    layer(outputs_of(BOB) / "mine.gpkg", "bob's")

    with caller_directory_scope(BOB):
        result = export_to_gpkg("outputs/mine.gpkg", "copied")

    assert "Exported" in result


# ── an output name stays inside the caller's own directory ───────────────


def test_a_tool_refuses_an_output_name_with_a_directory_part(tree):
    layer(outputs_of(BOB) / "mine.gpkg", "bob's")
    alice_before = list(outputs_of(ALICE).iterdir())

    with caller_directory_scope(BOB):
        for name in (f"../{ALICE}/stolen.gpkg", "sub/stolen.gpkg", "../stolen.gpkg"):
            with pytest.raises(utils.PathRefused):
                export_to_gpkg("mine.gpkg", name)

    assert list(outputs_of(ALICE).iterdir()) == alice_before
    # refused rather than quietly rewritten to a name in the caller's own dir
    assert not (outputs_of(BOB) / "stolen.gpkg").exists()


def test_a_tool_refuses_an_absolute_output_name(tree):
    layer(outputs_of(BOB) / "mine.gpkg", "bob's")
    target = tree / "outside" / "planted.gpkg"

    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused):
            export_to_gpkg("mine.gpkg", str(target))

    assert not target.exists()


def test_an_output_name_that_is_a_symlink_out_is_refused(tree):
    """A name can be a link before it is a file, and a write would follow it."""
    layer(outputs_of(BOB) / "mine.gpkg", "bob's")
    alices = layer(outputs_of(ALICE) / "target.gpkg", "alice's")
    before = alices.read_bytes()
    (outputs_of(BOB) / "planted.gpkg").symlink_to(alices)

    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused):
            export_to_gpkg("mine.gpkg", "planted.gpkg")

    assert alices.read_bytes() == before


def test_the_refusal_reaches_a_tool_that_reports_its_own_errors(tree):
    """Most tools wrap the call, so the refusal has to survive as readable text."""
    layer(outputs_of(BOB) / "points.gpkg")
    secret = layer(outputs_of(ALICE) / "secret.gpkg", "alice's")

    with caller_directory_scope(BOB):
        result = spatial_join("points.gpkg", str(secret))

    assert "absolute path" in result
    assert "polygons_path" in result


# ── shared reference data is still readable ──────────────────────────────


def test_a_natural_earth_set_is_still_readable_by_name(tree):
    layer(tree / "natural_earth_110m" / "ne_110m_populated_places.shp", "london")

    with caller_directory_scope(BOB):
        result = export_to_gpkg("ne_110m_populated_places.shp", "places")

    assert "Exported" in result
    assert (outputs_of(BOB) / "places.gpkg").exists()


def test_geocode_place_still_reads_the_reference_set(tree):
    """Its dataset paths are fixed here, so nothing a caller writes picks them."""
    layer(tree / "natural_earth_110m" / "ne_110m_populated_places.shp", "Testville")

    assert "Testville" in geocode_place("Testville")


def test_the_population_raster_is_found_at_the_tree_root(tree):
    """Reference data, not anyone's file, so it lives where no caller may write."""
    path = tree / "ghsl_pop.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=2,
        width=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 2, 1, 1),
    ) as raster:
        raster.write(np.ones((2, 2), dtype="float32"), 1)

    with caller_directory_scope(BOB):
        assert utils.population_raster_path() == str(path)


def test_a_caller_cannot_name_a_raster_outside_their_own_files(tree):
    hidden = layer(outputs_of(ALICE) / "pop.gpkg", "alice's")

    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused):
            utils.tool_input_path_or_none("ghsl_raster_path", str(hidden))


def test_the_natural_earth_dataset_name_cannot_climb_out_of_its_directory():
    """The name is not only part of a url: the download is saved under it too."""
    for name in ("../../outside/planted", "admin_0/countries", ".."):
        with pytest.raises(pydantic.ValidationError):
            DownloadNaturalEarthArgs(dataset=name)

    assert DownloadNaturalEarthArgs(dataset="admin_0_countries").dataset
