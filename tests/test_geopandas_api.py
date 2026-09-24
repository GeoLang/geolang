"""Every function name geopandas_api advertises has to be one it can run.

The generic path resolves a name with `getattr(gpd, name)`, so a name that is
neither a branch in the tool nor an attribute of geopandas answers every call
with "Function not found". `buffer` and `to_file` are methods of a
GeoDataFrame, not functions of the module.
"""

import pathlib

import geopandas as gpd
import pytest

from src.agents.tools.geopandas_api import ALLOWED_FUNCTIONS, geopandas_api
from src.core import utils

NO_SUCH_FUNCTION = "Function not found"
NOT_ALLOWED = "Unsupported function"
# handled by a branch in the tool rather than by a geopandas attribute
LOCAL_FUNCTIONS = {"filter", "proximity_analysis"}


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    # the tree dirs are read once at import, so the env var alone misses them
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    return pathlib.Path(utils.caller_outputs_dir())


@pytest.mark.parametrize("function_name", sorted(ALLOWED_FUNCTIONS))
def test_every_advertised_function_is_one_the_tool_can_run(outputs, function_name):
    assert function_name in LOCAL_FUNCTIONS or hasattr(gpd, function_name)

    # the call itself may fail on missing arguments, it may not fail on the name
    result = geopandas_api(function_name=function_name)

    assert NO_SUCH_FUNCTION not in result, result
    assert NOT_ALLOWED not in result, result


@pytest.mark.parametrize("function_name", ["buffer", "to_file"])
def test_a_geodataframe_method_is_refused_by_name(outputs, function_name):
    """A method of a GeoDataFrame is not a function of the geopandas module."""
    assert function_name not in ALLOWED_FUNCTIONS

    result = geopandas_api(function_name=function_name)

    assert NOT_ALLOWED in result


SWITZERLAND_POPULATION = 8574832


@pytest.fixture
def countries(outputs):
    from shapely.geometry import Point

    gpd.GeoDataFrame(
        {
            "NAME": ["Switzerland", "Austria"],
            "POP_EST": [SWITZERLAND_POPULATION, 8877067],
            "CONTINENT": ["Europe", "Europe"],
        },
        geometry=[Point(8.2, 46.8), Point(14.1, 47.6)],
        crs="EPSG:4326",
    ).to_file(outputs / "europe_countries.gpkg", driver="GPKG")
    return "europe_countries.gpkg"


def test_read_file_returns_the_rows_of_the_columns_asked_for(countries):
    result = geopandas_api(
        function_name="read_file", dataset_path=countries, columns="NAME, POP_EST"
    )

    assert f"Switzerland,{SWITZERLAND_POPULATION}" in result
    assert "Columns: NAME, POP_EST, CONTINENT" in result


def test_read_file_without_columns_reads_no_values(countries):
    result = geopandas_api(function_name="read_file", dataset_path=countries)

    assert "Columns: NAME, POP_EST, CONTINENT" in result
    assert "Switzerland" not in result


def test_read_file_names_a_column_the_file_lacks(countries):
    result = geopandas_api(
        function_name="read_file", dataset_path=countries, columns="NAME,POPULATION"
    )

    assert "Error: no column POPULATION" in result
    assert "Switzerland" not in result


def test_the_logged_arguments_are_only_the_ones_given(countries):
    result = geopandas_api(
        function_name="read_file", dataset_path=countries, columns="NAME"
    )

    arguments = next(line for line in result.splitlines() if line.startswith("Arguments:"))
    assert "module" not in arguments
    assert "'columns': 'NAME'" in arguments
