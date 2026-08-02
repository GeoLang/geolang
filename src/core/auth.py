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
before anyone has signed in, the static viewer, and reading a share by id, whose
whole point is a link that works for someone who never signs in.
"""

from __future__ import annotations

import os
from typing import Annotated

import jwt
from fastapi import Header, HTTPException

from src.core.user_token import bearer_token

SECRET_ENV = "PLATFORM_JWT_SECRET"


def platform_secret() -> str | None:
    """The shared HS256 secret, or None when the gate is off."""
    return (os.environ.get(SECRET_ENV) or "").strip() or None


def require_platform_token(token: str | None) -> None:
    """Reject anything that is not a live platform token. No-op in dev mode."""
    secret = platform_secret()
    if secret is None:
        return

    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")

    try:
        # naming the algorithm keeps a token that asks for "none", or an RS256
        # token forged with the public key, from being accepted
        jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["exp"]}
        )
    except jwt.PyJWTError:
        # the reason is not echoed back: separating "expired" from "bad
        # signature" helps an attacker more than a caller
        raise HTTPException(status_code=401, detail="invalid or expired token")


def platform_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    """Route dependency form of the gate, for routes that never read the token."""
    require_platform_token(bearer_token(authorization))
