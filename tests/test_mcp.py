"""The MCP endpoint: one tool surface, the same gate, over streamable HTTP.

The wire is driven directly (JSON-RPC over POST) rather than through the SDK
client, so what is under test is what an external agent actually sends.

Two constraints shape the harness. The session manager may only be run once per
process, so the client is entered once for the whole module. And the endpoint
checks the Host header, so the client speaks to localhost rather than
TestClient's default `testserver`.
"""

import asyncio
import json
import threading
from contextlib import contextmanager
from datetime import timedelta

import jwt
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import BaseModel
from websockets.asyncio.server import serve

from src.agents.agent_manager import load_external_tools
from src.api import mcp_server, server
from src.api.live_document import DOCUMENT_HEADER
from src.core import agora
from src.core.auth import (
    MAXIMUM_MCP_TOKEN_LIFETIME_SECONDS,
    MCP_CLAIM,
    MCP_CLAIM_VALUE,
    SECRET_ENV,
    sign_platform_token,
)
from src.core.user_token import current_user_token
from tests.test_agora import FakeAgora
from tests.test_route_auth import SECRET, mint

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

VIEWER_CMD = {"action": "fly_to", "params": {"lon": 2.35, "lat": 48.85}}
DOCUMENT_ID = "0f8b1c2d-3e4f-4a5b-8c7d-9e0f1a2b3c4d"


def mcp_mint(**claims):
    """A token for this endpoint, carrying what `POST /mcp/token` puts in one."""
    return mint(**{MCP_CLAIM: MCP_CLAIM_VALUE, **claims})


@pytest.fixture(scope="module")
def client():
    with TestClient(server.app, base_url="http://localhost:8080") as test_client:
        yield test_client


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)


@pytest.fixture
def open_mode(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)


def call(client, method, params=None, token=None, request_id=1, document=None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if document:
        headers[DOCUMENT_HEADER] = document
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers)


def result_of(response):
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]


def text_of(response):
    result = result_of(response)
    assert len(result["content"]) == 1, result
    return result["content"][0]["text"]


# ── the tool surface ─────────────────────────────────────────────────────


def test_list_tools_is_the_manifest(open_mode, client):
    tools = result_of(call(client, "tools/list", {}))["tools"]
    offered = {func.__name__ for func, _ in mcp_server.external_tools()}
    manifest = [t for t in server.tool_manifest() if t["name"] in offered]

    assert len(tools) == len(manifest)
    assert len(tools) > 30
    assert [t["name"] for t in tools] == [t["name"] for t in manifest]
    assert [t["inputSchema"] for t in tools] == [t["parameters"] for t in manifest]
    assert [t["description"] for t in tools] == [t["description"] for t in manifest]


# ── what an external agent is not offered ────────────────────────────────


def test_a_tool_that_runs_caller_code_is_not_listed(open_mode, client):
    """`/chat` keeps sql_query. Here the sql's author is not whose browser runs it."""
    tools = result_of(call(client, "tools/list", {}))["tools"]

    assert "sql_query" in {t["name"] for t in server.tool_manifest()}
    assert "sql_query" not in {t["name"] for t in tools}


def test_a_tool_that_runs_caller_code_cannot_be_called(open_mode, client):
    """Leaving it out of the manifest is not the gate: naming it must fail too."""
    response = call(
        client, "tools/call", {"name": "sql_query", "arguments": {"sql": "SELECT 1"}}
    )

    assert response.json()["error"]["code"] == -32602


def test_the_exclusion_is_the_tool_modules_own_declaration():
    """No name list in the endpoint: the tool declares it, so a new one is caught."""
    loaded = {func.__name__: func for func, _ in load_external_tools()}

    assert mcp_server.runs_caller_code(loaded["sql_query"])
    assert not mcp_server.runs_caller_code(loaded["viewer_control"])


def test_a_tool_runs_and_comes_back_as_text(open_mode, client):
    response = call(
        client,
        "tools/call",
        {"name": "viewer_control", "arguments": {"action": "fly_to", "lon": 2.35, "lat": 48.85}},
    )

    assert result_of(response)["isError"] is False
    assert text_of(response).startswith("__VIEWER_CMD__:")


def test_a_marker_reaches_the_caller_unmodified(open_mode, client):
    """A later phase routes the markers. Until then they must survive the trip."""
    response = call(
        client,
        "tools/call",
        {"name": "viewer_control", "arguments": {"action": "fly_to", "lon": 2.35, "lat": 48.85}},
    )

    marker, _, payload = text_of(response).partition(":")
    assert marker == "__VIEWER_CMD__"
    assert json.loads(payload) == VIEWER_CMD


