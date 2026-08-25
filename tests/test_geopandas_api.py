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
