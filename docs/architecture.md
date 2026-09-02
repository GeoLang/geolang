# Architecture Overview

GeoLang is a FastAPI service that owns the geospatial tools and exposes an agent to ViewTopia (and other clients) over HTTP + Server-Sent Events. The agent loop lives in **sibyl**, a separate Rust service. GeoLang is the integration surface: it serves sibyl a tool manifest, runs the tools, and renders sibyl's run events as AG-UI.

## Process topology

```
┌──────────────┐    HTTP/SSE     ┌──────────────────────┐  POST /runs     ┌──────────────┐
│  ViewTopia   │ ───────────────►│   GeoLang API        │ ──────────────► │    sibyl     │
│  (browser)   │ ◄─────────────  │   (FastAPI, :8080)   │ ◄────────────── │   (:8090)    │
└──────────────┘   viewer_cmd    │                      │  NDJSON events  │              │
                                 │  loads tools from    │ ◄────────────── │  agent loop  │
                                 │  src/agents/tools/   │  GET /tools     │  + sessions  │
                                 │  and dispatches them │  POST /tools/x  │  + LLM calls │
                                 └──────────┬───────────┘                 └──────┬───────┘
                                            │                                    │
                          ┌─────────────────┼────────────────┐                   │
                          ▼                 ▼                ▼                   ▼
                    ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌────────────────┐
                    │ Ptolemy  │     │ Geokode  │     │ Itinera  │     │  LLM provider  │
                    │ TileTopia│     │ (geo-    │     │ (routing)│     │  (xAI / OpenAI │
                    │ (HTTP)   │     │  coding) │     │          │     │    via sibyl)  │
                    └──────────┘     └──────────┘     └──────────┘     └────────────────┘
```

