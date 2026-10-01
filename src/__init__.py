import os

GDAL_SKIP_ENV = "GDAL_SKIP"
# a vrt document under any suffix reads whatever local file it names
VRT_DRIVERS = ("VRT", "OGR_VRT")


def skip_vrt_drivers() -> None:
    already_skipped = os.environ.get(GDAL_SKIP_ENV, "").replace(",", " ").split()
    os.environ[GDAL_SKIP_ENV] = " ".join(dict.fromkeys([*already_skipped, *VRT_DRIVERS]))


# pyogrio and rasterio read GDAL_SKIP once, when they are first imported
skip_vrt_drivers()
