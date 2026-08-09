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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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
LAYER_DATA_SUFFIX = ".geojson"
LAYER_TAG_SUFFIX = ".json"
# how long a published file nothing references is kept, so a publish happening
# right now is never swept before its own operation is acked
UNREFERENCED_AGE_SECONDS = 24 * 60 * 60
# how long a published file survives with nothing fetching it and no document we
# can check naming it. Every viewer join fetches the layers it draws, so this
# only comes for a file nobody has drawn and nothing reachable references.
MAXIMUM_UNUSED_AGE_SECONDS = 90 * 24 * 60 * 60
# documents rejoined per publish, which is what bounds what a tool call pays
SWEEP_DOCUMENT_LIMIT = 3
# agora's own word for a document that is not there, from its join refusal.
# Anything else keeps the files, so a rewording stops the sweep rather than
# emptying a document that is alive. Nothing in agora answers this yet: a
# deleted document cascades its members away, so the rejoin is refused as "not a
# member", which is what a plain removal looks like too and is no evidence the
# document died.
DOCUMENT_GONE_REASON = "no such document"
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


@dataclass(frozen=True)
class Binding:
    """The document a request writes to, and the identity it writes as."""

    document_id: str
    token: str
    # the agent subject, when there is one to rejoin the document as later. A
    # share link has none: its token is agora's to keep hashed, not ours to
    # write to disk.
    subject: str | None


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


def store_layer_data(body: bytes, binding: Binding) -> str:
    """Write features guests can fetch, and answer with the url that serves them.

    The token in the name is the whole credential, so it is minted here and the
    file is never written to again. Beside it goes the document the file was
    published into, which is the only handle a later publish has for deciding
    the file is dead. The data lands first, so a crash between the two leaves a
    tagless file expiry will reap rather than a tag pointing at nothing.

    The tag's own timestamp stays at the publish, which is what the
    unreferenced age is measured from. The data file's is the last use.
    """
    token = secrets.token_urlsafe(LIVE_DATA_TOKEN_BYTES)
    directory = utils.LIVE_DATA_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{token}{LAYER_DATA_SUFFIX}").write_bytes(body)

    tag = {"document": binding.document_id}
    if binding.subject:
        tag["subject"] = binding.subject
    (directory / f"{token}{LAYER_TAG_SUFFIX}").write_text(json.dumps(tag))
    return f"{public_url()}/{LIVE_DATA_PATH}/{token}"


@dataclass(frozen=True)
class PublishedLayer:
    """A file this service published, as its tag describes it."""

    token: str
    document: str
    # who to rejoin the document as, absent when a share link published the file
    subject: str | None
    written_at: float


def published_layers() -> list[PublishedLayer]:
    """Every published file whose tag says which document it belongs to.

    A file with no tag, an unreadable one, or one naming anything but this
    service's own agent identity is left out, so a tag that cannot be trusted
    makes its file untouchable rather than fair game.
    """
    directory = utils.LIVE_DATA_DIR
    if not directory.is_dir():
        return []

    found = []
    for path in sorted(directory.glob(f"*{LAYER_TAG_SUFFIX}")):
        token = path.name[: -len(LAYER_TAG_SUFFIX)]
        if not LIVE_DATA_TOKEN_PATTERN.fullmatch(token):
            continue
        try:
            tag = json.loads(path.read_text())
            document = str(tag["document"])
            written_at = path.stat().st_mtime
        except (OSError, ValueError, TypeError, KeyError):
            logger.warning(f"published layer {token} has no readable tag")
            continue
        if not document:
            continue
        subject = str(tag.get("subject") or "")
        found.append(
            PublishedLayer(
                token=token,
                document=document,
                subject=subject if subject.startswith(AGENT_SUBJECT_PREFIX) else None,
                written_at=written_at,
            )
        )
    return found


def live_data_token(url: str) -> str | None:
    """The token of a url this service published, or None for any other url.

    A layer entry can name any url a member put there, and only the ones this
    service serves are ours to delete.
    """
    prefix = f"{public_url()}/{LIVE_DATA_PATH}/"
    if not url.startswith(prefix):
        return None
    token = url[len(prefix) :]
    return token if LIVE_DATA_TOKEN_PATTERN.fullmatch(token) else None


