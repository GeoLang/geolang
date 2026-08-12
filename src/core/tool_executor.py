"""Where a tool call runs: in this process, or in the isolated executor.

A tool hands caller-written arguments to geopandas, QGIS and DuckDB, so a
process that runs one can be made to run other things. This process holds the
platform signing secret, which is worth every user's identity on every service
in the platform. With `GEOLANG_EXECUTOR_URL` set the call is forwarded to a
process that holds no signing secret, no service account and no model key: the
only credential it ever sees is the caller's own bearer, for the length of the
call.

Unset, tools run here. That is the standalone stack, the test suite and a
single-tenant self-host, where there is no second tenant for an escape to reach.
Nothing in the process can tell a single-tenant deployment from a multi-tenant
one, so this is a deployment's choice rather than something checked here.

The forwarded call also carries which outputs directory the files belong in.
Only this side holds the secret that turns a bearer into a subject, so the
executor would otherwise have to write every caller's files to one directory.
"""

from __future__ import annotations

import logging
import os

import httpx

from src.core.user_token import user_token_scope
from src.core.utils import current_caller_directory

logger = logging.getLogger(__name__)

EXECUTOR_URL_ENV = "GEOLANG_EXECUTOR_URL"
EXECUTOR_SECRET_ENV = "GEOLANG_EXECUTOR_SECRET"
# says the caller is the API rather than anything else that reached the port
EXECUTOR_SECRET_HEADER = "X-Geolang-Executor"
# a tool call routinely blocks for minutes: QGIS sessions, OSM extracts
EXECUTOR_TIMEOUT_SECONDS = 900.0


def executor_url() -> str | None:
    """Where the isolated executor answers, or None when tools run here."""
    return (os.environ.get(EXECUTOR_URL_ENV) or "").strip().rstrip("/") or None


def executor_secret() -> str | None:
    """The shared value the executor checks its callers against."""
    return (os.environ.get(EXECUTOR_SECRET_ENV) or "").strip() or None


def execute_tool(name: str, func, args: dict, token: str | None) -> str:
    """Run one already-validated tool call and answer with its result.

    Raises what the tool raised, so both callers report a failure the way they
    always have.
    """
    url = executor_url()
    if url is None:
        with user_token_scope(token):
            return str(func(**args))
    return _execute_remotely(url, name, args, token)


def _execute_remotely(url: str, name: str, args: dict, token: str | None) -> str:
    headers = {}
    secret = executor_secret()
    if secret:
        headers[EXECUTOR_SECRET_HEADER] = secret
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with user_token_scope(token):
        outputs_directory = current_caller_directory()

    try:
        response = httpx.post(
            f"{url}/run/{name}",
            # its own field, not one of the args: args are the caller's to write
            json={"args": args, "outputs_directory": outputs_directory},
            headers=headers,
            timeout=EXECUTOR_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"the tool executor is unreachable: {e}") from e

    if response.status_code != 200:
        raise RuntimeError(
            f"the tool executor refused the call with {response.status_code}"
        )

    body = response.json()
    if "error" in body:
        # the traceback stayed in the executor's log, where the code that raised
        # it also lives
        raise RuntimeError(str(body["error"]))
    return str(body["result"])


def report_configuration() -> None:
    """Log where tool code will run, and what that costs when it is here.

    A missing executor is not refused. Running tools in this process is a
    legitimate deployment, and a refusal would take out every gated self-host
    that has exactly one tenant.
    """
    from src.core.auth import SECRET_ENV, platform_secret

    url = executor_url()
    if url is None:
        if platform_secret() is None:
            logger.info("tools run in this process")
            return
        logger.warning(
            f"tools run in this process, which holds {SECRET_ENV}. A tool that "
            f"escapes its arguments can read that secret and sign a token for "
            f"any user on any service. Set {EXECUTOR_URL_ENV} before serving "
            "more than one tenant."
        )
        return

    if executor_secret() is None:
        logger.warning(
            f"{EXECUTOR_URL_ENV} is set but {EXECUTOR_SECRET_ENV} is not, so "
            "the executor will refuse every call."
        )
    logger.info(f"tools run in the executor at {url}")
