# Architecture Overview

GeoLang is a thin FastAPI service that wraps a [Letta](https://github.com/letta-ai/letta) stateful agent and exposes it to ViewTopia (and other clients) over HTTP + Server-Sent Events. The "intelligence" lives in Letta; GeoLang is the integration surface that gives that agent geospatial tools and a viewer protocol.

## Process topology

```
┌──────────────┐    HTTP/SSE     ┌──────────────────────┐    Letta SDK    ┌──────────────┐
│  ViewTopia   │ ───────────────►│   GeoLang API        │ ──────────────► │ Letta server │
│  (browser)   │ ◄─────────────  │   (FastAPI, :8080)   │ ◄────────────── │   (:8283)    │
└──────────────┘   viewer_cmd    │                      │                 │              │
                                 │  loads tools from    │                 │  agent state │
                                 │  src/agents/tools/   │                 │  + LLM proxy │
                                 └──────────┬───────────┘                 └──────┬───────┘
                                            │                                    │
                          ┌─────────────────┼────────────────┐                   │
                          ▼                 ▼                ▼                   ▼
                    ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌────────────────┐
                    │ Ptolemy  │     │ Geokode  │     │ Itinera  │     │  LLM provider  │
                    │ TileTopia│     │ (geo-    │     │ (routing)│     │  (xAI / OpenAI │
                    │ (HTTP)   │     │  coding) │     │          │     │   / vLLM)      │
                    └──────────┘     └──────────┘     └──────────┘     └────────────────┘
```

- **Letta** owns the conversation, working memory blocks, and tool-call orchestration. GeoLang never sees raw token streams — it sees structured events (text, tool calls, tool results).
- **GeoLang API** ([`src/api/server.py`](../src/api/server.py)) is a single-file FastAPI app. On startup it scans `src/agents/tools/`, registers each tool with Letta, and either resumes an existing agent (via `.agent_id`) or creates a fresh one.
- **Tools** are plain Python functions discovered by `pkgutil.iter_modules` of the `tools` package. Each module exports `TOOL_FUNCTION` and optionally `TOOL_SCHEMA` (pydantic) and `TOOL_HELPERS`. They run in the GeoLang process and may shell out to QGIS, GeoPandas, or downstream services.
- **ViewTopia** consumes `/chat/stream` SSE and dispatches `viewer_cmd` events through [`viewer-commands.js`](https://github.com/GeoLang/viewtopia/blob/main/src/viewer-commands.js).

## SSE event vocabulary

`/chat/stream` emits newline-delimited `data: {...}` events. Types:

| `type` | Shape | Meaning |
|---|---|---|
| `progress` | `{text}` | Human-readable status while a tool runs. |
| `text` | `{text}` | An assistant message chunk. The viewer renders the last one received. |
| `viewer_cmd` | `{cmd: {action, params}}` | Imperative instruction for the viewer. See [`viewer_integration.md`](viewer_integration.md). |
| `ui_spec` | `{spec}` | Structured UI hint (e.g. `{type: "map", layers: [...]}`) — viewer-rendered. |
| `followups` | `{items}` | Suggested next prompts to surface in the chat UI. |
| `error` | `{text}` | Tool or LLM failure. |
| `done` | — | End of stream. |

## Tool registration flow

1. `load_external_tools()` reloads the `tools` package from disk on every startup, picks up modules exporting `TOOL_FUNCTION`.
2. `register_tool()` extracts the function's source (and any `TOOL_HELPERS`) and upserts it as a Letta tool. Source is concatenated so helper functions are visible inside the sandbox Letta runs the tool in.
3. The agent is created (or resumed) with the full tool name list.
4. On every restart the persona block is re-synced and any new tools are attached to the existing agent without resetting conversation state.

This means **editing a file in `src/agents/tools/` and restarting the API is enough** — there is no separate manifest, registry, or migration step.

## State and persistence

- **Agent ID** — `.agent_id` in `TOOL_EXEC_DIR`. Delete to force a fresh agent.
- **Conversation memory** — owned by Letta (Postgres-backed in the supplied compose).
- **Sessions** — `.sessions.json` is a thin GeoLang-side mapping for multi-conversation UX; switching sessions swaps the active `agent_id`.
- **User datasets** — `user_data/catalogue.json` lists uploaded files; the actual files live in `user_data/`.
- **Outputs** — tool results (GeoJSON, GPKG, rendered images) land in `outputs/` and are served at `/outputs/{filename}`.

## Surface boundaries

| Concern | Owner |
|---|---|
| LLM choice, context window, tool-call loop | Letta |
| Tool implementation, viewer protocol, file I/O | GeoLang API |
| Map rendering, DuckDB-WASM analysis, layer state | ViewTopia |
| Versioned geodatabase, multi-user editing | Ptolemy |
| OGC services (WMS/WFS/WMTS) | Fenestra |
| 3D Tiles | TileTopia |

The agent should prefer **client-side** work (a `viewer_cmd` like `sql_query` running in DuckDB-WASM) over server-side work (a Ptolemy REST call) when the data is reachable from the browser — it's lower latency and frees the server. See [`viewer_integration.md`](viewer_integration.md) for the heuristic.

## Known sharp edges

- **`src/core/` and `src/agents/agent_manager.py` are stubs** — the live code path is `src/api/server.py`. The stubs may be vestigial; treat them as such until reorganised.
- **CORS is wide-open** (`allow_origins=["*"]`) in [`server.py`](../src/api/server.py) — fine for development, must be tightened in any shared deployment.
- **API keys in compose** — `docker-compose.yml` has provider keys inline. Move to a `.env` file before sharing the repo (and rotate the existing ones).
- **Tool source is sent to Letta** — the entire `inspect.getsource(func)` text is uploaded as a Letta tool. Don't put secrets in tool module bodies; they will be persisted to the Letta backend.
