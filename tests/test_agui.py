"""Tests for the AG-UI endpoint and the shared normalized-event renderers."""
import asyncio
import json

from src.api import server


# a synthetic normalized (kind, payload) sequence shared by the render tests
SYNTHETIC_EVENTS = [
    ("text", "Here is your map."),
    ("progress", "Geocoding Paris…"),
    ("viewer_cmd", {"action": "flyTo", "params": {"center": [2.35, 48.85], "zoom": 12}}),
    ("ui_spec", {"type": "map", "center": [2.35, 48.85], "zoom": 12,
                 "layers": [{"name": "cafes", "file": "outputs/cafes.gpkg"}]}),
]


async def _aiter(items):
    for it in items:
        yield it


def _collect(agen):
    async def run():
        return [frame async for frame in agen]

    return asyncio.run(run())


def _sse_data_objects(frames):
    """Parse the JSON objects out of `data: {...}` SSE frames."""
    objs = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data: "):
                objs.append(json.loads(line[len("data: "):]))
    return objs


def test_agui_stream_maps_events_to_agui_types():
    frames = _collect(
        server.agui_stream(_aiter(SYNTHETIC_EVENTS), thread_id="t1", run_id="r1")
    )
    objs = _sse_data_objects(frames)

    # run start/finish wrapping
    assert objs[0]["type"] == "RUN_STARTED"
    assert objs[0]["threadId"] == "t1" and objs[0]["runId"] == "r1"
    assert objs[-1]["type"] == "RUN_FINISHED"
    assert objs[-1]["threadId"] == "t1" and objs[-1]["runId"] == "r1"

    types = [o["type"] for o in objs]
    assert types == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "CUSTOM",  # progress
        "CUSTOM",  # viewer_cmd
        "CUSTOM",  # ui_spec
        "RUN_FINISHED",
    ]

    # assistant text: one message_id across start/content/end, camelCase keys
    start, content, end = objs[1], objs[2], objs[3]
    assert start["role"] == "assistant"
    assert start["messageId"] == content["messageId"] == end["messageId"]
    assert content["delta"] == "Here is your map."

    # customs carry name/value with the right payloads
    progress, viewer, ui = objs[4], objs[5], objs[6]
    assert progress["name"] == "progress" and progress["value"] == {"text": "Geocoding Paris…"}
    assert viewer["name"] == "viewer_cmd"
    assert viewer["value"] == {"action": "flyTo", "params": {"center": [2.35, 48.85], "zoom": 12}}
    assert ui["name"] == "ui_spec"
    assert ui["value"]["type"] == "map" and ui["value"]["layers"][0]["file"] == "outputs/cafes.gpkg"


def test_agui_stream_maps_error_to_run_error():
    frames = _collect(
        server.agui_stream(_aiter([("error", "boom")]), thread_id="t1", run_id="r1")
    )
    types = [o["type"] for o in _sse_data_objects(frames)]
    assert types == ["RUN_STARTED", "RUN_ERROR", "RUN_FINISHED"]
    err = _sse_data_objects(frames)[1]
    assert err["message"] == "boom"


def test_legacy_stream_unchanged_for_same_sequence():
    frames = _collect(server.legacy_stream(_aiter(SYNTHETIC_EVENTS)))
    objs = _sse_data_objects(frames)
    assert objs == [
        {"type": "text", "text": "Here is your map."},
        {"type": "progress", "text": "Geocoding Paris…"},
        {"type": "viewer_cmd", "cmd": {"action": "flyTo",
                                       "params": {"center": [2.35, 48.85], "zoom": 12}}},
        {"type": "ui_spec", "spec": {"type": "map", "center": [2.35, 48.85], "zoom": 12,
                                     "layers": [{"name": "cafes", "file": "outputs/cafes.gpkg"}]}},
        {"type": "done"},
    ]


def test_routes_registered():
    paths = {r.path for r in server.app.routes}
    assert "/chat/stream" in paths
    assert "/chat/agui" in paths
