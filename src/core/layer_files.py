from pathlib import Path

from src.core.errors import LayerFileRefused

# GDAL opens '<driver>:<path>' with that driver and no other
DRIVER_BY_SUFFIX = {".geojson": "GeoJSON", ".json": "GeoJSON", ".gpkg": "GPKG"}
SHAPEFILE_SUFFIX = ".shp"
SHAPEFILE_FIRST_BYTES = bytes.fromhex("0000270a")
LAYER_FILE_SUFFIXES = (*DRIVER_BY_SUFFIX, SHAPEFILE_SUFFIX)


def read_layer_file(path, **read_options):
    import geopandas as gpd

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == SHAPEFILE_SUFFIX:
        # the shapefile driver takes no prefix
        with open(path, "rb") as file:
            if file.read(len(SHAPEFILE_FIRST_BYTES)) != SHAPEFILE_FIRST_BYTES:
                raise LayerFileRefused(f"'{path.name}' is not a shapefile")
        return gpd.read_file(path, **read_options)
    if suffix not in DRIVER_BY_SUFFIX:
        raise LayerFileRefused(
            f"'{path.name}' is not a layer file type that can be read. "
            f"Name one of {', '.join(LAYER_FILE_SUFFIXES)}"
        )
    return gpd.read_file(f"{DRIVER_BY_SUFFIX[suffix]}:{path}", **read_options)
