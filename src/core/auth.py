"""The bearer gate on the API.

Platform JWTs are HS256 over `{sub, exp, role}`, the shape ptolemy mints and
geodukt validates, so one token works across the platform.
`PLATFORM_JWT_SECRET` carries it and the service refuses to start without one,
which is what ptolemy and interiora already do.

Running with no gate at all takes `GEOLANG_ALLOW_UNAUTHENTICATED=1` as well, so
it is a thing someone chose rather than a variable someone forgot. That is the
standalone stack, the test suite and the eval harness, none of which hold a
token. A platform token is worth as much as an SSH key to this host: any holder
can run tools in this process, so treat it that way.

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
mounted app, not a FastAPI route, so the dependency system never sees it. It
takes a token minted by `POST /mcp/token`, marked with a private claim. A plain
platform token is refused there, so handing an outside agent a way in is a
deliberate act with an expiry on it rather than pasting the token you already
hold.

That marker only narrows which door of geolang the token opens. It is still an
ordinary platform token everywhere else, because a tool's outbound calls go out
as the token that arrived.
"""

from __future__ import annotations

import os
import time
from typing import Annotated

import jwt
from fastapi import Header, HTTPException

from src.core.user_token import bearer_token

SECRET_ENV = "PLATFORM_JWT_SECRET"
UNAUTHENTICATED_ENV = "GEOLANG_ALLOW_UNAUTHENTICATED"
TRUTHY = {"1", "true", "yes", "on"}

# which of geolang's doors a token is for. A private claim, so it is ignored by
# every other service rather than rejected the way an `aud` would be.
MCP_CLAIM = "geolang_use"
MCP_CLAIM_VALUE = "mcp"
MAXIMUM_MCP_TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60


def platform_secret() -> str | None:
    """The shared HS256 secret, or None when the gate is off."""
    return (os.environ.get(SECRET_ENV) or "").strip() or None


def authentication_disabled() -> bool:
    """Whether this process was told in writing to run without the gate."""
    return (os.environ.get(UNAUTHENTICATED_ENV) or "").strip().lower() in TRUTHY


def require_configuration() -> None:
    """Refuse to start unauthenticated unless someone asked for that.

    An unset secret used to mean an open API, so one missing variable served
    every tool to anyone who could reach the port and still looked healthy.
    """
    if platform_secret() is None and not authentication_disabled():
        raise RuntimeError(
            f"{SECRET_ENV} is not set. Set it to the shared platform secret, or "
            f"set {UNAUTHENTICATED_ENV}=1 to serve every tool to anyone who can "
            "reach this port."
        )


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


def _sign(claims: dict) -> str | None:
    """One place that signs, or None when the gate is off."""
    secret = platform_secret()
    if secret is None:
        return None
    return jwt.encode(claims, secret, algorithm="HS256")


def sign_platform_token(subject: str, name: str, lifetime_seconds: int) -> str | None:
    """Mint a platform token of our own, or None when the gate is off.

    The only caller is the live document bridge, which needs an identity of its
    own to write as. Nothing here decides what that identity may do: the
    document's member list does, and only the caller's own token can add to it.
    """
    return _sign(
        {"sub": subject, "name": name, "exp": int(time.time()) + lifetime_seconds}
    )


def sign_mcp_token(subject: str, name: str, lifetime_seconds: int) -> str | None:
    """Mint a token `/mcp` will accept, or None when the gate is off.

    The marker is a private claim rather than `aud` because every service in the
    platform decodes with an audience of None, which rejects any token carrying
    one. A tool's outbound calls go out as this very token, so an `aud` here
    would fail at ptolemy, geodukt and agora rather than at us.
    """
    return _sign(
        {
            "sub": subject,
            "name": name,
            "exp": int(time.time()) + lifetime_seconds,
            MCP_CLAIM: MCP_CLAIM_VALUE,
        }
    )


def mcp_token_error(token: str | None) -> str | None:
    """Why `token` may not be presented at `/mcp`, or None when it may.

    The marker says which door the token is for, and nothing more: everywhere
    else in the platform this is an ordinary token with an ordinary token's
    reach.
    """
    detail = platform_token_error(token)
    if detail is not None:
        return detail

    claims = platform_claims(token)
    if claims is None:
        return None

    if claims.get(MCP_CLAIM) != MCP_CLAIM_VALUE:
        return "this endpoint needs a token from POST /mcp/token"
    return None