def test_bad_arguments_come_back_as_an_error_result(open_mode, client):
    # the model has to see the reason and correct itself, so this is a result
    # rather than a protocol error
    response = call(client, "tools/call", {"name": "viewer_control", "arguments": {}})

    assert result_of(response)["isError"] is True
    assert text_of(response).startswith("❌ Invalid arguments")


def test_an_unknown_tool_is_a_protocol_error(open_mode, client):
    response = call(client, "tools/call", {"name": "no_such_tool", "arguments": {}})

    assert response.json()["error"]["code"] == -32602


# ── who the tool runs as ─────────────────────────────────────────────────


def test_the_callers_bearer_is_what_the_tool_acts_as(gated, client, monkeypatch):
    """The token on the MCP request is the one the tool's outbound calls carry."""
    seen = []

    class NoArgs(BaseModel):
        pass

    def record_token():
        """A tool that reports whose token it is running under."""
        seen.append(current_user_token())
        return "ok"

    record_token.__name__ = "record_token"
    monkeypatch.setattr(mcp_server, "load_external_tools", lambda: [(record_token, NoArgs)])

    token = mcp_mint()
    response = call(
        client, "tools/call", {"name": "record_token", "arguments": {}}, token=token
    )

    assert text_of(response) == "ok"
    assert seen == [token]
    # and the scope is not left behind for the next caller
    assert current_user_token() is None


# ── the live document a call can bind itself to ──────────────────────────


@contextmanager
def agora_serving(fake, monkeypatch):
    """`fake` on a loopback port, served from a thread of its own.

    The endpoint answers in the test client's event loop, so the document it
    writes to has to be served from another one.
    """
    ready = threading.Event()
    state = {}

    def serve_until_stopped():
        async def main():
            async with serve(fake.handle, "127.0.0.1", 0) as server:
                state["port"] = server.sockets[0].getsockname()[1]
                state["stop"] = asyncio.Event()
                ready.set()
                await state["stop"].wait()

        state["loop"] = asyncio.new_event_loop()
        state["loop"].run_until_complete(main())

    thread = threading.Thread(target=serve_until_stopped, daemon=True)
    thread.start()
    assert ready.wait(10), "the fake agora never came up"
    monkeypatch.setenv(agora.AGORA_URL_ENV, f"http://127.0.0.1:{state['port']}")
    try:
        yield state["port"]
    finally:
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(10)


def test_a_bound_call_writes_to_the_document_and_still_returns_its_result(
    gated, client, monkeypatch
):
    fake = FakeAgora()

    with agora_serving(fake, monkeypatch) as port:
        with respx.mock(base_url=f"http://127.0.0.1:{port}") as mock:
            grant = mock.put(
                f"/documents/{DOCUMENT_ID}/members/agent:u1"
            ).respond(204)
            response = call(
                client,
                "tools/call",
                {
                    "name": "viewer_control",
                    "arguments": {"action": "fly_to", "lon": 2.35, "lat": 48.85},
                },
                token=mcp_mint(),
                document=DOCUMENT_ID,
            )

    content = result_of(response)["content"]
    # the tool's own text is untouched, the document write is reported beside it
    assert content[0]["text"] == f"__VIEWER_CMD__:{json.dumps(VIEWER_CMD)}"
    assert content[1]["text"] == "Live document: camera moved."
    assert grant.call_count == 1
    assert [frame["type"] for frame in fake.received] == ["presence"]
    assert fake.received[0]["viewport"] == {"center": [2.35, 48.85], "zoom": 16}


def test_a_document_that_cannot_be_written_never_costs_the_result(
    gated, client, monkeypatch
):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://127.0.0.1:1")

    with respx.mock(base_url="http://127.0.0.1:1") as mock:
        mock.put(url__startswith="http://").respond(204)
        response = call(
            client,
            "tools/call",
            {
                "name": "viewer_control",
                "arguments": {"action": "fly_to", "lon": 2.35, "lat": 48.85},
            },
            token=mcp_mint(),
            document=DOCUMENT_ID,
        )

    result = result_of(response)
    assert result["isError"] is False
    assert result["content"][0]["text"].startswith("__VIEWER_CMD__:")
    assert "nothing was written" in result["content"][1]["text"]


def test_an_unbound_call_is_exactly_what_it_was(gated, client, monkeypatch):
    """No header, no document, and nothing added to the result."""
    monkeypatch.setattr(
        mcp_server, "publish", lambda *args: pytest.fail("published without a binding")
    )

    response = call(
        client,
        "tools/call",
        {
            "name": "viewer_control",
            "arguments": {"action": "fly_to", "lon": 2.35, "lat": 48.85},
        },
        token=mcp_mint(),
    )

    assert text_of(response) == f"__VIEWER_CMD__:{json.dumps(VIEWER_CMD)}"


