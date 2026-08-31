import json
import os
import re
import time
from pydantic import BaseModel, Field
from typing import Optional

from src.core.utils import caller_outputs_dir, tool_input_path_or_none

# a shade-by part has to be one column name, or the viewer has nothing to look
# up in the file's properties
SHADE_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

DEFAULT_LAYER_COLOR = "#3388ff"
# a map call with no layers takes the layers the run just wrote: grok omits
# the argument and then repeats the same call until the run is aborted
RECENT_LAYER_WINDOW_SECONDS = 15 * 60
RECENT_LAYER_LIMIT = 6
RECENT_LAYER_COLORS = ("#3388ff", "#ff6b35", "#2f9e44", "#9c36b5", "#e8590c", "#0c8599")
# grok repeats a refused map call unchanged until sibyl aborts the run
EMPTY_MAP_NOTE = (
    "Emitted a map with no layers: none were given and nothing was written in "
    f"the last {RECENT_LAYER_WINDOW_SECONDS // 60} minutes, so the user is "
    "looking at an empty map. Run the analysis tools to write layer files and "
    "call emit_ui_spec again with 'name|file|color' entries, or answer without "
    "a map. Do not repeat this call unchanged."
)


def recent_output_layers() -> list[tuple[str, str, str, str]]:
    """The caller's freshly written layers, oldest first, as layer entries."""
    directory = caller_outputs_dir()
    now = time.time()
    fresh = []
    for name in os.listdir(directory):
        if not name.endswith(".gpkg"):
            continue
        written = os.path.getmtime(os.path.join(directory, name))
        if now - written > RECENT_LAYER_WINDOW_SECONDS:
            continue
        fresh.append((written, name))
    fresh.sort()
    return [
        (
            name[: -len(".gpkg")].replace("_", " "),
            f"outputs/{name}",
            RECENT_LAYER_COLORS[index % len(RECENT_LAYER_COLORS)],
            "",
        )
        for index, (_, name) in enumerate(fresh[-RECENT_LAYER_LIMIT:])
    ]


class EmitUISpecArgs(BaseModel):
    ui_type: str = Field(
        ...,
        description="'map', 'image', or 'table'.",
    )
    center_lon: Optional[float] = Field(
        None,
        description="Map centre. Required for 'map'.",
    )
    center_lat: Optional[float] = Field(
        None,
        description="Map centre. Required for 'map'.",
    )
    zoom: Optional[int] = Field(
        13,
        description="Map zoom level.",
    )
    layers: Optional[str] = Field(
        None,
        description=(
            "Semicolon-separated layers for 'map', each 'name|file_path|color|"
            "shade_by', e.g. 'Cafes|outputs/cafes.gpkg|#ff0000'. color defaults to "
            "#3388ff. shade_by is one column of that file to shade by instead of "
            "drawing the layer in one colour."
        ),
    )
    image_path: Optional[str] = Field(
        None,
        description="Required for 'image', e.g. 'outputs/map.png'.",
    )
    title: Optional[str] = Field(
        None,
        description="Title for image or table.",
    )
    columns: Optional[str] = Field(
        None,
        description="Semicolon-separated column names for 'table', e.g. 'Name;Score;Rank'.",
    )
    rows: Optional[str] = Field(
        None,
        description=(
            "Rows for 'table', separated by '||' with cells by '|', e.g. "
            "'London|85|1||Paris|72|2'."
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
    """Emit a UI specification for the client to render. Call this after
    producing any map, image, or table output."""

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
                                    str(item.get("color") or DEFAULT_LAYER_COLOR),
                                    str(item.get("shade_by") or "").strip(),
                                ))
                    except json.JSONDecodeError:
                        pass
                if not entries:
                    for layer_str in s.split(";"):
                        parts = [p.strip() for p in layer_str.split("|")]
                        if len(parts) >= 2:
                            parts += [""] * (4 - len(parts))
                            entries.append((parts[0], parts[1], parts[2] or DEFAULT_LAYER_COLOR, parts[3]))
            layers_given = bool((layers or "").strip())
            if not entries and not layers_given:
                entries = recent_output_layers()
            unusable = [
                shade_by
                for _, _, _, shade_by in entries
                if shade_by and not SHADE_FIELD_PATTERN.match(shade_by)
            ]
            if unusable:
                return (
                    f"ERROR: cannot shade by {', '.join(unusable)}. The fourth part of a "
                    "layer entry is one column name in that file, e.g. "
                    "'Gaps|outputs/gaps.gpkg|#ff6b35|gap_score' — not a description, a "
                    "value or a list. Leave it off if no column is worth colouring by."
                )
            layer_list = []
            seen_files = set()
            for name, file, color, shade_by in entries:
                if not file or file in seen_files:
                    continue
                seen_files.add(file)
                layer = {"name": name, "file": file, "color": color}
                if shade_by:
                    layer["shade_by"] = shade_by
                layer_list.append(layer)
            # layers the model wrote out itself and none of them usable: it has
            # something to correct, so say what the format is
            if not layer_list and layers_given:
                return (
                    "ERROR: a map spec needs at least one layer, given as "
                    "'name|file|color' entries separated by ';' "
                    "(e.g. 'Buffer|outputs/buffer.gpkg|#ff6b35'). Generate the layer "
                    "files first; to only move the camera use viewer_control instead."
                )
            # the viewer fetches these by name, so a layer it could not read is
            # reported here rather than rendering as a blank map
            missing = [
                layer["file"]
                for layer in layer_list
                if not tool_input_path_or_none("layers", layer["file"])
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
            rendered = f"__UI_SPEC__:{json.dumps(spec)}"
            if not layer_list:
                return f"{EMPTY_MAP_NOTE}\n{rendered}"
            return rendered

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
