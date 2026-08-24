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
- `POST /tools/{name}` with `{"args": {...}}` runs the tool and returns `{"result": "<string>"}`. Validation errors and exceptions come back as `200` with a ❌ result so the agent can recover. The routes are sync `def`, so FastAPI runs them in its threadpool, tools block for minutes.
- `POST /runs` on sibyl takes `{"system_prompt", "message"}` and streams NDJSON events (`text`, `tool_call`, `tool_return`, `error`, `done`). `agent_event_stream` normalises those into the `(kind, payload)` tuples the AG-UI renderer consumes.

`PLATFORM_JWT_SECRET` is required to start. With it, every route that runs code, writes a file, or reads back a session or a user's data requires a live HS256 platform token and answers `401` otherwise. `/health`, the `GET /tools` manifest, the static viewer, reading a share by id and reading a live layer by its token stay open. `GEOLANG_ALLOW_UNAUTHENTICATED=1` opens all of it, and is the only way to get there.

## Where tool code runs

A tool hands caller-written arguments to geopandas, QGIS and DuckDB, so a process running one can be made to run other things. The API process holds `PLATFORM_JWT_SECRET`, which signs a token for any user on any service, so with tools in it no tenant can be promised isolation from another.

`GEOLANG_EXECUTOR_URL` moves tool code into [`src/api/executor.py`](../src/api/executor.py), a process given no signing secret, no service account and no model key. `POST /tools/{name}` and the MCP `call_tool` both go through `execute_tool` in [`src/core/tool_executor.py`](../src/core/tool_executor.py), which forwards or runs locally; the split is invisible to sibyl and to MCP clients.

What stayed in the API: the platform gate, token exchange, minting MCP tokens, and the live-document write. What crosses to the executor: the tool name, its validated arguments, a role-free token with only that tool's downstream operation scopes, and the name of the outputs directory the call's files belong in. A separate `agora:write` token is minted after the tool returns and never crosses into the executor.

That last one crosses because the executor cannot work it out: turning a bearer into a subject needs the signing secret it is deliberately not given, so left to itself it would write every caller's files to one directory. The API sends the name it verified, `anonymous` included, and the executor re-checks that it is a single path component of the shape the naming produces. A name that fails is refused rather than replaced with a shared one.

Arguments are validated on both sides. The API's copy fails fast and keeps an unknown tool off the wire; the executor's is the one that matters, because the endpoint is on a network.

`GEOLANG_EXECUTOR_SECRET` says the caller is the API. Whoever is inside the executor already knows it, which is the point: it claims nothing about that process, it keeps anything else on the network from running tools there. The executor refuses to start without it.

Unset, tools run in the API process, which is the standalone stack, the test suite, the eval harness and any single-tenant self-host. Nothing in the process can tell one deployment from the other, so this is not refused: the API logs a warning naming the cost when the gate is on and no executor is configured.

A tool holds a bearer while it runs, but that bearer expires within five minutes and only carries the exact downstream operation scopes mapped to the tool name. Unknown tools and tools with no downstream service need get an empty scope array.

`outputs/` is one directory per caller, named for the `sub` of the token that arrived and created on first use. The routes that serve a file by name resolve it inside that directory, so one user's filenames and files are not another's to list or fetch. A caller with no verified subject, which is every caller when the gate is off, writes to a fixed `anonymous` directory that no subject can name. Files written before either split stay in the parent, in both trees, and are no longer listed or served. That hides uploads that predate it, which was accepted rather than migrated.

An output file has a lifetime, since the caller's own `DELETE /outputs/{filename}` reaches only their directory and a file no tool announced is deleted by nothing. [`src/api/outputs_retention.py`](../src/api/outputs_retention.py) walks every caller directory at API startup and once a day after that, deletes the regular files last written more than `GEOLANG_OUTPUTS_RETENTION_DAYS` ago, and removes a directory it emptied. It runs in the API server and not in the executor, which mounts the same volume, so one process is the only deleter. Files in the outputs parent itself, the ones that predate the per-caller split, are left where they are: nothing serves them either.

