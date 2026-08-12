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

The signature and `exp` are checked. `role` is not. Before a tool runs, this
service exchanges the user or MCP token for a five-minute, role-free token with
only the exact downstream operation scopes that tool needs.

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

That marker only narrows which door of geolang the token opens. The MCP token
never reaches a tool or a downstream service.
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
MCP_SOURCE_ROLE_CLAIM = "source_role"
MAXIMUM_MCP_TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60

TOOL_TOKEN_USE_CLAIM = "token_use"
TOOL_TOKEN_USE = "tool"
TOOL_SCOPE_CLAIM = "scope"
TOOL_TOKEN_LIFETIME_SECONDS = 5 * 60


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
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["exp"]}
        )
    except jwt.PyJWTError:
        # the reason is not echoed back: separating "expired" from "bad
        # signature" helps an attacker more than a caller
        return "invalid or expired token"

    if TOOL_TOKEN_USE_CLAIM in claims:
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


def source_token_role(token: str | None) -> str | None:
    """The verified platform role from which a tool token would derive."""
    claims = platform_claims(token)
    if claims is None or TOOL_TOKEN_USE_CLAIM in claims:
        return None
    claim = (
        MCP_SOURCE_ROLE_CLAIM
        if claims.get(MCP_CLAIM) == MCP_CLAIM_VALUE
        else "role"
    )
    role = claims.get(claim)
    return role if isinstance(role, str) and role else None


def _sign(claims: dict) -> str | None:
    """One place that signs, or None when the gate is off."""
    secret = platform_secret()
    if secret is None:
        return None
    return jwt.encode(claims, secret, algorithm="HS256")


def sign_tool_token(
    subject: str,
    name: str,
    lifetime_seconds: int,
    scopes: list[str] | tuple[str, ...],
    expires_at: int | None = None,
) -> str | None:
    """Mint a role-free token limited to exact downstream operations."""
    if not subject:
        raise ValueError("tool token subject must not be empty")
    if not 0 < lifetime_seconds <= TOOL_TOKEN_LIFETIME_SECONDS:
        raise ValueError("tool token lifetime must be between 1 and 300 seconds")
    unique_scopes = list(dict.fromkeys(scopes))
    if any(not isinstance(scope, str) or not scope for scope in unique_scopes):
        raise ValueError("tool scopes must be non-empty strings")

    expiry = int(time.time()) + lifetime_seconds
    if expires_at is not None:
        expiry = min(expiry, expires_at)

    claims = {
        "sub": subject,
        "exp": expiry,
        TOOL_TOKEN_USE_CLAIM: TOOL_TOKEN_USE,
        TOOL_SCOPE_CLAIM: unique_scopes,
    }
    if name:
        claims["name"] = name
    return _sign(claims)


def exchange_tool_token(
    source_token: str | None, scopes: list[str] | tuple[str, ...]
) -> str | None:
    """Exchange a verified user or MCP token for one short tool credential."""
    claims = platform_claims(source_token)
    if claims is None or TOOL_TOKEN_USE_CLAIM in claims:
        return None

    subject = str(claims.get("sub") or "")
    expires_at = claims.get("exp")
    if not subject or not isinstance(expires_at, (int, float)):
        return None

    return sign_tool_token(
        subject,
        str(claims.get("name") or ""),
        TOOL_TOKEN_LIFETIME_SECONDS,
        scopes,
        int(expires_at),
    )


def sign_mcp_token(
    subject: str, name: str, source_role: str, lifetime_seconds: int
) -> str | None:
    """Mint a token `/mcp` will accept, or None when the gate is off.

    The marker is a private claim rather than `aud` because the shared platform
    token shape has no audience. This token stops at the tool boundary, where it
    is exchanged for one carrying exact downstream operation scopes.
    """
    claims = {
        "sub": subject,
        "name": name,
        "exp": int(time.time()) + lifetime_seconds,
        MCP_CLAIM: MCP_CLAIM_VALUE,
    }
    if source_role:
        claims[MCP_SOURCE_ROLE_CLAIM] = source_role
    return _sign(claims)


def mcp_token_error(token: str | None) -> str | None:
    """Why `token` may not be presented at `/mcp`, or None when it may.

    The marker says which geolang door the token is for. It is exchanged before
    any tool or downstream service receives a bearer.
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
