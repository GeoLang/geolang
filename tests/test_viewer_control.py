"""What the viewer will accept, decided here rather than in the browser.

`run` is the only action, and its name comes from the catalogue the viewer sends
with the message, so this file guards the two things the tool still decides: the
url the viewer is allowed to fetch, and that every parameter the model wrote
reaches the command.
"""

import json

import pytest
from pydantic import ValidationError

from src.agents.tools.viewer_control import ViewerControlArgs, viewer_control

PARIS = {"lon": 2.35, "lat": 48.85}


def test_run_is_accepted():
    args = ViewerControlArgs(action="run", name="camera.fly_to", **PARIS)

    assert args.action == "run"
    assert viewer_control(**args.model_dump(exclude_unset=True)).startswith(
        "__VIEWER_CMD__:"
    )


@pytest.mark.parametrize(
    "action", ["fly_to", "add_marker", "load_tileset", "run_script", "", "RUN", "eval"]
)
def test_an_action_other_than_run_is_refused(action):
    with pytest.raises(ValidationError):
        ViewerControlArgs(action=action, name="camera.fly_to", **PARIS)


def test_sql_query_is_not_reachable_through_this_tool():
    """It has a tool of its own, and that one is kept off the MCP surface."""
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="sql_query")


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
        ViewerControlArgs(action="run", name="data.import_url", url=url)


@pytest.mark.parametrize(
    "url",
    [
        ["javascript:alert(1)"],
        {"url": "file:///etc/passwd"},
        {"data:text/html,x": None},
        42,
    ],
)
def test_a_url_inside_a_wrapper_is_read_out_of_it_and_refused(url):
    """url is no longer a declared field, so the guard reads the wrappers itself."""
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="run", name="data.import_url", url=url)


def test_a_url_that_is_json_text_but_not_an_object_is_still_refused():
    """Only an object moves into args, so this one reaches the scheme check."""
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="run", name="data.import_url", url='"javascript:alert(1)"')


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///etc/passwd", ["javascript:alert(1)"]]
)
def test_a_url_inside_the_argument_object_is_refused(url):
    """The viewer reads args.url and fetches it, so this path is checked too."""
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="run", name="data.import_url", args={"url": url})


def test_a_url_written_into_url_as_an_argument_object_is_refused():
    """The object moves into args, and the check follows it there."""
    with pytest.raises(ValidationError):
        ViewerControlArgs(
            action="run", name="data.import_url", url='{"url": "javascript:alert(1)"}'
        )


@pytest.mark.parametrize("url", ["http://example.com/a.json", "https://example.com/a.json"])
def test_an_http_url_is_accepted(url):
    assert ViewerControlArgs(action="run", name="data.import_url", url=url).url == url


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


def test_arguments_written_as_json_text_reach_the_viewer_as_an_object():
    assert _run_command(name="layers.set_opacity", args='{"layer": "L1", "opacity": 0.4}') == {
        "action": "run",
        "params": {"name": "layers.set_opacity", "args": {"layer": "L1", "opacity": 0.4}},
    }


def test_run_parameters_given_as_plain_fields_reach_the_viewer():
    assert _run_command(name="layers.set_visible", layer="Parcels", visible=False) == {
        "action": "run",
        "params": {"name": "layers.set_visible", "args": {"layer": "Parcels", "visible": False}},
    }


def test_run_parameters_written_under_the_old_field_names_reach_the_viewer():
    """The tool no longer declares lon, lat or height, and still carries them."""
    assert _run_command(name="camera.fly_to", **PARIS, height=800) == {
        "action": "run",
        "params": {
            "name": "camera.fly_to",
            "args": {"lon": 2.35, "lat": 48.85, "height": 800},
        },
    }


def test_an_argument_object_wins_over_a_field_of_the_same_name():
    assert _run_command(name="camera.fly_to", lon=0.0, args='{"lon": 2.35, "lat": 48.85}') == {
        "action": "run",
        "params": {"name": "camera.fly_to", "args": {"lon": 2.35, "lat": 48.85}},
    }


def test_run_parameters_written_into_url_are_taken_as_the_arguments():
    """grok writes the object into url, whatever the schema says."""
    assert _run_command(name="basemap.set", url='{"basemap": "satellite"}') == {
        "action": "run",
        "params": {"name": "basemap.set", "args": {"basemap": "satellite"}},
    }


def test_an_http_url_inside_the_argument_object_reaches_the_viewer():
    assert _run_command(
        name="data.import_url", args={"url": ["https://example.com/a.json"], "layer": "L1"}
    ) == {
        "action": "run",
        "params": {
            "name": "data.import_url",
            "args": {"url": "https://example.com/a.json", "layer": "L1"},
        },
    }


def test_a_url_reaches_the_viewer_as_an_argument():
    assert _run_command(name="data.import_url", url="https://example.com/a.json") == {
        "action": "run",
        "params": {
            "name": "data.import_url",
            "args": {"url": "https://example.com/a.json"},
        },
    }


def test_the_manifest_admits_fields_it_does_not_list():
    assert ViewerControlArgs.model_json_schema()["additionalProperties"] is True


def test_the_manifest_advertises_args_as_text():
    """A bare object schema with no properties is one a model leaves empty."""
    assert ViewerControlArgs.model_json_schema()["properties"]["args"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]


@pytest.mark.parametrize("text", ['["Parcels", false]', '"Parcels"', "Parcels", "42"])
def test_arguments_that_are_not_an_object_are_refused(text):
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="run", name="layers.set_visible", args=text)


# ── the shapes a model writes the action name in ─────────────────────────


@pytest.mark.parametrize(
    "written",
    [
        "camera.fly_to",
        ["camera.fly_to"],
        {"name": "camera.fly_to"},
        {"name": ["camera.fly_to"]},
        {"camera.fly_to": None},
    ],
)
def test_the_name_is_read_out_of_every_shape_a_model_writes_it_in(written):
    assert ViewerControlArgs(action="run", name=written).name == "camera.fly_to"


def test_a_name_written_as_a_two_element_list_is_refused():
    with pytest.raises(ValidationError):
        ViewerControlArgs(action="run", name=["camera.fly_to", "camera.set_pitch"])
