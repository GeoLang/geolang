"""A tool result's map effects, landing in a live agora document.

The document side is a real websocket server in the test process, the same fake
the agora client's own tests use, so what is asserted is the frames a viewer
would receive rather than a mock's call log.

The layer entries and the viewport are the viewer's contract: the numbers and
key names below are the ones viewtopia reads, so a drift there fails here.
"""

import asyncio
import json
import time

import jwt
import pytest
import respx
from websockets.asyncio.server import serve

from src.api import live_document
from src.core import agora, utils
from src.core.auth import SECRET_ENV
from tests.test_agora import SNAPSHOT, FakeAgora
from tests.test_route_auth import SECRET, mint

DOCUMENT_ID = "0f8b1c2d-3e4f-4a5b-8c7d-9e0f1a2b3c4d"
LINK_TOKEN = "kXv3-2_QeR9tYuI0pAsDfg"

POINT = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "depot"},
            "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
        }
    ],
}


def ui_spec(*files) -> str:
    layers = [
        {"name": file.split(".")[0].title(), "file": file, "color": "#3388ff"}
        for file in files
    ]
    return "Done. __UI_SPEC__:" + json.dumps({"type": "map", "layers": layers})


def viewer_command(action="fly_to", **params) -> str:
    return "__VIEWER_CMD__:" + json.dumps({"action": action, "params": params})


def reader(**files):
    """A layer reader over a fixed set of names, standing in for the file tree."""

    def read(name):
        return files.get(name)

    return read


@pytest.fixture
def caller(monkeypatch):
    """A live platform token, and the secret that makes it one."""
    monkeypatch.setenv(SECRET_ENV, SECRET)
    return mint(sub="u1", name="Ada")


