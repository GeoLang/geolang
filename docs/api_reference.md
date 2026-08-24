# API Reference

The GeoLang FastAPI app ([`src/api/server.py`](../src/api/server.py)) exposes the agent, dataset, session, share, export, and MCP surfaces. All routes are unversioned and return JSON unless noted.

Base URL in development: `http://localhost:8080`. In the bundled platform deployment: `http://<host>:8080/agent/*` (path-stripped by the load balancer).

## Conventions

- Errors use FastAPI's default `{"detail": "..."}` shape with the HTTP status reflecting the failure mode (`503` if sibyl is unreachable, `404` for missing resources, `500` for unhandled exceptions).
- All filesystem outputs land under `TOOL_EXEC_DIR/outputs/<caller>/`, one directory per token subject, and are served from `/outputs/{filename}` to that caller only. A file is named by its basename: the directory it lives in follows from the bearer, never from the path asked for.
- `/geojson/{file}` and `/stats/{file}` look in the caller's own outputs directory, their own `user_data/<caller>/` and its subdirectories, and every `natural_earth*` set on disk. The natural earth sets are shared reference data, the other two are not. Nothing else in the tree is served: a file loose at `TOOL_EXEC_DIR`, or in the `outputs/` or `user_data/` root, is `404`.

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

User-uploaded files for the agent to operate on. Uploads are one directory per caller, `user_data/<caller>/`, named for the token subject exactly as the outputs directory is, so one caller is one name in both trees. A caller sees only their own uploads.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/datasets` | List the caller's own catalogued datasets, from `user_data/<caller>/catalogue.json`. |
| `POST` | `/upload` | Multipart upload into the caller's own directory; appends to their catalogue. |
| `GET` | `/stats/{filename:path}` | Summary statistics for a vector dataset. |
| `GET` | `/geojson/{filename:path}` | Serve a dataset as GeoJSON (with reprojection). |

## Outputs and downloads

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/outputs/{filename}` | Serve a tool output file. |
| `DELETE` | `/outputs/{filename}` | Delete one of the caller's own output files. `404` if there is no such file. |
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
| `GET` | `/live-data/{token}` | Features published to a live document, open read. See [writing to a live map](#writing-to-a-live-map). |

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

`approve_workflow` is not dispatched here and is not in the manifest: it answers `404` like any unknown name. It records the user pressing approve, and a caller that could ask a tool route for it never had to press anything. `POST /workflow/approve` below is the only way in.

### `POST /workflow/approve`
Request `{ "manifest_toml": "<the plan's own manifest>" }`, response `{ "approved": <bool>, "message": "<string>" }`. The viewer's approve button posts this before it posts `run_workflow`, and `run_workflow` refuses a manifest that has no approval, so a model that calls it on its own gets an error rather than a run.

`approved` is false when the manifest was never planned by this caller, when its TOML does not parse, and when a `path` points outside the caller's own directories. Nothing is recorded in those cases: an approval only ever attaches to a plan record, so the two halves cannot arrive out of order. Planning the same manifest again drops the earlier approval, and both halves expire together an hour after the plan.

The digest is taken from the confined manifest text, so the bytes the viewer posts back from the plan and the bytes the model posts to `run_workflow` land on one record. Anything else, an edit included, is refused.

Same bearer requirement as `POST /tools/{name}`, and the record is keyed to that caller: the plan, the approval and the run all have to be the same person.

An `Authorization: Bearer <jwt>` header sets the caller identity. Geolang exchanges it before execution for a role-free token that expires within five minutes and carries only the exact downstream operation scopes that named tool needs. `PTOLEMY_API_TOKEN` is the service account ptolemy falls back on in unauthenticated standalone mode.

With `PLATFORM_JWT_SECRET` set, that header is required and must be a live HS256 platform token: signature and `exp` are checked, anything else is `401`. The role is not checked here, the services a tool calls enforce their own. The same requirement covers `POST /chat/agui`, `POST /workflow/approve`, the file writers (`/upload`, `/draw`, `/export-pdf`, `/export-png`), the sibyl proxies (`/sessions*`, `/models`, `/model`), the reads (`/datasets`, `/outputs/{file}`, `/download/{file}`, `/geojson/{file}`, `/stats/{file}`), `DELETE /outputs/{file}` and `POST /share`. Without the secret every route is open, which is the standalone dev flow and what the eval harness uses. `/health`, `GET /tools`, the static viewer, reading a share by id and reading a live layer by its token are never gated.

## MCP

### `POST /mcp`
The tools over the [Model Context Protocol](https://modelcontextprotocol.io/), streamable HTTP transport, for external agents such as Claude or Cursor. Externally that is `/agent/mcp`. `tools/list` returns the manifest above with `parameters` renamed to `inputSchema`; `tools/call` runs the tool and returns its string as one text content block, markers included, plus a second block when the call is bound to a live document. Bad arguments and tool exceptions come back as a result with `isError` and a ❌ text, an unknown tool as JSON-RPC `-32602`.

A tool whose module sets `TOOL_RUNS_CALLER_CODE = True`, `TOOL_NEEDS_USER_APPROVAL = True` or `TOOL_APPROVAL_ROUTE_ONLY = True` is left out of both `tools/list` and `tools/call`, and answers `-32602` like any other unknown name. `sql_query` runs SQL the caller wrote in whichever browser receives the command, which the `/chat` path can assume is the caller's own and this one cannot. `run_workflow` runs a manifest the user pressed approve on in their viewer, and an agent arriving here has none, so it could only ever be refused. `approve_workflow` is that press, which is nothing an agent may make on the user's behalf.

Every request, `initialize` included, needs a bearer minted by `POST /mcp/token`. A plain platform token answers `401` with `this endpoint needs a token from POST /mcp/token`, so an unauthenticated caller never learns which tools exist.

### `POST /mcp/token`
Mints the token an MCP client authenticates with. Needs a live platform token of its own, and signs for that caller's `sub`, so the outside agent acts as whoever asked for it and nobody else.

Request `{"lifetime_seconds": <int>}`, optional, defaulting to and capped at `MAXIMUM_MCP_TOKEN_LIFETIME_SECONDS` (30 days). Outside `0 < n <= cap` the route answers `422`. Response `{"token": "<jwt>", "expires_at": <unix seconds>}`, where `expires_at` is read back out of the minted token rather than computed beside it. With no secret set the route answers `503`: there is nothing to sign with.

The MCP token carries the private claim `geolang_use: "mcp"`, which only opens this endpoint, and the source platform role in `source_role`. Neither reaches a tool or downstream service. Before each execution geolang mints a role-free JWT carrying the same `sub`, `token_use: "tool"`, and a JSON string array named `scope`. Its `exp` is the earlier of the MCP token's expiry or five minutes from exchange. `ptolemy_query` gets `ptolemy:read`, `list_tilesets` gets `tiletopia:read`, and `run_workflow` gets `geodukt:run`. Other tools get an empty array. Exact scope strings are enforced downstream, with no role fallback. The source role also caps the exchange: a viewer can receive read scopes, but not `geodukt:run`.

A bound result uses a separate `agora:write` token minted after the tool returns. The tool and remote executor never receive that token. The live document bridge's `agent:<sub>` WebSocket token is also role-free and limited to `agora:write`.

The endpoint is stateless: no session id is issued and nothing is kept between requests, so a call is only ever as authorised as the bearer it arrives with.

Every request needs `Authorization: Bearer <jwt>`, `initialize` included. A missing or bad token is `401` with `WWW-Authenticate: Bearer`. Without `PLATFORM_JWT_SECRET` the endpoint is open, like the rest of the API.

`MCP_ALLOWED_HOSTS` must name the public hostname, or every call answers `421`: the transport checks the `Host` header against it to block DNS rebinding, and behind the platform proxy the Host is the public name rather than localhost.

Client config:
```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": { "Authorization": "Bearer <jwt>" }
} } }
```

### Writing to a live map

Add `X-Agora-Document` and a call's map effects also land in a live [agora](https://github.com/GeoLang/agora) document, so every open viewer sees them as the tool runs. The value is a document id or a share link token. Without the header nothing is bound and a call behaves exactly as above.

```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": {
      "Authorization": "Bearer <jwt>",
      "X-Agora-Document": "<document id or share link token>"
    }
} } }
```

What travels: the layers of an `__UI_SPEC__` map become `layers/<id>` entries, and a `fly_to` or `set_view` from `__VIEWER_CMD__` becomes one presence viewport, which peers following the agent match. Other markers are ignored. A layer's colour becomes `styleOverrides.color`, but only on an entry that carries no overrides yet, so a member's own restyle survives a re-run.

A layer's features ride inside the document while they fit under 48KiB, and above that are written to `GET /live-data/{token}`, which every member fetches. That route needs no bearer: a share link guest has none, and the 32-byte token in the URL is the whole credential.

Published files are cleaned up by the publishes that follow them, with no scheduler anywhere. Republishing a layer deletes the file its previous entry named, once the replacing write is acked. Every publish also drops that document's files it has stopped naming, once they are a day old, and rejoins up to three other documents it published into to do the same. A rejoin agora refuses keeps every file: only its exact "no such document" deletes one, and nothing in agora produces that today, so a document that went away is reaped by expiry instead.

Last, a file expires **90 days** after the last time anything fetched it or a document confirmed it still draws it. A viewer join fetches the layers it draws, so any document someone still opens keeps its files whatever it was published through. The accepted consequence: a document nobody has opened or republished for 90 days loses its oversized layers, and the next person to open it sees those layer entries with no features behind them. Layers small enough to ride inside the document are unaffected, since no file backs them.

The tool's own result text is never changed by any of this. A document write is reported as a second text content block beside it, whether it succeeded or failed, so a document that could not be written never costs the caller the tool result.

The agent joins as `agent:<caller sub>` using a short-lived `agora:write` token signed with `PLATFORM_JWT_SECRET`. It is put on the document by a membership grant made with a separate `agora:write` token for the caller's subject. Agora still applies the caller's document membership, so the agent cannot reach a document its caller could not edit. Binding to a document id therefore needs that secret set and a live platform token. A share link binding writes as the link's own session instead and is refused unless the link grants edit.

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

**Workflows**: `list_workflow_operations` (geodukt transform catalog), `plan_workflow` (validate a geodukt TOML manifest, emit the plan as a `__PLAN__` marker for approval), `run_workflow` (execute the approved manifest). Shared client code sits in `_geodukt.py`, which the loader skips because of the leading underscore. `approve_workflow.py` is loaded but is not one of the tools counted above: it records the user pressing approve and only `POST /workflow/approve` dispatches it.

Every step of a `__PLAN__` payload carries `runs_caller_code`. It is true when the step's `operation` names a tool module that sets `TOOL_RUNS_CALLER_CODE = True`, so the panel can mark that step as an escape hatch before the user approves the plan, rather than the user having to trust the prose around it. The tool's own declaration is the only source, so there is no second list to drift. geodukt rejects any operation it does not have, so the flag can only be true on a build without `/validate`, where the plan is `validated: false` and nothing checked the manifest.

**Export & discovery** — `export_to_gpkg`, `list_user_datasets`, `list_outputs`, `list_qgis_algorithms`, `check_qgis_status`, `run_qgis_algorithm`.

**Escape hatches** — `geopandas_api` (arbitrary GeoPandas), `pyqgis_api` (arbitrary PyQGIS), `viewer_control` (raw viewer commands), `emit_ui_spec` (structured map hints).

Adding a tool is a single file: drop a module into `src/agents/tools/` exporting `TOOL_FUNCTION` and `TOOL_SCHEMA`. No restart needed. See [architecture.md](architecture.md#tool-manifest-and-execution).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PLATFORM_JWT_SECRET` | — | Shared HS256 secret. Required: the service refuses to start without it unless `GEOLANG_ALLOW_UNAUTHENTICATED` is set. |
| `GEOLANG_ALLOW_UNAUTHENTICATED` | unset | `1` runs with no gate at all, and is the only thing that keeps the `*_API_TOKEN` service-account fallbacks and the `CORS_ORIGINS` wildcard available. Standalone stack only. |
| `CORS_ORIGINS` | — | Comma-separated browser origins allowed to call the API. Required when the gate is on, where `*` is refused. |
| `SIBYL_URL` | `http://localhost:8090` | sibyl agent service endpoint. |
| `MCP_ALLOWED_HOSTS` | localhost only | Comma-separated `Host` values `/mcp` answers on, read at startup. A `host:*` entry matches any port. |
| `AGORA_URL` | `http://agora:3000` | agora live document service. The websocket follows it, so `https` there means `wss`. |
| `GEOLANG_PUBLIC_URL` | `/agent` | Where a browser reaches this service, used to build the `/live-data/{token}` URLs published into a document. |
| `TOOL_EXEC_DIR` | repo root | Working directory for tool I/O. Holds the `outputs/` and `user_data/` roots, each one directory per caller. |
| `GEOLANG_OUTPUTS_RETENTION_DAYS` | `30` | How long an output file is kept. The API server deletes older files from every caller directory at startup and once a day after that. `0` keeps everything. |
| `APP_BASE_URL` | `http://localhost:8080` | URL Playwright loads for `/export-pdf` and `/export-png`. |
| `PTOLEMY_URL` | `http://ptolemy:3000` | Ptolemy geodatabase endpoint (`ptolemy_query`). |
| `PTOLEMY_API_TOKEN` | — | Optional bearer token when Ptolemy auth is enabled. |
| `GEOKODE_URL` | — | geokode endpoint; when set, `geocode_place` tries it first. |
| `ITINERA_URL` | — | itinera endpoint; when set, `compute_route` tries it first. |
| `TILETOPIA_URL` | `http://tiletopia:3000` | TileTopia endpoint (`list_tilesets`). |
| `GEODUKT_URL` | `http://geodukt:8080` | geodukt-server endpoint (`plan_workflow`, `run_workflow`, `list_workflow_operations`). |
