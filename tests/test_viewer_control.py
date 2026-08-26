"""What the viewer will accept, decided here rather than in the browser.

`action` used to be any string and `url` any string, so the tool would hand the
viewer whatever it was given and the viewer would run whatever it understood.
"""

import json

import pytest
from pydantic import ValidationError

from src.agents.tools.viewer_control import (
    REQUIRED_PARAMETERS,
    ViewerControlArgs,
    viewer_control,
)

PARIS = {"lon": 2.35, "lat": 48.85}


def test_a_known_action_with_its_parameters_is_accepted():
    args = ViewerControlArgs(action="fly_to", **PARIS)

    assert args.action == "fly_to"
    assert viewer_control(**args.model_dump(exclude_unset=True)).startswith(
        "__VIEWER_CMD__:"
    )


@pytest.mark.parametrize("action", ["run_script", "", "FLY_TO", "eval"])
def test_an_action_the_viewer_does_not_have_is_refused(action):
    with pytest.raises(ValidationError):
        ViewerControlArgs(action=action, **PARIS)


def test_sql_query_is_not_reachable_through_this_tool():
    """It has a tool of its own, and that one is kept off the MCP surface."""
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="sql_query")


@pytest.mark.parametrize("action, required", sorted(REQUIRED_PARAMETERS.items()))
def test_an_action_missing_what_it_needs_is_refused(action, required):
    with pytest.raises(ValidationError) as raised:
        ViewerControlArgs(action=action)

    for name in required:
        assert name in str(raised.value)


def test_an_action_that_needs_nothing_takes_nothing():
    assert ViewerControlArgs(action="screenshot").action == "screenshot"


# ── the url the viewer fetches ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:application/json,{}",
        "blob:https://viewer.example.com/1234",
        "file:///etc/passwd",
        "/relative/path.json",
    ],
)
def test_a_url_that_is_not_a_network_address_is_refused(url):
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="add_geojson", url=url)


@pytest.mark.parametrize("url", ["http://example.com/a.json", "https://example.com/a.json"])
def test_an_http_url_is_accepted(url):
    assert ViewerControlArgs(action="add_geojson", url=url).url == url


def test_the_command_carries_the_action_and_parameters_through():
    args = ViewerControlArgs(action="load_tileset", url="https://example.com/t.json")

    _, _, payload = viewer_control(**args.model_dump(exclude_unset=True)).partition(":")

    assert json.loads(payload) == {
        "action": "load_tileset",
        "params": {"url": "https://example.com/t.json"},
    }


# ── the open action, whose name comes from the viewer's own catalogue ─────


def _run_command(**fields) -> dict:
    args = ViewerControlArgs(action="run", **fields)
    _, _, payload = viewer_control(**args.model_dump(exclude_unset=True)).partition(":")
    return json.loads(payload)


def test_run_without_a_name_is_refused():
    with pytest.raises(ValidationError) as raised:
        ViewerControlArgs(action="run", args={"layer": "Parcels"})

    assert "name" in str(raised.value)


def test_run_carries_the_catalogue_name_and_its_arguments():
    assert _run_command(
        name="layers.set_visible", args={"layer": "Parcels", "visible": False}
    ) == {
        "action": "run",
        "params": {
            "name": "layers.set_visible",
            "args": {"layer": "Parcels", "visible": False},
        },
    }


def test_run_without_arguments_sends_an_empty_object():
    assert _run_command(name="live.leave") == {
        "action": "run",
        "params": {"name": "live.leave", "args": {}},
    }


def test_arguments_written_as_json_text_decode_to_an_object():
    args = ViewerControlArgs(
        action="run", name="layers.set_opacity", args='{"layer": "L1", "opacity": 0.4}'
    )

    assert args.args == {"layer": "L1", "opacity": 0.4}


@pytest.mark.parametrize("text", ['["Parcels", false]', '"Parcels"', "Parcels", "42"])
def test_arguments_that_are_not_an_object_are_refused(text):
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="run", name="layers.set_visible", args=text)
