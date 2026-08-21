"""A tool argument must not name a file the caller could not ask a route for.

The routes were confined first, the tool arguments were not: a tool resolved its
own path arguments against the whole tree, so an absolute path, or one climbing
out of the caller's directory, reached another caller's files. Every named input
now goes through `tool_input_path`, and every output name through
`tool_output_path`, which are the same search dirs and roots `/geojson` uses.

The tree dirs are read once at import, so these tests point the module-level
copies at a tmp_path rather than only setting `TOOL_EXEC_DIR`.
"""

import importlib
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
from src.agents.tools.run_qgis_algorithm import (
    COMMAND_LINE_PARAMETERS,
    confined_parameters,
)
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
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
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


def test_a_refusal_survives_a_reload_of_the_module_that_raises_it(tree):
    """The route tests reload utils, and a tool that held the old class 500s.

    A caller catching the class it imported first has to keep catching what
    the reloaded module raises, or a refusal stops being a 400.
    """
    caught_before_the_reload = utils.PathRefused
    importlib.reload(utils)

    with caller_directory_scope(BOB):
        with pytest.raises(caught_before_the_reload):
            utils.tool_output_path("output_filename", "../escape.gpkg")


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


# ── a QGIS parameter is confined by the type the algorithm gives it ──────

# the types QGIS really reports for these, read off parameterDefinitions()
FIELD_CALCULATOR_TYPES = {
    "INPUT": "source",
    "FIELD_NAME": "string",
    "FORMULA": "expression",
    "OUTPUT": "sink",
}
EXTRACT_BY_ATTRIBUTE_TYPES = {
    "INPUT": "source",
    "FIELD": "field",
    "VALUE": "string",
    "OUTPUT": "sink",
    "FAIL_OUTPUT": "sink",
}


def test_a_value_parameter_is_not_treated_as_a_path(tree):
    """A formula divides, an attribute value carries a slash. Neither is a file."""
    with caller_directory_scope(BOB):
        confined = confined_parameters(
            {"FORMULA": '"population" / "area"', "FIELD_NAME": "density"},
            FIELD_CALCULATOR_TYPES,
        )
        by_attribute = confined_parameters(
            {"VALUE": "Kingston/St. Andrew", "FIELD": "parish"},
            EXTRACT_BY_ATTRIBUTE_TYPES,
        )

    assert confined["FORMULA"] == '"population" / "area"'
    assert by_attribute["VALUE"] == "Kingston/St. Andrew"


def test_an_input_layer_parameter_resolves_to_the_callers_own_file(tree):
    mine = layer(outputs_of(BOB) / "points.gpkg", "bob's")

    with caller_directory_scope(BOB):
        confined = confined_parameters({"INPUT": "points.gpkg"}, FIELD_CALCULATOR_TYPES)

    assert confined["INPUT"] == str(mine)


def test_an_input_layer_parameter_refuses_another_callers_file(tree):
    secret = layer(outputs_of(ALICE) / "secret.gpkg", "alice's")

    with caller_directory_scope(BOB):
        for attempt in (str(secret), f"../{ALICE}/secret.gpkg", "../../etc/passwd"):
            with pytest.raises(utils.PathRefused):
                confined_parameters({"INPUT": attempt}, FIELD_CALCULATOR_TYPES)


def test_a_layer_parameter_keeps_the_suffix_that_names_its_table(tree):
    mine = layer(outputs_of(BOB) / "points.gpkg", "bob's")

    with caller_directory_scope(BOB):
        confined = confined_parameters(
            {"INPUT": "points.gpkg|layername=points"}, FIELD_CALCULATOR_TYPES
        )

    assert confined["INPUT"] == f"{mine}|layername=points"


def test_a_second_destination_lands_in_the_callers_own_outputs(tree):
    with caller_directory_scope(BOB):
        confined = confined_parameters(
            {"FAIL_OUTPUT": "rejects.gpkg"}, EXTRACT_BY_ATTRIBUTE_TYPES
        )

        with pytest.raises(utils.PathRefused):
            confined_parameters(
                {"FAIL_OUTPUT": f"../{ALICE}/planted.gpkg"}, EXTRACT_BY_ATTRIBUTE_TYPES
            )

    assert confined["FAIL_OUTPUT"] == str(outputs_of(BOB) / "rejects.gpkg")


