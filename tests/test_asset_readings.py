"""The tool that answers what the sensors on a live map are reporting.

Every call goes through agora's asset routes, so the fake here answers exactly
what agora documents: one entry per asset, its liveness, and its latest value
per reading kind.
"""

import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.agents.tools.asset_readings import MAXIMUM_ASSETS_REPORTED, asset_readings
from src.api import server
from src.api.live_document import DOCUMENT_HEADER
from src.core import agora
from src.core.auth import SECRET_ENV
from src.core.bound_document import bound_document_scope, current_bound_document
from src.core.user_token import user_token_scope

AGORA_URL = "http://agora:3000"
DOCUMENT_ID = "0f8b1c2d-3e4f-4a5b-8c7d-9e0f1a2b3c4d"
LINK_TOKEN = "sharelinktoken123"
ASSETS_PATH = f"/documents/{DOCUMENT_ID}/assets"

ASSETS = [
    {
        "asset": "TWIN-01",
        "feed": "f1",
        "online": True,
        "values": [
            {"kind": "temperature", "value": 21.5, "at": "2026-08-25T12:00:00Z"},
            {"kind": "humidity", "value": 44.0, "at": "2026-08-25T12:00:01Z"},
        ],
    },
    {
        "asset": "TWIN-02",
        "feed": "f1",
        "online": True,
        "values": [
            {"kind": "temperature", "value": 33.25, "at": "2026-08-25T12:00:02Z"}
        ],
    },
    {
        "asset": "COLD-ROOM",
        "feed": "f2",
        "online": False,
        "values": [
            {"kind": "temperature", "value": 3.0, "at": "2026-08-25T02:59:00Z"}
        ],
    },
]


@pytest.fixture(autouse=True)
def agora_at_a_known_url(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, AGORA_URL)


def read(payload=None, at_payload=None, token="caller.jwt", **arguments):
    """Run the tool against a fake agora, and answer with the parsed result."""
    with respx.mock(base_url=AGORA_URL, assert_all_called=False) as mock:
        live = mock.get(ASSETS_PATH).respond(
            200, json={"assets": ASSETS if payload is None else payload}
        )
        at_route = mock.get(f"{ASSETS_PATH}/at").respond(
            200, json={"assets": ASSETS if at_payload is None else at_payload}
        )
        with user_token_scope(token):
            result = asset_readings(**arguments)
    return result, live, at_route


def answer(**arguments):
    result, _, _ = read(**arguments)
    assert not result.startswith("ERROR"), result
    return json.loads(result)


def assets_named(parsed):
    return [asset["asset"] for asset in parsed["assets"]]


# ── the two routes ───────────────────────────────────────────────────────


def test_the_live_route_answers_every_asset_with_its_values():
    parsed = answer(document_id=DOCUMENT_ID)

    assert parsed["document_id"] == DOCUMENT_ID
    assert parsed["at"] is None
    assert parsed["asset_count"] == 3
    assert parsed["offline_count"] == 1
    assert assets_named(parsed) == ["TWIN-01", "TWIN-02", "COLD-ROOM"]
    assert parsed["assets"][0]["values"] == {
        "temperature": {"value": 21.5, "at": "2026-08-25T12:00:00Z"},
        "humidity": {"value": 44.0, "at": "2026-08-25T12:00:01Z"},
    }
    assert parsed["summary"] == "3 assets, 1 offline"


def test_a_time_reads_the_at_route_and_reports_it():
    result, live, at_route = read(
        document_id=DOCUMENT_ID, at="2026-08-25T03:00:00Z", at_payload=[ASSETS[2]]
    )
    parsed = json.loads(result)

    assert not live.called
    assert at_route.calls.last.request.url.params["t"] == "2026-08-25T03:00:00Z"
    assert parsed["at"] == "2026-08-25T03:00:00Z"
    assert assets_named(parsed) == ["COLD-ROOM"]


def test_the_callers_bearer_reaches_agora():
    _, live, _ = read(document_id=DOCUMENT_ID, token="scoped.jwt")

    assert live.calls.last.request.headers["authorization"] == "Bearer scoped.jwt"


# ── the filters ──────────────────────────────────────────────────────────


def test_a_kind_keeps_that_reading_and_drops_assets_without_it():
    parsed = answer(document_id=DOCUMENT_ID, kind="humidity")

    assert assets_named(parsed) == ["TWIN-01"]
    assert parsed["assets"][0]["values"] == {
        "humidity": {"value": 44.0, "at": "2026-08-25T12:00:01Z"}
    }
    assert parsed["summary"] == "3 assets, 1 offline, 1 matching"


def test_an_asset_id_keeps_one_asset():
    parsed = answer(document_id=DOCUMENT_ID, asset_id="TWIN-02")

    assert assets_named(parsed) == ["TWIN-02"]


def test_above_keeps_the_assets_over_the_threshold():
    parsed = answer(document_id=DOCUMENT_ID, kind="temperature", above=30.0)

    assert assets_named(parsed) == ["TWIN-02"]
    assert parsed["summary"] == "3 assets, 1 offline, 1 above 30.0 temperature"


def test_below_keeps_the_assets_under_the_threshold():
    parsed = answer(document_id=DOCUMENT_ID, kind="temperature", below=10)

    assert assets_named(parsed) == ["COLD-ROOM"]


def test_above_and_below_together_keep_the_band_between_them():
    parsed = answer(document_id=DOCUMENT_ID, kind="temperature", above=10, below=30)

    assert assets_named(parsed) == ["TWIN-01"]


def test_offline_only_keeps_the_assets_that_stopped_reporting():
    parsed = answer(document_id=DOCUMENT_ID, offline_only=True)

    assert assets_named(parsed) == ["COLD-ROOM"]
    assert parsed["offline_count"] == 1


