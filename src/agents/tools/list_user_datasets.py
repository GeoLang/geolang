from pydantic import BaseModel


class ListUserDatasetsArgs(BaseModel):
    pass  # no arguments needed


def list_user_datasets() -> str:
    """
    List all datasets the user has uploaded, including file paths, geometry type,
    row count, CRS, and column names. Call this whenever the user refers to their
    own data or a specific dataset by name.
    """
    import os
    import json

    exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
    catalogue_path = os.path.join(exec_dir, "user_data", "catalogue.json")

    if not os.path.exists(catalogue_path):
        return "No user datasets uploaded yet. Ask the user to upload data via the web interface."

    try:
        with open(catalogue_path) as f:
            catalogue = json.load(f)
    except Exception as e:
        return f"Error reading catalogue: {e}"

    if not catalogue:
        return "No user datasets uploaded yet."

    lines = [f"User has {len(catalogue)} uploaded dataset(s):"]
    for ds in catalogue:
        cols = ds.get("columns", [])
        col_preview = ", ".join(cols[:8]) + ("..." if len(cols) > 8 else "")
        full_path = os.path.join(exec_dir, ds["relative_path"])
        lines.append(
            f"\n  Name: {ds['name']}"
            f"\n  Path: {full_path}"
            f"\n  Geometry: {ds['geometry_type']}, {ds['row_count']} features, CRS: {ds['crs']}"
            f"\n  Columns: {col_preview}"
        )
    return "\n".join(lines)


TOOL_FUNCTION = list_user_datasets
TOOL_SCHEMA = ListUserDatasetsArgs
