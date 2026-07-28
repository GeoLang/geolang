# GeoLang

**AI-powered geospatial agent**: a natural language interface to GIS operations. The agent loop runs in [sibyl](../sibyl), a separate Rust service. GeoLang owns the tools, the persona, and the viewer protocol.

[![CI](https://github.com/GeoLang/geolang/actions/workflows/ci.yml/badge.svg)](https://github.com/GeoLang/geolang/actions)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## Features

- Natural language geospatial queries
- Integration with GeoLang platform services (Ptolemy, Geokode, Itinera, TileTopia)
- 36 geospatial tools served to sibyl over HTTP and executed in-process
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

## Platform Integration

When running as part of the full GeoLang platform (via `viewtopia/docker-compose.platform.yml`),
GeoLang serves the API on port **8080** and sibyl runs alongside it on **8090**.

---

## License

AGPL-3.0-or-later, see [LICENSE](LICENSE).

Copyright (C) 2026 Grok Image Compression Inc.