def test_a_threshold_without_a_kind_is_refused_before_agora_is_called():
    result, live, _ = read(document_id=DOCUMENT_ID, above=30)

    assert result.startswith("ERROR")
    assert "kind" in result
    assert not live.called


# ── the bound document ───────────────────────────────────────────────────


def test_the_bound_map_is_the_default_document():
    with bound_document_scope(DOCUMENT_ID):
        parsed = answer()

    assert parsed["document_id"] == DOCUMENT_ID


def test_an_argument_beats_the_bound_map():
    other = "11111111-2222-4333-8444-555555555555"
    with bound_document_scope(other):
        with respx.mock(base_url=AGORA_URL) as mock:
            route = mock.get(ASSETS_PATH).respond(200, json={"assets": []})
            with user_token_scope("caller.jwt"):
                asset_readings(document_id=DOCUMENT_ID)

    assert route.called


def test_no_document_anywhere_is_an_error_naming_the_argument():
    result, live, _ = read()

    assert result.startswith("ERROR")
    assert "document_id" in result
    assert not live.called


# ── what agora refuses ───────────────────────────────────────────────────


def test_a_document_that_is_not_there_is_an_error_not_a_raise():
    with respx.mock(base_url=AGORA_URL) as mock:
        mock.get(ASSETS_PATH).respond(404, json={"error": "no such document"})
        with user_token_scope("caller.jwt"):
            result = asset_readings(document_id=DOCUMENT_ID)

    assert result.startswith("ERROR")
    assert "no such document" in result


def test_a_document_the_caller_may_not_read_is_an_error_not_a_raise():
    with respx.mock(base_url=AGORA_URL) as mock:
        mock.get(ASSETS_PATH).respond(403, json={"error": "not a member"})
        with user_token_scope("caller.jwt"):
            result = asset_readings(document_id=DOCUMENT_ID)

    assert result.startswith("ERROR")
    assert "not a member" in result


def test_an_unreachable_agora_is_an_error_not_a_raise():
    with respx.mock(base_url=AGORA_URL) as mock:
        mock.get(ASSETS_PATH).mock(side_effect=httpx.ConnectError("no route"))
        with user_token_scope("caller.jwt"):
            result = asset_readings(document_id=DOCUMENT_ID)

    assert result.startswith("ERROR")
    assert "unreachable" in result


# ── the cap ──────────────────────────────────────────────────────────────


def test_more_assets_than_the_cap_are_cut_and_the_summary_says_so():
    crowd = [
        {
            "asset": f"TWIN-{index:04d}",
            "feed": "f1",
            "online": True,
            "values": [
                {"kind": "temperature", "value": 20.0, "at": "2026-08-25T12:00:00Z"}
            ],
        }
        for index in range(MAXIMUM_ASSETS_REPORTED + 1)
    ]
    parsed = answer(document_id=DOCUMENT_ID, payload=crowd)

    assert parsed["asset_count"] == MAXIMUM_ASSETS_REPORTED + 1
    assert parsed["match_count"] == MAXIMUM_ASSETS_REPORTED + 1
    assert len(parsed["assets"]) == MAXIMUM_ASSETS_REPORTED
    assert f"first {MAXIMUM_ASSETS_REPORTED} listed" in parsed["summary"]


# ── the header that binds a call to a map ────────────────────────────────


class NoArgs(BaseModel):
    pass


def report_the_bound_map():
    """Answer with the document this call is bound to."""
    return str(current_bound_document())


# no lifespan: the MCP session manager it starts may only run once per process
client = TestClient(server.app)


@pytest.fixture
def only_tool_is_the_probe(monkeypatch):
    monkeypatch.setattr(
        server, "load_external_tools", lambda: [(report_the_bound_map, NoArgs)]
    )
    monkeypatch.delenv(SECRET_ENV, raising=False)


def run_bound(document):
    headers = {DOCUMENT_HEADER: document} if document else {}
    response = client.post(
        "/tools/report_the_bound_map", json={"args": {}}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_the_document_header_reaches_the_tool(only_tool_is_the_probe):
    assert run_bound(DOCUMENT_ID) == DOCUMENT_ID


def test_a_share_link_token_binds_no_map(only_tool_is_the_probe):
    """agora answers its asset routes to members, and a link guest is not one."""
    assert run_bound(LINK_TOKEN) == "None"


def test_no_header_binds_no_map(only_tool_is_the_probe):
    assert run_bound(None) == "None"


# ── the same map reaches the chat run ────────────────────────────────────


def drain(events):
    async def collect():
        return [event async for event in events]

    return asyncio.run(collect())


def sibyl_run_body(route):
    return json.loads(route.calls.last.request.content)


def test_the_run_request_carries_the_bound_document():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        with bound_document_scope(DOCUMENT_ID):
            drain(server.agent_event_stream("what is TWIN-01 reading"))

    assert sibyl_run_body(route)["document"] == DOCUMENT_ID


def test_a_run_bound_to_no_map_sends_no_document():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        drain(server.agent_event_stream("what is TWIN-01 reading"))

    # absent, not null: sibyl treats the field as optional
    assert "document" not in sibyl_run_body(route)


def chat(headers):
    body = {
        "threadId": "t1",
        "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": "what is it reading"}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )
        response = client.post("/chat/agui", json=body, headers=headers)
        assert response.status_code == 200, response.text
    return sibyl_run_body(route)


def test_the_chat_route_binds_the_document_header_for_the_run(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    assert chat({DOCUMENT_HEADER: DOCUMENT_ID})["document"] == DOCUMENT_ID


def test_a_chat_from_no_map_sends_no_document(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    assert "document" not in chat({})