def publish(fake, result, monkeypatch, read=None, binding=DOCUMENT_ID, token=None):
    """Run `result` through the bridge against `fake`, and answer with its note."""

    async def main():
        async with serve(fake.handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setenv(agora.AGORA_URL_ENV, f"http://127.0.0.1:{port}")
            with respx.mock(base_url=f"http://127.0.0.1:{port}", assert_all_called=False) as mock:
                mock.put(url__startswith="http://").respond(204)
                note = await live_document.publish(
                    binding, token, result, read or reader()
                )
            # presence has no ack, so give the server a turn to read it
            await asyncio.sleep(0.05)
            return note

    return asyncio.run(main())


def operations(fake) -> list:
    """The (key, value) pairs the fake was sent, batches flattened."""
    pairs = []
    for frame in fake.received:
        if frame["type"] == "op":
            pairs.append((frame["key"], frame["value"]))
        elif frame["type"] == "batch":
            pairs.extend((op["key"], op["value"]) for op in frame["ops"])
    return pairs


def presence(fake) -> list:
    return [frame for frame in fake.received if frame["type"] == "presence"]


# ── the binding header ───────────────────────────────────────────────────


def test_no_header_is_no_document():
    assert live_document.document_binding({}) is None
    assert live_document.document_binding({live_document.DOCUMENT_HEADER: " "}) is None


def test_the_header_carries_the_binding():
    headers = {live_document.DOCUMENT_HEADER: f" {DOCUMENT_ID} "}

    assert live_document.document_binding(headers) == DOCUMENT_ID


def test_a_document_id_and_a_link_token_are_told_apart():
    assert live_document.document_id_of(DOCUMENT_ID) == DOCUMENT_ID
    assert live_document.document_id_of(LINK_TOKEN) is None


def test_a_result_with_no_map_effects_never_connects(caller, monkeypatch):
    fake = FakeAgora()

    note = publish(fake, "Nothing to draw here.", monkeypatch, token=caller)

    assert note is None
    assert fake.received == []


# ── layers ───────────────────────────────────────────────────────────────


def test_a_ui_spec_layer_becomes_an_entry_the_viewer_can_draw(caller, monkeypatch):
    fake = FakeAgora()

    note = publish(
        fake,
        ui_spec("depots.gpkg"),
        monkeypatch,
        read=reader(**{"depots.gpkg": POINT}),
        token=caller,
    )

    (key, entry), = operations(fake)
    layer_id = live_document.layer_id_for("depots.gpkg")
    assert key == f"layers/{layer_id}"
    assert entry == {
        "layerId": layer_id,
        "name": "Depots",
        "type": "geojson",
        "visible": True,
        "opacity": 1,
        "order": "V",
        "source": {"kind": "geojson", "geojson": POINT},
    }
    assert note == "Live document: 1 layer published."


def test_a_layer_id_is_the_same_however_the_file_was_named():
    """The model writes the same file three ways, and it is one layer."""
    first = live_document.layer_id_for("outputs/roads.gpkg")

    assert live_document.layer_id_for("roads.gpkg") == first
    assert live_document.layer_id_for("roads") == first
    assert live_document.layer_id_for("other.gpkg") != first


def test_a_new_layer_is_ordered_after_the_ones_already_there(caller, monkeypatch):
    existing = {
        "someone-elses": {"layerId": "someone-elses", "name": "Base", "order": "V"}
    }
    fake = FakeAgora(snapshot={**SNAPSHOT, "state": {"layers": existing}})

    publish(
        fake,
        ui_spec("depots.gpkg"),
        monkeypatch,
        read=reader(**{"depots.gpkg": POINT}),
        token=caller,
    )

    (_, entry), = operations(fake)
    assert entry["order"] > "V"


def test_two_new_layers_keep_their_ui_spec_order(caller, monkeypatch):
    fake = FakeAgora()

    publish(
        fake,
        ui_spec("first.gpkg", "second.gpkg"),
        monkeypatch,
        read=reader(**{"first.gpkg": POINT, "second.gpkg": POINT}),
        token=caller,
    )

    orders = [entry["order"] for _, entry in operations(fake)]
    assert orders[0] < orders[1]


def test_rewriting_a_layer_keeps_what_the_document_says_about_it(caller, monkeypatch):
    """A member hid it and renamed it, and new features must not undo that."""
    layer_id = live_document.layer_id_for("depots.gpkg")
    existing = {
        layer_id: {
            "layerId": layer_id,
            "name": "Renamed by a member",
            "type": "geojson",
            "visible": False,
            "opacity": 0.5,
            "order": "b",
            "styleOverrides": {"style": {"opacity": 0.2}},
        }
    }
    fake = FakeAgora(snapshot={**SNAPSHOT, "state": {"layers": existing}})

    publish(
        fake,
        ui_spec("depots.gpkg"),
        monkeypatch,
        read=reader(**{"depots.gpkg": POINT}),
        token=caller,
    )

    (_, entry), = operations(fake)
    assert entry["name"] == "Renamed by a member"
    assert entry["visible"] is False
    assert entry["opacity"] == 0.5
    assert entry["order"] == "b"
    assert entry["styleOverrides"] == {"style": {"opacity": 0.2}}
    assert entry["source"]["geojson"] == POINT


def test_a_missing_file_is_reported_and_the_others_still_go(caller, monkeypatch):
    fake = FakeAgora()

    note = publish(
        fake,
        ui_spec("gone.gpkg", "depots.gpkg"),
        monkeypatch,
        read=reader(**{"depots.gpkg": POINT}),
        token=caller,
    )

    assert len(operations(fake)) == 1
    assert note == "Live document: 1 layer published. gone.gpkg was not found."


def test_an_unreadable_file_does_not_reach_the_document(caller, monkeypatch):
    def read(name):
        raise ValueError(f"/absolute/path/{name} is not a vector file")

    fake = FakeAgora()

    note = publish(fake, ui_spec("junk.gpkg"), monkeypatch, read=read, token=caller)

    assert fake.received == []
    assert note == "Live document: nothing to publish. junk.gpkg could not be read."
    # the reader quotes the absolute path, which maps out the container
    assert "/absolute/path" not in note


# ── layer data too large to carry ────────────────────────────────────────


def big_feature_collection(features: int) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": f"site {index}", "note": "x" * 200},
                "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
            }
            for index in range(features)
        ],
    }


def test_a_layer_over_the_inline_limit_travels_as_a_url(caller, monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "LIVE_DATA_DIR", tmp_path / "live_data")
    monkeypatch.setenv(live_document.PUBLIC_URL_ENV, "/agent")
    collection = big_feature_collection(300)
    fake = FakeAgora()

    publish(
        fake,
        ui_spec("big.gpkg"),
        monkeypatch,
        read=reader(**{"big.gpkg": collection}),
        token=caller,
    )

    (_, entry), = operations(fake)
    assert entry["source"]["kind"] == "url"
    assert entry["source"]["format"] == "geojson"
    assert agora.value_bytes(entry) < live_document.MAXIMUM_INLINE_SOURCE_BYTES

    token = entry["source"]["url"].rsplit("/", 1)[1]
    assert entry["source"]["url"] == f"/agent/live-data/{token}"
    written = (tmp_path / "live_data" / f"{token}.geojson").read_text()
    assert json.loads(written) == collection


