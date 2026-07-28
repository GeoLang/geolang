"""
TileTopia viewer control tool for GeoLang agent.

Emits viewer commands that the TileTopia frontend interprets
to fly to locations, show/hide layers, toggle classification, etc.
"""
import json
from pydantic import BaseModel, Field
from typing import Optional


class ViewerControlArgs(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Viewer action to perform. One of: "
            "'fly_to' (requires lon, lat), "
            "'set_view' (lon, lat, heading, pitch), "
            "'add_marker' (lon, lat, label, color), "
            "'clear_entities', "
            "'load_tileset' (url, label), "
            "'classify' (attribute), "
            "'add_geojson' (url, color, label), "
            "'set_time' (iso), "
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
    url: Optional[str] = Field(None, description="URL for tileset or GeoJSON")
    attribute: Optional[str] = Field(None, description="Attribute name for classification (default 'Classification')")
    iso: Optional[str] = Field(None, description="ISO 8601 date string for time slider")


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
) -> str:
    """Control the TileTopia 3D viewer. Use this to fly the camera to a location,
    add markers, load tilesets, apply point cloud classification colours, etc.
    The command is sent to the viewer frontend which executes it.
    After geocoding a place, call this with action='fly_to' and the coordinates."""
    # NOTE: tools run in the geolang process now, but imports stay inside the
    # function body so each tool stays self-contained.
    import json

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
