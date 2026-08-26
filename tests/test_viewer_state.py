"""What the viewer tells the model about itself, and how it reaches the run.

The catalogue is the viewer's, not this repo's, so these tests pin the rendering
rather than any particular action name.
"""

import asyncio
import json

import respx
from fastapi.testclient import TestClient

from src.agents.agent_manager import PERSONA
from src.api import server
from src.api.viewer_state import system_prompt_for

SET_VISIBLE = {
    "name": "layers.set_visible",
    "description": "Show or hide a layer",
    "parameters": {
        "type": "object",
        "properties": {
            "layer": {"type": "string", "description": "layer id or name"},
            "visible": {"type": "boolean"},
        },
        "required": ["layer", "visible"],
    },
    "reads": False,
    "destructive": False,
}

BASEMAP_SET = {
    "name": "basemap.set",
    "description": "Switch the basemap",
    "parameters": {
        "type": "object",
        "properties": {
            "basemap": {"type": "string", "enum": ["osm", "satellite", "dark"]},
            "fade": {"type": "boolean"},
        },
        "required": ["basemap"],
    },
}

VIEWER = {"basemap": "osm", "layers": [{"id": "l1", "name": "Parcels"}]}


def _state(*actions, viewer=VIEWER):
    return {"viewer": viewer, "actions": list(actions)}


def _section(prompt: str, heading: str) -> str:
    """The lines under a heading, up to the blank line that ends the section."""
    body = prompt.split(f"{heading}\n", 1)[1]
    return body.split("\n\n", 1)[0]


# ── no catalogue, no section ─────────────────────────────────────────────


def test_no_state_is_the_persona_unchanged():
    assert system_prompt_for(None) == PERSONA


def test_a_state_with_no_catalogue_is_the_persona_unchanged():
    assert system_prompt_for({}) == PERSONA
    assert system_prompt_for({"viewer": VIEWER}) == PERSONA
    assert system_prompt_for({"viewer": VIEWER, "actions": []}) == PERSONA
    assert system_prompt_for({"actions": "layers.set_visible"}) == PERSONA
    assert system_prompt_for("layers.set_visible") == PERSONA


# ── the sections the model reads ─────────────────────────────────────────


def test_the_viewer_snapshot_travels_as_compact_json():
    prompt = system_prompt_for(_state(SET_VISIBLE))

    assert _section(prompt, "Viewer state:") == json.dumps(
        VIEWER, separators=(",", ":")
    )
    assert prompt.startswith(PERSONA)


def test_a_catalogue_with_no_viewer_renders_an_empty_snapshot():
    prompt = system_prompt_for({"actions": [SET_VISIBLE]})

    assert _section(prompt, "Viewer state:") == "{}"


def test_every_action_is_one_line_naming_its_parameters():
    prompt = system_prompt_for(_state(SET_VISIBLE, BASEMAP_SET))

    assert _section(prompt, "Viewer actions:").splitlines() == [
        "layers.set_visible(layer: string, visible: boolean): Show or hide a layer",
        "basemap.set(basemap: osm|satellite|dark, fade?: boolean): Switch the basemap",
    ]


def test_an_action_that_takes_nothing_still_shows_its_parentheses():
    nothing = {"name": "scenario.stop", "description": "Stop comparing"}

    line = _section(system_prompt_for(_state(nothing)), "Viewer actions:")

    assert line == "scenario.stop(): Stop comparing"


def test_a_read_action_and_a_destructive_one_are_marked():
    listing = {"name": "project.list", "description": "List projects", "reads": True}
    leaving = {"name": "live.leave", "description": "Leave", "destructive": True}

    lines = _section(
        system_prompt_for(_state(listing, leaving)), "Viewer actions:"
    ).splitlines()

    assert lines == [
        "project.list(): List projects [reads]",
        "live.leave(): Leave [asks to confirm]",
    ]


def test_the_instructions_name_the_tool_call_and_the_read_answer():
    prompt = system_prompt_for(_state(SET_VISIBLE))

    assert "action='run'" in prompt
    assert "Result of <name>:" in prompt
    assert "find_feature" in prompt


# ── the state reaches the run ────────────────────────────────────────────


def _sibyl_body(route):
    return json.loads(route.calls.last.request.content)


def _drain(agen):
    async def run():
        return [event async for event in agen]

    return asyncio.run(run())


AGUI_BODY = {
    "threadId": "t1",
    "runId": "r1",
    "messages": [{"id": "m1", "role": "user", "content": "hide the parcels"}],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}


def _done_run(sibyl):
    return sibyl.post("/runs").respond(
        200, content=json.dumps({"kind": "done"}) + "\n"
    )


def test_chat_agui_puts_the_viewers_catalogue_in_the_system_prompt():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = _done_run(sibyl)
        response = TestClient(server.app).post(
            "/chat/agui", json=dict(AGUI_BODY, state=_state(SET_VISIBLE))
        )
        assert response.status_code == 200

    assert _sibyl_body(route)["system_prompt"] == system_prompt_for(_state(SET_VISIBLE))


def test_a_run_with_no_state_sends_the_persona():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = _done_run(sibyl)
        _drain(server.agent_event_stream("hide the parcels"))

    assert _sibyl_body(route)["system_prompt"] == PERSONA
