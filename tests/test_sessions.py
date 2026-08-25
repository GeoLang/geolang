"""The session endpoints are thin proxies over sibyl, which owns session state."""
import asyncio
import json

import httpx
import pytest
import respx
from fastapi import HTTPException

from src.api import server

SESSIONS = [
    {"id": "s2", "name": "Session 2", "created_at": "2026-01-02", "active": True},
    {"id": "s1", "name": "Session 1", "created_at": "2026-01-01", "active": False},
]

BEARER = "Bearer head.payload.sig"


def test_list_forwards_sibyls_sessions():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.get("/sessions").respond(200, json=SESSIONS)

        assert asyncio.run(server.list_sessions()) == SESSIONS


def test_new_names_the_session_after_the_existing_count():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.get("/sessions").respond(200, json=SESSIONS)
        created = {
            "id": "s3",
            "name": "Session 3",
            "created_at": "2026-01-03",
            "active": True,
        }
        route = sibyl.post("/sessions").respond(200, json=created)

        result = asyncio.run(server.create_session())

    assert result == {"id": "s3", "name": "Session 3"}
    assert json.loads(route.calls.last.request.content) == {"name": "Session 3"}


def test_delete_of_the_active_session_stays_a_400():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.delete("/sessions/s2").respond(400, json={"detail": "session is active"})

        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.delete_session("s2"))

    assert exc.value.status_code == 400
    assert "Cannot delete the active session" in exc.value.detail


def test_switch_to_an_unknown_session_is_a_404():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.post("/sessions/gone/activate").respond(404)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.switch_session(server.SwitchSessionRequest(session_id="gone")))

    assert exc.value.status_code == 404


def test_rename_of_someone_elses_session_is_a_404():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.patch("/sessions/theirs").respond(404, json={"error": "session not found"})

        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                server.rename_session(
                    "theirs", server.RenameSessionRequest(name="stolen"), BEARER
                )
            )

    assert exc.value.status_code == 404


def test_delete_of_someone_elses_session_is_a_404():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.delete("/sessions/theirs").respond(404, json={"error": "session not found"})

        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.delete_session("theirs", BEARER))

    assert exc.value.status_code == 404


def test_sibyl_being_down_is_a_503():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.get("/sessions").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.list_sessions())

    assert exc.value.status_code == 503
    assert "unreachable" in exc.value.detail


def test_every_session_route_calls_sibyl_as_the_caller():
    """sibyl decides ownership from this header, so a dropped one loses the session."""
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        listed = sibyl.get("/sessions").respond(200, json=SESSIONS)
        created = sibyl.post("/sessions").respond(
            200, json={"id": "s3", "name": "Session 3"}
        )
        activated = sibyl.post("/sessions/s1/activate").respond(
            200, json={"id": "s1", "name": "Session 1"}
        )
        renamed = sibyl.patch("/sessions/s1").respond(200, json={"id": "s1"})
        deleted = sibyl.delete("/sessions/s1").respond(200, json={"deleted": "s1"})

        asyncio.run(server.list_sessions(BEARER))
        asyncio.run(server.create_session(BEARER))
        asyncio.run(
            server.switch_session(server.SwitchSessionRequest(session_id="s1"), BEARER)
        )
        asyncio.run(
            server.rename_session("s1", server.RenameSessionRequest(name="new"), BEARER)
        )
        asyncio.run(server.delete_session("s1", BEARER))

        for route in (listed, created, activated, renamed, deleted):
            assert route.calls.last.request.headers["authorization"] == BEARER


def test_a_note_to_the_agent_carries_the_callers_bearer():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/sessions/s1/messages").respond(204)

        asyncio.run(server.notify_agent("[Dataset uploaded] roads", "s1", BEARER))

        assert route.calls.last.request.headers["authorization"] == BEARER
        assert json.loads(route.calls.last.request.content) == {
            "content": "[Dataset uploaded] roads"
        }


def test_a_note_without_a_thread_never_reaches_sibyl():
    with respx.mock(base_url=server.SIBYL_URL, assert_all_called=False) as sibyl:
        route = sibyl.post(path__startswith="/sessions").respond(204)

        asyncio.run(server.notify_agent("nowhere to put this", None, BEARER))

        assert not route.called
