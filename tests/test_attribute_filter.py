from pathlib import Path

import geopandas as gpd
import pandas
import pytest
from shapely.geometry import Point

from src.agents.tools._attribute_filter import (
    SUPPORTED_FILTERS,
    FilterRefused,
    attribute_filter_mask,
)
from src.agents.tools.download_natural_earth import download_natural_earth_dataset
from src.agents.tools.geopandas_api import geopandas_api
from src.core import utils

COUNTRIES = {
    "NAME": ["Switzerland", "Norway", "Brazil", "Japan"],
    "CONTINENT": ["Europe", "Europe", "South America", "Asia"],
    "REGION_UN": ["Europe", "Europe", "Americas", "Asia"],
    "SUBREGION": ["Western Europe", "Northern Europe", "South America", "Eastern Asia"],
    "POP_EST": [8574832, 5347896, 211049527, 126264931],
    "MIN_ZOOM": [0.0, -1.5, 0.0, None],
}
COUNTRY_POINTS = [Point(8.2, 46.8), Point(8.5, 61.0), Point(-51.9, -14.2), Point(138.3, 36.2)]


@pytest.fixture
def countries_frame():
    return gpd.GeoDataFrame(COUNTRIES, geometry=COUNTRY_POINTS, crs="EPSG:4326")


def names(frame, expression):
    return list(frame[attribute_filter_mask(frame, expression)]["NAME"])


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("CONTINENT == 'Europe'", ["Switzerland", "Norway"]),
        ("REGION_UN == 'Americas'", ["Brazil"]),
        ("SUBREGION == 'Northern Europe'", ["Norway"]),
        ("CONTINENT == 'Europe' and POP_EST > 6000000", ["Switzerland"]),
        ("REGION_UN == 'Americas' or REGION_UN == 'Asia'", ["Brazil", "Japan"]),
        ("not (CONTINENT == 'Europe')", ["Brazil", "Japan"]),
        ("CONTINENT != 'Europe' and (POP_EST < 150000000 or NAME == 'Brazil')", ["Brazil", "Japan"]),
        ("NAME in ['Norway', 'Japan']", ["Norway", "Japan"]),
        ("NAME not in ('Norway', 'Japan')", ["Switzerland", "Brazil"]),
        ("5000000 <= POP_EST <= 10000000", ["Switzerland", "Norway"]),
        ("MIN_ZOOM < -1", ["Norway"]),
        ("MIN_ZOOM == None", ["Japan"]),
        ("MIN_ZOOM != None and 'Europe' == CONTINENT", ["Switzerland", "Norway"]),
        ("REGION_UN == CONTINENT", ["Switzerland", "Norway", "Japan"]),
    ],
)
def test_a_supported_filter_selects_the_rows_it_describes(countries_frame, expression, expected):
    assert names(countries_frame, expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "CONTINENT.str.startswith('E')",
        "NAME.__class__ == 'x'",
        "len(NAME) > 5",
        "__import__('os') == 1",
        "NAME[0] == 'S'",
        "POP_EST > @threshold",
        "__class__ == 1",
        "POPULATION > 1",
        "CONTINENT",
        "1 == 1",
        "NAME is None",
        "NAME in {'Norway'}",
        "NAME in NAME",
        "'Europe' in CONTINENT",
        "POP_EST > -'x'",
        "POP_EST + 1 > 2",
        "(lambda: 1)() == 1",
        "MIN_ZOOM > None",
        "x = 1",
        "NAME == '" + "x" * 5000 + "'",
    ],
)
def test_anything_outside_the_supported_filters_is_refused(countries_frame, expression):
    with pytest.raises(FilterRefused) as refusal:
        attribute_filter_mask(countries_frame, expression)

    assert str(refusal.value).startswith(SUPPORTED_FILTERS)


@pytest.fixture
def evaluator_disabled(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a filter reached pandas' evaluator")

    monkeypatch.setattr(pandas.DataFrame, "query", refuse)
    monkeypatch.setattr(pandas.DataFrame, "eval", refuse)
    monkeypatch.setattr(pandas, "eval", refuse)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    return tmp_path


def test_geopandas_api_filters_without_pandas_evaluator(tree, countries_frame, evaluator_disabled):
    countries_frame.to_file(Path(utils.caller_outputs_dir()) / "countries.gpkg", driver="GPKG")

    result = geopandas_api(
        function_name="filter",
        dataset_path="countries.gpkg",
        filter_query="CONTINENT == 'Europe' and POP_EST > 6000000",
        output_path="rich_europe.gpkg",
    )
    refused = geopandas_api(
        function_name="filter",
        dataset_path="countries.gpkg",
        filter_query="NAME.__class__ == 'x'",
        output_path="never.gpkg",
    )

    saved = gpd.read_file(Path(utils.caller_outputs_dir()) / "rich_europe.gpkg")
    assert "Filtered to 1 features" in result
    assert list(saved["NAME"]) == ["Switzerland"]
    assert SUPPORTED_FILTERS in refused
    assert not (Path(utils.caller_outputs_dir()) / "never.gpkg").exists()


def test_download_natural_earth_filters_without_pandas_evaluator(tree, countries_frame, evaluator_disabled):
    shared = tree / utils.NATURAL_EARTH_DIRECTORY_NAME / "50m"
    shared.mkdir(parents=True)
    countries_frame.drop(columns=["MIN_ZOOM"]).to_file(shared / "ne_50m_admin_0_countries.shp")

    result = download_natural_earth_dataset(
        scale="50m",
        dataset="admin_0_countries",
        filter_query="REGION_UN == 'Americas' or SUBREGION == 'Northern Europe'",
        output_filename="picked",
    )
    refused = download_natural_earth_dataset(
        scale="50m",
        dataset="admin_0_countries",
        filter_query="CONTINENT.str.len() > 1",
        output_filename="never",
    )

    saved = gpd.read_file(Path(utils.caller_outputs_dir()) / "picked.gpkg")
    assert "filtered to 2 features" in result
    assert sorted(saved["NAME"]) == ["Brazil", "Norway"]
    assert SUPPORTED_FILTERS in refused
    assert not (Path(utils.caller_outputs_dir()) / "never.gpkg").exists()
