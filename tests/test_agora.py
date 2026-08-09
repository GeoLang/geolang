"""The agora client, against a websocket server running in the test process.

A fake agora rather than a mocked client: what is under test is the bytes on the
wire, since the viewer and the Rust server are the ones that have to read them.
"""

import asyncio
import json

import httpx
import pytest
import respx
from websockets.asyncio.server import serve

from src.core import agora

DOCUMENT_ID = "0f8b1c2d-3e4f-4a5b-8c7d-9e0f1a2b3c4d"
TOKEN = "a.platform.token"
SNAPSHOT = {
    "type": "snapshot",
    "seq": 7,
    "state": {"meta": {"name": "Site study"}, "layers": {}},
    "actor": "agent:u1",
    "role": "edit",
}
PEERS = {"type": "peers", "peers": [{"actor": "u1", "name": "Ada", "role": "edit"}]}


class FakeAgora:
    """Records what the client sent and answers with what agora would."""

    def __init__(self, snapshot=None, ack=True, error=None, before_ack=()):
        self.snapshot = snapshot if snapshot is not None else SNAPSHOT
        self.ack = ack
        self.error = error
        self.before_ack = before_ack
        self.received = []
        self.authorization = None
        self.path = None

    async def handle(self, connection):
        self.authorization = connection.request.headers.get("authorization")
        self.path = connection.request.path
        await connection.send(json.dumps(self.snapshot))
        await connection.send(json.dumps(PEERS))
        async for raw in connection:
            frame = json.loads(raw)
            self.received.append(frame)
            if frame["type"] == "presence":
                continue
            for extra in self.before_ack:
                await connection.send(json.dumps(extra))
            if self.error is not None:
                await connection.send(
                    json.dumps({"type": "error", "reason": self.error})
                )
                continue
            if self.ack:
                await connection.send(
                    json.dumps(
                        {"type": "ack", "clientSeq": frame["clientSeq"], "seq": 8}
                    )
                )


