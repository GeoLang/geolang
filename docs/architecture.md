# Architecture Overview

GeoLang is a FastAPI service that holds the geospatial tools and exposes an agent to ViewTopia and other clients over HTTP and Server-Sent Events. The agent loop runs in **sibyl**, a separate Rust service. GeoLang serves sibyl a tool manifest, runs the tools, and renders sibyl's run events as AG-UI.

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
                    │ TileTopia│     │ (geo-    │     │ (routing)│     │ (OpenAI-style, │
                    │ geodukt  │     │  coding) │     │          │     │ cloud or local)│
                    └──────────┘     └──────────┘     └──────────┘     └────────────────┘
```

- **sibyl** holds the conversation, sessions, history, and the tool-call loop. GeoLang never sees raw token streams. It reads NDJSON run events (text, tool calls, tool returns, errors).
- **GeoLang API** ([`src/api/server.py`](../src/api/server.py)) is the FastAPI app. It serves the tool manifest at `GET /tools`, executes a tool at `POST /tools/{name}`, and forwards `/sessions*`, `/models` and `/model*` to sibyl. `POST /chat/agui` opens a sibyl run and renders its events as AG-UI SSE.
- **Tools** are plain Python functions found by `pkgutil.iter_modules` over the `tools` package. Each module exports `TOOL_FUNCTION` and `TOOL_SCHEMA` (pydantic). They may call QGIS, GeoPandas, or platform services.
- **The tool executor** ([`src/api/executor.py`](../src/api/executor.py)) is where that code runs when `GEOLANG_EXECUTOR_URL` is set: a second process holding no platform signing secret, no service account and no model key. Each call runs in a worker process of its own, killed if it exceeds its memory or time limit. The API validates arguments, then [`src/core/tool_executor.py`](../src/core/tool_executor.py) either forwards the call or runs it in the API process. See the README's "Where tool code runs".
- **ViewTopia** reads `/chat/agui` and dispatches `viewer_cmd` custom events through [`viewer/commands.ts`](https://github.com/GeoLang/viewtopia/blob/master/src/viewer/commands.ts). It sends its own state with every message: a snapshot of what is on screen and the catalogue of actions it can run, both of which go into the run's system prompt.
- **Outside agents** reach the same tools over the Model Context Protocol at `POST /mcp` ([`src/api/mcp_server.py`](../src/api/mcp_server.py)), a raw ASGI endpoint with its own bearer check rather than a FastAPI route. sibyl is not involved, since an MCP client runs its own loop.
- **agora** is the live document service. With an `X-Agora-Document` header, an MCP call's map output is written into that document as the tool runs, so every open viewer redraws, and `asset_readings` answers about that document's sensors.

## SSE event vocabulary

`/chat/agui` emits `data: {...}` [AG-UI](https://docs.ag-ui.com/) events, opened by `RUN_STARTED`:

| Event | Shape | Meaning |
|---|---|---|
| `CUSTOM` name=`progress` | `{value: {text}}` | Readable status while a tool runs, and the first line of a tool error. |
| `TEXT_MESSAGE_START/CONTENT/END` | `{messageId, delta}` | An assistant message. The full text arrives as one CONTENT delta. |
| `CUSTOM` name=`viewer_cmd` | `{value: {action, params}}` | An instruction for the viewer. See [`viewer_integration.md`](viewer_integration.md). |
| `CUSTOM` name=`ui_spec` | `{value}` | A layout hint, such as `{type: "map", layers: [...]}`, that the viewer renders. |
| `CUSTOM` name=`plan` | `{value: {title, project, validated, steps, datasets, outputs, formats, manifest}}` | A geodukt workflow awaiting the user's approval, from `plan_workflow`. `manifest` is the TOML `run_workflow` executes once they approve, and `validated` is false when geodukt has no `/validate` route to check it. |
| `CUSTOM` name=`run` | `{value: {id, title, status, message, steps, outputs}}` | The per-step outcome of a `run_workflow` call and the output files it wrote. |
| `RUN_ERROR` | `{message}` | Tool or LLM failure. Ends the stream, no `RUN_FINISHED` follows. |
| `RUN_FINISHED` | none | End of a run that did not fail. |

A `: keepalive` SSE comment goes out after every 15 seconds without an event.

## Tool manifest and execution

1. `load_external_tools()` in [`agent_manager.py`](../src/agents/agent_manager.py) reloads the `tools` package from disk on every call, picking up modules that export `TOOL_FUNCTION`. Modules whose names start with `_` are shared helpers and are skipped.
2. `GET /tools` turns those into a manifest: `name` from the function, `description` from its docstring, `parameters` from `TOOL_SCHEMA.model_json_schema()`. A module without a `TOOL_SCHEMA` is logged and left out, and so is one whose third-party imports this install cannot satisfy. [`tool_imports.py`](../src/agents/tool_imports.py) reads that from the module source, because the tools import their dependencies inside their function bodies. `approve_workflow` sets `TOOL_APPROVAL_ROUTE_ONLY` and is left out too.
3. A run whose viewer offered an action that does a tool's job goes without that tool: `hidden_tools` in [`viewer_state.py`](../src/api/viewer_state.py) checks each module's `TOOL_SUPERSEDED_BY` against the catalogue, and the names go to sibyl as `without_tools` on the run request.
4. sibyl decides to call a tool and posts `{"args": {...}}` to `POST /tools/{name}`. GeoLang validates the arguments against `TOOL_SCHEMA`, exchanges the caller's bearer for a scoped tool token, then runs the function in FastAPI's threadpool, since tools can block for minutes, or forwards the call to the executor when one is configured.
5. Everything comes back as `{"result": "<string>"}`, including failures, which start with ❌ so the agent can recover.

Because the reload happens on every request, **editing a file in `src/agents/tools/` takes effect without a restart**. There is no registry beyond the package directory.

## State and persistence

- **Conversation history and sessions**: stored in sibyl. GeoLang's `/sessions*` routes forward to it and store nothing.
- **System prompt**: `PERSONA` in [`agent_manager.py`](../src/agents/agent_manager.py), plus the viewer's state and action catalogue when the request carries them, sent as `system_prompt` on every run. A prompt edit takes effect on the next message.
- **User datasets**: one directory per caller under `user_data/`, named for the token subject. `user_data/<caller>/catalogue.json` lists that caller's uploads and the files sit beside it.
- **Outputs**: tool results (GeoJSON, GPKG, rendered images) are written to the caller's own directory under `outputs/` and served to that caller at `/outputs/{filename}`. [`outputs_retention.py`](../src/api/outputs_retention.py) sweeps every caller directory at API startup and once a day, deleting files older than `GEOLANG_OUTPUTS_RETENTION_DAYS`, default 30. Only the API server sweeps, since the executor mounts the same volume.
- **Workflow plan records**: kept in memory for an hour, in the executor when one is configured, so a restart drops unapproved plans.
- **Shares**: stored in `.shares.json` under `TOOL_EXEC_DIR`.

## Surface boundaries

| Concern | Owner |
|---|---|
| LLM choice, context window, tool-call loop, history | sibyl |
| Tool implementation, viewer protocol, file I/O | GeoLang API |
| Map rendering, DuckDB-WASM analysis, layer state | ViewTopia |
| Versioned geodatabase, multi-user editing | Ptolemy |
| Multi-step pipelines (`plan_workflow`, `run_workflow`) | geodukt |
| Live shared map documents | agora |
| OGC services (WMS/WFS/WMTS) | Fenestra |
| 3D Tiles | TileTopia |

`sql_query` hands the viewer DuckDB SQL to run in the browser, and is meant for a one-off question over data the browser already reaches. Analysis of more than one step goes through `plan_workflow` and `run_workflow`, which give the user a reviewable plan and output files. See [`viewer_integration.md`](viewer_integration.md).

## Known limitations

- **Tools run in the API process unless `GEOLANG_EXECUTOR_URL` is set.** The platform compose runs `geolang-executor`. The standalone stack and the tests run tools in process, and the API logs a warning when the auth gate is on. See [`DESIGN.md`](DESIGN.md).
- **CORS is a named list** once the auth gate is on. `CORS_ORIGINS` is required with the gate on, and `*` in it is refused. Only `GEOLANG_ALLOW_UNAUTHENTICATED=1` allows `*`.
- **Tool routes need a platform JWT** when the gate is on. `GET /tools` stays open so sibyl can fetch the manifest.
