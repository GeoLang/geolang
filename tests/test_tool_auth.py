"""The bearer gate on `POST /tools/{name}`.

`PLATFORM_JWT_SECRET` is the switch: set, a tool call needs a live HS256
platform token, unset, the route is open so the standalone stack and the eval
harness keep working. Every test here sets or clears the variable through
monkeypatch, so the rest of the suite still runs in dev mode.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api import server
from src.core.auth import SECRET_ENV, platform_secret

client = TestClient(server.app)

# 32 bytes, the minimum HS256 key length RFC 7518 asks for
SECRET = "test-platform-secret-0123456789ab"


def mint(secret=SECRET, lifetime=timedelta(hours=1), algorithm="HS256", **claims):
    """A platform token: HS256 over {sub, exp, role}, same shape ptolemy mints."""
    payload = {
        "sub": "u1",
        "exp": datetime.now(timezone.utc) + lifetime,
        "role": "editor",
        **claims,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture
def probe(monkeypatch):
    """Register one tool and report the calls that reached it."""
    calls = []

    class ProbeArgs(BaseModel):
        pass

    def probe():
        """Record that this tool ran."""
        calls.append(True)
        return "ok"

    monkeypatch.setattr(server, "load_external_tools", lambda: [(probe, ProbeArgs)])
    return calls


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)


@pytest.fixture
def open_mode(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)


def call(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/tools/probe", json={"args": {}}, headers=headers)


# ── the switch ───────────────────────────────────────────────────────────


def test_an_unset_or_empty_secret_leaves_the_gate_off(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    assert platform_secret() is None
    # a variable declared but never filled in must not read as a secret
    monkeypatch.setenv(SECRET_ENV, "")
    assert platform_secret() is None
    monkeypatch.setenv(SECRET_ENV, "   ")
    assert platform_secret() is None
    monkeypatch.setenv(SECRET_ENV, SECRET)
    assert platform_secret() == SECRET


# ── gate on ──────────────────────────────────────────────────────────────


def test_a_valid_token_runs_the_tool(gated, probe):
    response = call(mint())

    assert response.status_code == 200
    assert response.json()["result"] == "ok"
    assert probe == [True]


def test_no_token_is_rejected(gated, probe):
    assert call().status_code == 401
    assert probe == []


def test_garbage_is_rejected(gated, probe):
    assert call("not-a-jwt").status_code == 401
    assert call("a.b.c").status_code == 401
    assert probe == []


def test_an_expired_token_is_rejected(gated, probe):
    assert call(mint(lifetime=timedelta(seconds=-1))).status_code == 401
    assert probe == []


def test_a_token_signed_with_another_secret_is_rejected(gated, probe):
    assert call(mint(secret="a-different-secret-0123456789abcd")).status_code == 401
    assert probe == []


def test_an_unsigned_token_is_rejected(gated, probe):
    # alg "none" is the classic bypass: the payload looks right, nothing signed it
    unsigned = jwt.encode({"sub": "u1", "exp": 9999999999, "role": "admin"}, key=None, algorithm="none")

    assert call(unsigned).status_code == 401
    assert probe == []


def test_a_token_with_no_expiry_is_rejected(gated, probe):
    # a token that never expires cannot be revoked by waiting
    assert call(jwt.encode({"sub": "u1", "role": "admin"}, SECRET, algorithm="HS256")).status_code == 401
    assert probe == []


def test_a_non_bearer_authorization_is_rejected(gated, probe):
    response = client.post(
        "/tools/probe", json={"args": {}}, headers={"Authorization": f"Basic {mint()}"}
    )

    assert response.status_code == 401
    assert probe == []


def test_an_unknown_tool_is_still_401_without_a_token(gated):
    # the gate runs before the lookup, so a stranger cannot probe the catalogue
    assert client.post("/tools/nonexistent", json={"args": {}}).status_code == 401


def test_the_role_is_not_checked_here(gated, probe):
    # downstream services enforce their own RBAC, this gate only proves identity
    assert call(mint(role="viewer")).status_code == 200
    assert probe == [True]


def test_the_validated_token_is_forwarded_unchanged(gated, monkeypatch):
    from src.core.user_token import current_user_token

    seen = []

    class ProbeArgs(BaseModel):
        pass

    def probe():
        """Report the caller this tool call is running as."""
        seen.append(current_user_token())
        return "ok"

    monkeypatch.setattr(server, "load_external_tools", lambda: [(probe, ProbeArgs)])

    token = mint()
    assert call(token).status_code == 200
    assert seen == [token]


def test_the_manifest_stays_open(gated):
    # sibyl fetches it at startup, before anyone has signed in
    assert client.get("/tools").status_code == 200


# ── gate off ─────────────────────────────────────────────────────────────


def test_without_a_secret_a_tokenless_call_still_runs(open_mode, probe):
    response = call()

    assert response.status_code == 200
    assert response.json()["result"] == "ok"
    assert probe == [True]


def test_without_a_secret_an_unverifiable_token_is_still_forwarded(open_mode, probe):
    # dev mode does not start rejecting tokens it cannot check
    assert call("not-a-jwt").status_code == 200
    assert probe == [True]
