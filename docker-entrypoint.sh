#!/bin/bash
set -e

VENV_DIR="${TOOL_EXEC_DIR:-/app/geolang}/${TOOL_EXEC_VENV_NAME:-env}"
REQS="${TOOL_EXEC_DIR:-/app/geolang}/requirements.txt"
MARKER="$VENV_DIR/.populated"

if [ ! -f "$REQS" ]; then
  echo "[entrypoint] ERROR: $REQS not found. The host volume mount likely points at the wrong directory." >&2
  echo "[entrypoint] Check docker-compose.yml: the bind-mount source must be the geolang repo root." >&2
  exit 1
fi

if [ ! -f "$MARKER" ]; then
  echo "[entrypoint] Populating Letta tool-exec venv at $VENV_DIR …"
  # Wipe any partial/foreign venv (e.g. one created by Letta on-demand with no deps,
  # or one whose shebangs were baked on a different host path).
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --no-cache-dir -r "$REQS"
  touch "$MARKER"
  echo "[entrypoint] Venv ready."
else
  echo "[entrypoint] Venv already populated ($MARKER present), skipping."
fi

# Chain to the base Letta image's original entrypoint (postgres init + server startup)
exec /usr/local/bin/letta-docker-entrypoint.sh "$@"
