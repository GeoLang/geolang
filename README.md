# GeoLang

GeoLang is the geospatial tool service of the GeoLang platform: a FastAPI app that serves 41 geospatial tools, runs them, and streams chat runs to ViewTopia as AG-UI events. The agent loop runs in [sibyl](https://github.com/GeoLang/sibyl), a separate Rust service. geolang serves sibyl the tool manifest at `GET /tools`, runs each call at `POST /tools/{name}`, and supplies the system prompt and the viewer protocol.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## Features

- Chat requests in plain language, answered with tools that call the platform services Ptolemy, Geokode, Itinera, TileTopia, geodukt and agora
- Every tool served to sibyl over HTTP, and 39 of them to outside agents over MCP. The MCP manifest leaves out `sql_query`, which declares `TOOL_RUNS_CALLER_CODE`, and `run_workflow`, which declares `TOOL_NEEDS_USER_APPROVAL`. Tool code runs in the API process, or in a separate executor process that holds no platform secret
- A chat run leaves out a tool when the viewer's action catalogue offers an action that does its job. The tool module names those actions in `TOOL_SUPERSEDED_BY`, and `/chat/agui` sends sibyl the tool names as `without_tools`. `calculate_isochrones` names `analysis.travel_time`, `terrain_profile` names `analysis.terrain_profile` and `analysis.cross_section`, `compare_layers` names `scenario.compare`, and `ptolemy_query` names `dataset.list` and `dataset.draw_branch`
- Plan, approve, then run for multi-step geoprocessing. The model writes a [geodukt](https://github.com/GeoLang/geodukt) TOML manifest, `plan_workflow` validates it and sends the plan to the viewer, the user presses approve, and `run_workflow` executes it. `run_workflow` refuses a manifest that `plan_workflow` never planned, and one the approve button never posted to `POST /workflow/approve`
- MCP tool calls can write their map layers into a live agora document, so every open viewer redraws while an outside agent works

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Python 3.11+ and uv, for running the API on the host
- An OpenAI-compatible provider key (xAI Grok by default), a local llama-server, or both

### Configure

Keep provider keys out of `docker-compose.yml`. Put them in `.env`, which compose reads, or export them:

```bash
export SIBYL_CLOUD_API_KEY="your-provider-key-here"
```

The key is optional. sibyl sends it as a bearer to `SIBYL_CLOUD_API_BASE`, default `https://api.x.ai/v1`. `SIBYL_CLOUD_MODELS` lists the cloud models the viewer offers. `SIBYL_LOCAL_API_BASE` and `SIBYL_LOCAL_MODELS` add local models, see [sibyl's README](https://github.com/GeoLang/sibyl) for the llama-server launch. `SIBYL_LOCAL2_API_BASE` and `SIBYL_LOCAL2_MODELS` add a second local server. `docker-compose.yml` passes all of them to sibyl.

The viewer's Settings panel switches between local and cloud models and can paste a cloud key, base and model list. sibyl stores them in sqlite, uses them from the next message, and prefers them over env on the next start. `GET /models` never returns the key. With no key and no local server sibyl still starts, and a run fails until Settings supplies one.

### Run both services

```bash
docker compose up -d --build
docker compose logs -f geolang
```

That starts geolang on `8080` (FastAPI, tools, QGIS) and sibyl on `8090` (agent loop, sessions, history). sibyl fetches the tool manifest from `http://geolang:8080/tools` and calls back into `/tools/{name}` to run one. Sessions are in sibyl's SQLite database on the `sibyl-data` volume.

### Run the API server on the host

```bash
# sibyl must be reachable, SIBYL_URL defaults to http://localhost:8090
GEOLANG_ALLOW_UNAUTHENTICATED=1 uv run --with-requirements requirements.txt \
  --with-requirements requirements_client.txt \
  -- python -m uvicorn src.api.server:app --reload --port 8080
```

The server refuses to start without either `PLATFORM_JWT_SECRET` or `GEOLANG_ALLOW_UNAUTHENTICATED=1`, see [Authenticating the API](#authenticating-the-api).

Both requirements files are needed. `requirements_client.txt` names geopandas but none of osmnx, rasterio, scipy or matplotlib, so with it alone `GET /tools` leaves out every tool that imports one of them. The startup log names each tool left out and the packages it needs. `pyqgis_api` is left out the same way wherever the QGIS bindings are absent.

`TOOL_EXEC_DIR` defaults to the repo root, found from `src/core/utils.py`. Set it to put `outputs/` and `user_data/` elsewhere.

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md): process topology, SSE event vocabulary, tool manifest flow
- [`docs/api_reference.md`](docs/api_reference.md): every HTTP route, the tool catalogue and the environment variables
- [`docs/viewer_integration.md`](docs/viewer_integration.md): the `viewer_cmd` protocol ViewTopia runs, including `sql_query` for in-browser DuckDB
- [`docs/DESIGN.md`](docs/DESIGN.md): why the design is shaped the way it is, and the improvements still open

## Tests

The same commands CI runs:

```bash
uv venv --python 3.11
uv pip install -r requirements_client.txt -r requirements.txt pytest respx
uv run python -m pytest tests/ -q
```

The QGIS tool tests skip where the QGIS bindings are not importable.

`tests/test_nl_evals.py` runs real agent prompts against the local model. It skips unless the geolang API, sibyl on a local profile and the llama server are all up. `NL_EVAL_ALLOW_CLOUD=1` lets it run on a cloud profile, which spends credits.

```bash
uv run --with pytest --with httpx python -m pytest tests/test_nl_evals.py -v
```

The site selection prompts upload a candidate sites CSV through `POST /upload` first, so that route has to accept the token the evals present.

The evals and the eval runners below find their token in `NL_EVAL_TOKEN`, or mint one from `PLATFORM_JWT_SECRET`. With neither they send no token, which only works against a stack started with the gates off. `NL_EVAL_GEOLANG` and `NL_EVAL_SIBYL` override `http://localhost:8080` and `http://localhost:8090`.

## Tool sweep

Calls every tool in the manifest once through `POST /tools/{name}` against a live platform stack. viewtopia's `platform-sweep.yml` runs it nightly against the nginx origin the viewer uses. A manifest tool with no sample arguments in [`tool_sweep/arguments.py`](tool_sweep/arguments.py) fails the run.

```bash
# PLATFORM_TOKEN is the bearer, a gated stack refuses the call without one
python -m tool_sweep.runner --base-url http://localhost:5174/agent

# leave out the tools that call a third party, and the ones marked as crashing the executor
python -m tool_sweep.runner --skip-external --skip-crashing

python -m tool_sweep.runner --only clip_layer,voronoi
```

Each result is appended to `outputs/tool_sweep.jsonl` (`--results`) as its tool finishes, so a killed run still shows which tool it stopped on. When every tool has run, the sweep deletes the output files they reported through `DELETE /outputs/{name}`. `tests/test_tool_sweep.py` runs the `offline` entries of the same table through the in-process app on every push.

## Workflow evals

Scores whether a model builds the right geodukt pipeline for a request. Scoring compares the manifest the model wrote against the expected pipeline graph, never its prose, so the same manifest always gets the same score. The model is not deterministic, so a single run can land anywhere in its spread. Quote a repeated run.

```bash
# against the profile sibyl has active. Needs geolang api, sibyl, and a geodukt
# the tool executor can reach. Prints SKIP with the reason otherwise
python -m evals.runner

# each task five times, reporting means and the flaky tasks
python -m evals.runner --repeat 5

# cloud profiles cost credits, so they are opt-in
python -m evals.runner --allow-cloud --only buffer-depots-gpkg

# no stack: score manifests captured earlier, or the reference answers
python -m evals.runner --manifests evals/reference
```

`--repeat N` runs every task N times, each in a new session. A task's score is the mean over its runs and its checks come from its worst run, so a task that passes only sometimes cannot report a clean sheet. The report names the flaky tasks and gives their range. `--repeat` needs the stack, since a captured manifest scores the same every time.

Reports are written to `evals/reports/` (`--out`) as JSON and markdown, tagged with the profile, model and timestamp. `--capture DIR` saves each manifest the model wrote, so a run can be scored again later without asking the model.

Each task in `evals/tasks/` is one TOML file: the request, the input layers it assumes exist (created before a stack run unless `--no-fixtures`), and the pipeline a correct answer builds. Every expected element is one check worth one point and the task score is `passed/total`, so pinning three parameters weights parameters more. A task with `unavailable = "<operation>"` is a negative task, passed by *not* building a manifest that uses an operation geodukt cannot run.

To add one, put a task file in `evals/tasks/` and a reference answer named `<task id>.toml` in `evals/reference/`. A test asserts every reference answer scores 1.0, which keeps a task from expecting something impossible.

## Viewer evals

Scores whether a model maps a chat prompt onto one of the viewer's own actions instead of a tool. One TOML file per task under `evals/viewer/tasks/`, scored on the `viewer_control` calls the run made.

The viewer state and the action catalogue are fixtures, `evals/viewer/snapshot.json` and `evals/viewer/catalogue.json`, copied from viewtopia's test fixtures and sent to sibyl the way `/chat/agui` sends the live ones. A task can carry a `[snapshot]` table whose fields replace the shared snapshot's for that task. A call the viewer would refuse comes back as the next user message, and so does a `reads` action's result, taken from `evals/viewer/reads_results.json`. geodukt is not involved, so an unreachable geodukt does not skip the run.

```bash
# needs geolang api and sibyl. One run is a poor estimate of a score
python -m evals.viewer_runner --repeat 3

# run on one sibyl profile without switching the active one
python -m evals.viewer_runner --profile <profile id from GET /models> --repeat 3

# keep every event of every run, to read back what a failed task did
python -m evals.viewer_runner --transcripts evals/reports/transcripts.jsonl

# no model and no network: score a recording against the current tasks
python -m evals.viewer_runner --replay evals/viewer/recordings/grok-2026-08-29.json
```

`--record PATH` writes the calls each task drew so they can be replayed later. `tests/test_viewer_replay.py` replays the checked-in recording and fails when any recorded score changes, which makes this eval a CI gate. Refresh the two fixtures from viewtopia rather than editing them, as [`evals/viewer/README.md`](evals/viewer/README.md) describes.

## Platform Integration

In the full platform (`viewtopia/docker-compose.platform.yml`) geolang serves the API on port **8080** and sibyl runs beside it on **8090**. nginx serves geolang under the viewer's origin at `/agent/`. The browser never calls sibyl directly.

### Authenticating the API

Set `PLATFORM_JWT_SECRET` to the shared platform secret and every route except the open ones below needs an `Authorization: Bearer <jwt>` header holding a live HS256 token, the same `{sub, exp, role}` tokens ptolemy mints and geodukt's `/run` accepts. The signature and `exp` are checked. Before a tool runs, geolang exchanges that token for a role-free token that expires within five minutes and carries only the downstream operation scopes mapped to that tool. Four tools have scopes mapped, listed in [`docs/api_reference.md`](docs/api_reference.md#post-mcptoken). Every other tool gets an empty scope list, so for those the exchange only shortens the expiry and drops the role.

The service refuses to start without that variable. Running with no authentication takes `GEOLANG_ALLOW_UNAUTHENTICATED=1`, which the standalone `docker-compose.yml` and the test suite set. That compose file also sets `SIBYL_ALLOW_UNAUTHENTICATED=1` for sibyl. Never set it where the port is reachable.

Gated deployments must also name the browser origins allowed to call the API in `CORS_ORIGINS`, comma separated. Startup fails without it, and `*` is refused while the gate is on, because a wildcard plus credentials lets any page a signed-in user visits spend their token here.

Open routes: `/health`, `GET /`, `/static/*`, `GET /tools` (sibyl fetches it before anyone has signed in), `GET /debug/tools` (every tool name), `GET /live-data/{token}` (reaches what its token names), and `GET /share/{id}` and `GET /share/{id}/data`. A share reader gets the view and the summary, not the layers behind them. `POST /mcp` and `POST /mcp/token` check a token themselves, see [MCP for outside agents](#mcp-for-outside-agents).

A live token reaches a lot. `geopandas_api` evaluates a pandas query the caller writes, `run_qgis_algorithm` runs any allowlisted QGIS vector or raster algorithm with caller-chosen parameters, and `sql_query` sends caller-written SQL to the browser. Together they can read and write everything under the caller's own directories in `outputs/` and `user_data/`. Keep tokens short-lived and out of commits.

`pyqgis_api` takes only `function_name`, `uri` and `layer_name`, so most processing algorithms fail for want of their parameters. Its `uri` goes through `tool_input_path` like every other input path.

### Where tool code runs

By default a tool runs in the API process. That process holds `PLATFORM_JWT_SECRET`, so a tool that can be made to run something other than geoprocessing can read the secret and sign a token for any user on any platform service. Tenants are not isolated from each other in that mode.

Set `GEOLANG_EXECUTOR_URL` to move tool code into a separate process that holds no signing secret, no service account token and no model API key. The only credential it sees is the scoped token minted for that call. Both processes need the same `TOOL_EXEC_DIR`, because the API serves the files the tools write.

```bash
# the executor, with no secrets in its environment
GEOLANG_EXECUTOR_SECRET=<random> TOOL_EXEC_DIR=... \
  python -m uvicorn src.api.executor:app --port 8081

# the API, pointed at it
PLATFORM_JWT_SECRET=<platform secret> CORS_ORIGINS=http://localhost:5174 \
  GEOLANG_EXECUTOR_URL=http://localhost:8081 GEOLANG_EXECUTOR_SECRET=<same random> \
  python -m uvicorn src.api.server:app --port 8080
```

`GEOLANG_EXECUTOR_SECRET` tells the executor its caller is the API, so nothing else on the network can run tools there. It says nothing about the executor itself, whose contents are assumed readable by an attacker. The executor refuses to start without it. In the platform stack it publishes no port, drops all capabilities and runs under memory, CPU and process limits.

Inside the executor each call runs in its own worker process, started ahead of time and used once. A run that exceeds `GEOLANG_TOOL_MEMORY_LIMIT_MB` (default 3072) or `GEOLANG_TOOL_TIMEOUT_SECONDS` (default 840) has its worker killed, and the caller is told which limit which tool hit. `GEOLANG_TOOL_MAX_CONCURRENT` (default 2) caps the runs in flight, and a call past that is told the executor is busy instead of being queued. An oversized request fails only its own call.

Requests to Nominatim and Overpass are paced across every tool run in the process that runs tools and its workers: at most one request per host every 1.1 seconds, Nominatim's published limit. The last request time sits in a locked file per host under the temp directory, because every tool run is its own process. That covers the tools' own requests and the ones osmnx sends.

With no executor configured, tools run in the API process. That is fine for one tenant and is what the standalone stack and the test suite do. With the gate on and no executor, the API logs a warning at startup and keeps running.

Outputs are split by caller. Each caller reads and writes their own directory under `outputs/`, named from the subject of their token. The executor is told which directory that is, since deriving it needs the signing secret, and it refuses a name that is not a single directory of the expected shape.

Uploads are split the same way. A caller uploads into `user_data/<caller>/`, the same directory name as their outputs, with their own `catalogue.json` inside it. `/datasets`, `/upload`, `/draw` and `list_user_datasets` see only that caller's files. Files directly in `user_data/`, outside any caller directory, are neither listed nor served.

Output files are deleted by age. The API server sweeps every caller's outputs directory at startup and once a day, deletes files at any depth last written more than `GEOLANG_OUTPUTS_RETENTION_DAYS` ago (default 30), and removes directories the sweep emptied. Each pass logs the file count and bytes freed. `0` keeps everything. `GEOLANG_USER_DATA_RETENTION_DAYS` does the same for uploads in `user_data/`, in the same pass. It is off by default. An upload whose file the sweep deleted drops out of `/datasets` and `list_user_datasets`. The executor, which mounts the same volume, does not sweep. A caller can delete one of their own files with `DELETE /outputs/{filename}`.

`/upload` has size limits and a daily budget. `GEOLANG_UPLOAD_MAX_REQUEST_MEGABYTES` caps the request body. It is checked while the body is read, and a larger body gets 413 before the rest of it arrives. `GEOLANG_UPLOAD_MAX_FILE_MEGABYTES` caps the uploaded file. For a `.zip`, `GEOLANG_UPLOAD_MAX_ZIP_ENTRIES` caps the entry count and `GEOLANG_UPLOAD_MAX_UNZIPPED_MEGABYTES` caps the unzipped size the archive declares. Both are checked before anything is unzipped, and a zip entry whose path leaves its folder is refused. `GEOLANG_UPLOAD_FILES_PER_DAY` and `GEOLANG_UPLOAD_MEGABYTES_PER_DAY` cap uploads per UTC day across all callers, and `GEOLANG_UPLOAD_FILES_PER_CALLER_PER_DAY` and `GEOLANG_UPLOAD_MEGABYTES_PER_CALLER_PER_DAY` cap them per token subject. A zip counts as the larger of its own size and its unzipped size. An upload past a daily cap gets 429 and nothing is written. Unset or `0` means no limit for each of these. The daily counts are kept in memory, so a restart resets them.

Tool runs have their own limits, counted for every tool call whether it comes from `POST /tools/{name}`, `POST /workflow/approve` or MCP. sibyl calls `POST /tools/{name}` with the user's own bearer, so a chat's tool calls count against that user. `GEOLANG_TOOL_RUNS_PER_DAY` caps tool runs per UTC day across all callers and `GEOLANG_TOOL_RUNS_PER_CALLER_PER_DAY` caps them per token subject. `GEOLANG_TOOL_RUNS_AT_ONCE_PER_CALLER` caps one subject's runs in flight, so one user cannot hold every executor slot. `GEOLANG_OUTPUT_MEGABYTES_PER_CALLER_PER_DAY` caps how much a subject's tool runs add to their outputs directory per UTC day: the directory is measured before and after each run, and once the day's growth is over the cap the next call is refused. A refused call gets 429 and the tool does not run. Unset or `0` means no limit. The counts are kept in memory in the API process.

A tool argument that names a file is a filename, not a path. It is looked up in the caller's own outputs directory, their own `user_data/` directory, and the natural earth reference sets, the same places `/geojson` serves from. An absolute path is refused, and an output filename with a directory part is refused rather than trimmed, so two callers cannot be pointed at one file.

`plan_workflow` and `run_workflow` rewrite each `[[source]]` and `[[sink]]` `path` into the caller's own directories before the manifest reaches geodukt: `outputs/foo.gpkg` becomes `outputs/<caller>/foo.gpkg`, which is what `list_outputs` and the download routes serve. An absolute path outside those directories is refused. geodukt has no confinement root of its own, so this rewrite is the only check.

### MCP for outside agents

The tools are served over the Model Context Protocol at `POST /mcp`, which is `/agent/mcp` behind the platform proxy. It takes a token of its own, so mint one first:

```bash
curl -X POST https://<host>/agent/mcp/token \
  -H "Authorization: Bearer <your platform jwt>" \
  -H "Content-Type: application/json" \
  -d '{"lifetime_seconds": 604800}'
# {"token": "<mcp jwt>", "expires_at": 1760000000}
```

`lifetime_seconds` is optional, up to and defaulting to 30 days. Then point Claude, Cursor or any MCP client at it:

```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": { "Authorization": "Bearer <mcp jwt>" }
} } }
```

A plain platform token answers `401` with `this endpoint needs a token from POST /mcp/token`. With the gate on the bearer is required on every MCP request, and its subject is copied into each tool's execution token.

`MCP_ALLOWED_HOSTS` must name the public hostname, or the transport's DNS-rebinding check answers `421` to everything. The default is localhost only, and the platform compose does not set it, so the `https://<host>/agent/mcp` examples need it set on the geolang service first. See [`docs/api_reference.md`](docs/api_reference.md#mcp).

The MCP token only opens `/mcp`. Every other gated route, `POST /mcp/token` included, answers it with `401`. Before each tool runs, geolang exchanges it for a role-free JWT with `token_use: "tool"` and an exact `scope` array, expiring at the earlier of the MCP token's expiry or five minutes. The MCP token keeps the minting token's role in a private claim, so the exchange cannot delegate an operation that role could not perform. The executor never receives an agora scope. When a result has to be written to a live document, geolang-api mints a separate `agora:write` token after the tool returns and keeps it out of the tool process.

`sql_query` and `run_workflow` are not offered here. `sql_query` runs caller-written SQL in whichever browser receives it, which is safe only when the caller owns that browser. `run_workflow` needs the user to press approve in their viewer, and an outside agent has no viewer.

Add an `X-Agora-Document` header and each call's map output also lands in that live agora document, so every open viewer redraws while the outside agent works:

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

The layers a tool emits become document layers, and a `camera.fly_to` moves the agent's presence. The agent joins as its own member, added by a grant made with the caller's token, so it can only reach documents its caller could already edit. `AGORA_URL` says where agora is. See [writing to a live map](docs/api_reference.md#writing-to-a-live-map).

---

## License

AGPL-3.0-or-later, see [LICENSE](LICENSE).

Copyright (C) 2026 Grok Image Compression Inc.