def run_against(fake, scenario, monkeypatch):
    """Serve `fake` on a loopback port and run `scenario(session)` against it."""

    async def main():
        async with serve(fake.handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setenv(agora.AGORA_URL_ENV, f"http://127.0.0.1:{port}")
            async with agora.open_session(DOCUMENT_ID, TOKEN) as session:
                return await scenario(session)

    return asyncio.run(main())


# ── the connection ───────────────────────────────────────────────────────


def test_the_bearer_and_the_document_travel_on_the_connection(monkeypatch):
    fake = FakeAgora()

    async def scenario(session):
        return session.actor, session.role, session.layers

    actor, role, layers = run_against(fake, scenario, monkeypatch)

    assert fake.authorization == f"Bearer {TOKEN}"
    assert fake.path == f"/ws?doc={DOCUMENT_ID}"
    assert (actor, role) == ("agent:u1", "edit")
    assert layers == {}


def test_the_opening_snapshot_is_what_the_session_reads(monkeypatch):
    entry = {"layerId": "cafes", "name": "Cafes", "order": "V"}
    fake = FakeAgora(snapshot={**SNAPSHOT, "state": {"layers": {"cafes": entry}}})

    async def scenario(session):
        return session.layers

    assert run_against(fake, scenario, monkeypatch) == {"cafes": entry}


def test_a_view_role_session_refuses_to_write(monkeypatch):
    """The role comes from agora, so a downgrade there stops the write here."""
    fake = FakeAgora(snapshot={**SNAPSHOT, "role": "view"})

    async def scenario(session):
        with pytest.raises(agora.AgoraError, match="may not write"):
            await session.send_operations([("layers/cafes", {"name": "Cafes"})])
        return session.role

    assert run_against(fake, scenario, monkeypatch) == "view"
    assert fake.received == []


def test_a_refused_join_is_an_error(monkeypatch):
    class Refusing(FakeAgora):
        async def handle(self, connection):
            await connection.send(
                json.dumps({"type": "error", "reason": "document not found"})
            )

    async def main():
        fake = Refusing()
        async with serve(fake.handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setenv(agora.AGORA_URL_ENV, f"http://127.0.0.1:{port}")
            with pytest.raises(agora.AgoraError, match="document not found"):
                async with agora.open_session(DOCUMENT_ID, TOKEN):
                    pass

    asyncio.run(main())


# ── operations ───────────────────────────────────────────────────────────


def test_one_operation_goes_out_as_an_op_frame(monkeypatch):
    fake = FakeAgora()
    entry = {"layerId": "cafes", "name": "Cafes", "order": "V"}

    async def scenario(session):
        await session.send_operations([("layers/cafes", entry)])

    run_against(fake, scenario, monkeypatch)

    assert fake.received == [
        {"type": "op", "clientSeq": 1, "key": "layers/cafes", "value": entry}
    ]


def test_several_operations_go_out_as_one_batch(monkeypatch):
    fake = FakeAgora()

    async def scenario(session):
        await session.send_operations(
            [("layers/a", {"name": "A"}), ("layers/b", None)]
        )

    run_against(fake, scenario, monkeypatch)

    assert fake.received == [
        {
            "type": "batch",
            "clientSeq": 1,
            "ops": [
                {"key": "layers/a", "value": {"name": "A"}},
                {"key": "layers/b", "value": None},
            ],
        }
    ]


def test_the_write_waits_for_its_own_ack(monkeypatch):
    """Frames for other peers arrive on the same socket and are not the ack."""
    fake = FakeAgora(
        before_ack=[
            {"type": "op", "seq": 8, "actor": "u2", "key": "layers/x", "value": {}},
            {"type": "ack", "clientSeq": 99, "seq": 8},
        ]
    )

    async def scenario(session):
        await session.send_operations([("layers/a", {"name": "A"})])
        return True

    assert run_against(fake, scenario, monkeypatch) is True


def test_an_error_frame_fails_the_write(monkeypatch):
    fake = FakeAgora(error="document state limit reached")

    async def scenario(session):
        with pytest.raises(agora.AgoraError, match="document state limit reached"):
            await session.send_operations([("layers/a", {"name": "A"})])

    run_against(fake, scenario, monkeypatch)


def test_an_oversized_value_never_reaches_the_wire(monkeypatch):
    fake = FakeAgora()
    huge = {"blob": "x" * (agora.MAXIMUM_OPERATION_VALUE_BYTES + 1)}

    async def scenario(session):
        with pytest.raises(agora.AgoraError, match="operation cap"):
            await session.send_operations([("layers/a", huge)])

    run_against(fake, scenario, monkeypatch)

    assert fake.received == []


def test_more_operations_than_a_batch_holds_are_split(monkeypatch):
    fake = FakeAgora()
    count = agora.MAXIMUM_OPERATIONS_PER_BATCH + 2
    operations = [(f"layers/l{index}", {"name": index}) for index in range(count)]

    async def scenario(session):
        await session.send_operations(operations)

    monkeypatch.setattr(agora, "BATCH_INTERVAL_SECONDS", 0)
    run_against(fake, scenario, monkeypatch)

    assert [len(frame["ops"]) for frame in fake.received] == [
        agora.MAXIMUM_OPERATIONS_PER_BATCH,
        2,
    ]
    # a fresh clientSeq per frame, which is what the acks are matched on
    assert [frame["clientSeq"] for frame in fake.received] == [1, 2]


def test_a_batch_is_split_before_it_grows_past_the_frame_limit(monkeypatch):
    """agora closes the connection on an oversized frame instead of refusing it."""
    fake = FakeAgora()
    value = {"blob": "x" * (32 * 1024)}
    operations = [(f"layers/l{index}", value) for index in range(8)]

    async def scenario(session):
        await session.send_operations(operations)

    monkeypatch.setattr(agora, "BATCH_INTERVAL_SECONDS", 0)
    run_against(fake, scenario, monkeypatch)

    assert len(fake.received) > 1
    for frame in fake.received:
        assert len(json.dumps(frame)) <= agora.MAXIMUM_FRAME_BYTES
    sent = [op["key"] for frame in fake.received for op in frame["ops"]]
    assert sent == [key for key, _ in operations]


def test_an_oversized_value_stops_the_write_before_any_frame(monkeypatch):
    """One refusable operation fails the whole write rather than half of it."""
    fake = FakeAgora()
    huge = {"blob": "x" * (agora.MAXIMUM_OPERATION_VALUE_BYTES + 1)}
    operations = [("layers/a", {"name": "A"}), ("layers/b", huge)]

    async def scenario(session):
        with pytest.raises(agora.AgoraError, match="operation cap"):
            await session.send_operations(operations)

    run_against(fake, scenario, monkeypatch)

    assert fake.received == []


def test_nothing_is_sent_for_an_empty_write(monkeypatch):
    fake = FakeAgora()

    async def scenario(session):
        await session.send_operations([])

    run_against(fake, scenario, monkeypatch)

    assert fake.received == []


# ── presence ─────────────────────────────────────────────────────────────


def test_presence_carries_the_viewport_the_viewer_reads(monkeypatch):
    fake = FakeAgora()
    viewport = {"center": [2.35, 48.85], "zoom": 12}

    async def scenario(session):
        await session.send_presence(viewport)
        # presence has no ack, so give the server a turn to read it
        await asyncio.sleep(0.05)

    run_against(fake, scenario, monkeypatch)

    assert fake.received == [
        {
            "type": "presence",
            "cursor": None,
            "selection": [],
            "viewport": {"center": [2.35, 48.85], "zoom": 12},
        }
    ]


# ── the http side ────────────────────────────────────────────────────────


def test_granting_edit_acts_as_the_caller(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    with respx.mock(base_url="http://agora:3000") as mock:
        route = mock.put(
            f"/documents/{DOCUMENT_ID}/members/agent:u1"
        ).respond(204)
        asyncio.run(agora.grant_edit_role(DOCUMENT_ID, "agent:u1", "caller-token"))

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer caller-token"
    assert json.loads(request.content) == {"role": "edit"}


def test_a_refused_grant_surfaces_agoras_reason(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    with respx.mock(base_url="http://agora:3000") as mock:
        mock.put(f"/documents/{DOCUMENT_ID}/members/agent:u1").respond(
            403, json={"error": "edit role required"}
        )
        with pytest.raises(agora.AgoraError, match="edit role required"):
            asyncio.run(agora.grant_edit_role(DOCUMENT_ID, "agent:u1", "caller-token"))


def test_an_unreachable_agora_is_an_error(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    with respx.mock(base_url="http://agora:3000") as mock:
        mock.put(f"/documents/{DOCUMENT_ID}/members/agent:u1").mock(
            side_effect=httpx.ConnectError("no route")
        )
        with pytest.raises(agora.AgoraError, match="unreachable"):
            asyncio.run(agora.grant_edit_role(DOCUMENT_ID, "agent:u1", "caller-token"))


def test_resolving_a_share_link_needs_no_token(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")
    resolution = {"doc": DOCUMENT_ID, "role": "edit", "sessionToken": "session.jwt"}

    with respx.mock(base_url="http://agora:3000") as mock:
        route = mock.get("/links/abc123").respond(200, json=resolution)
        assert asyncio.run(agora.resolve_share_link("abc123")) == resolution

    assert "authorization" not in route.calls.last.request.headers


def test_a_link_token_cannot_climb_out_of_its_path(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    with respx.mock(base_url="http://agora:3000") as mock:
        route = mock.get(url__startswith="http://agora:3000/links/").respond(
            404, json={"error": "not found"}
        )
        with pytest.raises(agora.AgoraError):
            asyncio.run(agora.resolve_share_link("../documents"))

    assert route.calls.last.request.url.raw_path == b"/links/..%2Fdocuments"


# ── urls ─────────────────────────────────────────────────────────────────


def test_the_websocket_url_follows_the_http_one(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "https://maps.example.com/")

    assert agora.websocket_url(DOCUMENT_ID) == (
        f"wss://maps.example.com/ws?doc={DOCUMENT_ID}"
    )


def test_without_the_variable_it_is_the_service_name(monkeypatch):
    monkeypatch.delenv(agora.AGORA_URL_ENV, raising=False)

    assert agora.agora_url() == agora.DEFAULT_AGORA_URL
    assert agora.websocket_url("d").startswith("ws://agora:3000/ws?doc=")
