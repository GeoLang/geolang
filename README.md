# GeoLang

**AI-powered geospatial agent**: a natural language interface to GIS operations. The agent loop runs in [sibyl](../sibyl), a separate Rust service. GeoLang owns the tools, the persona, and the viewer protocol.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## Features

- Natural language geospatial queries
- Integration with GeoLang platform services (Ptolemy, Geokode, Itinera, TileTopia)
- 39 geospatial tools served to sibyl over HTTP, and 37 of them to outside agents over MCP: the MCP manifest drops `sql_query`, which declares `TOOL_RUNS_CALLER_CODE`, and `run_workflow`, which declares `TOOL_NEEDS_USER_APPROVAL`. Tool code runs in the API process, or in an isolated executor that holds no platform secret
- Plan-then-execute for multi-step geoprocessing: the model composes a [geodukt](../geodukt) TOML manifest, `plan_workflow` validates it and streams the plan, the user presses approve in the viewer, and `run_workflow` executes it. Both halves are checked rather than trusted to the persona: `run_workflow` refuses a manifest `plan_workflow` never validated, and one the approve button never posted to `POST /workflow/approve`
- AG-UI event stream for ViewTopia

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.11+ (for the FastAPI server on the host)
- An LLM provider API key (xAI Grok by default, any OpenAI-compatible endpoint works)

### Configure

GeoLang reads provider keys from environment variables. **Do not commit keys to
`docker-compose.yml`.** Use a `.env` file or shell exports:

```bash
export XAI_API_KEY="your-xai-key-here"
```

sibyl reads the key from `XAI_API_KEY` only and sends it as a bearer token to
whatever `SIBYL_API_BASE` points at. To use another OpenAI-compatible provider,
set `SIBYL_API_BASE` and put that provider's key in `XAI_API_KEY`.

### Run both services

```bash
docker compose up -d --build

# Watch startup
docker compose logs -f geolang
```

That starts geolang on `8080` (FastAPI, tools, QGIS) and sibyl on `8090` (agent
loop, sessions, history). sibyl fetches the tool manifest from
`http://geolang:8080/tools` and calls back into `/tools/{name}` to run one.
Sessions live in sibyl's SQLite database on the `sibyl-data` volume.

### Run the API server on the host

```bash
# sibyl must be reachable. SIBYL_URL defaults to http://localhost:8090
uv run --with-requirements requirements.txt \
  --with-requirements requirements_client.txt \
  -- python -m uvicorn src.api.server:app --reload --port 8080
# → http://localhost:8080/
```

Both files are needed. `requirements_client.txt` names none of the geospatial
libraries, and tools import lazily, so with it alone the server starts and `GET
/tools` still advertises 39 while roughly 25 of them fail at call time. `requests`
is declared in neither file and arrives transitively through osmnx.

`TOOL_EXEC_DIR` auto-detects the geolang repo root from `src/core/utils.py`, so
no env var is needed in dev. Override it via the `TOOL_EXEC_DIR` env var if you
want outputs elsewhere.

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — process topology, SSE event vocabulary, tool manifest flow
- [`docs/api_reference.md`](docs/api_reference.md) — all HTTP endpoints and the tool catalogue
- [`docs/viewer_integration.md`](docs/viewer_integration.md) — `viewer_cmd` protocol for ViewTopia (including `sql_query` for in-browser DuckDB)
- [`docs/DESIGN.md`](docs/DESIGN.md) — open improvements, known sharp edges, decision log

## Tests

```bash
# unit tests, run inside the container with temporary test dependencies
docker exec viewtopia-geolang-api-1 sh -c "uv run --with pytest --with respx -- python -m pytest tests/ -q"

# NL evals: real agent runs against the local model. Auto-skipped unless
# geolang api, sibyl (local mode), and the llama server are all up.
uv run --with pytest --with httpx python -m pytest tests/test_nl_evals.py -v
```

## Tool sweep

