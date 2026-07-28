"""agent_event_stream turns sibyl's NDJSON run events into normalized events."""
import asyncio
import json

import respx

from src.agents.agent_manager import PERSONA
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
