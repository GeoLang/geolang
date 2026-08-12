"""The bearer gate on `POST /tools/{name}`.

`PLATFORM_JWT_SECRET` is the switch: set, a tool call needs a live HS256
platform token, unset, the route is open so the standalone stack and the eval
harness keep working. Every test here sets or clears the variable through
monkeypatch, so the rest of the suite still runs in dev mode.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api import server
from src.core import utils
from src.core.auth import (
    SECRET_ENV,
    TOOL_SCOPE_CLAIM,
    TOOL_TOKEN_LIFETIME_SECONDS,
    TOOL_TOKEN_USE,
    TOOL_TOKEN_USE_CLAIM,
    platform_secret,
)
from src.core.user_token import user_token_scope

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


def test_the_tool_receives_a_short_role_free_token(gated, monkeypatch):
    from src.core.user_token import current_user_token

    seen = []

    class ProbeArgs(BaseModel):
        pass

    def probe():
        """Report the caller this tool call is running as."""
        seen.append(current_user_token())
        return "ok"

    monkeypatch.setattr(server, "load_external_tools", lambda: [(probe, ProbeArgs)])

    source = mint(role="admin")
    source_claims = jwt.decode(source, SECRET, algorithms=["HS256"])
    assert call(source).status_code == 200

    assert len(seen) == 1
    claims = jwt.decode(seen[0], SECRET, algorithms=["HS256"])
    assert claims["sub"] == source_claims["sub"]
    assert claims[TOOL_TOKEN_USE_CLAIM] == TOOL_TOKEN_USE
    assert claims[TOOL_SCOPE_CLAIM] == []
    assert "role" not in claims
    assert claims["exp"] <= source_claims["exp"]
    assert claims["exp"] - int(time.time()) <= TOOL_TOKEN_LIFETIME_SECONDS


@pytest.mark.parametrize(
    "claims",
    [
        {TOOL_TOKEN_USE_CLAIM: TOOL_TOKEN_USE, TOOL_SCOPE_CLAIM: []},
        {TOOL_TOKEN_USE_CLAIM: "other", TOOL_SCOPE_CLAIM: []},
    ],
)
def test_a_downstream_tool_token_cannot_open_the_tool_route(gated, probe, claims):
    assert call(mint(**claims)).status_code == 401
    assert probe == []


def test_the_manifest_stays_open(gated):
    # sibyl fetches it at startup, before anyone has signed in
    assert client.get("/tools").status_code == 200


# ── one directory per caller ─────────────────────────────────────────────


@pytest.fixture
def outputs_root(monkeypatch, tmp_path):
    """Files a tool writes land under tmp_path instead of the real tree."""
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    return tmp_path / "outputs"


def wrote(subject, filename):
    """A file in `subject`'s own outputs directory, put there the way a tool does."""
    with user_token_scope(mint(sub=subject)):
        (Path(utils.caller_outputs_dir()) / filename).write_text("x")


def listing_for(subject):
    response = client.post(
        "/tools/list_outputs",
        json={"args": {}},
        headers={"Authorization": f"Bearer {mint(sub=subject)}"},
    )
    assert response.status_code == 200
    return response.json()["result"]


def test_list_outputs_shows_only_the_callers_own_files(gated, outputs_root):
    wrote("alice", "alice_sites.gpkg")
    wrote("bob", "bob_sites.gpkg")

    assert "alice_sites.gpkg" in listing_for("alice")
    assert "bob_sites.gpkg" not in listing_for("alice")
    assert "alice_sites.gpkg" not in listing_for("bob")


def test_a_tool_writes_into_the_callers_own_directory(gated, outputs_root, monkeypatch):
    seen = []

    class ProbeArgs(BaseModel):
        pass

    def probe():
        """Report the directory this tool call would write to."""
        seen.append(utils.caller_outputs_dir())
        return "ok"

    monkeypatch.setattr(server, "load_external_tools", lambda: [(probe, ProbeArgs)])
    assert call(mint(sub="alice")).status_code == 200
    assert call(mint(sub="bob")).status_code == 200

    assert len(set(seen)) == 2
    assert all(Path(directory).parent == outputs_root for directory in seen)


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
