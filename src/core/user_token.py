"""The caller's bearer token, for the length of one tool call.

The viewer's platform JWT rides the whole chain: viewer -> /chat/agui -> sibyl
-> /tools/{name} -> the services a tool calls (ptolemy, tiletopia, geodukt). It
is forwarded opaquely, never re-signed and never swapped for a token of our own.

Tools are called by name with only their schema arguments, so the token travels
in a context variable rather than through every tool signature. It is scoped to
one call and reset afterwards, so a pooled worker thread cannot carry one
caller's identity into the next request.

A headless run (the eval harness, a sibyl session with no token) carries no
token: services are then called anonymously, public reads work and gated writes
fail loud.

Never log it, never write it to disk.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

_user_token: ContextVar[str | None] = ContextVar("user_token", default=None)


def bearer_token(header_value: str | None) -> str | None:
    """The token out of an `Authorization: Bearer <jwt>` header value."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


@contextmanager
def user_token_scope(token: str | None):
    """Run the block as the holder of `token`. None means anonymous."""
    reset = _user_token.set(token or None)
    try:
        yield
    finally:
        _user_token.reset(reset)


def current_user_token() -> str | None:
    return _user_token.get()


def service_headers(fallback_env: str | None = None) -> dict:
    """Authorization header for an outbound call to a platform service.

    The caller's own token wins, so a tool acts as the person who asked.
    `fallback_env` names a service-account token variable to fall back on when
    there is no caller.
    """
    token = current_user_token()
    if not token and fallback_env:
        token = (os.environ.get(fallback_env) or "").strip() or None
    return {"Authorization": f"Bearer {token}"} if token else {}
