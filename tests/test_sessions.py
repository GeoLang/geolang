"""Session listing prunes entries whose Letta agent is gone, e.g. after a pgdata wipe."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from letta_client import NotFoundError

from src.api import server


def _not_found():
    request = httpx.Request("GET", "http://letta/agents/x")
    return NotFoundError("no such agent", response=httpx.Response(404, request=request), body=None)


class _Agents:
    def __init__(self, known, error=None):
        self.known = known
        self.error = error

    def retrieve(self, agent_id):
        if agent_id not in self.known:
            raise self.error or _not_found()
        return SimpleNamespace(id=agent_id)


def _install(monkeypatch, tmp_path, agents, sessions):
    sessions_file = tmp_path / ".sessions.json"
    sessions_file.write_text(json.dumps(sessions))
    monkeypatch.setattr("src.core.utils.SESSIONS_FILE", str(sessions_file))
    monkeypatch.setattr(server, "AGENT_ID_FILE", str(tmp_path / ".agent_id"))
    monkeypatch.setattr(server, "client", SimpleNamespace(agents=agents))
    monkeypatch.setattr(server, "agent_id", "agent-live")
    return sessions_file


def _stored(sessions_file):
    return list(json.loads(sessions_file.read_text()))


TWO_SESSIONS = {
    "agent-live": {"name": "Live", "created_at": "2026-01-02"},
    "agent-gone": {"name": "Gone", "created_at": "2026-01-01"},
}


def test_list_prunes_dead_sessions(monkeypatch, tmp_path):
    sessions_file = _install(monkeypatch, tmp_path, _Agents(known={"agent-live"}), TWO_SESSIONS)

    result = asyncio.run(server.list_sessions())

    assert [s["id"] for s in result] == ["agent-live"]
    assert _stored(sessions_file) == ["agent-live"]


def test_list_keeps_sessions_when_letta_is_down(monkeypatch, tmp_path):
    agents = _Agents(known=set(), error=RuntimeError("connection refused"))
    sessions_file = _install(monkeypatch, tmp_path, agents, TWO_SESSIONS)

    with pytest.raises(RuntimeError):
        asyncio.run(server.list_sessions())

    assert _stored(sessions_file) == ["agent-live", "agent-gone"]


def test_switch_to_dead_session_404s_and_prunes(monkeypatch, tmp_path):
    sessions_file = _install(monkeypatch, tmp_path, _Agents(known={"agent-live"}), TWO_SESSIONS)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.switch_session(server.SwitchSessionRequest(session_id="agent-gone")))

    assert exc.value.status_code == 404
    assert _stored(sessions_file) == ["agent-live"]
    assert server.agent_id == "agent-live"
