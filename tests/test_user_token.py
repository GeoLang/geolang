"""Per-user identity: the caller's bearer travels to every service a tool calls.

viewer -> /chat/agui -> sibyl -> /tools/{name} -> ptolemy/tiletopia/geodukt. The
token is forwarded opaquely and held only for the call, so these tests check both
ends: what geolang sends sibyl, and what a tool's outbound request carries.
"""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
import respx
from fastapi.testclient import TestClient

from src.api import server
from src.core.user_token import (
    bearer_token,
    current_user_token,
    service_headers,
    user_token_scope,
)

client = TestClient(server.app)

TOKEN = "header.payload.signature"


# ── the header parser ────────────────────────────────────────────────────


def test_bearer_token_reads_the_authorization_header():
    assert bearer_token(f"Bearer {TOKEN}") == TOKEN
    # schemes are case-insensitive per RFC 7235
    assert bearer_token(f"bearer {TOKEN}") == TOKEN


def test_bearer_token_ignores_anything_that_is_not_a_bearer():
    assert bearer_token(None) is None
    assert bearer_token("") is None
    assert bearer_token("Bearer") is None
    assert bearer_token("Bearer ") is None
    assert bearer_token(f"Basic {TOKEN}") is None
    assert bearer_token(TOKEN) is None


# ── the call-scoped context ──────────────────────────────────────────────


def test_the_token_is_only_set_inside_its_scope():
    assert current_user_token() is None
    with user_token_scope(TOKEN):
        assert current_user_token() == TOKEN
    assert current_user_token() is None


def test_the_scope_is_cleared_even_when_the_tool_raises():
    with pytest.raises(RuntimeError), user_token_scope(TOKEN):
        raise RuntimeError("tool blew up")
    assert current_user_token() is None


def test_an_anonymous_scope_does_not_inherit_the_outer_token():
    with user_token_scope(TOKEN):
        with user_token_scope(None):
            assert current_user_token() is None
        assert current_user_token() == TOKEN


# ── outbound headers ─────────────────────────────────────────────────────


def test_no_caller_and_no_fallback_means_no_authorization(monkeypatch):
    monkeypatch.delenv("PTOLEMY_API_TOKEN", raising=False)
    assert service_headers() == {}
    assert service_headers("PTOLEMY_API_TOKEN") == {}


def test_the_callers_token_is_sent():
    with user_token_scope(TOKEN):
        assert service_headers() == {"Authorization": f"Bearer {TOKEN}"}


def test_a_headless_run_falls_back_to_the_service_account(monkeypatch):
    monkeypatch.setenv("PTOLEMY_API_TOKEN", "service-account")
    assert service_headers("PTOLEMY_API_TOKEN") == {"Authorization": "Bearer service-account"}
    # the fallback is opt-in per service, not a blanket default
    assert service_headers() == {}


def test_the_caller_wins_over_the_service_account(monkeypatch):
    monkeypatch.setenv("PTOLEMY_API_TOKEN", "service-account")
    with user_token_scope(TOKEN):
        assert service_headers("PTOLEMY_API_TOKEN") == {"Authorization": f"Bearer {TOKEN}"}


# ── /chat/agui hands the token to sibyl ──────────────────────────────────


def _drain(agen):
    async def run():
        return [event async for event in agen]

    return asyncio.run(run())


def _sibyl_body(route):
    return json.loads(route.calls.last.request.content)


def test_the_run_request_carries_the_callers_token():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        _drain(server.agent_event_stream("buffer the depots", user_token=TOKEN))

    assert _sibyl_body(route)["user_token"] == TOKEN


def test_a_headless_run_sends_no_token_at_all():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        _drain(server.agent_event_stream("buffer the depots"))

    # absent, not null: sibyl treats the field as optional
    assert "user_token" not in _sibyl_body(route)