def test_a_published_url_names_a_token_nobody_could_guess(caller, monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "LIVE_DATA_DIR", tmp_path / "live_data")
    urls = set()
    for _ in range(3):
        fake = FakeAgora()
        publish(
            fake,
            ui_spec("big.gpkg"),
            monkeypatch,
            read=reader(**{"big.gpkg": big_feature_collection(300)}),
            token=caller,
        )
        (_, entry), = operations(fake)
        urls.add(entry["source"]["url"])

    # the same features every time, and never the same url: the token is minted,
    # not derived from anything a stranger could reconstruct
    assert len(urls) == 3
    for url in urls:
        assert live_document.LIVE_DATA_TOKEN_PATTERN.fullmatch(url.rsplit("/", 1)[1])


def test_a_layer_too_large_to_publish_is_left_out(caller, monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "LIVE_DATA_DIR", tmp_path / "live_data")
    monkeypatch.setattr(live_document, "MAXIMUM_PUBLISHED_LAYER_BYTES", 1024)
    fake = FakeAgora()

    note = publish(
        fake,
        ui_spec("big.gpkg"),
        monkeypatch,
        read=reader(**{"big.gpkg": big_feature_collection(300)}),
        token=caller,
    )

    assert fake.received == []
    assert note == "Live document: nothing to publish. big.gpkg is too large to publish."
    assert not (tmp_path / "live_data").exists()


# ── the camera ───────────────────────────────────────────────────────────


def test_a_fly_to_moves_the_agents_presence(caller, monkeypatch):
    fake = FakeAgora()

    note = publish(
        fake, viewer_command(lon=2.35, lat=48.85), monkeypatch, token=caller
    )

    assert presence(fake) == [
        {
            "type": "presence",
            "cursor": None,
            "selection": [],
            # zoom 16 is what the viewer's own fly_to lands on at its default
            # camera height of 1000 metres
            "viewport": {"center": [2.35, 48.85], "zoom": 16},
        }
    ]
    assert note == "Live document: camera moved."


def test_the_last_camera_command_is_the_one_that_lands(caller, monkeypatch):
    fake = FakeAgora()
    result = "\n".join(
        [
            viewer_command(lon=1.0, lat=1.0),
            viewer_command("set_view", lon=2.35, lat=48.85),
        ]
    )

    publish(fake, result, monkeypatch, token=caller)

    (frame,) = presence(fake)
    assert frame["viewport"] == {"center": [2.35, 48.85], "zoom": 14}


def test_a_command_that_does_not_move_the_camera_is_ignored(caller, monkeypatch):
    fake = FakeAgora()

    note = publish(
        fake, viewer_command("add_marker", lon=1.0, lat=1.0), monkeypatch, token=caller
    )

    assert note is None
    assert fake.received == []


def test_the_height_to_zoom_curve_is_the_viewers():
    assert live_document.height_to_zoom(1000) == 16
    assert live_document.height_to_zoom(5000) == 14
    # clamped at both ends, as the viewer clamps it
    assert live_document.height_to_zoom(1) == live_document.MAXIMUM_ZOOM
    assert live_document.height_to_zoom(10_000_000_000) == live_document.MINIMUM_ZOOM


def test_layers_and_the_camera_travel_together(caller, monkeypatch):
    fake = FakeAgora()
    result = ui_spec("depots.gpkg") + "\n" + viewer_command(lon=2.35, lat=48.85)

    note = publish(
        fake, result, monkeypatch, read=reader(**{"depots.gpkg": POINT}), token=caller
    )

    assert len(operations(fake)) == 1
    assert len(presence(fake)) == 1
    # the layer lands before the camera, so peers are not flown to an empty map
    assert fake.received[0]["type"] == "op"
    assert note == "Live document: 1 layer published, camera moved."


# ── the fractional index ─────────────────────────────────────────────────


def test_the_first_index_is_the_one_the_viewer_generates():
    assert live_document.order_after(None) == "V"


def test_an_index_always_sorts_after_the_one_below_it():
    order = None
    for _ in range(200):
        following = live_document.order_after(order)
        assert order is None or following > order
        assert live_document.valid_order(following)
        order = following


def test_an_index_the_viewer_would_reject_is_not_built_on():
    entries = {"a": {"order": "V0"}, "b": {"order": ""}, "c": {"order": 7}}

    assert live_document.last_order(entries) is None
    assert live_document.last_order({"a": {"order": "W"}}) == "W"


# ── who the agent is ─────────────────────────────────────────────────────


def test_the_agent_is_its_own_identity_derived_from_the_caller(caller):
    subject, name = live_document.agent_identity(caller)

    assert subject == "agent:u1"
    assert name == "GeoLang agent (Ada)"


def test_a_forged_caller_token_is_nobody(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)

    assert live_document.agent_identity("not-a-jwt") is None
    assert live_document.agent_identity(mint(secret="another-platform-secret-0123456789")) is None
    assert live_document.agent_identity(None) is None