Every tool in the manifest, one `POST /tools/{name}` each, against a live
platform stack. viewtopia's `platform-sweep.yml` runs it nightly against the
nginx origin the viewer uses. A manifest tool with no sample arguments in
[`tool_sweep/arguments.py`](tool_sweep/arguments.py) fails the run, so a new tool
cannot ship unswept.

```bash
# PLATFORM_TOKEN is the bearer; the stack refuses the call without one
python -m tool_sweep.runner --base-url http://localhost:5174/agent

# leave out the tools that call a third party
python -m tool_sweep.runner --skip-external
```

Each result is appended to the JSONL file as its tool finishes, so a killed run
still says which tool it died on. `tests/test_tool_sweep.py` runs the `offline`
entries of the same table through the in-process app on every push.

## Workflow evals

Measures whether a model builds the right geodukt pipeline, so "model X scores Y
on N tasks" is a number rather than an impression. Scoring compares the manifest
the model composed against the expected pipeline graph, never its prose, so the
same manifest always scores the same.

Scoring is deterministic but the model is not: a task the model gets right most
of the time still fails sometimes, so a single run can report anything within
that spread. Quote a repeated run, not one lucky pass.

```bash
# against whatever model sibyl is running. Needs geolang api, sibyl, and a
# geodukt the tool executor can reach. Skips cleanly with the reason otherwise.
python -m evals.runner

# what to quote: each task five times, reporting means and the flaky ones
python -m evals.runner --repeat 5

# cloud profiles cost credits, so they are opt-in
python -m evals.runner --allow-cloud --only buffer-depots-gpkg

# no stack: score manifests captured earlier, or the reference answers
python -m evals.runner --manifests evals/reference
```

`--repeat N` runs every task N times in its own session. A task's score is the
mean over its runs and its checks come from its worst run, so a task that only
passes sometimes cannot report a clean sheet. The report names the flaky tasks
and gives their range; `--repeat` needs the stack, since a captured manifest
scores the same every time.

Reports land in `evals/reports/` as JSON and markdown, tagged with the profile,
model and timestamp. `--capture DIR` saves each model manifest so a run can be
re-scored later without spending another run.

Each task in `evals/tasks/` is one TOML file: the request, the input layers it
assumes exist (created before a stack run), and the pipeline a correct answer
builds. Every expected element is one check worth one point and the task score is
`passed/total`, so pinning three parameters weights parameters more. A task with
`unavailable = "<operation>"` is a negative task, passed by *not* building a
manifest that reaches for an operation geodukt cannot run.

To add one, drop a task file in `evals/tasks/` and a reference answer named
`<task id>.toml` in `evals/reference/`. A test asserts every reference answer
scores 1.0, which is what keeps a task from expecting something impossible.

## Platform Integration

When running as part of the full GeoLang platform (via `viewtopia/docker-compose.platform.yml`),
GeoLang serves the API on port **8080** and sibyl runs alongside it on **8090**.

### Authenticating the API

Set `PLATFORM_JWT_SECRET` to the shared platform secret and every route that
runs code, writes a file, or reads back a session or a user's data requires an
`Authorization: Bearer <jwt>` header holding a live HS256 token, the same
`{sub, exp, role}` tokens ptolemy mints and geodukt's `/run` accepts. Signature
and `exp` are checked. At the tool boundary, geolang exchanges that token for a
role-free token that expires within five minutes and carries only the downstream
operation scopes mapped to that tool. Only 3 of the 39 tools have any scopes
mapped today; the other 36 exchange to an empty scope list, so for them the
exchange shortens the expiry and drops the role but narrows no operation.

The service refuses to start without that variable, as ptolemy and interiora
already do. Running with no authentication at all takes a second, explicit
`GEOLANG_ALLOW_UNAUTHENTICATED=1`, which is what `docker-compose.yml` sets for
the standalone stack. Never set it where the port is reachable.

**Treat a platform token like an SSH key to this host.** `geopandas_api`,
`run_qgis_algorithm` and `sql_query` take expressions and algorithm parameters
the caller chooses, so anyone holding a live token can compute arbitrary things
and read and write everything under their own directories in `outputs/` and
`user_data/`. That is by design for a geoprocessing agent. Scope the token
short, never commit it, and rotate it like a key.