`user_data/` is split the same way and by the same name. The directory a caller uploads into is `user_data/<their-directory>/`, from the same subject and the same `caller_directory_name`, so one caller is one name in both trees and nothing has to map between them. `catalogue.json` moved inside it, so a caller's catalogue lists their uploads and no one else's, and `/datasets`, `/upload`, `/draw` and `list_user_datasets` all read and write the caller's own copy. The natural earth sets stay shared: they are reference data nobody uploads.

A tool argument naming a file is confined the same way, through `tool_input_path` and `tool_output_path` in [`src/core/utils.py`](../src/core/utils.py). Both layers call the one `layer_search_dirs()` and `allowed_roots()` there, so what a tool may open cannot drift from what a route may serve: the caller's own outputs, their own user_data, and the natural earth sets, and nothing else. `plan_workflow` and `run_workflow` rewrite each manifest `path` onto those same directories before geodukt sees it, because geodukt has no confinement root of its own. `pyqgis_api` runs `uri` through `tool_input_path`. An absolute path is refused rather than resolved, which is a deliberate break with what the tool schemas used to advertise: taking the basename instead would quietly open a different file than the one named. An output filename is one path component or it is refused, because trimming it to the basename would put two callers' different requests on one file.

The shared reference data those tools legitimately read is not an exemption. `geocode_place` and `population_raster_path` name their datasets in code rather than from an argument, which is why the tree root can be read for them at all: nothing a caller writes chooses the name. `population_raster_path` searches the caller's own two directories and then the tree root. It stopped searching the `user_data` root when that became one directory per caller, because a file there is now either somebody's upload or a leftover from before the split rather than a place a deployment puts a shared raster. `run_qgis_algorithm` asks the algorithm what each parameter is, through `parameterDefinitions()`, and confines only the ones QGIS would open or write as a file. That is why its confinement runs after the QGIS initialisation rather than before it. Everything else is a value and is passed on untouched, so a field calculator formula dividing one field by another stays a formula. A parameter that names layers inside a structure this tool cannot take apart, such as the layer list a dxf export takes, is refused instead of passed on unchecked. So are `EXTRA`, `OPTIONS` and `CREATION_OPTIONS` on a `gdal:` algorithm: those are pasted into the command line GDAL is run with, and a token beginning with a hyphen is passed on unquoted, so a file named in one would arrive as arguments of its own rather than as a value anything here resolves. The same three names on a `native:` algorithm go to the raster writer and are left alone.

QGIS starts once per process. A `QgsApplication` can be built once: building another after `exitQgis()` dies with SIGSEGV, so while each QGIS tool owned its own init and teardown, whichever of them ran second killed the executor and every tool called after that reported it unreachable. [`src/core/qgis_session.py`](../src/core/qgis_session.py) builds the application on first use, bridges the system QGIS python paths onto `sys.path`, adds the native algorithm provider, initialises the processing plugin, and never calls `exitQgis`. A start that failed is remembered and re-raised, because retrying it would build the second application. It sits in core rather than beside the tools because the tool loader imports tool modules under a second package name and reloads them, so a session held there would exist once per import name.

## 🔴 Rotate the API keys that were once committed to `docker-compose.yml`

`XAI_API_KEY` and `OPENAI_API_KEY` were committed as literals and are still in the git history. Treat them as leaked and rotate them at the provider console. Compose reads them from `.env` now. A `gitleaks` pre-commit hook would stop the next one.

## ✅ Tighten CORS for any non-dev deployment (done 2026-08)

`cors_origins()` in [`server.py`](../src/api/server.py) reads `CORS_ORIGINS` as a comma-separated list. With `PLATFORM_JWT_SECRET` set the variable is required and `*` is refused, so a gated deployment cannot serve a wildcard to credentialed requests. The wildcard survives only under `GEOLANG_ALLOW_UNAUTHENTICATED`, which is the standalone stack.

## ✅ Audit the `viewer_control` and `sql_query` tool surface (done 2026-08)

`viewer_control`'s `action` is a `Literal` closed over what viewtopia's command registry implements, `sql_query` is not among them, actions that need coordinates or a url are refused without them, and `url` must be `http` or `https`. Verified on the viewtopia side that a command url only ever reaches `fetch()` and `Cesium3DTileset.fromUrl()`, never the DOM, so the scheme check is the whole exposure.

