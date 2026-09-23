# API Reference

The GeoLang FastAPI app ([`src/api/server.py`](../src/api/server.py)) serves the chat, tool, session, model, dataset, share, export and MCP routes. Routes are unversioned and return JSON unless noted.

Base URL in development: `http://localhost:8080`. In the platform stack nginx serves the app at `http://<host>:5174/agent/` and strips the `/agent` prefix. Port 8080 is published there too.

## Conventions

- Errors use FastAPI's `{"detail": "..."}` shape. `503` means sibyl is unreachable, `404` a missing resource, `500` an unhandled exception.
- Tool output files are written to `TOOL_EXEC_DIR/outputs/<caller>/`, one directory per token subject, and served from `/outputs/{filename}` to that caller only. A file is named by its basename. The directory comes from the bearer, never from the path asked for.
- `/geojson/{file}` and `/stats/{file}` look in the caller's own outputs directory, their own `user_data/<caller>/` and its subdirectories, and every `natural_earth*` set on disk. The natural earth sets are shared, the other two are not. Nothing else in the tree is served: a file directly in `TOOL_EXEC_DIR`, `outputs/` or `user_data/` is `404`.

## Chat

### `POST /chat/agui`
SSE stream of [AG-UI protocol](https://docs.ag-ui.com/) events. Takes a `RunAgentInput` body (`threadId`, `runId`, `messages`, `state`). The last user message is the prompt and `threadId` is the sibyl session. See [architecture.md](architecture.md#sse-event-vocabulary) for the event mapping and [viewer_integration.md](viewer_integration.md) for `viewer_cmd`.

Each line is an SSE `data: <json>` payload, opened by `RUN_STARTED` and closed by `RUN_FINISHED`, or by `RUN_ERROR` when the run fails. A `: keepalive` comment goes out after every 15 seconds of agent silence.

The `Authorization: Bearer <jwt>` header is sent to sibyl as the run's `user_token`, and sibyl sends it back on every tool call of that run, so the tools reach ptolemy, tiletopia and geodukt as that user. An `X-Agora-Document` header is sent along the same way, so a tool that reads a live map answers about that document.

The body's `state` is the viewer's own, in two parts: `viewer`, a snapshot of what is on screen (camera, renderer, basemap, layers, project, dataset, live document, history, picked feature), and `actions`, the catalogue of named actions that viewer can run. Each catalogue entry has a `name`, a `description`, JSON Schema `parameters`, and the flags `reads` and `destructive`. [`src/api/viewer_state.py`](../src/api/viewer_state.py) appends both to the persona as a `Viewer state:` line of compact JSON and a `Viewer actions:` line per action, so the model answers about the map in front of the asker and can name an action back. A `state` with no `actions` list sends the persona unchanged.

A tool whose job one of the offered actions already does is left out of the run. The tool module names those actions in `TOOL_SUPERSEDED_BY`, and `/chat/agui` sends the tool names to sibyl as `without_tools`, so the model is not given both:

| Tool | Left out when the catalogue offers |
|---|---|
| `calculate_isochrones` | `analysis.travel_time` |
| `terrain_profile` | `analysis.terrain_profile`, `analysis.cross_section` |
| `compare_layers` | `scenario.compare` |
| `ptolemy_query` | `dataset.list`, `dataset.draw_branch` |

The model runs an action with `viewer_control(action='run', name=..., ...)`, the action's parameters as plain fields of the call. JSON text of the parameter object in `args` is accepted too, and so is that text written into `url`, which is where grok puts it. Either way the call emits `__VIEWER_CMD__:{"action": "run", "params": {"name": "<catalogue name>", "args": {...}}}` for the viewer to execute. A `reads` action answers a question rather than changing the map: the viewer sends the answer back as the next run's user message, `Result of <name>: <text>`. A `destructive` action is one the viewer asks the person to confirm first.

## Sessions

Sessions are stored in sibyl. These routes forward to it with the caller's bearer and keep no state of their own.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sessions` | The caller's sessions, newest first, with `active` marking the current one. |
| `POST` | `/sessions/new` | Create a session named `Session N` and make it active. |
| `POST` | `/sessions/switch` | Body `{"session_id"}`. Make that session active. `404` if unknown. |
| `PUT` | `/sessions/{session_id}/rename` | Body `{"name"}`. Rename a session. |
| `DELETE` | `/sessions/{session_id}` | Delete a session. `400` for the active one. |

## Models

The model profile routes, forwarded to sibyl with its status and body unchanged. Shapes and status codes are in [sibyl's README](https://github.com/GeoLang/sibyl#api).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/models` | Profiles, providers and the active profile. |
| `PUT` | `/model` | Switch the active profile. |
| `PUT` | `/model/cloud` | Set the default cloud provider's base, key and model list. Admin role only. |
| `PUT` | `/model/providers` | Add or update a named cloud or local provider. Admin role only. |
| `DELETE` | `/model/providers/{provider_id}` | Remove a provider and its profiles. Admin role only. |

## Datasets

User-uploaded files for the tools to read. Uploads go into one directory per caller, `user_data/<caller>/`, named for the token subject exactly as the outputs directory is. A caller sees only their own uploads.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/datasets` | The caller's catalogued datasets, from `user_data/<caller>/catalogue.json`. |
| `POST` | `/upload` | Multipart upload into the caller's own directory, appended to their catalogue. |
| `GET` | `/stats/{filename:path}` | Feature count, geometry type, polygon area, a category breakdown and numeric ranges for a vector layer. |
| `GET` | `/geojson/{filename:path}` | A vector layer as GeoJSON in EPSG:4326. |

`/upload` takes a `file` field and an optional `thread_id`. A `.zip` is unpacked and its first `.shp` or `.gpkg` used. A `.csv` needs lat and lon columns and is converted to GPKG. Every upload is reprojected to EPSG:4326. With a `thread_id`, a note naming the dataset and its columns is appended to that sibyl session.

## Outputs and downloads

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/outputs/{filename}` | Serve one of the caller's output files. |
| `DELETE` | `/outputs/{filename}` | Delete one of the caller's output files. `404` if there is no such file. |
| `GET` | `/download/{filename}` | The same file with `Content-Disposition: attachment`. |

## Drawing and export

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/draw` | Save a GeoJSON feature drawn on the map as a GPKG dataset. |
| `POST` | `/export-pdf` | Render a view to PDF with headless Chromium, written to the caller's outputs. |
| `POST` | `/export-png` | Render a view to PNG the same way. |

`/draw` request, `thread_id` optional:
```json
{ "name": "study_area", "geojson": { "type": "FeatureCollection", "features": [...] }, "thread_id": "<sibyl session>" }
```

With a `thread_id`, a note naming the file and its centre is appended to that sibyl session.

The export routes load `APP_BASE_URL` with the view in the query string and answer `{"pdf_filename" | "png_filename", "download_url"}`.

## Sharing

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/share` | Store a view (`title`, `summary`, `layers`, `center`, `zoom`) and answer `{"share_id", "url"}`. |
| `GET` | `/share/{share_id}` | The static viewer page, which loads the share from its URL. Open. |
| `GET` | `/share/{share_id}/data` | The stored view as JSON. Open. |
| `GET` | `/live-data/{token}` | Features published to a live document. Open. See [writing to a live map](#writing-to-a-live-map). |

A share id is 16 random bytes, and it is the only credential for reading one. The layers a share names are still behind the gate, so a signed-out reader gets the view and the summary, not the data.

## Tools

sibyl reads the manifest, picks a tool, and posts the arguments back for GeoLang to run, in the API process or in the executor when `GEOLANG_EXECUTOR_URL` is set.

### `GET /tools`
```json
{ "tools": [ { "name": "geocode_place", "description": "…", "parameters": { "type": "object", ... } } ] }
```
`name` is the tool function's name, `description` its docstring, and `parameters` the JSON schema of the module's `TOOL_SCHEMA`. A module without a schema is left out.

A tool is offered only where the packages it needs are installed. The tools import their dependencies inside their function bodies, so [`src/agents/tool_imports.py`](../src/agents/tool_imports.py) reads each module's source instead. An import inside a `try` that catches `ImportError` is optional, and so is one inside a `try` catching `Exception`, unless that `try` wraps the whole tool body. `qgis` counts as installed when it resolves on the QGIS system paths that `qgis_session` adds to `sys.path` at call time. The startup log names each tool left out and the packages it needs.

### `POST /tools/{name}`
Request `{ "args": { "place_name": "Paris" } }`, response `{ "result": "<string>" }`. `404` for an unknown tool. A dotted name such as `layers.set_visible` gets a `404` whose detail says to run it through `viewer_control`. Bad arguments and tool exceptions come back as `200` with a `result` starting with ❌, so the agent can read the failure and recover. Calls can take minutes.

Add `"notify": true` and a `"thread_id"` when running a tool outside the model's turn, such as the viewer's plan approval calling `run_workflow`. The result, with its markers stripped, is appended to that sibyl session so the model can answer questions about it. Without a `thread_id` nothing is appended. sibyl never sets `notify`, so a run the model asked for is not reported back to it twice.

`approve_workflow` is not dispatched here and is not in the manifest. It answers `404` like any unknown name. It records the user pressing approve, and a caller that could reach it through a tool route would not have to press anything. `POST /workflow/approve` below is the only way in.

An `X-Agora-Document` header binds the call to a live map, which `asset_readings` answers about. See [reading a live map](#reading-a-live-map).

The `Authorization: Bearer <jwt>` header sets the caller. Before execution geolang exchanges it for a role-free token that expires within five minutes and carries only the downstream operation scopes that tool needs, listed under [`POST /mcp/token`](#post-mcptoken). `PTOLEMY_API_TOKEN` is the service account `ptolemy_query` falls back on when no bearer came, in unauthenticated mode only.

### `POST /workflow/approve`
Request `{ "manifest_toml": "<the plan's own manifest>" }`, response `{ "approved": <bool>, "message": "<string>" }`. The viewer's approve button posts this before it posts `run_workflow`, and `run_workflow` refuses a manifest with no approval, so a model that calls it on its own gets an error rather than a run.

`approved` is false when this caller never planned the manifest, when its TOML does not parse, and when a `path` points outside the caller's own directories. Nothing is recorded in those cases: an approval only attaches to a plan record, so the two cannot arrive out of order. Planning the same manifest again drops the earlier approval, and both expire an hour after the plan. The records are kept in memory, in the executor when one is configured, so a restart drops them.

The record is keyed on a digest of the confined manifest text, so the bytes the viewer posts back from the plan and the bytes the model posts to `run_workflow` match one record. Any other text, an edit included, is refused. The record is also keyed to the caller: the plan, the approval and the run all have to come from the same person.

### Which routes need a token

With `PLATFORM_JWT_SECRET` set, every route needs `Authorization: Bearer <jwt>` holding a live HS256 platform token, except the open ones: `/health`, `GET /`, `/static/*`, `GET /tools`, `GET /debug/tools`, `GET /live-data/{token}`, `GET /share/{share_id}` and `GET /share/{share_id}/data`. The signature and `exp` are checked, anything else is `401`. A token minted by `POST /mcp/token` is `401` here too. The role is not checked here, the services a tool calls enforce their own. `POST /mcp` and `POST /mcp/token` check their own token, below.

Without the secret, and with `GEOLANG_ALLOW_UNAUTHENTICATED=1`, every route is open. That is the standalone stack and the test suite.

## MCP

### `POST /mcp`
The tools over the [Model Context Protocol](https://modelcontextprotocol.io/), streamable HTTP transport, for external agents such as Claude or Cursor. Behind the platform proxy that is `/agent/mcp`. `tools/list` returns the manifest above with `parameters` renamed to `inputSchema`. `tools/call` runs the tool and returns its string as one text content block, markers included, plus a second block when the call is bound to a live document. Bad arguments and tool exceptions come back as a result with `isError` and a ❌ text, an unknown tool as JSON-RPC `-32602`. A `GET` answers `405`: the endpoint is stateless and offers no server-sent stream.

A tool whose module sets `TOOL_RUNS_CALLER_CODE = True`, `TOOL_NEEDS_USER_APPROVAL = True` or `TOOL_APPROVAL_ROUTE_ONLY = True` is left out of both `tools/list` and `tools/call`, and answers `-32602` like an unknown name. That drops `sql_query`, `run_workflow` and `approve_workflow`. `sql_query` runs caller-written SQL in whichever browser receives the command, which is safe on `/chat`, where the caller owns that browser, and not here. `run_workflow` needs a manifest the user approved in their viewer, and an outside agent has none. `approve_workflow` is that approval, which an agent may not give on the user's behalf.

Every request, `initialize` included, needs a bearer minted by `POST /mcp/token`. A missing or bad token is `401` with `WWW-Authenticate: Bearer`, so an unauthenticated caller never learns which tools exist. A plain platform token is `401` with `this endpoint needs a token from POST /mcp/token`. Without `PLATFORM_JWT_SECRET` the endpoint is open, like the rest of the API.

No session id is issued and nothing is kept between requests, so a call is only as authorised as the bearer it arrives with.

`MCP_ALLOWED_HOSTS` must name the public hostname, or every call answers `421`. The transport checks the `Host` header against it to block DNS rebinding, and behind the platform proxy the Host is the public name, not localhost.

Client config:
```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": { "Authorization": "Bearer <mcp jwt>" }
} } }
```

### `POST /mcp/token`
Mints the token an MCP client authenticates with. Needs a live platform token of its own, and signs for that caller's `sub`, so the outside agent acts as whoever asked for it.

Request `{"lifetime_seconds": <int>}`, optional, defaulting to and capped at `MAXIMUM_MCP_TOKEN_LIFETIME_SECONDS` (30 days). Outside `0 < n <= cap` the route answers `422`. Response `{"token": "<jwt>", "expires_at": <unix seconds>}`, where `expires_at` is read back out of the minted token. With no secret set the route answers `503`, since there is nothing to sign with.

The MCP token carries the private claim `geolang_use: "mcp"`, which only opens this endpoint, and the source platform role in `source_role`. Neither reaches a tool or downstream service. Before each execution geolang mints a role-free JWT carrying the same `sub`, `token_use: "tool"`, and a JSON string array named `scope`. Its `exp` is the earlier of the MCP token's expiry or five minutes from the exchange. Four tools have scopes mapped:

| Tool | Scope |
|---|---|
| `ptolemy_query` | `ptolemy:read` |
| `list_tilesets` | `tiletopia:read` |
| `asset_readings` | `agora:read` |
| `run_workflow` | `geodukt:run` |

Every other tool gets an empty array. The downstream services enforce the exact scope strings, with no role fallback. The source role also caps the exchange: a viewer role can receive the read scopes but not `geodukt:run`. `/chat/agui` and `POST /tools/{name}` use the same exchange.

A bound result is written with a separate `agora:write` token minted after the tool returns. The tool and the executor never receive that token. The live document bridge's `agent:<sub>` WebSocket token is also role-free and limited to `agora:write`.

### Writing to a live map

Add `X-Agora-Document` and a call's map output also lands in a live [agora](https://github.com/GeoLang/agora) document, so every open viewer sees it as the tool runs. The value is a document id or a share link token. Without the header nothing is bound and a call behaves as above.

```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": {
      "Authorization": "Bearer <mcp jwt>",
      "X-Agora-Document": "<document id or share link token>"
    }
} } }
```

What is written: the layers of an `__UI_SPEC__` map become `layers/<id>` entries, and a `__VIEWER_CMD__` naming the catalogue action `camera.fly_to` with `lon` and `lat` becomes one presence viewport, which peers following the agent match. Other markers are ignored. A layer's colour becomes `styleOverrides.color`, but only on an entry that has no overrides yet, so a member's own restyle survives a re-run.

A layer whose document entry fits under 48 KiB carries its features inline. A larger one, up to 32 MiB, is written to a file served at `GET /live-data/{token}`, which every member fetches. That route needs no bearer, because a share link guest has none. The 32-byte token in the URL is the whole credential.

Published files are deleted by later publishes, with no scheduler. Republishing a layer deletes the file its previous entry named, once the replacing write is acknowledged. Every publish also deletes that document's files it no longer names once they are a day old, and rejoins up to three other documents it published into to do the same. A rejoin that agora refuses keeps every file. Only agora's exact `no such document` refusal deletes one, and agora sends no such refusal today, so files of a deleted document go by the expiry below.

A file expires **90 days** after the last time anything fetched it or a document confirmed it still draws it. A viewer that joins fetches the layers it draws, so any document someone still opens keeps its files. A document nobody has opened or republished for 90 days loses its large layers, and the next person to open it sees those layer entries with no features. Layers small enough to be inline are unaffected.

The tool's own result text is never changed. The document write is reported as a second text content block beside it, whether it succeeded or failed, so a failed write never costs the caller the tool result.

The agent joins as `agent:<caller sub>` with a short-lived `agora:write` token signed with `PLATFORM_JWT_SECRET`. It is added to the document by a membership grant made with a separate `agora:write` token for the caller's subject. agora still applies the caller's document membership, so the agent cannot reach a document its caller could not edit. Binding to a document id therefore needs the secret set and a live platform token. A share link binding writes as the link's own session and is refused unless the link grants edit.

### Reading a live map

The same header names the map `asset_readings` answers about, so a question about what the sensors are doing needs no argument. `POST /tools/{name}` and `POST /chat/agui` take the header too, and on those routes it does nothing else. Only a document id binds a map for a read: agora answers its asset routes to members, and a share link guest is not one. A `document_id` argument on the call wins over the header.

## Debug

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, returns `{"status": "ok"}`. |
| `GET` | `/debug/tools` | Names of the tools the manifest offers. |

## Tool catalogue

41 tools live under [`src/agents/tools/`](../src/agents/tools/), one module each. They are loaded from disk on every manifest or execution request. Categories:

**Data acquisition**: `geocode_place` (geokode, then Natural Earth populated places, then Nominatim), `batch_geocode`, `get_admin_boundary`, `download_natural_earth_dataset`, `download_osm_data` (refuses a place boundary over 50 km², `radius_m` searches around its centre instead), `download_population_grid`, `query_elevation`.

**Vector analysis**: `buffer_clip_dissolve`, `clip_layer`, `spatial_join`, `aggregate_by_region`, `cluster_points`, `voronoi`, `find_nearest`, `compare_layers`.

**Routing and accessibility**: `compute_route` (itinera where its extract covers both ends, public Valhalla otherwise), `calculate_isochrones`, `service_gap`, `score_sites`, `trade_area` (one travel-time catchment per candidate site, with the population, competitors, anchors and census figures inside it).

**Terrain and raster**: `terrain_profile`, `query_zonal_population`, `assess_environmental_risk`, `generate_heatmap`.

**Platform services**: `ptolemy_query` (geodatabase datasets, branches and features), `list_tilesets` (TileTopia assets and catalogue), `sql_query` (in-browser DuckDB Spatial through a viewer command), `asset_readings` (what the sensors on the live map report, now or at a given time, from agora).

**Workflows**: `list_workflow_operations` (geodukt transform catalogue), `plan_workflow` (validate a geodukt TOML manifest and emit the plan as a `__PLAN__` marker for approval), `run_workflow` (execute the approved manifest). Shared client code is in `_geodukt.py`, which the loader skips because of the leading underscore. `approve_workflow.py` is loaded but is not one of the tools counted above: it records the user pressing approve and only `POST /workflow/approve` dispatches it.

Every step of a `__PLAN__` payload carries `runs_caller_code`. It is true when the step's `operation` names a tool module that sets `TOOL_RUNS_CALLER_CODE = True`, so the plan panel can mark that step before the user approves. The tool's own declaration is the only source. geodukt rejects any operation it does not have, so the flag can only be true on a geodukt build without `/validate`, where the plan is `validated: false` and nothing checked the manifest.

**Export and discovery**: `export_to_gpkg`, `list_user_datasets`, `list_outputs`, `list_qgis_algorithms`, `check_qgis_status`, `run_qgis_algorithm`.

**Escape hatches**: `geopandas_api` (`read_file`, `sjoin`, `proximity_analysis`, or `filter` with a pandas query), `pyqgis_api` (loads a vector or raster layer, or runs a `native:` or `qgis:` algorithm with only `uri` and `layer_name` as parameters), `viewer_control` (whose only `action` is `run`, naming one entry of the viewer's action catalogue), `emit_ui_spec` (map, table and image hints for the viewer).

Adding a tool takes one module in `src/agents/tools/` exporting `TOOL_FUNCTION` and `TOOL_SCHEMA`, with no restart, plus sample arguments in `tool_sweep/arguments.py`, since the sweep fails on a manifest tool without them. See [architecture.md](architecture.md#tool-manifest-and-execution).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PLATFORM_JWT_SECRET` | unset | Shared HS256 secret. The service refuses to start without it unless `GEOLANG_ALLOW_UNAUTHENTICATED` is set. |
| `GEOLANG_ALLOW_UNAUTHENTICATED` | unset | `1` runs with no gate at all. It is also the only mode where the `PTOLEMY_API_TOKEN` fallback and a `*` in `CORS_ORIGINS` apply. Standalone stack only. |
| `CORS_ORIGINS` | unset | Comma-separated browser origins allowed to call the API. Required when the gate is on, where `*` is refused. Unset without the gate means `*`. |
| `SIBYL_URL` | `http://localhost:8090` | sibyl agent service. |
| `MCP_ALLOWED_HOSTS` | localhost only | Comma-separated `Host` values `/mcp` answers on, read at startup. A `host:*` entry matches any port. |
| `AGORA_URL` | `http://agora:3000` | agora live document service, for live-map writes and `asset_readings`. The WebSocket URL follows it, so `https` there means `wss`. |
| `GEOLANG_PUBLIC_URL` | `/agent` | Where a browser reaches this service, used to build the `/live-data/{token}` URLs written into a document. |
| `TOOL_EXEC_DIR` | repo root | Working directory for tool I/O. Holds the `outputs/` and `user_data/` roots, each one directory per caller. |
| `GEOLANG_OUTPUTS_RETENTION_DAYS` | `30` | How long an output file is kept. The API server deletes older files from every caller directory at startup and once a day. `0` keeps everything. |
| `GEOLANG_CHAT_RUNS_PER_DAY` | unset | Chat runs `/chat/agui` starts per UTC day, all callers together. Unset or `0` means no limit. Kept in memory, so a restart resets the count. |
| `GEOLANG_CHAT_RUNS_PER_CALLER_PER_DAY` | unset | Chat runs per UTC day for one token subject. Unset or `0` means no limit. Without the gate there is no subject, so only the global limit applies. |
| `GEOLANG_UPLOAD_MAX_REQUEST_MEGABYTES` | unset | Largest `/upload` request body. Checked while the body is read, a larger one gets 413. Unset or `0` means no limit. |
| `GEOLANG_UPLOAD_MAX_FILE_MEGABYTES` | unset | Largest uploaded file. A larger one gets 413 and is not written. Unset or `0` means no limit. |
| `GEOLANG_UPLOAD_MAX_ZIP_ENTRIES` | unset | Most entries an uploaded `.zip` may hold. Checked before unzipping, a larger archive gets 413. Unset or `0` means no limit. |
| `GEOLANG_UPLOAD_MAX_UNZIPPED_MEGABYTES` | unset | Largest total unzipped size an uploaded `.zip` may declare. Checked before unzipping, a larger archive gets 413. Unset or `0` means no limit. |
| `GEOLANG_UPLOAD_FILES_PER_DAY` | unset | Uploads per UTC day, all callers together. Past it `/upload` answers 429. Unset or `0` means no limit. Kept in memory. |
| `GEOLANG_UPLOAD_FILES_PER_CALLER_PER_DAY` | unset | Uploads per UTC day for one token subject. Unset or `0` means no limit. |
| `GEOLANG_UPLOAD_MEGABYTES_PER_DAY` | unset | Megabytes uploaded per UTC day, all callers together. A zip counts as the larger of its size and its unzipped size. Unset or `0` means no limit. |
| `GEOLANG_UPLOAD_MEGABYTES_PER_CALLER_PER_DAY` | unset | Megabytes uploaded per UTC day for one token subject, counted the same way. Unset or `0` means no limit. |
| `GEOLANG_EXECUTOR_URL` | unset | Where the tool executor answers. Unset runs tools in the API process. |
| `GEOLANG_EXECUTOR_SECRET` | unset | Shared value the executor checks its caller against. The executor refuses to start without it. |
| `GEOLANG_TOOL_MEMORY_LIMIT_MB` | `3072` | Executor only. Memory a tool run may use before its worker is killed. |
| `GEOLANG_TOOL_TIMEOUT_SECONDS` | `840` | Executor only. How long a tool run may take before its worker is killed. |
| `GEOLANG_TOOL_MAX_CONCURRENT` | `2` | Executor only. Tool runs in flight. A call past this is told the executor is busy. |
| `APP_BASE_URL` | `http://localhost:8080` | URL Playwright loads for `/export-pdf` and `/export-png`. |
| `PTOLEMY_URL` | `http://ptolemy:3000` | Ptolemy geodatabase (`ptolemy_query`). |
| `PTOLEMY_API_TOKEN` | unset | Service-account bearer for `ptolemy_query` when the caller sent none. Read only with `GEOLANG_ALLOW_UNAUTHENTICATED` set. |
| `GEOKODE_URL` | unset | geokode endpoint. When set, `geocode_place` tries it first. |
| `ITINERA_URL` | unset | itinera endpoint. When set, `compute_route` tries it first. |
| `TILETOPIA_URL` | `http://tiletopia:3000` | TileTopia endpoint (`list_tilesets`). |
| `GEODUKT_URL` | `http://geodukt:8080` | geodukt-server endpoint (`plan_workflow`, `run_workflow`, `list_workflow_operations`). |
