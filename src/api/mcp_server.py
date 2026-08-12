"""The MCP endpoint: geolang's tools over streamable HTTP.

Same tools, same argument schemas and the same bearer gate as
`POST /tools/{name}`. This is a second protocol over one tool surface, not a
second tool surface.

Stateless on purpose. A streamable-HTTP session id would be a second credential
the platform gate does not check, so nothing is kept between requests and every
request stands on its own bearer.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import anyio.to_thread
import mcp_types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from src.agents.agent_manager import load_external_tools, runs_caller_code
from src.api.live_document import document_binding, publish
from src.core.auth import mcp_token_error
from src.core.tool_executor import execute_tool
from src.core.user_token import bearer_token

logger = logging.getLogger(__name__)

SERVER_NAME = "geolang"
MCP_PATH = "/mcp"
ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"
LOCALHOST_HOST_PATTERNS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]

INSTRUCTIONS = (
    "GeoLang's geospatial tools. Tools read and write files under the server's "
    "outputs and user_data directories and call the platform's map services as "
    "the bearer that made the request."
)


def transport_security_settings() -> TransportSecuritySettings:
    """Which Host headers the endpoint answers on, read once at startup.

    The SDK's DNS-rebinding check compares the Host header against this list and
    answers 421 for anything else. Behind a proxy the Host is the public name,
    so a deployment has to name it in `MCP_ALLOWED_HOSTS`.
    """
    configured = [
        host.strip()
        for host in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",")
        if host.strip()
    ]
    hosts = configured or LOCALHOST_HOST_PATTERNS
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[
            f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")
        ],
    )


class PlatformTokenGate:
    """The platform gate in front of the mounted MCP app.

    Every MCP request carries the bearer, including `initialize`, so an
    unauthenticated caller never learns which tools exist. The token has to be
    one minted for this endpoint, not any platform token the caller happens to
    hold.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = bearer_token(Headers(scope=scope).get("authorization"))
            detail = mcp_token_error(token)
            if detail is not None:
                response = JSONResponse(
                    {"detail": detail},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class NoStandaloneStream:
    """Decline the GET stream, which the spec lets a server do.

    A stateless server has no session to route anything to, so left to the SDK
    the GET is an SSE stream that stays open forever and never delivers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET":
            response = Response(status_code=405, headers={"Allow": "POST"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def external_tools() -> list[tuple]:
    """The tools this endpoint offers: everything except tools that run a
    caller-written payload, which an external agent must never author for
    someone else's browser."""
    return [
        (func, schema)
        for func, schema in load_external_tools()
        if not runs_caller_code(func)
    ]


def _caller_token(request: Request | None) -> str | None:
    """The bearer of the HTTP request this MCP message arrived on."""
    if request is None:
        return None
    return bearer_token(request.headers.get("authorization"))


def _text_result(text: str, is_error: bool = False) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(text=text)], is_error=is_error
    )


def create_mcp_app(
    tool_manifest: Callable[[], list[dict]],
    read_geojson: Callable[[str], dict | None],
) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """The MCP endpoint as an ASGI app, plus the session manager to run.

    The session manager has to be entered from the parent app's lifespan: its
    task group is what serves every request, and without it the first call
    fails.

    `read_geojson` turns a layer file this service holds into features a live
    document can carry. It is passed in rather than imported: the file routes
    own the confinement rules, and this module must not be a second place that
    decides which files may be read.
    """

    async def list_tools(
        ctx: ServerRequestContext, params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        offered = {func.__name__ for func, _ in external_tools()}
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(
                    name=tool["name"],
                    description=tool["description"],
                    input_schema=tool["parameters"],
                )
                for tool in tool_manifest()
                if tool["name"] in offered
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext, params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        token = _caller_token(ctx.request)
        # before the lookup, so an unauthenticated caller learns nothing from an
        # unknown-tool error. The gate already ran, this is what stops a message
        # that reached a handler without one from executing anything.
        detail = mcp_token_error(token)
        if detail is not None:
            raise MCPError(code=mcp_types.INVALID_REQUEST, message=detail)

        # the same list the manifest is built from: a tool left out of it is
        # unknown here too, rather than merely unadvertised
        entry = next(
            (t for t in external_tools() if t[0].__name__ == params.name and t[1]),
            None,
        )
        if entry is None:
            raise MCPError(
                code=mcp_types.INVALID_PARAMS, message=f"Unknown tool: {params.name}"
            )
        func, schema = entry

        try:
            args = schema(**(params.arguments or {})).model_dump(exclude_unset=True)
        except ValidationError as e:
            return _text_result(f"❌ Invalid arguments: {e}", is_error=True)

        def run_tool():
            return execute_tool(params.name, func, args, token)

        try:
            # a tool call blocks for minutes, so it must not run on the event
            # loop that is serving every other MCP request
            result = await anyio.to_thread.run_sync(run_tool)
        except Exception as e:
            logger.exception(f"Tool {params.name} failed")
            return _text_result(f"❌ Tool execution failed: {e}", is_error=True)

        text = str(result)
        binding = document_binding(ctx.request.headers) if ctx.request else None
        if binding is None:
            return _text_result(text)

        # the tool ran, so its result reaches the caller whatever the document
        # did: the write is reported beside it, never in place of it
        note = await publish(binding, token, text, read_geojson)
        if note is None:
            return _text_result(text)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(text=text), mcp_types.TextContent(text=note)],
            is_error=False,
        )

    server = Server(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security_settings(),
    )
    return PlatformTokenGate(NoStandaloneStream(app)), server.session_manager
