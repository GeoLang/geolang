# Tests for agents
"""Default-agent resolution: restarts must reuse the existing agent, never mint a new one."""
from types import SimpleNamespace

from src.api import server


class _Agents:
    def __init__(self, known=None, by_name=None):
        self.known = known or {}
        self.by_name = by_name or {}

    def retrieve(self, agent_id):
        if agent_id not in self.known:
            raise RuntimeError(f"no such agent {agent_id}")
        return SimpleNamespace(id=agent_id, name=self.known[agent_id])

    def list(self, name, limit=None):
        return self.by_name.get(name, [])


def _install(monkeypatch, tmp_path, agents, saved_id=None):
    id_file = tmp_path / ".agent_id"
    if saved_id is not None:
        id_file.write_text(saved_id)
    monkeypatch.setattr(server, "AGENT_ID_FILE", str(id_file))
    monkeypatch.setattr(server, "client", SimpleNamespace(agents=agents))


def test_resolves_saved_id(monkeypatch, tmp_path):
    agents = _Agents(known={"agent-saved": "gis-agent"})
    _install(monkeypatch, tmp_path, agents, saved_id="agent-saved")
    assert server._resolve_default_agent() == "agent-saved"


def test_falls_back_to_name_when_saved_id_is_stale(monkeypatch, tmp_path):
    agents = _Agents(
        known={},
        by_name={"gis-agent": [SimpleNamespace(id="agent-live", name="gis-agent")]},
    )
    _install(monkeypatch, tmp_path, agents, saved_id="agent-gone")
    assert server._resolve_default_agent() == "agent-live"


def test_ignores_per_session_agents(monkeypatch, tmp_path):
    agents = _Agents(
        by_name={"gis-agent": [SimpleNamespace(id="agent-sess", name="gis-agent-20260101-000000")]}
    )
    _install(monkeypatch, tmp_path, agents)
    assert server._resolve_default_agent() is None
