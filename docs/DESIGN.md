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

`PLATFORM_JWT_SECRET` is required to start. With it, every route that runs code, writes a file, or reads back a session or a user's data requires a live HS256 platform token and answers `401` otherwise. `/health`, the `GET /tools` manifest, the static viewer, reading a share by id and reading a live layer by its token stay open. `GEOLANG_ALLOW_UNAUTHENTICATED=1` opens all of it, and is the only way to get there.

There is no tool sandbox any more. A tool runs with the API's privileges, in its process, against the bind-mounted repo.

## 🔴 Rotate the API keys that were once committed to `docker-compose.yml`

`XAI_API_KEY` and `OPENAI_API_KEY` were committed as literals and are still in the git history. Treat them as leaked and rotate them at the provider console. Compose reads them from `.env` now. A `gitleaks` pre-commit hook would stop the next one.

## ✅ Tighten CORS for any non-dev deployment (done 2026-08)

`cors_origins()` in [`server.py`](../src/api/server.py) reads `CORS_ORIGINS` as a comma-separated list. With `PLATFORM_JWT_SECRET` set the variable is required and `*` is refused, so a gated deployment cannot serve a wildcard to credentialed requests. The wildcard survives only under `GEOLANG_ALLOW_UNAUTHENTICATED`, which is the standalone stack.

## ✅ Audit the `viewer_control` and `sql_query` tool surface (done 2026-08)

`viewer_control`'s `action` is a `Literal` closed over what viewtopia's command registry implements, `sql_query` is not among them, actions that need coordinates or a url are refused without them, and `url` must be `http` or `https`. Verified on the viewtopia side that a command url only ever reaches `fetch()` and `Cesium3DTileset.fromUrl()`, never the DOM, so the scheme check is the whole exposure.

`/mcp` no longer offers `sql_query` at all, so what is left is the `/chat` path, where the SQL's author and the browser running it are the same person. A tool module declares itself with `TOOL_RUNS_CALLER_CODE = True` and the MCP endpoint drops it from both the manifest and the call path.

`/mcp` also takes a token of its own now, minted by `POST /mcp/token` and marked with a private `geolang_use` claim, so reaching an outside agent in is a deliberate act with an expiry rather than a paste of the token you already hold. What it is not is a reduced credential: tools call downstream with the token that arrived, so everywhere but this endpoint it is worth exactly what the minting token was worth. Narrowing that too needs per-tool scopes the platform has no notion of yet.

Still open, and viewer-side rather than here: DuckDB-WASM will fetch any domain the SQL names, so a `/chat` user's own agent can be talked into reading `http://attacker.example/leak` or an address on their corporate network. An allowlist of fetchable domains belongs in the viewer.

## A platform token is equivalent to code execution here

Not a defect to fix, a boundary to state. Tools run in the API process with its privileges and no sandbox, and the escape hatches are what makes the agent useful: `geopandas_api` takes a pandas `query` expression, `pyqgis_api` and `run_qgis_algorithm` take algorithm parameters, and every tool reads and writes under `outputs/` and `user_data/`. Hardening `filter_query`'s grammar was considered and rejected: it would narrow one expression parser while leaving the surface it sits on unchanged, and buy the illusion that a token holder is contained.

So the token is the security boundary and the only one. It should be scoped short, never committed, and rotated like an SSH key. Anything that hands one out, or accepts one from further away, is worth the same scrutiny as handing out shell access. A real sandbox is the precondition for a hosted deployment where callers are not already trusted.

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

## Published live layer data has a lifetime

A layer too large to carry inside a live document is written to `live_data/` and
served open by its token, so cleaning it up is this service's problem: agora
never says a layer or a document went away. Every publish does the cleaning, and
nothing schedules anything.

Each file is tagged with the document it was published into, and with the agent
subject when there was one. A publish then drops that document's files it no
longer references once they are a day old, and rejoins up to three other
documents it has files for to do the same to theirs.

A rejoin that agora refuses keeps every file, and only its exact "no such
document" deletes one. Today nothing produces that: agora has no deletion route,
and `members` cascades on document delete, so a rejoin to a document that went
away is refused as "not a member" instead, which is also what a plain member
removal looks like. Deleting on that would take out files a live document still
draws, so the branch stays pinned to the narrow reason and those files are left
to expiry below. It starts doing work if agora ever reports a document as gone.

A share link publishes with no subject to rejoin as, because agora keeps share
tokens hashed at rest and its session tokens embed the raw one, so persisting a
way back in would undo that. Those documents, and files written before tagging
existed, are covered by the last rule instead: a file expires 90 days after the
last fetch or the last time a document confirmed it. A viewer join fetches what
it draws, so use is what keeps a file alive. The accepted cost is that a
document untouched and unviewed for 90 days loses its oversized layers.

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

- **2026-06**: Split `server.py` (1481 → 1043 lines). Extracted `core/utils.py`, `agents/agent_manager.py`, `agents/workflows.py`. Tool source unchanged — the agent sandbox sees identical code. Path issue uncovered: `TOOL_EXEC_DIR` default `~/src/geolang` was wrong for `GeoLang/geolang` checkouts; fixed by auto-detecting the repo root.
- **2026-06**: Added `viewer_integration.md` documenting the SSE `viewer_cmd` protocol and the upcoming `sql_query` tool that pairs with ViewTopia's DuckDB-WASM integration.
- **2026-06**: Populated `architecture.md` and `api_reference.md` (were empty stubs).
- **2026-07**: Replaced the embedded agent-memory server with sibyl. Deleted the tool registration and sandbox machinery, the tool-exec venv entrypoint, `.agent_id`, and `.sessions.json`. Tools now run in-process behind `/tools`. The image is a plain `python:3.11-slim-bookworm` with QGIS.
- **2026-07**: Added pytest coverage for the AG-UI renderers, the sibyl run stream, the session proxies, and the tool manifest/executor.
