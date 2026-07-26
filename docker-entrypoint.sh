#!/bin/bash
set -e

VENV_DIR="${TOOL_EXEC_DIR:-/app/geolang}/${TOOL_EXEC_VENV_NAME:-env}"
REQS="${TOOL_EXEC_DIR:-/app/geolang}/requirements.txt"

if [ ! -f "$REQS" ]; then
  echo "[entrypoint] ERROR: $REQS not found. The host volume mount likely points at the wrong directory." >&2
  echo "[entrypoint] Check docker-compose.yml: the bind-mount source must be the geolang repo root." >&2
  exit 1
fi

# Probe the venv itself instead of a marker file: Letta used to wipe the venv dir
# (and any marker in it) on startup, and a partial venv must be rebuilt anyway.
if "$VENV_DIR/bin/python" -c "import geopandas, rasterio" >/dev/null 2>&1; then
  echo "[entrypoint] Venv already populated, skipping."
else
  echo "[entrypoint] Populating Letta tool-exec venv at $VENV_DIR …"
  # Wipe any partial/foreign venv (e.g. one created by Letta on-demand with no deps,
  # or one whose shebangs were baked on a different host path).
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --no-cache-dir -r "$REQS"
  echo "[entrypoint] Venv ready."
fi

# Chain to the base Letta image's own entrypoint (postgres init + server startup)
exec /usr/local/bin/docker-entrypoint.sh "$@"
