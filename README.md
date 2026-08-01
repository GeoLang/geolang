# GeoLang

**AI-powered geospatial agent**: a natural language interface to GIS operations. The agent loop runs in [sibyl](../sibyl), a separate Rust service. GeoLang owns the tools, the persona, and the viewer protocol.

[![CI](https://github.com/GeoLang/geolang/actions/workflows/ci.yml/badge.svg)](https://github.com/GeoLang/geolang/actions)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## Features

- Natural language geospatial queries
- Integration with GeoLang platform services (Ptolemy, Geokode, Itinera, TileTopia)
- 39 geospatial tools served to sibyl over HTTP and executed in-process
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

### Authenticating tool execution

`POST /tools/{name}` runs code and writes files, so on the platform it needs a
caller. Set `PLATFORM_JWT_SECRET` to the shared platform secret and the route
requires an `Authorization: Bearer <jwt>` header holding a live HS256 token,
the same `{sub, exp, role}` tokens ptolemy mints and geodukt's `/run` accepts.
Signature and `exp` are checked, nothing else, and the token is still forwarded
to the services the tool calls, which enforce their own roles.

Leave the variable unset and the route stays open. That is the standalone
`docker compose up` flow, the test suite and the eval harness, none of which
carry a token. `GET /tools` is never gated: sibyl fetches the manifest at
startup, before anyone has signed in.

---

## License

AGPL-3.0-or-later, see [LICENSE](LICENSE).

Copyright (C) 2026 Grok Image Compression Inc.

