"""The process that runs tool code, and holds nothing worth stealing.

A tool hands caller-written arguments to geopandas, QGIS and DuckDB, so this
process is treated as one an attacker may end up inside. It is given no platform
signing secret, no service account token and no model API key. The only
credential it sees is the caller's own bearer, which arrives per call, is
forwarded to the services that call's tool talks to, and is never written down.

`GEOLANG_EXECUTOR_SECRET` says the caller is the API. Whoever is inside here
already knows that value, which is the point: it keeps anything else that can
reach the port from running tools, and claims nothing about the process itself.

Arguments are validated again here rather than trusted from the API. This is a
network endpoint, and the schema is the only thing standing between a request
body and a tool's parameters.

The call names the outputs directory its files belong in, because verifying a
subject needs the signing secret this process is deliberately not given. The
name is checked here anyway, and a name that fails is refused: writing to the
shared parent instead is the leak this exists to close.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from threading import Thread
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ValidationError

from src.agents.agent_manager import load_external_tools
from src.core.tool_executor import EXECUTOR_SECRET_ENV, executor_secret
from src.core.user_token import bearer_token, user_token_scope
from src.core.utils import (
    caller_directory_scope,
    preload_geo_stack,
    valid_caller_directory_name,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def require_configuration() -> None:
    """Refuse to start with no shared secret, so nothing reachable can run tools."""
    if executor_secret() is None:
        raise RuntimeError(
            f"{EXECUTOR_SECRET_ENV} is not set. Generate a random value and give "
            "it to this process and to the API, so nothing else that reaches "
            "this port can run tools here."
        )


def executor_auth(
    x_geolang_executor: Annotated[str | None, Header()] = None,
) -> None:
    presented = (x_geolang_executor or "").strip()
    expected = executor_secret() or ""
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="missing or wrong executor secret")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Thread(target=preload_geo_stack, daemon=True).start()
    yield


require_configuration()

app = FastAPI(title="GeoLang tool executor", lifespan=lifespan)


class ToolCallRequest(BaseModel):
    args: dict = {}
    outputs_directory: str | None = None


# sync so FastAPI runs it in the threadpool: tools block for minutes
@app.post("/run/{name}", dependencies=[Depends(executor_auth)])
def run_tool(
    name: str,
    request: ToolCallRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Execute one tool as the holder of the bearer, and answer with its result."""
    directory = request.outputs_directory
    if directory is not None and not valid_caller_directory_name(directory):
        raise HTTPException(status_code=400, detail="malformed outputs directory")

    entry = next(
        (t for t in load_external_tools() if t[0].__name__ == name and t[1]), None
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")
    func, schema = entry

    try:
        args = schema(**request.args).model_dump(exclude_unset=True)
    except ValidationError as e:
        return {"error": f"Invalid arguments: {e}"}

    token = bearer_token(authorization)
    try:
        with user_token_scope(token), caller_directory_scope(directory):
            return {"result": str(func(**args))}
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return {"error": str(e)}


@app.get("/health")
def health():
    return {"status": "ok"}
