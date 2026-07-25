# GeoLang

**AI-powered geospatial agent** — natural language interface to GIS operations, powered by [Letta](https://github.com/letta-ai/letta).

[![CI](https://github.com/GeoLang/geolang/actions/workflows/ci.yml/badge.svg)](https://github.com/GeoLang/geolang/actions)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## Features

- Natural language geospatial queries
- Integration with GeoLang platform services (Ptolemy, Geokode, Itinera, TileTopia)
- Letta-based agent with persistent memory
- Embedding server support (vLLM + sentence-transformers)

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.11+ (for the FastAPI server on the host)
- An LLM provider API key (xAI Grok by default; OpenAI-compatible endpoints also work)

### Configure

GeoLang reads provider keys from environment variables. **Do not commit keys to
`docker-compose.yml`.** Use a `.env` file or shell exports:

```bash
export XAI_API_KEY="your-xai-key-here"
# OPENAI_API_KEY may also be set if you use an OpenAI-compatible endpoint.
```

### Run the Letta backend

```bash
# Build the geolang image (pinned to a known-good Letta base — see Dockerfile).
docker build -t letta-gis:latest .

# Start as a service (NOT `run --rm`, which is one-shot and exits).
docker compose up -d

# Watch startup
docker compose logs -f letta-gis
```

First-time startup will:

1. Run `docker-entrypoint.sh` which populates the Letta tool-exec venv at
   `./env/` from `requirements.txt` (writes a `.populated` marker; subsequent
   restarts skip this step).
2. Chain to the base Letta entrypoint which starts internal Postgres + Redis +
   the Letta server on port `8283`.

Letta is ready when you see `Uvicorn running on http://0.0.0.0:8283` in the
logs. The data dir is bind-mounted from `~/.letta/.persist/pgdata` — if you
ever change the pinned Letta version and hit an Alembic migration error, the
remedy is to wipe `~/.letta/.persist/pgdata` (and `.agent_id` / `.sessions.json`
in this repo) and start fresh.

### Run the GeoLang API server

```bash
# Optional: client deps for the embedding server / dev tooling
pip install -r requirements_client.txt

# Start the FastAPI app on the host (talks to the Letta container).
python -m uvicorn src.api.server:app --reload --port 8080
# → http://localhost:8080/
```

`TOOL_EXEC_DIR` auto-detects the geolang repo root from `src/core/utils.py`, so
no env var is needed in dev. Override it via the `TOOL_EXEC_DIR` env var if you
want outputs elsewhere.

### Embedding server (optional, for memory recall)

```bash
python3.12 -m venv ~/vllmenv
source ~/vllmenv/bin/activate
pip install -r requirements_vllm.txt
python -m vllm.entrypoints.openai.api_server \
  --model sentence-transformers/all-MiniLM-L6-v2 --port 8000
```

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — process topology, SSE event vocabulary, tool registration flow
- [`docs/api_reference.md`](docs/api_reference.md) — all HTTP endpoints and the tool catalogue
- [`docs/viewer_integration.md`](docs/viewer_integration.md) — `viewer_cmd` protocol for ViewTopia (including `sql_query` for in-browser DuckDB)
- [`docs/DESIGN.md`](docs/DESIGN.md) — open improvements, known sharp edges, decision log

## Platform Integration

When running as part of the full GeoLang platform (via `viewtopia/docker-compose.platform.yml`),
GeoLang runs on port **8283** internally and is exposed on port **8080** externally.

---

## License

AGPL-3.0-or-later, see [LICENSE](LICENSE).

Copyright (C) 2026 Grok Image Compression Inc.

