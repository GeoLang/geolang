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

# Chain to the base Letta image's own entrypoint (postgres init + server startup).
# Not exec'd: letta/server/startup.sh runs postgres as a background child and then
# execs the letta server, so with exec only the server would see SIGTERM and postgres
# would be SIGKILLed (crash recovery on the next boot). Stay as PID 1 and shut both down.
/usr/local/bin/docker-entrypoint.sh "$@" &
MAIN_PID=$!

stop_postgres() {
  [ -f "${PGDATA:-/var/lib/postgresql/data}/postmaster.pid" ] || return 0
  echo "[entrypoint] Stopping postgres …"
  gosu postgres pg_ctl -D "${PGDATA:-/var/lib/postgresql/data}" -m fast -w -t 5 stop || true
}

shutdown() {
  echo "[entrypoint] Signal received, shutting down …"
  kill -TERM "$MAIN_PID" 2>/dev/null || true
  # postgres talks to letta until the end, so let the server go first (docker's
  # default grace is 10s, so cap the wait well under it)
  for _ in $(seq 20); do
    kill -0 "$MAIN_PID" 2>/dev/null || break
    sleep 0.2
  done
  stop_postgres
  exit 0
}
trap shutdown TERM INT

wait "$MAIN_PID"
