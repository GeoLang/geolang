from pathlib import Path

from src.core.errors import LayerFileRefused

# GDAL opens '<driver>:<path>' with that driver and no other
DRIVER_BY_SUFFIX = {".geojson": "GeoJSON", ".json": "GeoJSON", ".gpkg": "GPKG"}
SHAPEFILE_SUFFIX = ".shp"
SHAPEFILE_FIRST_BYTES = bytes.fromhex("0000270a")
LAYER_FILE_SUFFIXES = (*DRIVER_BY_SUFFIX, SHAPEFILE_SUFFIX)
RASTER_DRIVER = "GTiff"
RASTER_FILE_SUFFIXES = (".tif", ".tiff")


def single_driver_source(path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == SHAPEFILE_SUFFIX:
        # the shapefile driver takes no prefix
        with open(path, "rb") as file:
            if file.read(len(SHAPEFILE_FIRST_BYTES)) != SHAPEFILE_FIRST_BYTES:
                raise LayerFileRefused(f"'{path.name}' is not a shapefile")
        return str(path)
    if suffix not in DRIVER_BY_SUFFIX:
        raise LayerFileRefused(
            f"'{path.name}' is not a layer file type that can be read. "
            f"Name one of {', '.join(LAYER_FILE_SUFFIXES)}"
        )
    return f"{DRIVER_BY_SUFFIX[suffix]}:{path}"


def read_layer_file(path, **read_options):
    import geopandas as gpd

    return gpd.read_file(single_driver_source(path), **read_options)


def open_raster_file(path):
    import rasterio

    if Path(path).suffix.lower() not in RASTER_FILE_SUFFIXES:
        raise LayerFileRefused(
            f"'{Path(path).name}' is not a raster file type that can be read. "
            f"Name one of {', '.join(RASTER_FILE_SUFFIXES)}"
        )
    return rasterio.open(path, driver=RASTER_DRIVER)


def require_file_matches_suffix(path) -> None:
    name = Path(path).name
    try:
        if Path(path).suffix.lower() in RASTER_FILE_SUFFIXES:
            open_raster_file(path).close()
        else:
            read_layer_file(path, rows=1)
    except LayerFileRefused:
        raise
    except Exception as error:
        raise LayerFileRefused(
            f"'{name}' does not hold the format its suffix names: {error}"
        ) from error
