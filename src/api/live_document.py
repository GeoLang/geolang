"""An MCP tool call's map effects, written into a live agora document.

A request binds itself to a document with the `X-Agora-Document` header, holding
either a document id or a share link token. Without the header nothing is
bound and a tool call behaves exactly as it did before.

The agent writes as an identity of its own rather than as the caller. What puts
that identity on a document is a membership grant made with the caller's own
token, so the agent can never reach a document its caller could not edit. A
share link binding writes as the link's own guest session instead, which is the
same authority a browser holding that link already has.

The layer and viewport shapes below are the viewer's contract, not agora's:
agora stores an operation value and never reads it, so nothing but the viewer
says what a layer entry looks like.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import uuid

import anyio.to_thread

from src.core import agora, utils
from src.core.auth import SECRET_ENV, platform_claims, sign_platform_token
from src.core.markers import UI_SPEC_MARKER, VIEWER_COMMAND_MARKER, marker_payloads

logger = logging.getLogger(__name__)

DOCUMENT_HEADER = "X-Agora-Document"

PUBLIC_URL_ENV = "GEOLANG_PUBLIC_URL"
# where a browser reaches this service, which is where nginx mounts it
DEFAULT_PUBLIC_URL = "/agent"

AGENT_SUBJECT_PREFIX = "agent:"
AGENT_NAME = "GeoLang agent"
# long enough for one connect, write and close, and worthless afterwards
AGENT_TOKEN_LIFETIME_SECONDS = 120

# the viewer's own headroom under agora's operation cap, measured over the whole
# serialized entry rather than the features alone
MAXIMUM_INLINE_SOURCE_BYTES = 48 * 1024
# a layer larger than this is left out rather than copied to disk
MAXIMUM_PUBLISHED_LAYER_BYTES = 32 * 1024 * 1024
LIVE_DATA_TOKEN_BYTES = 32
LIVE_DATA_PATH = "live-data"
# what a minted token looks like, so the open route can refuse anything else
# before it goes anywhere near the filesystem
LIVE_DATA_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{22,64}")
LAYER_TYPE = "geojson"
LAYER_ID_PREFIX = "geolang-"
LAYER_ID_DIGEST_CHARS = 12

ORDER_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# what the viewer's own camera commands land on, so the agent's presence matches
# what the command would have done in a browser
CAMERA_DEFAULT_HEIGHT_METRES = {"fly_to": 1000.0, "set_view": 5000.0}
CAMERA_ZOOM_NUMERATOR = 59_000_000.0
MINIMUM_CAMERA_HEIGHT_METRES = 200.0
MINIMUM_ZOOM = 3
MAXIMUM_ZOOM = 18


def public_url() -> str:
    return (os.environ.get(PUBLIC_URL_ENV) or DEFAULT_PUBLIC_URL).rstrip("/")


def document_binding(headers) -> str | None:
    """The document this request writes to, or None when it binds to none."""
    return (headers.get(DOCUMENT_HEADER) or "").strip() or None


# ── the viewer's fractional index ────────────────────────────────────────


def valid_order(order: object) -> bool:
    """What the viewer accepts as a fractional index it can insert against."""
    return (
        isinstance(order, str)
        and len(order) > 0
        and not order.endswith(ORDER_ALPHABET[0])
        and all(character in ORDER_ALPHABET for character in order)
    )


def order_after(lower: str | None) -> str:
    """The next fractional index above `lower`, as the viewer generates it."""
    prefix = ""
    remaining = lower or ""
    while True:
        digit = ORDER_ALPHABET.index(remaining[0]) if remaining else 0
        if len(ORDER_ALPHABET) - digit > 1:
            # javascript rounds a half up, python rounds it to even
            midpoint = math.floor((digit + len(ORDER_ALPHABET)) / 2 + 0.5)
            return prefix + ORDER_ALPHABET[midpoint]
        prefix += ORDER_ALPHABET[digit]
        remaining = remaining[1:]


def last_order(entries: dict) -> str | None:
    orders = [
        entry["order"]
        for entry in entries.values()
        if isinstance(entry, dict) and valid_order(entry.get("order"))
    ]
    return max(orders) if orders else None


# ── layers ───────────────────────────────────────────────────────────────


def layer_id_for(file: str) -> str:
    """A stable id per layer file, so re-running a tool replaces its layer.

    The name as written is not it: the same file reaches us as `roads`,
    `roads.gpkg` and `outputs/roads.gpkg`, and all three are one layer. A digest
    keeps the document key short whatever the file was called.
    """
    stem = os.path.splitext(os.path.basename(file))[0]
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()
    return f"{LAYER_ID_PREFIX}{digest[:LAYER_ID_DIGEST_CHARS]}"


def store_layer_data(body: bytes) -> str:
    """Write features guests can fetch, and answer with the url that serves them.

    The token in the name is the whole credential, so it is minted here and the
    file is never written to again.

    TODO: nothing prunes these, so a long lived deployment grows a file per
    published layer too large to inline.
    """
    token = secrets.token_urlsafe(LIVE_DATA_TOKEN_BYTES)
    directory = utils.LIVE_DATA_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{token}.geojson").write_bytes(body)
    return f"{public_url()}/{LIVE_DATA_PATH}/{token}"


def layer_entry(layer: dict, layer_id: str, current: dict, order: str) -> dict:
    """One layer as the viewer reads it, keeping what the document already says."""
    return {
        "layerId": layer_id,
        "name": str(current.get("name") or layer.get("name") or layer_id),
        "type": current.get("type") or LAYER_TYPE,
        "visible": current.get("visible", True),
        "opacity": current.get("opacity", 1),
        "order": order,
        **(
            {"styleOverrides": current["styleOverrides"]}
            if current.get("styleOverrides")
            else {}
        ),
    }


def layer_operations(layers: list, entries: dict, read_geojson) -> tuple[list, list]:
    """Operations for the layers of a ui_spec, plus what could not be published.

    Features travel inside the document while they fit under the viewer's inline
    limit, and as a url every member fetches when they do not.
    """
    operations = []
    problems = []
    previous_order = last_order(entries)
    for layer in layers:
        file = str(layer.get("file") or "").strip()
        if not file:
            continue
        try:
            geojson = read_geojson(file)
        except Exception:
            # the reader quotes the absolute path it choked on
            logger.exception(f"live layer {file} could not be read")
            problems.append(f"{file} could not be read")
            continue
        if geojson is None:
            problems.append(f"{file} was not found")
            continue

        layer_id = layer_id_for(file)
        current = entries.get(layer_id)
        current = current if isinstance(current, dict) else {}
        order = current["order"] if valid_order(current.get("order")) else None
        entry = layer_entry(layer, layer_id, current, order or order_after(previous_order))
        entry["source"] = {"kind": "geojson", "geojson": geojson}

        if agora.value_bytes(entry) >= MAXIMUM_INLINE_SOURCE_BYTES:
            body = json.dumps(geojson, separators=(",", ":")).encode("utf-8")
            if len(body) > MAXIMUM_PUBLISHED_LAYER_BYTES:
                problems.append(f"{file} is too large to publish")
                continue
            entry["source"] = {
                "kind": "url",
                "url": store_layer_data(body),
                "format": "geojson",
            }

        if order is None:
            previous_order = entry["order"]
        operations.append((f"layers/{layer_id}", entry))
    return operations, problems


# ── the camera ───────────────────────────────────────────────────────────


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def height_to_zoom(height: float) -> int:
    ratio = CAMERA_ZOOM_NUMERATOR / max(height, MINIMUM_CAMERA_HEIGHT_METRES)
    zoom = math.floor(math.log2(ratio) + 0.5)
    return max(MINIMUM_ZOOM, min(MAXIMUM_ZOOM, zoom))


def camera_viewport(commands) -> dict | None:
    """Where the last camera command among `commands` leaves the view."""
    viewport = None
    for command in commands:
        if not isinstance(command, dict):
            continue
        default_height = CAMERA_DEFAULT_HEIGHT_METRES.get(str(command.get("action")))
        parameters = command.get("params")
        if default_height is None or not isinstance(parameters, dict):
            continue
        longitude = _number(parameters.get("lon"))
        latitude = _number(parameters.get("lat"))
        if longitude is None or latitude is None:
            continue
        height = _number(parameters.get("height"))
        viewport = {
            "center": [longitude, latitude],
            "zoom": height_to_zoom(default_height if height is None else height),
        }
    return viewport


# ── who writes, and to which document ────────────────────────────────────


def agent_identity(caller_token: str | None) -> tuple[str, str] | None:
    """The subject and display name the agent writes under, from its caller.

    Derived from the caller, so one person's agent is one member of a document
    rather than a shared identity every caller's writes land under.
    """
    claims = platform_claims(caller_token)
    if not claims:
        return None
    subject = str(claims.get("sub") or "")
    if not subject:
        return None
    name = str(claims.get("name") or "").strip()
    return AGENT_SUBJECT_PREFIX + subject, f"{AGENT_NAME} ({name})" if name else AGENT_NAME


def document_id_of(binding: str) -> str | None:
    """`binding` as a document id, or None when it is a share link token."""
    try:
        return str(uuid.UUID(binding))
    except ValueError:
        return None


async def open_binding(binding: str, caller_token: str | None):
    """Resolve the header to a document and a token that may write to it."""
    document_id = document_id_of(binding)
    if document_id is None:
        resolution = await agora.resolve_share_link(binding)
        if resolution.get("role") != agora.EDIT_ROLE:
            raise agora.AgoraError("that share link is read only")
        session_token = str(resolution.get("sessionToken") or "")
        document = str(resolution.get("doc") or "")
        if not session_token or not document:
            raise agora.AgoraError("that share link did not resolve to a document")
        return document, session_token

    identity = agent_identity(caller_token)
    if identity is None:
        raise agora.AgoraError(
            f"binding to a document id needs {SECRET_ENV} set and a caller "
            "presenting a live platform token"
        )
    subject, name = identity
    token = sign_platform_token(subject, name, AGENT_TOKEN_LIFETIME_SECONDS)
    # the caller's own token, so agora refuses a grant the caller may not make
    await agora.grant_edit_role(document_id, subject, caller_token)
    return document_id, token


# ── the write ────────────────────────────────────────────────────────────


def summary(published: int, moved: bool, problems: list) -> str:
    done = []
    if published:
        done.append(f"{published} layer{'s' if published != 1 else ''} published")
    if moved:
        done.append("camera moved")
    parts = [", ".join(done) if done else "nothing to publish", *problems]
    return "Live document: " + ". ".join(parts) + "."


async def publish(binding: str, caller_token: str | None, result: str, read_geojson):
    """Write a tool result's map effects to the bound document.

    Answers with a line for the caller, or None when the result asked for
    nothing. Raises nothing: a document that could not be written must not cost
    the caller the tool result it is reported next to.
    """
    specs = list(marker_payloads(result, UI_SPEC_MARKER))
    commands = list(marker_payloads(result, VIEWER_COMMAND_MARKER))
    layers = [
        layer
        for spec in specs
        if isinstance(spec, dict) and spec.get("type") == "map"
        for layer in (spec.get("layers") or [])
        if isinstance(layer, dict)
    ]
    viewport = camera_viewport(commands)
    if not layers and viewport is None:
        return None

    try:
        document_id, token = await open_binding(binding, caller_token)
        async with agora.open_session(document_id, token) as session:
            # reading a layer file blocks for seconds, and this loop is serving
            # every other request
            operations, problems = await anyio.to_thread.run_sync(
                layer_operations, layers, session.layers, read_geojson
            )
            await session.send_operations(operations)
            if viewport is not None:
                await session.send_presence(viewport)
        return summary(len(operations), viewport is not None, problems)
    except agora.AgoraError as e:
        return f"Live document: nothing was written, {e}"
    except Exception as e:
        logger.exception("live document write failed")
        return f"Live document: nothing was written, {type(e).__name__}: {e}"
