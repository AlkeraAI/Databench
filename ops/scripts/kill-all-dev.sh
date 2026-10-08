#!/usr/bin/env bash
# NUCLEAR: force-kill EVERY worktree's dev servers across all branches.
#
# Every worktree runs the same module paths, so matching on those bare patterns
# (no port qualifier) sweeps up backend/gateway/worker/web for ALL workspaces at
# once, use this when you've lost track of what's running everywhere. To kill
# only the current worktree's stack, use `make kill-dev` instead.

set -uo pipefail

cd "$(dirname "$0")/../.."

# Broad, port-agnostic command substrings, identical across every worktree, so
# they match all of them. Also stops the reloaders so they can't respawn.
PATTERNS=(
  "backend[a-z_]*\.[a-z_]+:(app|create_app)"        # backend uvicorn (reloader + worker)
  "model_gateway[a-z_]*\.[a-z_]+:(app|create_app)"  # gateway uvicorn (reloader + worker)
  "python -m worker[a-z_.]* run"                    # temporal worker (`uv run` parent + python child)
  "@alkera/web[a-z-]* dev"  # vite dev server (pnpm wrapper)
)
# Classic ports as a backstop for orphaned listeners (covers `main`).
PORTS=(8000 8081 5173 9000)

killed=0
kill_pids() { # label, pids...
  local label="$1"; shift
  [ "$#" -eq 0 ] && return 0
  echo "==> Killing $label: $*"
  kill -9 "$@" 2>/dev/null || true
  killed=1
}

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
  echo "==> Nothing to kill: no dev servers running on any worktree."
fi
