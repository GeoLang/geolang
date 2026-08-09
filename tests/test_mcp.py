"""The MCP endpoint: one tool surface, the same gate, over streamable HTTP.

The wire is driven directly (JSON-RPC over POST) rather than through the SDK
client, so what is under test is what an external agent actually sends.

Two constraints shape the harness. The session manager may only be run once per
process, so the client is entered once for the whole module. And the endpoint
checks the Host header, so the client speaks to localhost rather than
TestClient's default `testserver`.
"""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api import mcp_server, server
from src.core.auth import SECRET_ENV
from src.core.user_token import current_user_token
from tests.test_route_auth import SECRET, mint

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

VIEWER_CMD = {"action": "fly_to", "params": {"lon": 2.35, "lat": 48.85}}


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


def call(client, method, params=None, token=None, request_id=1):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
    manifest = server.tool_manifest()

    assert len(tools) == len(manifest)
    assert len(tools) > 30
    assert [t["name"] for t in tools] == [t["name"] for t in manifest]
    assert [t["inputSchema"] for t in tools] == [t["parameters"] for t in manifest]
    assert [t["description"] for t in tools] == [t["description"] for t in manifest]


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

    token = mint()
    response = call(
        client, "tools/call", {"name": "record_token", "arguments": {}}, token=token
    )

    assert text_of(response) == "ok"
    assert seen == [token]
    # and the scope is not left behind for the next caller
    assert current_user_token() is None


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
    tools = result_of(call(client, "tools/list", {}, token=mint()))["tools"]

    assert len(tools) > 30


def test_without_a_secret_a_tokenless_call_is_served(open_mode, client):
    assert result_of(call(client, "tools/list", {}))["tools"]


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