`pyqgis_api` still only takes `function_name`, `uri` and `layer_name`, so most
algorithms reject the call for want of their parameters. The `uri` it does take
goes through `tool_input_path`, the same confinement as the tools above.

What that reaches is bounded by where the tool runs, which is the next section.

Gated deployments must also name the browser origins allowed to call the API in
`CORS_ORIGINS`, comma separated. Startup fails without it, and `*` is refused
while the gate is on, because a wildcard plus credentials means any page a
signed-in user visits can spend their token here.

Gated: `POST /tools/{name}` and `POST /chat/agui`, the file writers `/upload`,
`/draw`, `/export-pdf` and `/export-png`, the sibyl proxies `/sessions*`,
`/models` and `/model`, and the reads `/datasets`, `/outputs/{file}`,
`/download/{file}`, `/geojson/{file}` and `/stats/{file}`. Creating a share is
gated too.

Open: `/health`, the viewer's static assets, the `GET /tools` manifest sibyl
fetches at startup before anyone has signed in, `GET /debug/tools`, which carries
no auth dependency and returns every tool name, `GET /live-data/{token}`, which
is open by design and reaches what its token names, and reading a share by id,
whose whole point is a link that works for someone who never signs in. That
reader gets the view and the summary, not the layers behind them.

`POST /mcp/token` is in neither list. It hangs off no gate dependency and checks
the platform bearer itself, so it needs a live platform token either way.

With `GEOLANG_ALLOW_UNAUTHENTICATED=1` and no secret the whole API stays open.
That is the standalone `docker compose up` flow, the test suite and the eval
harness, none of which carry a token. With the gate on the client has to send
the header on every call, layer fetches and download links included.

### Where tool code runs

By default a tool runs in the API process. That process holds
`PLATFORM_JWT_SECRET`, so a tool that can be made to run something other than
geoprocessing can read the secret and sign a token for any user on any service
in the platform. One tenant cannot be promised isolation from another while that
is true.

Set `GEOLANG_EXECUTOR_URL` to move tool code into a separate process that holds
no signing secret, no service account token and no model API key. The only
credential it sees is the short scoped token minted for that call. Both processes share the same
`TOOL_EXEC_DIR`, because a tool writes the output files the API then serves.

```bash
# the executor, with nothing worth stealing in its environment
GEOLANG_EXECUTOR_SECRET=<random> TOOL_EXEC_DIR=... \
  python -m uvicorn src.api.executor:app --port 8081

# the API, pointed at it
GEOLANG_EXECUTOR_URL=http://localhost:8081 GEOLANG_EXECUTOR_SECRET=<same> \
  python -m uvicorn src.api.server:app --port 8080
```

`GEOLANG_EXECUTOR_SECRET` is how the executor knows its caller is the API. It
claims nothing about the executor itself, whose contents are assumed reachable:
it keeps anything else on the network from running tools there. The executor
refuses to start without it, publishes no port in the platform stack, drops all
capabilities and runs under memory, CPU and process limits.

Leaving the executor unset is a deployment's choice to run tools in the API
process, which is fine for a single tenant and is what the standalone stack, the
test suite and the eval harness do. With the gate on and no executor configured
the API logs a warning naming what that costs, and keeps running.

The tool holds a five-minute role-free bearer while it runs, limited to the
operations mapped to that tool. Outputs are split by caller, each caller reads
and writes their own directory under `outputs/`, keyed on the subject of the
token they presented. The executor is told which directory that is, since
naming it needs the signing secret the executor does not have, and it refuses a
name that is not a single directory of the expected shape.

Uploads are split the same way. A caller uploads into `user_data/<caller>/`,
under the same directory name as their outputs, with their own
`catalogue.json` inside it. `/datasets`, `/upload`, `/draw` and
`list_user_datasets` all see that caller's files and no one else's. Files left
in the flat `user_data/` directory from before the split stay on disk and are
no longer listed or served.

