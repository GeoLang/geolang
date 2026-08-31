from pydantic import BaseModel

from src.core.utils import load_catalogue


class ListUserDatasetsArgs(BaseModel):
    pass  # no arguments needed


def list_user_datasets() -> str:
    """
    List the datasets the user has uploaded, with filename, geometry type, row
    count, CRS and column names. Call this whenever the user refers to their own
    data or names a dataset.
    """
    try:
        catalogue = load_catalogue()
    except Exception as e:
        return f"Error reading catalogue: {e}"

    if not catalogue:
        return "No user datasets uploaded yet. Ask the user to upload data via the web interface."

    lines = [f"User has {len(catalogue)} uploaded dataset(s):"]
    for ds in catalogue:
        cols = ds.get("columns", [])
        col_preview = ", ".join(cols[:8]) + ("..." if len(cols) > 8 else "")
        lines.append(
            f"\n  Name: {ds['name']}"
            # the filename is what a tool argument takes: a path would be refused
            f"\n  Filename: {ds['filename']}"
            f"\n  Geometry: {ds['geometry_type']}, {ds['row_count']} features, CRS: {ds['crs']}"
            f"\n  Columns: {col_preview}"
        )
    return "\n".join(lines)


TOOL_FUNCTION = list_user_datasets
TOOL_SCHEMA = ListUserDatasetsArgs
