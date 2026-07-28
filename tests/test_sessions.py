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


def test_sibyl_being_down_is_a_503():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.get("/sessions").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(HTTPException) as exc:
            asyncio.run(server.list_sessions())

    assert exc.value.status_code == 503
    assert "unreachable" in exc.value.detail