# ── the gate ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, params",
    [
        ("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "0"}}),
        ("tools/list", {}),
        ("tools/call", {"name": "viewer_control", "arguments": {"action": "screenshot"}}),
    ],
)
def test_no_token_is_rejected(gated, client, method, params):
    response = call(client, method, params)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "missing bearer token"}


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        mint(secret="a-different-secret-0123456789abcd"),
        mint(lifetime=timedelta(seconds=-1)),
    ],
)
def test_a_forged_or_expired_token_is_rejected(gated, client, token):
    response = call(client, "tools/list", {}, token=token)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "invalid or expired token"}


def test_a_live_token_gets_through(gated, client):
    tools = result_of(call(client, "tools/list", {}, token=mcp_mint()))["tools"]

    assert len(tools) > 30


def test_without_a_secret_a_tokenless_call_is_served(open_mode, client):
    assert result_of(call(client, "tools/list", {}))["tools"]


# ── the token this endpoint takes ────────────────────────────────────────


def test_a_plain_platform_token_does_not_open_this_door(gated, client):
    """Holding a platform token is not the same as having been given MCP access."""
    response = call(client, "tools/list", {}, token=mint())

    assert response.status_code == 401
    assert response.json() == {"detail": "this endpoint needs a token from POST /mcp/token"}


def test_minting_needs_a_caller_of_its_own(gated, client):
    assert client.post("/mcp/token", json={}).status_code == 401


def test_a_minted_token_is_what_gets_in(gated, client):
    minted = client.post(
        "/mcp/token", json={}, headers={"Authorization": f"Bearer {mint()}"}
    ).json()

    tools = result_of(call(client, "tools/list", {}, token=minted["token"]))["tools"]

    assert len(tools) > 30
    assert minted["expires_at"] > 0


def test_a_minted_token_acts_as_whoever_asked_for_it(gated, client):
    minted = client.post(
        "/mcp/token",
        json={"lifetime_seconds": 3600},
        headers={"Authorization": f"Bearer {mint(sub='someone-else')}"},
    ).json()

    claims = jwt.decode(minted["token"], SECRET, algorithms=["HS256"])

    assert claims["sub"] == "someone-else"
    assert claims[MCP_CLAIM] == MCP_CLAIM_VALUE
    assert claims["exp"] == minted["expires_at"]


@pytest.mark.parametrize(
    "lifetime", [MAXIMUM_MCP_TOKEN_LIFETIME_SECONDS + 1, 0, -60]
)
def test_a_lifetime_outside_the_cap_is_refused(gated, client, lifetime):
    response = client.post(
        "/mcp/token",
        json={"lifetime_seconds": lifetime},
        headers={"Authorization": f"Bearer {mint()}"},
    )

    assert response.status_code == 422


def test_minting_says_so_when_there_is_nothing_to_sign_with(open_mode, client):
    """The authless stack needs no token, so there is nothing to mint one from."""
    response = client.post("/mcp/token", json={})

    assert response.status_code == 503


def test_the_bridges_agent_token_is_not_an_mcp_token(gated):
    """It writes to agora, never back to us, so it must not carry the marker."""
    token = sign_platform_token("agent:u1", "GeoLang agent", 120)

    claims = jwt.decode(token, SECRET, algorithms=["HS256"])

    assert claims["sub"] == "agent:u1"
    assert MCP_CLAIM not in claims


# ── transport ────────────────────────────────────────────────────────────


def test_a_foreign_host_header_is_refused(open_mode, client):
    """DNS-rebinding protection, on unless a deployment names its own host."""
    response = client.post(
        "http://evil.test/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=MCP_HEADERS,
    )

    assert response.status_code == 421


def test_the_standalone_stream_is_declined(open_mode, client):
    """Nothing can be routed to it without a session, so it must not hang open."""
    response = client.get("/mcp", headers={"Accept": "text/event-stream"})

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_the_allowed_hosts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(mcp_server.ALLOWED_HOSTS_ENV, "geo.example.com, other.example.com")

    settings = mcp_server.transport_security_settings()

    assert settings.enable_dns_rebinding_protection
    assert settings.allowed_hosts == ["geo.example.com", "other.example.com"]
    assert "https://geo.example.com" in settings.allowed_origins


def test_without_the_variable_only_localhost_is_allowed(monkeypatch):
    monkeypatch.delenv(mcp_server.ALLOWED_HOSTS_ENV, raising=False)

    settings = mcp_server.transport_security_settings()

    assert settings.allowed_hosts == mcp_server.LOCALHOST_HOST_PATTERNS
