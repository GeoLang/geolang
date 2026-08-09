"""The bearer gate on the rest of the API.

Same switch as the tool gate: with `PLATFORM_JWT_SECRET` set, every route that
runs code, writes a file, or reads back a session or a user's data needs a live
platform token. Unset, all of it is open, which is the standalone stack. Every
test sets or clears the variable through monkeypatch, so the rest of the suite
still runs in dev mode.

The handlers behind the gate are stubbed down to nothing (no browser, no sibyl,
no writes in the repo): what is under test is who gets past the gate, not what
the route then does.
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
import respx
from fastapi.testclient import TestClient

from src.api import server
from src.core.auth import SECRET_ENV, platform_auth

client = TestClient(server.app)

# 32 bytes, the minimum HS256 key length RFC 7518 asks for
SECRET = "test-platform-secret-0123456789ab"

SHARE_ID = "kXv3-2_QeR9tYuI0pAsDfg"
URL_SAFE = re.compile(r"[A-Za-z0-9_-]+")
SHARE = {"title": "Shared", "summary": "", "layers": [], "center": [], "zoom": 12}

AGUI_INPUT = {
    "thread_id": "t1",
    "run_id": "r1",
    "state": {},
    "messages": [],
    "tools": [],
    "context": [],
    "forwarded_props": {},
}


def mint(secret=SECRET, lifetime=timedelta(hours=1), **claims):
    """A platform token: HS256 over {sub, exp, role}, same shape ptolemy mints."""
    payload = {
        "sub": "u1",
        "exp": datetime.now(timezone.utc) + lifetime,
        "role": "editor",
        **claims,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# name, method, path, request kwargs
GATED = [
    ("chat", "post", "/chat/agui", {"json": AGUI_INPUT}),
    ("upload", "post", "/upload", {"files": {"file": ("a.txt", b"not geodata")}}),
    (
        "draw",
        "post",
        "/draw",
        {"json": {"geojson": {"type": "FeatureCollection", "features": []}}},
    ),
    ("export_pdf", "post", "/export-pdf", {"json": {}}),
    ("export_png", "post", "/export-png", {"json": {}}),
    ("outputs", "get", "/outputs/nope.gpkg", {}),
    ("download", "get", "/download/nope.gpkg", {}),
    ("geojson", "get", "/geojson/nope.gpkg", {}),
    ("stats", "get", "/stats/nope.gpkg", {}),
    ("datasets", "get", "/datasets", {}),
    ("sessions", "get", "/sessions", {}),
    ("session_new", "post", "/sessions/new", {}),
    ("session_switch", "post", "/sessions/switch", {"json": {"session_id": "s1"}}),
    ("session_rename", "put", "/sessions/s1/rename", {"json": {"name": "x"}}),
    ("session_delete", "delete", "/sessions/s1", {}),
    ("models", "get", "/models", {}),
    ("model_set", "put", "/model", {"json": {"profile": "local"}}),
    ("share_create", "post", "/share", {"json": {}}),
]

OPEN = [
    ("health", "get", "/health", {}),
    ("manifest", "get", "/tools", {}),
    ("share_data", "get", f"/share/{SHARE_ID}/data", {}),
    ("share_page", "get", f"/share/{SHARE_ID}", {}),
]

IDS = [row[0] for row in GATED]
OPEN_IDS = [row[0] for row in OPEN]

# every path the app answers without a platform token. Anything new has to be
# gated or argued for here.
UNGATED_PATHS = {
    "/health",
    "/tools",  # manifest, sibyl fetches it before anyone has signed in
    "/tools/{name}",  # gated in the handler, which needs the token itself
    "/mcp",  # gated by its own ASGI middleware, see test_mcp
    "/debug/tools",  # tool names, which the manifest already lists
    "/share/{share_id}",  # a share link is meant for someone who never signs in
    "/share/{share_id}/data",
    "/",  # the viewer shell, no data of its own
    "/static",  # js and css
    # fastapi's own, mounted below the dependency system
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)


@pytest.fixture
def open_mode(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)


# shares a gated call would have written
written = []


@pytest.fixture(autouse=True)
def stubs(monkeypatch, tmp_path):
    """Cut the gated handlers down to nothing: no browser, no sibyl, no writes."""
    monkeypatch.setattr(server, "USER_DATA_DIR", tmp_path / "user_data")
    monkeypatch.setattr(server, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    (tmp_path / "outputs").mkdir()

    async def no_notify(text):
        return None

    def no_browser():
        raise RuntimeError("no browser in tests")

    monkeypatch.setattr(server, "notify_agent", no_notify)
    monkeypatch.setattr(server, "load_shares", lambda: {SHARE_ID: SHARE})
    monkeypatch.setattr(server, "save_shares", lambda shares: written.append(shares))
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=no_browser),
    )

    with respx.mock(base_url=server.SIBYL_URL, assert_all_called=False) as sibyl:
        sibyl.get("/sessions").respond(200, json=[])
        sibyl.post("/sessions").respond(200, json={"id": "s1", "name": "Session 1"})
        sibyl.post("/sessions/s1/activate").respond(200, json={"id": "s1"})
        sibyl.patch("/sessions/s1").respond(200, json={"id": "s1", "name": "x"})
        sibyl.delete("/sessions/s1").respond(200, json={"deleted": "s1"})
        sibyl.get("/models").respond(200, json={"models": []})
        sibyl.put("/model").respond(200, json={"active": "local"})
        yield


def call(method, path, kwargs, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return getattr(client, method)(path, headers=headers, **kwargs)


# ── gate on ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("_name, method, path, kwargs", GATED, ids=IDS)
def test_no_token_is_rejected(gated, _name, method, path, kwargs):
    response = call(method, path, kwargs)

    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}


@pytest.mark.parametrize("_name, method, path, kwargs", GATED, ids=IDS)
def test_a_live_token_gets_through(gated, _name, method, path, kwargs):
    # what the handler then answers is its own business, 401 is the gate
    assert call(method, path, kwargs, token=mint()).status_code != 401


@pytest.mark.parametrize("_name, method, path, kwargs", GATED, ids=IDS)
def test_a_forged_or_expired_token_is_rejected(gated, _name, method, path, kwargs):
    for token in (
        "not-a-jwt",
        mint(secret="a-different-secret-0123456789abcd"),
        mint(lifetime=timedelta(seconds=-1)),
    ):
        response = call(method, path, kwargs, token=token)

        assert response.status_code == 401
        assert response.json() == {"detail": "invalid or expired token"}


def test_the_gate_runs_before_the_side_effect(gated, tmp_path):
    written.clear()

    assert call("post", "/share", {"json": {}}).status_code == 401
    assert (
        call(
            "post", "/upload", {"files": {"file": ("a.gpkg", b"not geodata")}}
        ).status_code
        == 401
    )

    assert written == []
    assert not (tmp_path / "user_data").exists()


@pytest.mark.parametrize("_name, method, path, kwargs", OPEN, ids=OPEN_IDS)
def test_an_open_route_stays_open(gated, _name, method, path, kwargs):
    assert call(method, path, kwargs).status_code == 200


def test_a_new_share_id_is_long_enough_to_be_the_credential(gated, monkeypatch):
    """Nothing but the id stands between a stranger and a share, so it is the
    one thing here that has to be unguessable."""
    store = {}
    monkeypatch.setattr(server, "load_shares", lambda: store)
    monkeypatch.setattr(server, "save_shares", store.update)

    created = call("post", "/share", {"json": {"title": "Coastline"}}, token=mint())
    share_id = created.json()["share_id"]

    assert created.status_code == 200
    assert len(share_id) >= 16
    assert URL_SAFE.fullmatch(share_id)
    # and the link works for someone who never signed in
    assert client.get(f"/share/{share_id}/data").json()["title"] == "Coastline"
    assert client.get(f"/share/{share_id}").status_code == 200


def test_two_shares_do_not_get_the_same_id(gated, monkeypatch):
    store = {}
    monkeypatch.setattr(server, "load_shares", lambda: store)
    monkeypatch.setattr(server, "save_shares", store.update)

    ids = {
        call("post", "/share", {"json": {}}, token=mint()).json()["share_id"]
        for _ in range(5)
    }

    assert len(ids) == 5


def test_a_share_is_readable_without_a_token_but_its_layers_are_not(gated):
    # the link is the whole point, the data behind it still needs a caller
    assert client.get(f"/share/{SHARE_ID}/data").json() == SHARE
    assert client.get("/geojson/shared_layer.gpkg").status_code == 401


def test_no_route_is_left_open_by_accident():
    """A route added without the gate fails here rather than shipping open."""
    def is_gated(route):
        dependant = getattr(route, "dependant", None)
        return dependant is not None and platform_auth in [
            d.call for d in dependant.dependencies
        ]

    ungated = {route.path for route in server.app.routes if not is_gated(route)}

    assert ungated == UNGATED_PATHS


# ── gate off ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("_name, method, path, kwargs", GATED, ids=IDS)
def test_without_a_secret_a_tokenless_call_is_served(open_mode, _name, method, path, kwargs):
    assert call(method, path, kwargs).status_code != 401


@pytest.mark.parametrize("_name, method, path, kwargs", GATED, ids=IDS)
def test_without_a_secret_an_unverifiable_token_is_served(
    open_mode, _name, method, path, kwargs
):
    # dev mode does not start rejecting tokens it cannot check
    assert call(method, path, kwargs, token="not-a-jwt").status_code != 401
