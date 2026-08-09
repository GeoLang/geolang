"""The bearer gate on the API.

Platform JWTs are HS256 over `{sub, exp, role}`, the shape ptolemy mints and
geodukt validates, so one token works across the platform. Setting
`PLATFORM_JWT_SECRET` turns the gate on, the same opt-in geodukt's `/run` uses.
Unset or empty means dev mode and no gate: the standalone stack, the test suite
and the eval harness all call the API without a token.

Only the signature and `exp` are checked. `role` is not: the services a tool
calls enforce their own rules and the token reaches them unchanged, so a role
check here would be a second, drifting copy of theirs.

Gated: everything that runs code, writes a file, or reads back a session or a
user's data. Open: `/health`, the `GET /tools` manifest sibyl fetches at startup
before anyone has signed in, the static viewer, reading a share by id, whose
whole point is a link that works for someone who never signs in, and reading a
live layer by its token, which a share link guest in a live document has to be
able to fetch without ever signing in.

`/mcp` is gated too, by ASGI middleware rather than a route dependency: it is a
mounted app, not a FastAPI route, so the dependency system never sees it.
"""

from __future__ import annotations

import os
import time
from typing import Annotated

import jwt
from fastapi import Header, HTTPException

from src.core.user_token import bearer_token

SECRET_ENV = "PLATFORM_JWT_SECRET"


def platform_secret() -> str | None:
    """The shared HS256 secret, or None when the gate is off."""
    return (os.environ.get(SECRET_ENV) or "").strip() or None


def platform_token_error(token: str | None) -> str | None:
    """Why `token` is not a live platform token, or None when it is one.

    The transports differ in how they answer (an HTTP status here, a JSON-RPC
    error on the MCP endpoint), so the check is separate from the rejection.
    """
    secret = platform_secret()
    if secret is None:
        return None

    if not token:
        return "missing bearer token"

    try:
        # naming the algorithm keeps a token that asks for "none", or an RS256
        # token forged with the public key, from being accepted
        jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["exp"]}
        )
    except jwt.PyJWTError:
        # the reason is not echoed back: separating "expired" from "bad
        # signature" helps an attacker more than a caller
        return "invalid or expired token"

    return None


def require_platform_token(token: str | None) -> None:
    """Reject anything that is not a live platform token. No-op in dev mode."""
    detail = platform_token_error(token)
    if detail is not None:
        raise HTTPException(status_code=401, detail=detail)


def platform_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    """Route dependency form of the gate, for routes that never read the token."""
    require_platform_token(bearer_token(authorization))


def platform_claims(token: str | None) -> dict | None:
    """The claims of a live platform token, or None when it is not one.

    Verified again rather than read: an unverified claim is an attacker's
    input, and this one decides which identity a document write is made under.
    """
    secret = platform_secret()
    if secret is None or not token:
        return None
    try:
        return jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["exp"]}
        )
    except jwt.PyJWTError:
        return None


def sign_platform_token(subject: str, name: str, lifetime_seconds: int) -> str | None:
    """Mint a platform token of our own, or None when the gate is off.

    The only caller is the live document bridge, which needs an identity of its
    own to write as. Nothing here decides what that identity may do: the
    document's member list does, and only the caller's own token can add to it.
    """
    secret = platform_secret()
    if secret is None:
        return None
    return jwt.encode(
        {"sub": subject, "name": name, "exp": int(time.time()) + lifetime_seconds},
        secret,
        algorithm="HS256",
    )
