import os

GDAL_SKIP_ENV = "GDAL_SKIP"
# a vrt or gdalg document under any suffix reads whatever local file it names
DRIVERS_READING_NAMED_FILES = ("VRT", "OGR_VRT", "GDALG")


def skip_drivers_reading_named_files() -> None:
    already_skipped = os.environ.get(GDAL_SKIP_ENV, "").replace(",", " ").split()
    os.environ[GDAL_SKIP_ENV] = " ".join(
        dict.fromkeys([*already_skipped, *DRIVERS_READING_NAMED_FILES])
    )


# pyogrio and rasterio read GDAL_SKIP once, when they are first imported
skip_drivers_reading_named_files()
