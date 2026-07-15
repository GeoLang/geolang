# API Reference

The GeoLang FastAPI app ([`src/api/server.py`](../src/api/server.py)) exposes the agent, dataset, session, share, and export surfaces. All routes are unversioned and return JSON unless noted.

Base URL in development: `http://localhost:8080`. In the bundled platform deployment: `http://<host>:8080/agent/*` (path-stripped by the load balancer).

## Conventions

- Errors use FastAPI's default `{"detail": "..."}` shape with the HTTP status reflecting the failure mode (`503` if the agent is not yet initialised, `404` for missing resources, `500` for unhandled exceptions).
- All filesystem outputs land under `TOOL_EXEC_DIR/outputs/` and are served from `/outputs/{filename}`.

## Chat

### `POST /chat`
One-shot request/response. Use for non-interactive scripting; prefer `/chat/stream` for UIs.

Request:
```json
{ "message": "Show me populated places above 1M people in Spain" }
```

Response:
```json
{
  "text": "Here are 8 places…",
  "ui_spec": { "type": "map", "layers": [ ... ] },
  "viewer_commands": [ { "action": "...", "params": { ... } } ]
}
```

### `POST /chat/stream`
SSE stream of agent events. See [architecture.md](architecture.md#sse-event-vocabulary) for the event type table and [viewer_integration.md](viewer_integration.md) for `viewer_cmd` semantics.

Each line is a standard SSE `data: <json>` payload, terminated by a `done` event. Keepalive comments (`: keepalive`) are emitted every 15s of agent silence.

## Sessions

GeoLang persists a lightweight session list in `.sessions.json`. Each session maps to a distinct Letta agent; switching swaps the active `agent_id`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sessions` | List sessions and the current one. |
| `POST` | `/sessions/new` | Create a new session (new agent). |
| `POST` | `/sessions/switch` | Switch active session by id. |
| `PUT` | `/sessions/{session_id}/rename` | Rename a session. |
| `DELETE` | `/sessions/{session_id}` | Delete a session (and its agent). |

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

## Debug

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness — returns `{"status": "ok"}` plus `agent_id`. |
| `GET` | `/debug/tools` | List tools registered with the active agent. |

## Tool catalogue

36 tools live under [`src/agents/tools/`](../src/agents/tools/). They auto-register on startup. Categories:

**Data acquisition** — `geocode_place` (geokode-first, Natural Earth fallback), `batch_geocode`, `get_admin_boundary`, `download_natural_earth_dataset`, `download_osm_data`, `download_population_grid`, `query_elevation`.

**Vector analysis** — `buffer_clip_dissolve`, `clip_layer`, `spatial_join`, `aggregate_by_region`, `cluster_points`, `voronoi`, `find_nearest`, `compare_layers`.

**Routing & accessibility** — `compute_route` (itinera-first, Valhalla fallback), `calculate_isochrones`, `service_gap`, `score_sites`.

**Terrain & raster** — `terrain_profile`, `query_zonal_population`, `assess_environmental_risk`, `generate_heatmap`.

**Platform services** — `ptolemy_query` (geodatabase datasets/branches/features), `list_tilesets` (TileTopia assets + catalog), `sql_query` (in-browser DuckDB Spatial via viewer command).

**Export & discovery** — `export_to_gpkg`, `list_user_datasets`, `list_outputs`, `list_qgis_algorithms`, `check_qgis_status`, `run_qgis_algorithm`.

**Escape hatches** — `geopandas_api` (arbitrary GeoPandas), `pyqgis_api` (arbitrary PyQGIS), `viewer_control` (raw viewer commands), `emit_ui_spec` (structured map hints).

Adding a tool is a single file: drop a module into `src/agents/tools/` exporting `TOOL_FUNCTION` (and optionally `TOOL_SCHEMA`, `TOOL_HELPERS`), then restart the API. See [architecture.md](architecture.md#tool-registration-flow).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LETTA_URL` | `http://localhost:8283` | Letta server endpoint. |
| `TOOL_EXEC_DIR` | `~/src/geolang` | Working directory for tool I/O, sessions, catalogue. |
| `OPENAI_API_KEY` / `XAI_API_KEY` | — | LLM provider key (xAI Grok by default). |
| `VLLM_API_BASE` | `http://localhost:8000` | Embedding server endpoint. |
| `PTOLEMY_URL` | `http://ptolemy:3000` | Ptolemy geodatabase endpoint (`ptolemy_query`). |
| `PTOLEMY_API_TOKEN` | — | Optional bearer token when Ptolemy auth is enabled. |
| `GEOKODE_URL` | — | geokode endpoint; when set, `geocode_place` tries it first. |
| `ITINERA_URL` | — | itinera endpoint; when set, `compute_route` tries it first. |
| `TILETOPIA_URL` | `http://tiletopia:3000` | TileTopia endpoint (`list_tilesets`). |