def test_the_agent_token_is_short_lived_and_signed_with_the_platform_secret(caller):
    subject, name = live_document.agent_identity(caller)
    token = live_document.sign_platform_token(
        subject, name, live_document.AGENT_TOKEN_LIFETIME_SECONDS
    )

    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["sub"] == "agent:u1"
    assert claims["name"] == "GeoLang agent (Ada)"
    assert claims["exp"] - int(time.time()) <= live_document.AGENT_TOKEN_LIFETIME_SECONDS


def test_without_a_platform_secret_a_document_id_cannot_be_bound(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    fake = FakeAgora()

    note = publish(fake, ui_spec("depots.gpkg"), monkeypatch, token=None)

    assert fake.received == []
    assert "nothing was written" in note
    assert SECRET_ENV in note


# ── membership ───────────────────────────────────────────────────────────


def test_the_membership_grant_is_made_with_the_callers_own_token(caller, monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    async def main():
        with respx.mock(base_url="http://agora:3000") as mock:
            route = mock.put(f"/documents/{DOCUMENT_ID}/members/agent:u1").respond(204)
            return route, await live_document.open_binding(DOCUMENT_ID, caller)

    route, (document_id, token) = asyncio.run(main())

    assert document_id == DOCUMENT_ID
    # the agent writes as itself, never as the caller
    assert token != caller
    assert jwt.decode(token, SECRET, algorithms=["HS256"])["sub"] == "agent:u1"
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {caller}"
    assert json.loads(request.content) == {"role": "edit"}


def test_a_caller_who_may_not_edit_writes_nothing(caller, monkeypatch):
    """agora refuses the grant, so the agent never reaches the document."""
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    with respx.mock(base_url="http://agora:3000") as mock:
        mock.put(url__startswith="http://").respond(
            403, json={"error": "edit role required"}
        )
        note = asyncio.run(
            live_document.publish(
                DOCUMENT_ID,
                caller,
                ui_spec("depots.gpkg"),
                reader(**{"depots.gpkg": POINT}),
            )
        )

    assert note == "Live document: nothing was written, agora refused the request: edit role required"


# ── share links ──────────────────────────────────────────────────────────


def test_a_share_link_writes_as_its_own_session(monkeypatch):
    """No platform identity and no grant: the link is the whole authority."""
    fake = FakeAgora()

    async def main():
        async with serve(fake.handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            monkeypatch.setenv(agora.AGORA_URL_ENV, base)
            with respx.mock(base_url=base, assert_all_called=False) as mock:
                mock.get(f"/links/{LINK_TOKEN}").respond(
                    200,
                    json={
                        "doc": DOCUMENT_ID,
                        "role": "edit",
                        "sessionToken": "session.jwt",
                    },
                )
                grant = mock.put(url__startswith="http://").respond(204)
                note = await live_document.publish(
                    LINK_TOKEN, None, ui_spec("depots.gpkg"), reader(**{"depots.gpkg": POINT})
                )
                return note, grant.called

    note, granted = asyncio.run(main())

    assert note == "Live document: 1 layer published."
    assert granted is False
    assert fake.authorization == "Bearer session.jwt"


def test_a_read_only_share_link_is_refused(monkeypatch):
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://agora:3000")

    with respx.mock(base_url="http://agora:3000") as mock:
        mock.get(f"/links/{LINK_TOKEN}").respond(
            200, json={"doc": DOCUMENT_ID, "role": "view", "sessionToken": "session.jwt"}
        )
        note = asyncio.run(
            live_document.publish(
                LINK_TOKEN, None, ui_spec("depots.gpkg"), reader(**{"depots.gpkg": POINT})
            )
        )

    assert note == "Live document: nothing was written, that share link is read only"


# ── failure never costs the tool result ──────────────────────────────────


def test_an_unreachable_agora_is_a_note_and_nothing_more(caller, monkeypatch):
    # nothing listening on this port, so the connection is refused outright
    monkeypatch.setenv(agora.AGORA_URL_ENV, "http://127.0.0.1:1")

    with respx.mock(base_url="http://127.0.0.1:1") as mock:
        mock.put(url__startswith="http://").respond(204)
        note = asyncio.run(
            live_document.publish(
                DOCUMENT_ID, caller, ui_spec("depots.gpkg"), reader(**{"depots.gpkg": POINT})
            )
        )

    assert note.startswith("Live document: nothing was written, agora is unreachable")


def test_a_refused_write_is_a_note_and_nothing_more(caller, monkeypatch):
    fake = FakeAgora(error="document state limit reached")

    note = publish(
        fake,
        ui_spec("depots.gpkg"),
        monkeypatch,
        read=reader(**{"depots.gpkg": POINT}),
        token=caller,
    )

    assert "document state limit reached" in note
