"""The current execution bearer, for the length of one tool call.

The viewer's platform JWT reaches geolang through sibyl. At the tool boundary,
geolang exchanges it for a five-minute, role-free token containing only that
tool's downstream operation scopes.

Tools are called by name with only their schema arguments, so the token travels
in a context variable rather than through every tool signature. It is scoped to
one call and reset afterwards, so a pooled worker thread cannot carry one
caller's identity into the next request.

A headless run carries no token in standalone mode. Services are then called
anonymously, public reads work and gated writes fail loud.

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

    A tool's short, scoped execution token wins and keeps the caller's subject.
    `fallback_env` names a service-account token variable to fall back on when
    there is no caller, which only happens with the gate switched off: with it
    on there is always a caller, and falling back would let a request that
    arrived with no identity act as the service.
    """
    # imported here because auth.py reads bearer_token from this module
    from src.core.auth import authentication_disabled

    token = current_user_token()
    if not token and fallback_env and authentication_disabled():
        token = (os.environ.get(fallback_env) or "").strip() or None
    return {"Authorization": f"Bearer {token}"} if token else {}