`/mcp` no longer offers `sql_query` at all, so what is left is the `/chat` path, where the SQL's author and the browser running it are the same person. A tool module declares itself with `TOOL_RUNS_CALLER_CODE = True` and the MCP endpoint drops it from both the manifest and the call path.

The same declaration labels a workflow plan: every step of a `__PLAN__` payload carries `runs_caller_code`, so the approval panel marks a step that runs caller-written code instead of leaving it to the persona prose a model can ignore. Nothing is refused on that basis: geodukt's validator already rejects an operation it does not have, and where it cannot be consulted the label is what the user approves on.

`/mcp` takes a token minted by `POST /mcp/token` and marked with a private `geolang_use` claim. It stops at the tool boundary. It also keeps the minting token's role in a private claim, so an exchange cannot delegate an operation the source role could not perform. Each execution exchanges it for a JWT with the same subject, `token_use: "tool"`, no role, and an exact JSON `scope` array. The expiry is capped by both the source token and five minutes. Downstream services distinguish these from normal user JWTs, require exact scopes, and never fall back to a role on a marked tool token.

Still open, and viewer-side rather than here: DuckDB-WASM will fetch any domain the SQL names, so a `/chat` user's own agent can be talked into reading `http://attacker.example/leak` or an address on their corporate network. An allowlist of fetchable domains belongs in the viewer.

## What `run_workflow` requires before it executes

Two records, both keyed to the caller and to the digest of the confined manifest text, both in [`src/core/planned_manifests.py`](../src/core/planned_manifests.py): `plan_workflow` validated this text, and the user pressed approve on it. Without the first the model is told to plan; without the second it is told the user has not approved and to ask them rather than retry.

The press arrives at `POST /workflow/approve`, which the viewer's approve button posts before it posts the run. The route dispatches `approve_workflow`, a tool module that is not offered as a tool: `TOOL_APPROVAL_ROUTE_ONLY = True` keeps it out of `GET /tools`, out of `POST /tools/{name}` and out of `/mcp`, because sibyl posts whatever tool name the model emitted and the record of a person pressing a button must not be one of them. It is a tool module because the record has to land in the process that runs `plan_workflow` and `run_workflow`, which is the executor wherever one is configured.

`run_workflow` declares `TOOL_NEEDS_USER_APPROVAL = True`, which drops it from `/mcp` the way `TOOL_RUNS_CALLER_CODE` drops `sql_query`. An agent reaching that endpoint has no viewer to press approve in.

The approval is not a credential: it is a record in the process, so both routes still take the caller's own platform token, and `platform_token_error` accepts an MCP-minted token as a platform token. A holder of one can therefore still reach `POST /tools/plan_workflow`, `POST /workflow/approve` and `POST /tools/run_workflow` over plain HTTP, which is the same reach it had before this gate existed. Narrowing the approval route to non-MCP tokens would close that and take `run_workflow` away from an MCP client entirely.

## A platform token is equivalent to code execution here

Not a defect to fix, a boundary to state. Tools run in the API process with its privileges and no sandbox, and the escape hatches are what makes the agent useful: `geopandas_api` takes a pandas `query` expression, `pyqgis_api` and `run_qgis_algorithm` take algorithm parameters, and every tool reads and writes under the caller's own directories in `outputs/` and `user_data/`. Hardening `filter_query`'s grammar was considered and rejected: it would narrow one expression parser while leaving the surface it sits on unchanged, and buy the illusion that a token holder is contained.

So the token is the security boundary and the only one. It should be scoped short, never committed, and rotated like an SSH key. Anything that hands one out, or accepts one from further away, is worth the same scrutiny as handing out shell access. A real sandbox is the precondition for a hosted deployment where callers are not already trusted.

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
- **2026-08**: Split tool execution out of the API process into `src/api/executor.py`, behind `GEOLANG_EXECUTOR_URL`. The platform stack runs it as `geolang-executor`: no published port, no `PLATFORM_JWT_SECRET`, no `.env`, all capabilities dropped, memory/CPU/process limits.
