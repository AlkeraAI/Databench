#!/usr/bin/env bash
# Run backend + worker + web + model gateway concurrently with prefixed output.
# Ctrl-C tears down EVERY child, gracefully if it can, forcibly if it won't.
#
# What runs is the open product: the backend and gateway factories, the open
# worker and @alkera/web. A checkout that composes more on top sets these (a
# composing Makefile passes the first four to every target):
#   ALKERA_ENV_DIR   the directory holding .env / .env.workspace / .env.local
#                    (default: this repository's root)
#   UV_RUN           how Python commands run (default `uv run`)
#   BACKEND_APP      uvicorn's app argument for the API
#                    (default `--factory backend.app_factory:create_app`)
#   WORKER_MODULE    the module run as `python -m <it> run` (default `worker`)
#   GATEWAY_APP      uvicorn's app argument for the gateway
#                    (default `--factory model_gateway.app_factory:create_app`)
#   WEB_PACKAGE      the pnpm package whose `dev` script serves the web app
#                    (default `@alkera/web`)
#   DEV_ALL_BEFORE   a script sourced after the environment loads, before anything starts
#   DEV_ALL_AFTER    a script sourced after everything started (it may start more,
#                    and may use `prefix`); both run in this shell
# The app names stay in the shape kill-dev.sh matches: backend…:app|create_app,
# model_gateway…:app|create_app, worker….

set -euo pipefail

cd "$(dirname "$0")/../.."
OPEN_ROOT="$(pwd)"
ENV_ROOT="${ALKERA_ENV_DIR:-$OPEN_ROOT}"
read -r -a UV_RUN_CMD <<< "${UV_RUN:-uv run}"
read -r -a BACKEND_APP_ARGS <<< "${BACKEND_APP:---factory backend.app_factory:create_app}"
read -r -a GATEWAY_APP_ARGS <<< "${GATEWAY_APP:---factory model_gateway.app_factory:create_app}"
WORKER_MODULE="${WORKER_MODULE:-worker}"
WEB_PACKAGE="${WEB_PACKAGE:-@alkera/web}"

# Pretty prefixes
prefix() {
  local label="$1"; local color="$2"
  while IFS= read -r line; do
    printf "\033[%sm[%s]\033[0m %s\n" "$color" "$label" "$line"
  done
}

# Every PID descended from us (depth-first, children before parents). We walk the
# real process tree instead of relying on `kill 0` alone because a plain
# process-group signal misses anything that re-sessioned itself (esbuild, some
# `uv`/reloader children), exactly the stuff that dangles after Ctrl-C.
descendants() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    descendants "$child"
    printf '%s\n' "$child"
  done
}

# Ensure children die with us, graceful SIGTERM, then SIGKILL the holdouts.
_cleaning_up=0
cleanup() {
  # A second Ctrl-C (or the EXIT trap firing after INT) must not restart teardown.
  [ "$_cleaning_up" -eq 1 ] && return
  _cleaning_up=1
  trap '' INT TERM   # ignore further signals while we tear down

  echo
  echo "==> Stopping all dev servers"

  # Polite first: SIGTERM the whole tree so servers can flush/close cleanly.
  # ($pids is a newline-separated PID list, word-splitting into separate kill
  # args is the intent here, so the SC2086 quote-suggestion does not apply.)
  local pids
  pids="$(descendants $$)"
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
  fi

  # Give them up to ~5s to exit on their own, polling so a clean shutdown is fast.
  for _ in $(seq 1 50); do
    pids="$(descendants $$)"
    [ -z "$pids" ] && break
    sleep 0.1
  done

  # Anything still running ignored SIGTERM; kill it. Re-snapshot first so
  # a child a reloader respawned mid-shutdown is caught too.
  pids="$(descendants $$)"
  if [ -n "$pids" ]; then
    echo "==> Force-killing stragglers: $(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Load env in the same order the Makefile does (later wins for overlapping keys):
#   .env (committed defaults) → .env.workspace (per-worktree ports/project) →
#   .env.local (the developer's own secrets, such as provider and OAuth keys, always wins).
# All exported so the subprocesses inherit them.
if [ -f "$ENV_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_ROOT/.env"
  set +a
fi
WORKSPACE_ENV_ROOT="$ENV_ROOT" bash ops/scripts/workspace-env.sh ensure >/dev/null 2>&1 || true
if [ -f "$ENV_ROOT/.env.workspace" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_ROOT/.env.workspace"
  set +a
fi
if [ -f "$ENV_ROOT/.env.local" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_ROOT/.env.local"
  set +a
fi

if [ -n "${DEV_ALL_BEFORE:-}" ]; then
  # shellcheck disable=SC1090
  source "$DEV_ALL_BEFORE"
fi

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
GATEWAY_PORT="${GATEWAY_PORT:-8081}"
# The worker's liveness port is per-worktree too (from .env.workspace); passing it
# explicitly puts it in the argv, which is what `make kill-dev` scopes the worker by.
ALKERA_WORKER_HEALTH_PORT="${ALKERA_WORKER_HEALTH_PORT:-9000}"

bash ops/scripts/print-urls.sh

echo "==> Starting backend / worker / web / model-gateway"

(
  # --timeout-graceful-shutdown: --reload restarts by shutting the server down
  # gracefully, which waits for open connections; without a deadline one open
  # event stream (fifty minutes) parks every later request in the accept
  # backlog and the reload never happens.
  cd apps/backend && "${UV_RUN_CMD[@]}" uvicorn "${BACKEND_APP_ARGS[@]}" --reload \
    --host "$API_HOST" --port "$API_PORT" --ws-max-size 2162688 --timeout-graceful-shutdown 5 2>&1
) | prefix "backend" "1;34" &

(
  # One process serves all four task queues locally, against the Temporal server
  # from `make infra-up`. Serving the default queue makes it reconcile the schedule
  # catalog on boot, so the periodic jobs fire without a separate scheduler.
  cd apps/worker && "${UV_RUN_CMD[@]}" python -m "$WORKER_MODULE" run \
    --queues "${ALKERA_TEMPORAL_TASK_QUEUES:-money,email,sync,default}" \
    --health-port "$ALKERA_WORKER_HEALTH_PORT" 2>&1
) | prefix "worker" "1;35" &

(
  pnpm --filter "$WEB_PACKAGE" dev 2>&1
) | prefix "web"    "1;32" &

(
  cd apps/model-gateway && "${UV_RUN_CMD[@]}" uvicorn "${GATEWAY_APP_ARGS[@]}" --reload \
    --host "$API_HOST" --port "$GATEWAY_PORT" --timeout-graceful-shutdown 5 2>&1
) | prefix "gateway" "1;36" &

if [ -n "${DEV_ALL_AFTER:-}" ]; then
  # shellcheck disable=SC1090
  source "$DEV_ALL_AFTER"
fi

wait
