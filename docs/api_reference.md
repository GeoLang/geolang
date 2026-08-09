# API Reference

The GeoLang FastAPI app ([`src/api/server.py`](../src/api/server.py)) exposes the agent, dataset, session, share, export, and MCP surfaces. All routes are unversioned and return JSON unless noted.

Base URL in development: `http://localhost:8080`. In the bundled platform deployment: `http://<host>:8080/agent/*` (path-stripped by the load balancer).

## Conventions

- Errors use FastAPI's default `{"detail": "..."}` shape with the HTTP status reflecting the failure mode (`503` if sibyl is unreachable, `404` for missing resources, `500` for unhandled exceptions).
- All filesystem outputs land under `TOOL_EXEC_DIR/outputs/` and are served from `/outputs/{filename}`.

## Chat

### `POST /chat/agui`
SSE stream of [AG-UI protocol](https://docs.ag-ui.com/) events. Accepts a `RunAgentInput` body (`threadId`, `runId`, `messages`); the last user message is the prompt. See [architecture.md](architecture.md#sse-event-vocabulary) for the event mapping and [viewer_integration.md](viewer_integration.md) for `viewer_cmd` semantics.

Each line is a standard SSE `data: <json>` payload, wrapped in `RUN_STARTED`/`RUN_FINISHED`. Keepalive comments (`: keepalive`) are emitted every 15s of agent silence.

An `Authorization: Bearer <jwt>` header is forwarded to sibyl as the run's `user_token`, and sibyl sends it back on every tool call of that run, so the tools reach ptolemy, tiletopia and geodukt as that user. Without one the run is anonymous: public reads work, gated writes fail.

## Sessions

Sessions live in sibyl. These routes are proxies and keep no state of their own.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sessions` | List sessions, newest first, with `active` marking the current one. |
| `POST` | `/sessions/new` | Create a session named `Session N` and activate it. |
| `POST` | `/sessions/switch` | Switch active session by id. `404` if unknown. |
| `PUT` | `/sessions/{session_id}/rename` | Rename a session. |
| `DELETE` | `/sessions/{session_id}` | Delete a session. `400` for the active one. |

## Datasets

User-uploaded files for the agent to operate on.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/datasets` | List catalogued datasets from `user_data/catalogue.json`. |
| `POST` | `/upload` | Multipart upload; appends to the catalogue. |
| `GET` | `/stats/{filename:path}` | Summary statistics for a vector dataset. |
| `GET` | `/geojson/{filename:path}` | Serve a dataset as GeoJSON (with reprojection). |

## Outputs and downloads

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/outputs/{filename}` | Serve a tool output file. |
| `GET` | `/download/{filename}` | Download a tool output file with `Content-Disposition: attachment`. |

## Drawing and export

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/draw` | Persist a user-drawn GeoJSON feature as a dataset. |
| `POST` | `/export-pdf` | Render the current view spec to PDF. |
| `POST` | `/export-png` | Render the current view spec to PNG. |

`/draw` request:
```json
{ "name": "study_area", "geojson": { "type": "FeatureCollection", "features": [...] } }
```

## Sharing

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/share` | Create a shareable link for a dataset / view. |

## Tools

sibyl reads the manifest, picks a tool, and posts the arguments back for GeoLang to execute in-process.

### `GET /tools`
```json
{ "tools": [ { "name": "geocode_place", "description": "…", "parameters": { "type": "object", ... } } ] }
```
`parameters` is the JSON schema of the module's `TOOL_SCHEMA`. Modules without one are skipped.

### `POST /tools/{name}`
Request `{ "args": { "place_name": "Paris" } }`, response `{ "result": "<string>" }`. `404` for an unknown tool. Bad arguments and tool exceptions come back as `200` with a `result` starting with ❌, so the agent can read the failure and recover. Calls can take minutes.

Add `"notify": true` when running a tool outside the model's turn, such as the viewer's plan-approval button calling `run_workflow`: the result is appended to the active sibyl session so the model can answer follow-up questions about it. sibyl never sets the flag, so a run the model itself asked for is not reported back to it twice.

An `Authorization: Bearer <jwt>` header sets the identity the tool's own outbound calls go out as, for the length of that call. `PTOLEMY_API_TOKEN` is the service account ptolemy falls back on when there is no caller.

With `PLATFORM_JWT_SECRET` set, that header is required and must be a live HS256 platform token: signature and `exp` are checked, anything else is `401`. The role is not checked here, the services a tool calls enforce their own. The same requirement covers `POST /chat/agui`, the file writers (`/upload`, `/draw`, `/export-pdf`, `/export-png`), the sibyl proxies (`/sessions*`, `/models`, `/model`), the reads (`/datasets`, `/outputs/{file}`, `/download/{file}`, `/geojson/{file}`, `/stats/{file}`) and `POST /share`. Without the secret every route is open, which is the standalone dev flow and what the eval harness uses. `/health`, `GET /tools`, the static viewer and reading a share by id are never gated.

## MCP

### `POST /mcp`
The same tools over the [Model Context Protocol](https://modelcontextprotocol.io/), streamable HTTP transport, for external agents such as Claude or Cursor. Externally that is `/agent/mcp`. `tools/list` returns the manifest above with `parameters` renamed to `inputSchema`; `tools/call` runs the tool and returns its string as one text content block, markers included. Bad arguments and tool exceptions come back as a result with `isError` and a ❌ text, an unknown tool as JSON-RPC `-32602`.

The endpoint is stateless: no session id is issued and nothing is kept between requests, so a call is only ever as authorised as the bearer it arrives with.

Every request needs `Authorization: Bearer <jwt>`, `initialize` included, and that bearer is the identity the tool's outbound calls go out as. A missing or bad token is `401` with `WWW-Authenticate: Bearer`. Without `PLATFORM_JWT_SECRET` the endpoint is open, like the rest of the API.

`MCP_ALLOWED_HOSTS` must name the public hostname, or every call answers `421`: the transport checks the `Host` header against it to block DNS rebinding, and behind the platform proxy the Host is the public name rather than localhost.

Client config:
```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": { "Authorization": "Bearer <jwt>" }
} } }
```

## Debug

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, returns `{"status": "ok"}`. |
| `GET` | `/debug/tools` | Names of the loaded tool modules. |

## Tool catalogue

39 tools live under [`src/agents/tools/`](../src/agents/tools/). They are loaded from disk on every manifest or execution request. Categories:

**Data acquisition** — `geocode_place` (geokode-first, Natural Earth fallback), `batch_geocode`, `get_admin_boundary`, `download_natural_earth_dataset`, `download_osm_data`, `download_population_grid`, `query_elevation`.

**Vector analysis** — `buffer_clip_dissolve`, `clip_layer`, `spatial_join`, `aggregate_by_region`, `cluster_points`, `voronoi`, `find_nearest`, `compare_layers`.

**Routing & accessibility** — `compute_route` (itinera-first, Valhalla fallback), `calculate_isochrones`, `service_gap`, `score_sites`.

**Terrain & raster** — `terrain_profile`, `query_zonal_population`, `assess_environmental_risk`, `generate_heatmap`.

**Platform services** — `ptolemy_query` (geodatabase datasets/branches/features), `list_tilesets` (TileTopia assets + catalog), `sql_query` (in-browser DuckDB Spatial via viewer command).

**Workflows**: `list_workflow_operations` (geodukt transform catalog), `plan_workflow` (validate a geodukt TOML manifest, emit the plan as a `__PLAN__` marker for approval), `run_workflow` (execute the approved manifest). Shared client code sits in `_geodukt.py`, which the loader skips because of the leading underscore.

**Export & discovery** — `export_to_gpkg`, `list_user_datasets`, `list_outputs`, `list_qgis_algorithms`, `check_qgis_status`, `run_qgis_algorithm`.

**Escape hatches** — `geopandas_api` (arbitrary GeoPandas), `pyqgis_api` (arbitrary PyQGIS), `viewer_control` (raw viewer commands), `emit_ui_spec` (structured map hints).

Adding a tool is a single file: drop a module into `src/agents/tools/` exporting `TOOL_FUNCTION` and `TOOL_SCHEMA`. No restart needed. See [architecture.md](architecture.md#tool-manifest-and-execution).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SIBYL_URL` | `http://localhost:8090` | sibyl agent service endpoint. |
| `MCP_ALLOWED_HOSTS` | localhost only | Comma-separated `Host` values `/mcp` answers on, read at startup. A `host:*` entry matches any port. |
| `TOOL_EXEC_DIR` | repo root | Working directory for tool I/O, outputs, catalogue. |
| `APP_BASE_URL` | `http://localhost:8080` | URL Playwright loads for `/export-pdf` and `/export-png`. |
| `PTOLEMY_URL` | `http://ptolemy:3000` | Ptolemy geodatabase endpoint (`ptolemy_query`). |
| `PTOLEMY_API_TOKEN` | — | Optional bearer token when Ptolemy auth is enabled. |
| `GEOKODE_URL` | — | geokode endpoint; when set, `geocode_place` tries it first. |
| `ITINERA_URL` | — | itinera endpoint; when set, `compute_route` tries it first. |
| `TILETOPIA_URL` | `http://tiletopia:3000` | TileTopia endpoint (`list_tilesets`). |
| `GEODUKT_URL` | `http://geodukt:8080` | geodukt-server endpoint (`plan_workflow`, `run_workflow`, `list_workflow_operations`). |