def test_chat_agui_forwards_the_authorization_header_into_the_run():
    body = {
        "threadId": "t1",
        "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        response = client.post(
            "/chat/agui", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200

    assert _sibyl_body(route)["user_token"] == TOKEN


def test_chat_agui_without_a_header_starts_an_anonymous_run():
    body = {
        "threadId": "t1",
        "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        assert client.post("/chat/agui", json=body).status_code == 200

    assert "user_token" not in _sibyl_body(route)


# ── /tools/{name} puts the token in the tool's context ───────────────────


def _probe_registry(monkeypatch, seen):
    """Register one tool that records the token its call ran under."""
    from pydantic import BaseModel

    class ProbeArgs(BaseModel):
        pass

    def probe():
        """Report the caller this tool call is running as."""
        seen.append(current_user_token())
        return "ok"

    monkeypatch.setattr(server, "load_external_tools", lambda: [(probe, ProbeArgs)])


def test_the_executor_runs_the_tool_as_the_caller(monkeypatch):
    seen = []
    _probe_registry(monkeypatch, seen)

    response = client.post(
        "/tools/probe", json={"args": {}}, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    assert seen == [TOKEN]


def test_a_tool_call_without_a_header_runs_anonymously(monkeypatch):
    seen = []
    _probe_registry(monkeypatch, seen)

    assert client.post("/tools/probe", json={"args": {}}).status_code == 200
    assert seen == [None]


def test_the_token_does_not_outlive_the_tool_call(monkeypatch):
    seen = []
    _probe_registry(monkeypatch, seen)

    client.post(
        "/tools/probe", json={"args": {}}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    client.post("/tools/probe", json={"args": {}})

    # the second call must not inherit the first caller's identity
    assert seen == [TOKEN, None]


def test_the_token_is_not_echoed_back_to_the_caller(monkeypatch):
    seen = []
    _probe_registry(monkeypatch, seen)

    response = client.post(
        "/tools/probe", json={"args": {}}, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert TOKEN not in response.text


# ── ptolemy: the caller's token, or the service account ──────────────────


class _FakePtolemy:
    """requests stand-in that records the headers each call went out with."""

    def __init__(self, payload):
        self.payload = payload
        self.headers = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.headers.append(headers or {})
        return SimpleNamespace(
            status_code=200,
            text=json.dumps(self.payload),
            json=lambda: self.payload,
            raise_for_status=lambda: None,
        )


@pytest.fixture
def ptolemy(monkeypatch, tmp_path):
    # the tool makes an outputs dir under TOOL_EXEC_DIR before it calls anything
    monkeypatch.setenv("TOOL_EXEC_DIR", str(tmp_path))

    def install(payload):
        fake = _FakePtolemy(payload)
        monkeypatch.setitem(sys.modules, "requests", fake)
        return fake

    return install


DATASETS = [{"id": "d1", "name": "parcels", "geometry_type": "POLYGON", "srid": 4326}]


def test_ptolemy_is_queried_as_the_caller(ptolemy, monkeypatch):
    monkeypatch.setenv("PTOLEMY_API_TOKEN", "service-account")
    fake = ptolemy(DATASETS)
    from src.agents.tools.ptolemy_query import ptolemy_query

    with user_token_scope(TOKEN):
        result = ptolemy_query("list_datasets")

    assert "parcels" in result
    assert fake.headers == [{"Authorization": f"Bearer {TOKEN}"}]


def test_a_headless_ptolemy_query_uses_the_service_account(ptolemy, monkeypatch):
    monkeypatch.setenv("PTOLEMY_API_TOKEN", "service-account")
    fake = ptolemy(DATASETS)
    from src.agents.tools.ptolemy_query import ptolemy_query

    ptolemy_query("list_datasets")

    assert fake.headers == [{"Authorization": "Bearer service-account"}]


def test_a_headless_ptolemy_query_without_a_service_account_is_anonymous(
    ptolemy, monkeypatch
):
    monkeypatch.delenv("PTOLEMY_API_TOKEN", raising=False)
    fake = ptolemy(DATASETS)
    from src.agents.tools.ptolemy_query import ptolemy_query

    ptolemy_query("list_datasets")

    assert fake.headers == [{}]
