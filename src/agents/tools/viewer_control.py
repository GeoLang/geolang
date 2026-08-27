"""
TileTopia viewer control tool for GeoLang agent.

Emits viewer commands that the TileTopia frontend interprets
to move the camera, show and hide layers, style layers, and so on.

`run` is the only action. It carries the name of an action the viewer listed in
the run's system prompt, so what it may reach is whatever that catalogue held.
`sql_query` is not in the catalogue: it has a tool of its own, which the MCP
surface does not offer.
"""
import json
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ViewerAction = Literal["run"]

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
            "Always 'run'. It runs one of the actions listed under 'Viewer actions' "
            "in the system prompt: give its name in 'name', and that action's own "
            "parameters as further fields of this call"
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
            "Required: the name of one action listed under 'Viewer actions' in the "
            "system prompt, spelled exactly as that list spells it"
        ),
    )
    # advertised as text, since a model given a bare object schema with no
    # properties leaves it empty and puts the values in some other field
    args: Optional[str] = Field(
        None,
        description=(
            "Only when the action's parameters cannot be given as fields of this "
            "call: JSON text of an object holding them, e.g. "
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
        if self.args is not None or self.url is None:
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
        if self.name is None:
            raise ValueError("run needs name")
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
    """Control the TileTopia 3D viewer. The command is sent to the viewer
    frontend which executes it.
    Always call this with action='run', name set to one of the actions listed
    under 'Viewer actions' in the system prompt, and that action's parameters as
    further fields of the same call, spelled as the entry spells them, e.g.
    action='run', name=<an entry from that list>, plus its parameters. Only pass
    args when a parameter cannot be given as a field of this call."""
    named = {
        "lon": lon, "lat": lat, "height": height,
        "heading": heading, "pitch": pitch, "duration": duration,
        "label": label, "color": color, "url": url,
        "attribute": attribute, "iso": iso,
    }
    given = {key: value for key, value in named.items() if value is not None}
    decoded = json.loads(args) if args else {}

    command = {
        "action": "run",
        "params": {"name": name, "args": {**given, **decoded, **parameters}},
    }
    return f"__VIEWER_CMD__:{json.dumps(command)}"


TOOL_FUNCTION = viewer_control
TOOL_SCHEMA = ViewerControlArgs
