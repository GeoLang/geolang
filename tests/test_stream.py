"""agent_event_stream turns sibyl's NDJSON run events into normalized events."""
import asyncio
import json

import respx

from src.agents.agent_manager import PERSONA
from src.agents.tools import a2ui
from src.api import server


def _ndjson(*events):
    return "".join(json.dumps(e) + "\n" for e in events).encode()


def _collect(message="show me Paris"):
    async def run():
        return [event async for event in server.agent_event_stream(message)]

    return asyncio.run(run())


def test_run_events_map_to_normalized_events():
    body = _ndjson(
        {"kind": "tool_call", "name": "geocode_place", "args": '{"place_name": "Paris"}'},
        {"kind": "tool_return", "name": "viewer_control",
         "content": '__VIEWER_CMD__:{"action": "fly_to", "params": {"lon": 2.35}}'},
        {"kind": "tool_return", "name": "export_to_gpkg", "content": "❌ No such file"},
        {"kind": "tool_return", "name": "emit_ui_spec",
         "content": '__UI_SPEC__:{"type": "map", "layers": [{"file": "outputs/x.gpkg"}]}'},
        {"kind": "text", "content": "Done."},
        {"kind": "done"},
    )
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(200, content=body)
        events = _collect()

    assert events == [
        ("progress", "Geocoding Paris…"),
        ("viewer_cmd", {"action": "fly_to", "params": {"lon": 2.35}}),
        ("progress", "❌ No such file"),
        ("text", "Done."),
        ("ui_spec", {"type": "map", "layers": [{"file": "outputs/x.gpkg"}]}),
    ]

    sent = json.loads(route.calls.last.request.content)
    assert sent == {"system_prompt": PERSONA, "message": "show me Paris"}


def test_error_event_ends_the_stream():
    body = _ndjson({"kind": "error", "message": "model exploded"}, {"kind": "text", "content": "unreachable"})
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        events = _collect()

    assert events == [("error", "model exploded")]


def test_empty_map_note_still_yields_a_ui_spec():
    body = _ndjson(
        {"kind": "tool_return", "name": "emit_ui_spec",
         "content": a2ui.EMPTY_MAP_NOTE + '\n__UI_SPEC__:{"type": "map", "layers": []}'},
        {"kind": "text", "content": "Nothing to draw yet."},
        {"kind": "done"},
    )
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        events = _collect()

    assert events == [
        ("text", "Nothing to draw yet."),
        ("ui_spec", {"type": "map", "layers": []}),
    ]


def _events_for(*run_events):
    body = _ndjson(*run_events, {"kind": "done"})
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/runs").respond(200, content=body)
        return _collect()


def _ui_specs(events):
    return [payload for kind, payload in events if kind == "ui_spec"]


def test_a_reply_naming_one_written_layer_maps_only_that_layer():
    events = _events_for(
        {"kind": "tool_return", "name": "clip_layer",
         "content": "Saved to outputs/roads.gpkg. Saved to outputs/rivers.gpkg."},
        {"kind": "text", "content": "Clipped the roads into roads.gpkg."},
    )

    assert _ui_specs(events) == [
        {"type": "map", "layers": [{"name": "roads", "file": "outputs/roads.gpkg"}]}
    ]


def test_a_reply_naming_no_layer_sends_no_map():
    events = _events_for(
        {"kind": "tool_return", "name": "clip_layer",
         "content": "Saved to outputs/roads.gpkg."},
        {"kind": "text", "content": "Here is a summary of the roads."},
    )

    assert _ui_specs(events) == []


def test_files_named_by_list_outputs_never_reach_the_map():
    events = _events_for(
        {"kind": "tool_return", "name": "list_outputs",
         "content": "Output files (2):\n  outputs/old_buffer.gpkg (12 KB)\n"
                    "  outputs/old_parks.gpkg (40 KB)"},
        {"kind": "text", "content": "You have old_buffer.gpkg and old_parks.gpkg."},
    )

    assert _ui_specs(events) == []
