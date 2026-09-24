from pydantic import BaseModel, Field
from typing import Optional
import os
import zipfile

from src.core.utils import (
    natural_earth_dirs,
    natural_earth_download_directory,
    tool_output_path,
)

from ._attribute_filter import attribute_filter_mask

GPKG_EXTENSION = ".gpkg"


class DownloadNaturalEarthArgs(BaseModel):
    scale: str = Field(
        "110m", description="'10m', '50m', or '110m'."
    )
    # the name goes into the download url and into the file it is saved as, so a
    # separator in it would write the download outside the reference directory
    dataset: str = Field(
        "populated_places",
        pattern=r"^[a-z0-9_]+$",
        description="Natural Earth dataset name, e.g. 'admin_0_countries'.",
    )
    filter_query: Optional[str] = Field(
        None,
        description=(
            "pandas query expression cutting the dataset to a subset, written "
            "literally, e.g. CONTINENT == 'Europe', REGION_UN == 'Americas', "
            "SUBREGION == 'Northern Europe'. Set it whenever the user asks for a "
            "regional subset: the output is then a GPKG of just those features."
        ),
    )
    output_filename: Optional[str] = Field(
        None,
        description=(
            "Name for the filtered GPKG, no directory. Only used with filter_query, "
            "and defaults to <dataset>_filtered.gpkg."
        ),
    )


def download_into(output_dir: str, scale: str, dataset: str) -> str | None:
    import requests

    url = f"https://naturalearth.s3.amazonaws.com/{scale}_cultural/ne_{scale}_{dataset}.zip"
    zip_path = os.path.join(output_dir, f"ne_{scale}_{dataset}.zip")
    os.makedirs(output_dir, exist_ok=True)
    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    # extractall strips absolute paths and ".." segments from member names
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(output_dir)
    os.remove(zip_path)

    shp_path = os.path.join(output_dir, f"ne_{scale}_{dataset}.shp")
    if os.path.exists(shp_path):
        return shp_path
    for root, _, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".shp") and dataset in f:
                return os.path.join(root, f)
    return None


def download_natural_earth_dataset(
    scale: str = "110m",
    dataset: str = "populated_places",
    filter_query: str = None,
    output_filename: str = None,
) -> str:
    """
    Generic Natural Earth downloader. If filter_query is provided, the downloaded
    shapefile is cut to the matching features and saved as a GPKG under outputs/.
    """
    scale = scale.lower().strip()
    if scale not in ["10m", "50m", "110m"]:
        scale = "110m"

    shapefile_name = f"ne_{scale}_{dataset}.shp"
    try:
        present = [
            path
            for directory in natural_earth_dirs(scale)
            if os.path.exists(path := os.path.join(directory, shapefile_name))
        ]
        if present:
            verb, shp_path = "Found", present[0]
        else:
            verb = "Downloaded"
            output_dir = str(natural_earth_download_directory(scale))
            shp_path = download_into(output_dir, scale, dataset)
            if not shp_path:
                return f"✅ Downloaded {scale} {dataset}, but no .shp found in {output_dir}"

        if not filter_query:
            return f"✅ {verb} {scale} {dataset} → {shp_path}"

        # Filter step
        import geopandas as gpd

        gdf = gpd.read_file(shp_path)
        try:
            filtered = gdf[attribute_filter_mask(gdf, filter_query)]
        except Exception as e:
            cols = ", ".join(gdf.columns)
            return (
                f"ERROR: filter_query {filter_query!r} failed: {e}. "
                f"Available columns: {cols}"
            )

        if len(filtered) == 0:
            return (
                f"ERROR: filter_query {filter_query!r} matched 0 features. "
                f"Sample values from likely columns: "
                f"CONTINENT={sorted(set(gdf['CONTINENT'])) if 'CONTINENT' in gdf.columns else 'N/A'}"
            )

        out_name = output_filename or f"{dataset}_filtered"
        # QGIS cannot open the layer without it
        if not out_name.endswith(GPKG_EXTENSION):
            out_name += GPKG_EXTENSION
        out_path = tool_output_path("output_filename", out_name)
        filtered.to_file(out_path, driver="GPKG")
        return (
            f"✅ {verb} {scale} {dataset} and filtered to {len(filtered)} features "
            f"with {filter_query!r} → outputs/{out_name}"
        )

    except Exception as e:
        return f"ERROR downloading {scale} {dataset}: {str(e)}"


# Required for auto-registration
TOOL_FUNCTION = download_natural_earth_dataset
TOOL_SCHEMA = DownloadNaturalEarthArgs
