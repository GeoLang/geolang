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


def take_a_scalar_out_of_a_wrapper(value, field_name):
    """`{"renderer": "cesium"}` and `{"cesium": null}` both read as "cesium".

    The viewer reads these two shapes for an action's own parameters, and so does
    a one-element list, so anything the tool still reads itself has to read them.
    """
    if isinstance(value, dict) and len(value) == 1:
        only_key, only_value = next(iter(value.items()))
        if only_key == field_name:
            value = only_value
        elif only_value is None:
            value = only_key
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def check_the_scheme_is_allowed(url):
    if not isinstance(url, str) or urlparse(url).scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError("url must be an http or https address")


class ViewerControlArgs(BaseModel):
    # a run's parameters arrive as plain fields of the call, whatever the
    # catalogue entry names them, so the schema has to admit fields not listed here
    model_config = ConfigDict(extra="allow")

    action: ViewerAction = Field(..., description="Always 'run'.")
    name: str = Field(
        ...,
        description=(
            "The action to run: one name from 'Viewer actions' in the system prompt, "
            "spelled as that list spells it, like 'camera.fly_to'. Never a value: for "
            "'switch to satellite' the name is 'basemap.set', not 'satellite'."
        ),
    )
    # advertised as text, since a model given a bare object schema with no
    # properties leaves it empty and puts the values in some other field
    args: Optional[str] = Field(
        None,
        description=(
            "Only for a parameter that cannot be a plain field of this call: JSON text "
            "of an object, e.g. '{\"layer\": \"Parcels\"}'"
        ),
    )

    @field_validator("*", mode="before")
    @classmethod
    def take_a_scalar_out_of_a_one_element_list(cls, value):
        # grok wraps a number or a string in a one-element list
        if isinstance(value, list) and len(value) == 1:
            return value[0]
        return value

    @field_validator("name", mode="before")
    @classmethod
    def take_a_scalar_out_of_an_object_naming_it(cls, value, info):
        return take_a_scalar_out_of_a_wrapper(value, info.field_name)

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
        extra = self.model_extra
        if extra is None or "url" not in extra:
            return self
        url = take_a_scalar_out_of_a_wrapper(extra["url"], "url")
        extra["url"] = url
        if self.args is not None or not isinstance(url, str):
            return self
        try:
            decoded = json.loads(url)
        except json.JSONDecodeError:
            return self
        if isinstance(decoded, dict):
            self.args = url
            del extra["url"]
        return self

    @model_validator(mode="after")
    def check_the_url_is_a_network_address(self):
        extra = self.model_extra or {}
        if extra.get("url") is not None:
            check_the_scheme_is_allowed(extra["url"])
        if self.args is None:
            return self
        # data.import_url, data.load_tileset and sql.attach_url all read args.url
        # and fetch it. none of them reads a url out of a nested object
        decoded = json.loads(self.args)
        if decoded.get("url") is None:
            return self
        url = take_a_scalar_out_of_a_wrapper(decoded["url"], "url")
        check_the_scheme_is_allowed(url)
        decoded["url"] = url
        self.args = json.dumps(decoded)
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
    """Control the TileTopia 3D viewer: the command is sent to the frontend,
    which runs it. Always call with action='run', name set to an action listed
    under 'Viewer actions' in the system prompt, and that action's own parameters
    as further top-level fields of the same call, named exactly as the catalogue
    names them."""
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