def entry_layer_data(entry: object) -> str | None:
    """The published file a layer entry draws from, when it draws from one."""
    if not isinstance(entry, dict):
        return None
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("kind") != "url":
        return None
    return live_data_token(str(source.get("url") or ""))


def replaced_layer_data(operations: list, entries: dict) -> list[str]:
    """Tokens of the files the operations are about to leave unreferenced."""
    tokens = []
    for key, _ in operations:
        token = entry_layer_data(entries.get(key.split("/", 1)[-1]))
        if token:
            tokens.append(token)
    return tokens


def referenced_layer_data(entries: dict, operations: list) -> set[str]:
    """Every published file the document names once the operations land."""
    layers = dict(entries)
    for key, value in operations:
        layer_id = key.split("/", 1)[-1]
        if value is None:
            layers.pop(layer_id, None)
        else:
            layers[layer_id] = value
    return {
        token for token in map(entry_layer_data, layers.values()) if token is not None
    }


def delete_layer_data(token: str) -> None:
    """Delete a published file and its tag, if the token names one of ours.

    The pattern and the confinement below are the whole check: nothing here
    trusts that a token came from a url or a filename we wrote.
    """
    if not LIVE_DATA_TOKEN_PATTERN.fullmatch(token):
        return
    directory = str(utils.LIVE_DATA_DIR)
    for suffix in (LAYER_DATA_SUFFIX, LAYER_TAG_SUFFIX):
        path = utils.resolve_under([f"{token}{suffix}"], [directory], [directory])
        if path:
            Path(path).unlink(missing_ok=True)


def prune_layer_data(tokens: list) -> None:
    """Delete published files nothing points at any more.

    Only ever called once the write that replaced them is acked, so a file is
    gone only when the entry that named it is.
    """
    for token in tokens:
        try:
            delete_layer_data(token)
        except OSError as e:
            logger.warning(f"could not prune published layer data: {e}")


def refresh_layer_data(tokens) -> None:
    """Date a file as used now, which is what keeps expiry away from it."""
    directory = str(utils.LIVE_DATA_DIR)
    for token in tokens:
        if not LIVE_DATA_TOKEN_PATTERN.fullmatch(token):
            continue
        path = utils.resolve_under(
            [f"{token}{LAYER_DATA_SUFFIX}"], [directory], [directory]
        )
        if not path:
            continue
        try:
            os.utime(path)
        except OSError as e:
            logger.warning(f"could not date published layer data: {e}")


def reconcile_document(document_id: str, referenced: set) -> None:
    """Match this document's published files to what it still references.

    A member deleting a layer leaves its file behind, and the document itself is
    the only record that it is gone. Files younger than the unreferenced age are
    left alone whatever the document says: a publish in flight elsewhere has not
    had its own operation acked yet.

    Referencing covers files tagged to another document, or to none at all, so a
    file this document draws is kept alive by being drawn.
    """
    refresh_layer_data(referenced)
    now = time.time()
    for published in published_layers():
        if published.document != document_id or published.token in referenced:
            continue
        if now - published.written_at < UNREFERENCED_AGE_SECONDS:
            continue
        prune_layer_data([published.token])


def expire_layer_data(verified: set) -> None:
    """Delete published files nothing has drawn or claimed for a long time.

    The last line, and the only one that reaches a file whose document cannot be
    checked at all: one published through a share link, or one written before
    tags existed. `verified` is what a document confirmed it references during
    this publish, which is never expired however old the file is.
    """
    directory = utils.LIVE_DATA_DIR
    if not directory.is_dir():
        return
    now = time.time()
    for path in sorted(directory.glob(f"*{LAYER_DATA_SUFFIX}")):
        token = path.name[: -len(LAYER_DATA_SUFFIX)]
        if token in verified or not LIVE_DATA_TOKEN_PATTERN.fullmatch(token):
            continue
        try:
            if now - path.stat().st_mtime < MAXIMUM_UNUSED_AGE_SECONDS:
                continue
        except OSError as e:
            logger.warning(f"could not read the age of a published layer: {e}")
            continue
        prune_layer_data([token])


