import json
import os
from pydantic import BaseModel, Field
from typing import Optional


class EmitUISpecArgs(BaseModel):
    ui_type: str = Field(
        ...,
        description=("Type of UI to render: 'map', 'image', or 'table'."),
    )
    center_lon: Optional[float] = Field(
        None,
        description="Map center longitude. Required for type 'map'.",
    )
    center_lat: Optional[float] = Field(
        None,
        description="Map center latitude. Required for type 'map'.",
    )
    zoom: Optional[int] = Field(
        13,
        description="Map zoom level (default 13).",
    )
    layers: Optional[str] = Field(
        None,
        description=(
            "Semicolon-separated list of layer specs for type 'map'. "
            "Each layer: 'name|file_path|color'. "
            "Example: 'Isochrones|outputs/london_isochrones.gpkg|#3388ff;Cafes|outputs/cafes.gpkg|#ff0000'. "
            "Color is optional (default #3388ff)."
        ),
    )
    image_path: Optional[str] = Field(
        None,
        description="Path to image file. Required for type 'image'. E.g. 'outputs/map.png'.",
    )
    title: Optional[str] = Field(
        None,
        description="Title for image or table.",
    )
    columns: Optional[str] = Field(
        None,
        description="Semicolon-separated column names for type 'table'. E.g. 'Name;Score;Rank'.",
    )
    rows: Optional[str] = Field(
        None,
        description=(
            "Table rows for type 'table'. Rows separated by '||', cells by '|'. "
            "Example: 'London|85|1||Paris|72|2||Berlin|68|3'."
        ),
    )


def emit_ui_spec(
    ui_type: str,
    center_lon: float = None,
    center_lat: float = None,
    zoom: int = 13,
    layers: str = None,
    image_path: str = None,
    title: str = None,
    columns: str = None,
    rows: str = None,
) -> str:
    """Emit a UI specification for the client to render. Call this after generating any map, image, or table output.
    For maps: provide ui_type='map', center_lon, center_lat, zoom, and layers (semicolon-separated).
    For images: provide ui_type='image', image_path, and title.
    For tables: provide ui_type='table', title, columns, and rows."""

    try:
        if ui_type == "map":
            entries = []
            if layers:
                s = layers.strip()
                if s.startswith("["):
                    # models often guess a json array instead of the pipe format
                    try:
                        for item in json.loads(s):
                            if isinstance(item, dict):
                                entries.append((
                                    str(item.get("name", "")),
                                    str(item.get("file") or item.get("file_path") or item.get("path") or ""),
                                    str(item.get("color") or "#3388ff"),
                                ))
                    except json.JSONDecodeError:
                        pass
                if not entries:
                    for layer_str in s.split(";"):
                        parts = [p.strip() for p in layer_str.split("|")]
                        if len(parts) >= 2:
                            parts += ["#3388ff"] * (3 - len(parts))
                            entries.append((parts[0], parts[1], parts[2] or "#3388ff"))
            layer_list = []
            seen_files = set()
            for name, file, color in entries:
                if not file or file in seen_files:
                    continue
                seen_files.add(file)
                layer_list.append({"name": name, "file": file, "color": color})
            # an empty-layer "success" lets the model declare victory over a
            # blank map (grok looped on exactly that), so fail with instructions
            if not layer_list:
                return (
                    "ERROR: a map spec needs at least one layer, given as "
                    "'name|file|color' entries separated by ';' "
                    "(e.g. 'Buffer|outputs/buffer.gpkg|#ff6b35'). Generate the layer "
                    "files first; to only move the camera use viewer_control instead."
                )
            exec_dir = os.environ.get("TOOL_EXEC_DIR", "/app/geolang")
            missing = [
                layer["file"]
                for layer in layer_list
                if not (
                    os.path.exists(layer["file"])
                    or os.path.exists(os.path.join(exec_dir, layer["file"]))
                )
            ]
            if missing:
                return (
                    f"ERROR: layer file(s) not found: {', '.join(missing)}. "
                    "Run the analysis tools to create them first, or call "
                    "list_outputs to see what exists."
                )
            spec = {"type": "map", "layers": layer_list}
            if center_lon is not None and center_lat is not None:
                spec["center"] = [center_lon, center_lat]
                spec["zoom"] = zoom or 13
            return f"__UI_SPEC__:{json.dumps(spec)}"

        elif ui_type == "image":
            spec = {"type": "image", "path": image_path or "", "title": title or ""}
            return f"__UI_SPEC__:{json.dumps(spec)}"

        elif ui_type == "table":
            col_list = [c.strip() for c in columns.split(";")] if columns else []
            row_list = []
            if rows:
                for row_str in rows.split("||"):
                    row_list.append([c.strip() for c in row_str.split("|")])
            spec = {
                "type": "table",
                "title": title or "",
                "columns": col_list,
                "rows": row_list,
            }
            return f"__UI_SPEC__:{json.dumps(spec)}"

        else:
            return (
                f"ERROR: Unknown ui_type '{ui_type}'. Use 'map', 'image', or 'table'."
            )

    except Exception as e:
        return f"ERROR: Failed to build UI spec: {str(e)}"


TOOL_FUNCTION = emit_ui_spec
TOOL_SCHEMA = EmitUISpecArgs
