"""The bearer token the evals present to sibyl.

sibyl gates `/runs` and `/sessions` behind a platform JWT, so a harness talking
to it directly needs one of its own the way viewtopia's scripts do. Claims are
`{sub, exp}` with no `aud`, which is what every platform service decodes, and
none of the per-door markers (`token_use`, `geolang_use`, `agora_use`) sibyl
refuses.

Give the harness `NL_EVAL_TOKEN`, or `PLATFORM_JWT_SECRET` to mint from. With
neither, requests go out unauthenticated, which is right for a stack running
with `SIBYL_ALLOW_UNAUTHENTICATED=1`.
"""

import os
import time

import jwt

TOKEN_ENV = "NL_EVAL_TOKEN"
SECRET_ENV = "PLATFORM_JWT_SECRET"
SUBJECT = "geolang-evals"
# long enough for a --repeat 3 sweep, short enough that a leaked one expires
TOKEN_LIFETIME_SECONDS = 6 * 60 * 60


def bearer_token() -> str:
    """The token to present, or "" when the stack is running with auth off."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        return ""
    claims = {"sub": SUBJECT, "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS}
    return jwt.encode(claims, secret, algorithm="HS256")


def auth_headers() -> dict:
    """For the session routes, which read the Authorization header."""
    token = bearer_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def run_body(body: dict) -> dict:
    """`body` with the token where /runs looks for it, which is the body itself.

    The run route takes `user_token` as a field and never reads a header, since
    that is the token it hands on to the tools it calls.
    """
    token = bearer_token()
    return {**body, "user_token": token} if token else body
