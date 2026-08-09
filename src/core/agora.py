"""Client for agora, the platform's live document service.

One connection per request that needs one: connect, read the opening snapshot,
send the operations, wait for their acks, close. Nothing is held open between
requests, so there is never a socket without an owner.

Only frames live here. What a layer entry looks like inside an operation is the
viewer's contract and belongs with the code that builds one.

agora's own caps are mirrored below so a frame it would refuse never leaves this
process, and the write path fails loud rather than half applying.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)

AGORA_URL_ENV = "AGORA_URL"
DEFAULT_AGORA_URL = "http://agora:3000"

EDIT_ROLE = "edit"
VIEW_ROLE = "view"

MAXIMUM_OPERATION_VALUE_BYTES = 64 * 1024
MAXIMUM_OPERATIONS_PER_SECOND = 60
MAXIMUM_OPERATIONS_PER_BATCH = 50
# agora counts one message per operation, so back to back full frames would trip
# its per-second limit
BATCH_INTERVAL_SECONDS = 1.0
# the document cap, which is the largest snapshot a join can be answered with
MAXIMUM_INBOUND_FRAME_BYTES = 4 * 1024 * 1024

CONNECT_TIMEOUT_SECONDS = 10.0
OPENING_TIMEOUT_SECONDS = 15.0
ACK_TIMEOUT_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 10.0


class AgoraError(Exception):
    """agora refused something, or could not be reached."""


def agora_url() -> str:
    return (os.environ.get(AGORA_URL_ENV) or DEFAULT_AGORA_URL).rstrip("/")


def websocket_url(document_id: str) -> str:
    base = agora_url()
    scheme, _, authority = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{authority}/ws?doc={quote(document_id, safe='')}"


def _encode(frame: dict) -> str:
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def value_bytes(value: object) -> int:
    """Size of an operation value on the wire, which is what agora measures."""
    return len(_encode(value).encode("utf-8"))


async def _receive(connection, timeout: float) -> dict:
    try:
        raw = await asyncio.wait_for(connection.recv(), timeout)
    except asyncio.TimeoutError as e:
        raise AgoraError("agora stopped answering") from e
    except WebSocketException as e:
        raise AgoraError(f"agora closed the connection: {e}") from e
    try:
        message = json.loads(raw)
    except ValueError as e:
        raise AgoraError("agora sent a frame that is not json") from e
    if not isinstance(message, dict):
        raise AgoraError("agora sent a frame that is not an object")
    return message


@dataclass
class AgoraSession:
    """One open document, from the snapshot to the close.

    `document` is the state agora had when we joined, which is what an order or
    a layer id has to be chosen against.
    """

    connection: object
    actor: str
    role: str
    document: dict
    client_seq: int = field(default=0, init=False)

    @property
    def layers(self) -> dict:
        layers = self.document.get("layers")
        return layers if isinstance(layers, dict) else {}

    async def send_operations(self, operations: list[tuple[str, object]]) -> None:
        """Write keys and wait for agora to order them. `None` value deletes."""
        if self.role != EDIT_ROLE:
            raise AgoraError("this session may not write to the document")
        if not operations:
            return
        frames = [
            operations[start : start + MAXIMUM_OPERATIONS_PER_BATCH]
            for start in range(0, len(operations), MAXIMUM_OPERATIONS_PER_BATCH)
        ]
        for index, frame_operations in enumerate(frames):
            if index:
                await asyncio.sleep(BATCH_INTERVAL_SECONDS)
            await self._send_frame(frame_operations)

    async def send_presence(self, viewport: dict) -> None:
        """Move the agent's camera, which peers following it will match.

        Presence is relayed and never stored, so there is no ack to wait for.
        """
        await self._send(
            {"type": "presence", "cursor": None, "selection": [], "viewport": viewport}
        )

    async def _send_frame(self, operations: list[tuple[str, object]]) -> None:
        for key, value in operations:
            size = value_bytes(value)
            if size > MAXIMUM_OPERATION_VALUE_BYTES:
                raise AgoraError(f"{key} is {size} bytes, over agora's operation cap")

        self.client_seq += 1
        client_seq = self.client_seq
        if len(operations) == 1:
            key, value = operations[0]
            frame = {"type": "op", "clientSeq": client_seq, "key": key, "value": value}
        else:
            frame = {
                "type": "batch",
                "clientSeq": client_seq,
                "ops": [{"key": key, "value": value} for key, value in operations],
            }
        await self._send(frame)
        await self._await_ack(client_seq)

    async def _send(self, frame: dict) -> None:
        try:
            await self.connection.send(_encode(frame))
        except WebSocketException as e:
            raise AgoraError(f"agora closed the connection: {e}") from e

    async def _await_ack(self, client_seq: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ACK_TIMEOUT_SECONDS
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AgoraError(f"agora did not acknowledge write {client_seq}")
            message = await _receive(self.connection, remaining)
            if message.get("type") == "error":
                raise AgoraError(f"agora refused the write: {message.get('reason')}")
            if message.get("type") == "ack" and message.get("clientSeq") == client_seq:
                return


async def _read_opening(connection) -> AgoraSession:
    """Read as far as the peers frame, which is agora saying the join is live."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OPENING_TIMEOUT_SECONDS
    actor = ""
    role = VIEW_ROLE
    document: dict = {}
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AgoraError("agora did not finish opening the document")
        message = await _receive(connection, remaining)
        kind = message.get("type")
        if kind == "snapshot":
            state = message.get("state")
            document = state if isinstance(state, dict) else {}
            actor = str(message.get("actor") or "")
            # no snapshot means no role, and the session then refuses to write
            role = str(message.get("role") or VIEW_ROLE)
        elif kind == "peers":
            return AgoraSession(
                connection=connection, actor=actor, role=role, document=document
            )
        elif kind == "error":
            raise AgoraError(f"agora refused the join: {message.get('reason')}")


@asynccontextmanager
async def open_session(document_id: str, token: str):
    """A live document, closed again when the block ends."""
    url = websocket_url(document_id)
    try:
        connection = await connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            max_size=MAXIMUM_INBOUND_FRAME_BYTES,
        )
    except (OSError, WebSocketException, asyncio.TimeoutError) as e:
        raise AgoraError(f"agora is unreachable: {e}") from e

    try:
        session = await _read_opening(connection)
    except BaseException:
        await connection.close()
        raise

    try:
        yield session
    finally:
        await connection.close()


def _refusal(response: httpx.Response) -> str:
    try:
        reason = response.json().get("error")
    except ValueError:
        reason = None
    return str(reason or response.status_code)


async def _request(method: str, path: str, token: str | None, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(
            base_url=agora_url(), timeout=HTTP_TIMEOUT_SECONDS
        ) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        raise AgoraError(f"agora is unreachable: {e}") from e
    if response.status_code >= 400:
        raise AgoraError(f"agora refused the request: {_refusal(response)}")
    if not response.content:
        return {}
    return response.json()


async def grant_edit_role(document_id: str, user_id: str, caller_token: str) -> None:
    """Give `user_id` edit rights on the document, acting as the caller.

    The caller's own token is what keeps the agent out of documents its caller
    could not edit: agora refuses a grant the caller may not make.
    """
    await _request(
        "PUT",
        f"/documents/{quote(document_id, safe='')}/members/{quote(user_id, safe='')}",
        caller_token,
        json={"role": EDIT_ROLE},
    )


async def resolve_share_link(link_token: str) -> dict:
    """`{doc, role, sessionToken}` for a share link. Open, by agora's design."""
    return await _request(
        "GET", f"/links/{quote(link_token, safe='')}", None
    )