- **sibyl** owns the conversation, sessions, history, and the tool-call loop. GeoLang never sees raw token streams, it sees NDJSON run events (text, tool calls, tool returns).
- **GeoLang API** ([`src/api/server.py`](../src/api/server.py)) is a single-file FastAPI app. It serves the tool manifest at `GET /tools`, executes a tool at `POST /tools/{name}`, and proxies `/sessions/*` to sibyl. `POST /chat/agui` opens a sibyl run and renders its events as AG-UI SSE.
- **Tools** are plain Python functions discovered by `pkgutil.iter_modules` of the `tools` package. Each module exports `TOOL_FUNCTION` and `TOOL_SCHEMA` (pydantic). They may shell out to QGIS, GeoPandas, or downstream services.
- **The tool executor** ([`src/api/executor.py`](../src/api/executor.py)) is where that code runs when `GEOLANG_EXECUTOR_URL` is set: a second process holding no platform signing secret, no service account and no model key. The API validates arguments, then [`src/core/tool_executor.py`](../src/core/tool_executor.py) either forwards the call or runs it here. See the README's "Where tool code runs".
- **ViewTopia** consumes `/chat/agui` (AG-UI protocol SSE) and dispatches `viewer_cmd` custom events through [`viewer/commands.ts`](https://github.com/GeoLang/viewtopia/blob/main/src/viewer/commands.ts). It sends its own state back on every message: a snapshot of what is on screen and the catalogue of actions it can run, both of which go into the run's system prompt.
- **Outside agents** reach the same tools over the Model Context Protocol at `POST /mcp` ([`src/api/mcp_server.py`](../src/api/mcp_server.py)), a raw ASGI endpoint with its own bearer gate rather than a FastAPI route. sibyl is not involved: an MCP client runs its own loop.
- **agora** is the live document service. With an `X-Agora-Document` header, a call's map effects are written into that document as the tool runs, so every open viewer redraws, and `asset_readings` answers about that document's sensors.

## SSE event vocabulary

`/chat/agui` emits newline-delimited `data: {...}` [AG-UI](https://docs.ag-ui.com/) events, wrapped in `RUN_STARTED`/`RUN_FINISHED`:

| event | Shape | Meaning |
|---|---|---|
| `CUSTOM` name=`progress` | `{value: {text}}` | Human-readable status while a tool runs. |
| `TEXT_MESSAGE_START/CONTENT/END` | `{messageId, delta}` | An assistant message; the full text arrives as one CONTENT delta. |
| `CUSTOM` name=`viewer_cmd` | `{value: {action, params}}` | Imperative instruction for the viewer. See [`viewer_integration.md`](viewer_integration.md). |
| `CUSTOM` name=`ui_spec` | `{value}` | Structured UI hint (e.g. `{type: "map", layers: [...]}`) — viewer-rendered. |
| `CUSTOM` name=`plan` | `{value: {title, project, validated, steps, datasets, outputs, formats, manifest}}` | A geodukt workflow awaiting the user's approval, from `plan_workflow`. `manifest` is the TOML `run_workflow` executes verbatim once they agree, and `validated` is false when geodukt has no `/validate` route to check it. |
| `RUN_ERROR` | `{message}` | Tool or LLM failure. |
| `RUN_FINISHED` | — | End of run. |

## Tool manifest and execution

1. `load_external_tools()` reloads the `tools` package from disk on every call, picking up modules that export `TOOL_FUNCTION`.
2. `GET /tools` turns those into a manifest: `name` from the function, `description` from its docstring, `parameters` from `TOOL_SCHEMA.model_json_schema()`. A module without a `TOOL_SCHEMA` is logged and left out, and so is one whose third-party imports this install cannot satisfy. [`tool_imports.py`](../src/agents/tool_imports.py) reads that off the module source, because the tools import their dependencies inside their function bodies.
3. A run whose viewer offered an action that does a tool's job goes without that tool: `hidden_tools` in [`viewer_state.py`](../src/api/viewer_state.py) reads each module's `TOOL_SUPERSEDED_BY` against the catalogue, and the names go to sibyl as `without_tools` on the run request.
4. sibyl decides to call a tool and posts `{"args": {...}}` to `POST /tools/{name}`. GeoLang validates the args against `TOOL_SCHEMA`, then runs the function in FastAPI's threadpool, since tools can block for minutes, or forwards the call to the executor when one is configured.
5. Everything comes back as `{"result": "<string>"}`, including failures, which start with ❌ so the agent can recover.

Because the reload happens per request, **editing a file in `src/agents/tools/` takes effect without a restart**. There is no registry or migration step.

## State and persistence

- **Conversation history and sessions** — owned by sibyl. GeoLang's `/sessions/*` routes are proxies, they store nothing.
- **Persona** — `PERSONA` in [`agent_manager.py`](../src/agents/agent_manager.py), sent as `system_prompt` on every run, so prompt edits take effect on the next message.
- **User datasets** — one directory per caller under `user_data/`, named for the token subject. `user_data/<caller>/catalogue.json` lists that caller's uploads and the files sit beside it.
- **Outputs** — tool results (GeoJSON, GPKG, rendered images) land in the caller's own directory under `outputs/` and are served to that caller at `/outputs/{filename}`. They are deleted by age: [`outputs_retention.py`](../src/api/outputs_retention.py) sweeps every caller directory at API startup and once a day after that, dropping files older than `GEOLANG_OUTPUTS_RETENTION_DAYS`, default 30. The sweep runs in the API server only, since the executor mounts the same volume.

## Surface boundaries

| Concern | Owner |
|---|---|
| LLM choice, context window, tool-call loop, history | sibyl |
| Tool implementation, viewer protocol, file I/O | GeoLang API |
| Map rendering, DuckDB-WASM analysis, layer state | ViewTopia |
| Versioned geodatabase, multi-user editing | Ptolemy |
| OGC services (WMS/WFS/WMTS) | Fenestra |
| 3D Tiles | TileTopia |

The agent should prefer **client-side** work (a `viewer_cmd` like `sql_query` running in DuckDB-WASM) over server-side work (a Ptolemy REST call) when the data is reachable from the browser — it's lower latency and frees the server. See [`viewer_integration.md`](viewer_integration.md) for the heuristic.

## Known limitations

- **Tools run in the API process unless `GEOLANG_EXECUTOR_URL` is set.** The platform compose runs `geolang-executor`. Standalone and tests stay in-process and log a warning when the auth gate is on. See [`DESIGN.md`](DESIGN.md).
- **CORS is origin-listed** once the auth gate is on. `CORS_ORIGINS` is a comma-separated list, it is required with the gate on, and `*` in it is refused. `GEOLANG_ALLOW_UNAUTHENTICATED=1` is the only way back to `*`.
- **Tool endpoints require a platform JWT** when the gate is on. `GET /tools` stays open so sibyl can fetch the manifest.