def test_a_temporary_destination_is_left_to_qgis(tree):
    """QGIS picks the file itself for this one, so there is no name to confine."""
    with caller_directory_scope(BOB):
        confined = confined_parameters(
            {"FAIL_OUTPUT": "TEMPORARY_OUTPUT"}, EXTRACT_BY_ATTRIBUTE_TYPES
        )

    assert confined["FAIL_OUTPUT"] == "TEMPORARY_OUTPUT"


def test_a_parameter_naming_layers_inside_a_structure_is_refused(tree):
    """dxf export takes its layers as objects, so no name here can be resolved."""
    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused):
            confined_parameters(
                {"LAYERS": [{"layer": f"outputs/{ALICE}/secret.gpkg"}]},
                {"LAYERS": "dxflayers"},
            )


def test_every_layer_in_a_multilayer_list_is_confined(tree):
    mine = layer(outputs_of(BOB) / "mine.gpkg", "bob's")
    secret = layer(outputs_of(ALICE) / "secret.gpkg", "alice's")

    with caller_directory_scope(BOB):
        confined = confined_parameters({"INPUT": ["mine.gpkg"]}, {"INPUT": "multilayer"})

        with pytest.raises(utils.PathRefused):
            confined_parameters(
                {"INPUT": ["mine.gpkg", str(secret)]}, {"INPUT": "multilayer"}
            )

    assert confined["INPUT"] == [str(mine)]


def test_a_gdal_command_line_parameter_is_refused(tree):
    """gdal pastes this in whole, so a file named in it would never be confined."""
    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused):
            confined_parameters(
                {"EXTRA": f"-input_file_list outputs/{ALICE}/rasters.txt"},
                {"EXTRA": "string"},
                COMMAND_LINE_PARAMETERS,
            )


def test_the_same_name_on_a_native_algorithm_stays_a_value(tree):
    """The native raster algorithms hand CREATION_OPTIONS to the writer instead."""
    with caller_directory_scope(BOB):
        confined = confined_parameters(
            {"CREATION_OPTIONS": "COMPRESS=DEFLATE"}, {"CREATION_OPTIONS": "string"}
        )

    assert confined["CREATION_OPTIONS"] == "COMPRESS=DEFLATE"


def test_an_unused_command_line_parameter_is_not_refused(tree):
    """An empty value is how the model says it is not using the option."""
    with caller_directory_scope(BOB):
        confined = confined_parameters(
            {"EXTRA": ""}, {"EXTRA": "string"}, COMMAND_LINE_PARAMETERS
        )

    assert confined["EXTRA"] == ""


def test_a_parameter_the_algorithm_does_not_define_is_passed_through(tree):
    """QGIS ignores a name it does not know, so nothing opens it."""
    with caller_directory_scope(BOB):
        confined = confined_parameters({"NOT_A_PARAMETER": "a/b"}, {"INPUT": "source"})

    assert confined["NOT_A_PARAMETER"] == "a/b"


def test_the_natural_earth_dataset_name_cannot_climb_out_of_its_directory():
    """The name is not only part of a url: the download is saved under it too."""
    for name in ("../../outside/planted", "admin_0/countries", ".."):
        with pytest.raises(pydantic.ValidationError):
            DownloadNaturalEarthArgs(dataset=name)

    assert DownloadNaturalEarthArgs(dataset="admin_0_countries").dataset


# ── pyqgis_api uri is confined the same way as every other input ─────────


@pytest.mark.parametrize("function_name", ["QgsVectorLayer", "QgsRasterLayer"])
def test_pyqgis_api_refuses_an_absolute_uri_outside_the_tree(tree, function_name):
    from src.agents.tools.pygis_api import confined_uri, pyqgis_api

    outside = str(tree / "outside" / "secret.gpkg")

    with caller_directory_scope(BOB):
        with pytest.raises(utils.PathRefused) as refusal:
            confined_uri(outside)
        result = pyqgis_api(function_name, uri=outside)

    assert "absolute path" in str(refusal.value)
    assert "absolute path" in result
    assert "QGIS init failed" not in result
