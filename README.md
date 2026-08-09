# GeoLang

**AI-powered geospatial agent**: a natural language interface to GIS operations. The agent loop runs in [sibyl](../sibyl), a separate Rust service. GeoLang owns the tools, the persona, and the viewer protocol.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## Features

- Natural language geospatial queries
- Integration with GeoLang platform services (Ptolemy, Geokode, Itinera, TileTopia)
- 39 geospatial tools served to sibyl over HTTP and executed in-process, and to outside agents over MCP
- Plan-then-execute for multi-step geoprocessing: the model composes a [geodukt](../geodukt) TOML manifest, `plan_workflow` validates it and streams the plan for the user to approve, `run_workflow` executes it
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
pip install -r requirements_client.txt

# sibyl must be reachable. SIBYL_URL defaults to http://localhost:8090
python -m uvicorn src.api.server:app --reload --port 8080
# → http://localhost:8080/
```

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
# unit tests, run inside the container (deps are pinned for its python)
docker exec viewtopia-geolang-api-1 sh -c "pip install -q pytest respx && python -m pytest tests/ -q"

# NL evals: real agent runs against the local model. Auto-skipped unless
# geolang api, sibyl (local mode), and the llama server are all up.
uv run --with pytest --with httpx python -m pytest tests/test_nl_evals.py -v
```

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
and `exp` are checked, nothing else, and the token is forwarded unchanged to the
services a tool calls, which enforce their own roles.

Gated: `POST /tools/{name}` and `POST /chat/agui`, the file writers `/upload`,
`/draw`, `/export-pdf` and `/export-png`, the sibyl proxies `/sessions*`,
`/models` and `/model`, and the reads `/datasets`, `/outputs/{file}`,
`/download/{file}`, `/geojson/{file}` and `/stats/{file}`. Creating a share is
gated too.

Open: `/health`, the viewer's static assets, the `GET /tools` manifest sibyl
fetches at startup before anyone has signed in, and reading a share by id, whose
whole point is a link that works for someone who never signs in. That reader
gets the view and the summary, not the layers behind them.

Leave the variable unset and the whole API stays open. That is the standalone
`docker compose up` flow, the test suite and the eval harness, none of which
carry a token. Turning it on means the client has to send the header on every
call, layer fetches and download links included.

### MCP for outside agents

The tools are served over the Model Context Protocol at `POST /mcp`,
`/agent/mcp` from outside. Point Claude, Cursor or any MCP client at it:

```json
{ "mcpServers": { "geolang": {
    "type": "http",
    "url": "https://<host>/agent/mcp",
    "headers": { "Authorization": "Bearer <jwt>" }
} } }
```

The bearer is required on every MCP request when `PLATFORM_JWT_SECRET` is set,
and is the identity the tools act as. Set `MCP_ALLOWED_HOSTS` to the public
hostname, otherwise the transport's DNS-rebinding check answers `421` to
everything. See [`docs/api_reference.md`](docs/api_reference.md#mcp).

`sql_query` is the one tool `/chat` has that this does not: it runs SQL the
caller wrote in a browser, which only makes sense when they are the same person.

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

