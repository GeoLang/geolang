import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point

from src.core.errors import LayerFileRefused
from src.core.layer_files import open_raster_file, read_layer_file

NAMED_FILE_MARKER = "named-file-marker-4c1e"
WRITE_DRIVER_BY_SUFFIX = {
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".gpkg": "GPKG",
    ".shp": "ESRI Shapefile",
}
KML_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>'
    "<name>parcel</name><Point><coordinates>1,2</coordinates></Point>"
    "</Placemark></Document></kml>"
)


def written_layer(directory, suffix, name="parcels"):
    path = directory / f"{name}{suffix}"
    frame = gpd.GeoDataFrame({"name": ["parcel"]}, geometry=[Point(1, 2)], crs="EPSG:4326")
    frame.to_file(path, driver=WRITE_DRIVER_BY_SUFFIX[suffix.lower()])
    return path


@pytest.mark.parametrize("suffix", [*WRITE_DRIVER_BY_SUFFIX, ".GeoJSON", ".GPKG"])
def test_a_layer_file_is_read_under_its_own_suffix(tmp_path, suffix):
    frame = read_layer_file(written_layer(tmp_path, suffix))

    assert frame["name"].tolist() == ["parcel"]


def test_read_options_reach_the_reader(tmp_path):
    path = tmp_path / "parcels.gpkg"
    gpd.GeoDataFrame(
        {"name": ["first", "second"]}, geometry=[Point(1, 2), Point(3, 4)], crs="EPSG:4326"
    ).to_file(path, driver="GPKG")

    frame = read_layer_file(path, rows=1)

    assert frame["name"].tolist() == ["first"]


@pytest.mark.parametrize(
    ("content_suffix", "named_suffix"),
    [
        (".geojson", ".gpkg"),
        (".geojson", ".shp"),
        (".gpkg", ".geojson"),
        (".gpkg", ".json"),
        (".gpkg", ".shp"),
    ],
)
def test_a_file_holding_another_format_is_not_read(tmp_path, content_suffix, named_suffix):
    content = written_layer(tmp_path, content_suffix, name="source").read_bytes()
    renamed = tmp_path / f"parcels{named_suffix}"
    renamed.write_bytes(content)

    with pytest.raises(Exception):
        read_layer_file(renamed)


@pytest.mark.parametrize("suffix", WRITE_DRIVER_BY_SUFFIX)
def test_a_kml_document_is_not_read_under_any_suffix(tmp_path, suffix):
    path = tmp_path / f"parcels{suffix}"
    path.write_text(KML_DOCUMENT)

    with pytest.raises(Exception):
        read_layer_file(path)


@pytest.mark.parametrize("suffix", WRITE_DRIVER_BY_SUFFIX)
def test_a_gdalg_document_does_not_read_the_file_it_names(tmp_path, suffix):
    named_file = tmp_path / "named.csv"
    named_file.write_text(f"name,lon,lat\n{NAMED_FILE_MARKER},1.0,2.0\n")
    path = tmp_path / f"parcels{suffix}"
    path.write_text(
        json.dumps(
            {
                "type": "gdal_streamed_alg",
                "command_line": f"gdal vector pipeline ! read {named_file}",
            }
        )
    )

    with pytest.raises(Exception) as refusal:
        read_layer_file(path)

    assert NAMED_FILE_MARKER not in str(refusal.value)


@pytest.mark.parametrize("name", ["parcels.kml", "parcels.fgb", "parcels.vrt", "parcels"])
def test_an_unlisted_suffix_is_refused_before_the_file_is_opened(tmp_path, name):
    with pytest.raises(LayerFileRefused):
        read_layer_file(tmp_path / name)


def written_raster(path, driver):
    with rasterio.open(
        path,
        "w",
        driver=driver,
        height=2,
        width=2,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(0, 2, 1, 1),
    ) as raster:
        raster.write(np.ones((2, 2), dtype="uint8"), 1)
    return path


@pytest.mark.parametrize("name", ["grid.tif", "grid.TIFF"])
def test_a_geotiff_is_opened(tmp_path, name):
    with open_raster_file(written_raster(tmp_path / name, "GTiff")) as raster:
        assert raster.read(1).tolist() == [[1, 1], [1, 1]]


def test_a_tif_holding_another_raster_format_is_not_opened(tmp_path):
    path = written_raster(tmp_path / "grid.tif", "PNG")

    with rasterio.open(path) as raster:
        assert raster.driver == "PNG"
    with pytest.raises(Exception):
        open_raster_file(path)


def test_a_raster_with_another_suffix_is_refused(tmp_path):
    with pytest.raises(LayerFileRefused):
        open_raster_file(written_raster(tmp_path / "grid.png", "PNG"))
