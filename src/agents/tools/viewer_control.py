"""
TileTopia viewer control tool for GeoLang agent.

Emits viewer commands that the TileTopia frontend interprets
to fly to locations, show/hide layers, toggle classification, etc.

`action` is closed over what the viewer's command registry actually implements,
so a name it would only log and drop never leaves here, and a caller cannot
reach for a command by writing one. `sql_query` is deliberately absent: it has a
tool of its own, which the MCP surface does not offer.

`run` is the one open action. It carries the name of an action the viewer listed
in the run's system prompt, so what it may reach is whatever that catalogue held.
"""
import json
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ViewerAction = Literal[
    "run",
    "fly_to",
    "set_view",
    "add_marker",
    "clear_entities",
    "add_geojson",
    "load_tileset",
    "screenshot",
    "switch_tab",
    "switch_renderer",
    "add_heatmap",
    "add_hexbin",
    "add_arcs",
    "add_scatter",
    "add_screengrid",
    "style_by_height",
    "style_by_classification",
    "style_by_property",
    "measure_distance",
    "measure_area",
    "measure_height",
    "annotate",
    "terrain_profile",
    "show_timeline",
    "split_view",
    "viewshed",
    "volume",
    "slope_map",
    "aspect_map",
    "contour_lines",
    "shadow_analysis",
    "load_google_3d",
    "import_model",
    "weather",
    "traffic",
    "flood",
    "save_bookmark",
    "play_story",
]

# what the viewer cannot do anything without, so the model is told here rather
# than by a command that silently does nothing
REQUIRED_PARAMETERS: dict[str, tuple[str, ...]] = {
    "run": ("name",),
    "fly_to": ("lon", "lat"),
    "set_view": ("lon", "lat"),
    "add_marker": ("lon", "lat"),
    "add_geojson": ("url",),
    "load_tileset": ("url",),
    "style_by_property": ("attribute",),
}

# the viewer fetches this url, so anything that is not a plain network address
# is a way to hand its origin a payload instead
ALLOWED_URL_SCHEMES = {"http", "https"}


class ViewerControlArgs(BaseModel):
    # a run's parameters arrive as plain fields of the call, whatever the
    # catalogue entry names them, so the schema has to admit fields not listed here
    model_config = ConfigDict(extra="allow")

    action: ViewerAction = Field(
        ...,
        description=(
            "Viewer action to perform. Common ones: "
            "'run' (requires name, plus that action's own parameters as further "
            "fields of this call): runs one of the actions listed under 'Viewer "
            "actions' in the system prompt, "
            "'fly_to' (requires lon, lat), "
            "'set_view' (lon, lat, heading, pitch), "
            "'add_marker' (lon, lat, label, color), "
            "'clear_entities', "
            "'load_tileset' (url, label), "
            "'style_by_classification', "
            "'add_geojson' (url, color, label), "
            "'screenshot'"
        ),
    )
    lon: Optional[float] = Field(None, description="Longitude")
    lat: Optional[float] = Field(None, description="Latitude")
    height: Optional[float] = Field(None, description="Camera height in metres (default 1000)")
    heading: Optional[float] = Field(None, description="Camera heading in degrees")
    pitch: Optional[float] = Field(None, description="Camera pitch in degrees")
    duration: Optional[float] = Field(None, description="Flight duration in seconds")
    label: Optional[str] = Field(None, description="Label text for marker or tileset")
    color: Optional[str] = Field(None, description="CSS colour string, e.g. '#ff0000'")
    url: Optional[str] = Field(None, description="http or https URL for tileset or GeoJSON")
    attribute: Optional[str] = Field(None, description="Attribute name for classification (default 'Classification')")
    iso: Optional[str] = Field(None, description="ISO 8601 date string for time slider")
    name: Optional[str] = Field(
        None,
        description=(
            "For action='run': the name of one action listed under 'Viewer actions' "
            "in the system prompt, e.g. 'layers.set_visible'"
        ),
    )
    # advertised as text, since a model given a bare object schema with no
    # properties leaves it empty and puts the values in some other field
    args: Optional[str] = Field(
        None,
        description=(
            "For action='run', only when the parameters cannot be given as fields "
            "of this call: JSON text of an object holding them, e.g. "
            "'{\"layer\": \"Parcels\", \"visible\": false}'"
        ),
    )

    @field_validator("args", mode="before")
    @classmethod
    def keep_args_as_json_text_of_an_object(cls, value):
        if value is None:
            return None
        if isinstance(value, dict):
            return json.dumps(value)
        if not isinstance(value, str):
            raise ValueError("args must be JSON text holding an object")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError("args must be JSON text holding an object")
        if not isinstance(decoded, dict):
            raise ValueError("args must be JSON text holding an object")
        return value

    @model_validator(mode="after")
    def take_run_arguments_written_into_url(self):
        # grok writes the run's argument object into url, whatever the schema says
        if self.action != "run" or self.args is not None or self.url is None:
            return self
        try:
            decoded = json.loads(self.url)
        except json.JSONDecodeError:
            return self
        if isinstance(decoded, dict):
            self.args = self.url
            self.url = None
        return self

    @model_validator(mode="after")
    def check_action_is_usable(self):
        missing = [
            name
            for name in REQUIRED_PARAMETERS.get(self.action, ())
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"{self.action} needs {', '.join(missing)}")
        if self.url is not None and urlparse(self.url).scheme not in ALLOWED_URL_SCHEMES:
            raise ValueError("url must be an http or https address")
        return self


def viewer_control(
    action: str,
    lon: float = None,
    lat: float = None,
    height: float = None,
    heading: float = None,
    pitch: float = None,
    duration: float = None,
    label: str = None,
    color: str = None,
    url: str = None,
    attribute: str = None,
    iso: str = None,
    name: str = None,
    args: str = None,
    **parameters,
) -> str:
    """Control the TileTopia 3D viewer. Use this to fly the camera to a location,
    add markers, load tilesets, apply point cloud classification colours, etc.
    The command is sent to the viewer frontend which executes it.
    After geocoding a place, call this with action='fly_to' and the coordinates.
    To change anything else about the viewer, call this with action='run', name set
    to one of the actions listed under 'Viewer actions' in the system prompt, and
    that action's parameters as further fields of the same call, spelled as the
    entry spells them, e.g. action='run', name='layers.set_visible',
    layer='Parcels', visible=false."""
    if action == "run":
        decoded = json.loads(args) if args else {}
        command = {"action": "run", "params": {"name": name, "args": {**decoded, **parameters}}}
        return f"__VIEWER_CMD__:{json.dumps(command)}"

    params = {}
    for key, val in [
        ("lon", lon), ("lat", lat), ("height", height),
        ("heading", heading), ("pitch", pitch), ("duration", duration),
        ("label", label), ("color", color), ("url", url),
        ("attribute", attribute), ("iso", iso),
    ]:
        if val is not None:
            params[key] = val

    cmd = {"action": action, "params": params}
    return f"__VIEWER_CMD__:{json.dumps(cmd)}"


TOOL_FUNCTION = viewer_control
TOOL_SCHEMA = ViewerControlArgs
