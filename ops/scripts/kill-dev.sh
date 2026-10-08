#!/usr/bin/env bash
# Force-kill ONLY this worktree's dev servers (what `make dev-all` spawned here):
#   backend   uvicorn backend…:app|create_app       (--port $API_PORT)
#   gateway   uvicorn model_gateway…:app|create_app (--port $GATEWAY_PORT)
#   web       vite                                  (listening on $WEB_PORT)
#   worker    python -m worker… run                 (--health-port $ALKERA_WORKER_HEALTH_PORT)
#
# The module names match the open entry points and any composition root that
# extends them (dev-all.sh takes them from DATABENCH_*_APP / DATABENCH_WORKER_MODULE).
# Each worktree runs the same module paths, so we CANNOT match on the module
# alone; that would also kill sibling worktrees. Instead we scope to this
# workspace's identifiers: the `--port <N>` in the uvicorn argv (unique per
# worktree, present on both the reloader and its child so respawns are stopped),
# the worker's `--health-port <N>`, and this workspace's listening ports. For the
# all-worktrees hammer, use `make kill-all-dev`.

set -uo pipefail

cd "$(dirname "$0")/../.."

# Resolve this workspace's ports/identifiers the same way dev-all.sh does.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [ -f .env.workspace ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.workspace
  set +a
fi

API_PORT="${API_PORT:-8000}"
GATEWAY_PORT="${GATEWAY_PORT:-8081}"
WEB_PORT="${WEB_PORT:-5173}"
ALKERA_WORKER_HEALTH_PORT="${ALKERA_WORKER_HEALTH_PORT:-9000}"
PROJECT="${COMPOSE_PROJECT_NAME:-alkera}"

# Port-qualified command patterns (kill reloaders before freeing ports so they
# can't respawn) + the worker's per-workspace health port (both `uv run` and the
# python child carry it in their argv).
PATTERNS=(
  "backend[a-z_]*\.[a-z_]+:(app|create_app).*--port ${API_PORT}"
  "model_gateway[a-z_]*\.[a-z_]+:(app|create_app).*--port ${GATEWAY_PORT}"
  "python -m worker[a-z_.]* run.*--health-port ${ALKERA_WORKER_HEALTH_PORT}"
)
PORTS=("$API_PORT" "$GATEWAY_PORT" "$WEB_PORT" "$ALKERA_WORKER_HEALTH_PORT")

killed=0
kill_pids() { # label, pids...
  local label="$1"; shift
  [ "$#" -eq 0 ] && return 0
  echo "==> Killing $label: $*"
  kill -9 "$@" 2>/dev/null || true
  killed=1
}

# Patterns first so reloaders die before we free their ports (no respawn race).
for pat in "${PATTERNS[@]}"; do
  # shellcheck disable=SC2046
  kill_pids "'$pat'" $(pgrep -f "$pat" 2>/dev/null || true)
done

for port in "${PORTS[@]}"; do
  # shellcheck disable=SC2046
  # LISTENERS only. Without -sTCP:LISTEN, lsof also names every process holding
  # a connection to or from the port, and on a Mac that includes OrbStack's own
  # process, which carries a local box's connections to the API: a kill -9 of it
  # takes Docker down for every worktree.
  kill_pids "port $port" $(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null || true)
done

if [ "$killed" -eq 0 ]; then
  echo "==> Nothing to kill: no dev servers running for $PROJECT."
fi