Output files are deleted by age. The API server sweeps every caller's outputs
directory once at startup and once a day after that, deletes the files last
written more than `GEOLANG_OUTPUTS_RETENTION_DAYS` ago, default 30, and removes
a directory the sweep emptied. Each pass logs how many files it removed and how
many bytes that freed. Set the variable to `0` to keep everything forever. The
sweep runs in the API server and not in the executor, so the two processes
sharing the volume do not both delete from it. A caller can also delete one of
their own files early with `DELETE /outputs/{filename}`.

A tool argument that names a file is a filename, not a path. It is looked up in
the caller's own outputs directory, their own `user_data/` directory, and in the
natural earth reference sets, the same three places `/geojson` serves from. An absolute path
is refused with an error rather than opened, and an output filename carrying a
directory part is refused rather than trimmed, so no two callers can be steered
onto one file.

`plan_workflow` and `run_workflow` rewrite each `[[source]]` and `[[sink]]`
`path` onto the caller's own directories before the manifest reaches geodukt.
`outputs/foo.gpkg` becomes `outputs/<caller>/foo.gpkg`, which is what
`list_outputs` and the download routes serve. An absolute path outside those
directories is refused. geodukt itself still has no confinement root: the
rewrite is the check.

### MCP for outside agents

The tools are served over the Model Context Protocol at `POST /mcp`,
`/agent/mcp` from outside. It takes a token of its own, so mint one first:

```bash
curl -X POST https://<host>/agent/mcp/token \
  -H "Authorization: Bearer <your platform jwt>" \
  -H "Content-Type: application/json" \
  -d '{"lifetime_seconds": 604800}'
# {"token": "<mcp jwt>", "expires_at": 1760000000}
```

`lifetime_seconds` is yours to choose up to 30 days, and defaults to 30 days.
Then point Claude, Cursor or any MCP client at it:

```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": { "Authorization": "Bearer <mcp jwt>" }
} } }
```

**Migration:** a plain platform token used to work here and now answers `401`
with `this endpoint needs a token from POST /mcp/token`. Mint one and swap it
into the client config.

The bearer is required on every MCP request when the gate is on and supplies
the subject copied into each execution token. Set `MCP_ALLOWED_HOSTS` to the public hostname,
otherwise the transport's DNS-rebinding check answers `421` to everything. See
[`docs/api_reference.md`](docs/api_reference.md#mcp).

The platform compose sets `MCP_ALLOWED_HOSTS` nowhere, and the default is
localhost only, so as shipped every MCP request through a real hostname gets a
`421` and only local access works. The `https://<host>/agent/mcp` examples above
need the variable set on the geolang service first.

The MCP token only opens this service. Before each tool runs, geolang exchanges
it for a role-free JWT with `token_use: "tool"` and an exact `scope` array. The
exchange token expires at the earlier of the MCP token's expiry or five minutes.
The MCP token keeps the minting token's role in a private claim so the exchange
cannot delegate an operation that role could not perform directly.
The executor never receives an Agora scope. If a bound result needs a live
document write, geolang-api mints a separate `agora:write` token after the tool
returns and keeps it out of the tool process.

Two tools `/chat` has are missing here. `sql_query` runs SQL the caller wrote in
a browser, which only makes sense when they are the same person. `run_workflow`
runs a manifest the user pressed approve on in their viewer, and an agent
arriving here has no viewer, so it could only ever be refused.

Add one more header and the call's map effects also land in a live agora
document, so every open viewer redraws while the outside agent works:

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

The layers a tool emits become document layers and a `fly_to` moves the agent's
presence. The agent joins as its own member, put there by a grant made with the
caller's token, so it can only reach documents its caller could already edit.
`AGORA_URL` says where agora is. See
[writing to a live map](docs/api_reference.md#writing-to-a-live-map).

---

## License

AGPL-3.0-or-later, see [LICENSE](LICENSE).

Copyright (C) 2026 Grok Image Compression Inc.