def sweepable_documents(current_document: str) -> list[tuple[str, str]]:
    """Other documents worth rejoining, oldest first, and who to rejoin as.

    A document holding even one recent file is left alone, and one with no
    stored subject cannot be rejoined at all.
    """
    now = time.time()
    grouped: dict[str, list[PublishedLayer]] = {}
    for published in published_layers():
        if published.document != current_document:
            grouped.setdefault(published.document, []).append(published)

    ready = []
    for document_id, files in grouped.items():
        if any(now - file.written_at < UNREFERENCED_AGE_SECONDS for file in files):
            continue
        subject = next((file.subject for file in files if file.subject), None)
        if subject is None:
            continue
        ready.append((min(file.written_at for file in files), document_id, subject))

    ready.sort()
    return [(document_id, subject) for _, document_id, subject in ready[:SWEEP_DOCUMENT_LIMIT]]


async def sweep_other_documents(current_document: str) -> set[str]:
    """Rejoin a few documents we published into, and clear what they dropped.

    Runs after the write, so agora answered a moment ago and each join here is
    a live round trip rather than a timeout. Answers with the files those
    documents confirmed they still reference.
    """
    verified: set[str] = set()
    for document_id, subject in await anyio.to_thread.run_sync(
        sweepable_documents, current_document
    ):
        token = sign_platform_token(subject, AGENT_NAME, AGENT_TOKEN_LIFETIME_SECONDS)
        if token is None:
            return verified
        try:
            async with agora.open_session(document_id, token) as session:
                referenced = referenced_layer_data(session.layers, [])
        except agora.AgoraError as e:
            if e.reason != DOCUMENT_GONE_REASON:
                # anything but "the document is gone" leaves the files alone: a
                # refused join is no evidence that nothing references them
                logger.info(f"live data sweep left {document_id} alone: {e}")
                continue
            referenced = set()
        verified |= referenced
        await anyio.to_thread.run_sync(reconcile_document, document_id, referenced)
    return verified


def style_overrides(layer: dict, current: dict) -> dict | None:
    """How the layer is drawn: what the document says, else the ui_spec colour.

    A member who restyled the layer keeps their styling, so the colour is only
    offered to an entry that carries no overrides yet.
    """
    if current.get("styleOverrides"):
        return current["styleOverrides"]
    color = str(layer.get("color") or "").strip()
    return {"color": color} if color else None


def layer_entry(layer: dict, layer_id: str, current: dict, order: str) -> dict:
    """One layer as the viewer reads it, keeping what the document already says."""
    overrides = style_overrides(layer, current)
    return {
        "layerId": layer_id,
        "name": str(current.get("name") or layer.get("name") or layer_id),
        "type": current.get("type") or LAYER_TYPE,
        "visible": current.get("visible", True),
        "opacity": current.get("opacity", 1),
        "order": order,
        **({"styleOverrides": overrides} if overrides else {}),
    }


def layer_operations(
    layers: list, entries: dict, read_geojson, binding: Binding
) -> tuple[list, list]:
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
                "url": store_layer_data(body, binding),
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


async def open_binding(binding: str, caller_token: str | None) -> Binding:
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
        return Binding(document_id=document, token=session_token, subject=None)

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
    return Binding(document_id=document_id, token=token, subject=subject)


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
        bound = await open_binding(binding, caller_token)
        async with agora.open_session(bound.document_id, bound.token) as session:
            entries = session.layers
            # reading a layer file blocks for seconds, and this loop is serving
            # every other request
            operations, problems = await anyio.to_thread.run_sync(
                layer_operations, layers, entries, read_geojson, bound
            )
            replaced = replaced_layer_data(operations, entries)
            await session.send_operations(operations)
            prune_layer_data(replaced)
            referenced = referenced_layer_data(entries, operations)
            if viewport is not None:
                await session.send_presence(viewport)
        note = summary(len(operations), viewport is not None, problems)
    except agora.AgoraError as e:
        return f"Live document: nothing was written, {e}"
    except Exception as e:
        logger.exception("live document write failed")
        return f"Live document: nothing was written, {type(e).__name__}: {e}"

    # the write is done and reported: housekeeping below must not take it back
    try:
        await anyio.to_thread.run_sync(
            reconcile_document, bound.document_id, referenced
        )
        verified = referenced | await sweep_other_documents(bound.document_id)
        await anyio.to_thread.run_sync(expire_layer_data, verified)
    except Exception:
        logger.exception("live data cleanup failed")
    return note
