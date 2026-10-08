#!/usr/bin/env bash
#
# Start a local developer box for THIS worktree's stack: a pool box run as a
# Docker container on this machine, every chat under gVisor (the `localdev`
# compute provider). Run with `make dev-box`, after `make dev-all`.
#
# It goes through the same admin provision route the console uses, so the box is
# provisioned, claims its machine credential and comes up `ready` exactly as a
# remote machine does. Idempotent: with a local box already serving it only says
# so. Once provisioned, the backend keeps the box running (it starts or
# recreates it at backend start and every 30 s), so this is run once.
#
#   1. reads this worktree's ports from .env.workspace;
#   2. builds the box image if this machine does not have it yet;
#   3. signs in as the seeded admin (AUTH_DEV_ADMIN_EMAIL / admin, or
#      DEV_BOX_EMAIL / DEV_BOX_PASSWORD) on the API;
#   4. grants the admin's org compute if it has none, so chats place on the box;
#   5. provisions one `localdev` box unless one is already serving;
#   6. waits until the box is `ready` (the first boot stages gVisor's rootfs and
#      syncs the daemon's venv, a few minutes; later ones take seconds).
#
# Exit codes: 0 ready; 3 the stack is not up or not configured; 4 Docker is not
# running; 5 a step the API refused; 6 the box did not come up in time.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

log() { printf '\033[36m[dev-box]\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit "${2:-1}"; }

[ -f .env.workspace ] || die ".env.workspace is missing; run any make target once (make urls)" 3
env_key() { grep -E "^$1=" .env.workspace | tail -1 | cut -d= -f2- | tr -d '"'; }
API_PORT="$(env_key API_PORT)"
WEB_PORT="$(env_key WEB_PORT)"
[ -n "$API_PORT" ] && [ -n "$WEB_PORT" ] || die "API_PORT / WEB_PORT are not in .env.workspace" 3
API_URL="http://127.0.0.1:$API_PORT"
ORIGIN="http://localhost:$WEB_PORT"
EMAIL="${DEV_BOX_EMAIL:-${AUTH_DEV_ADMIN_EMAIL:-admin@example.com}}"
PASSWORD="${DEV_BOX_PASSWORD:-admin}"
READY_TIMEOUT="${DEV_BOX_READY_TIMEOUT:-900}"

docker info >/dev/null 2>&1 || die "Docker is not running; start OrbStack or Docker Desktop" 4
curl -fsS --max-time 5 "$API_URL/health/live" >/dev/null 2>&1 \
  || die "the API is not answering at $API_URL; run make dev-all first" 3

log "building the box image if this machine does not have it"
TAG="$(uv run --frozen python -c '
from alkera_core.compute.localdev import IMAGE_CONTEXT, default_source_root, image_tag
print(image_tag(default_source_root() / IMAGE_CONTEXT))')"
if ! docker image inspect "$TAG" >/dev/null 2>&1; then
  docker build -t "$TAG" deploy/docker/localdev-box
fi

log "building the box's Linux opencode harness if it is missing or stale"
bash ops/scripts/dev/build-localdev-agent.sh

jar="$(mktemp)"
trap 'rm -f "$jar"' EXIT
api() {
  # api METHOD PATH [JSON]: one cookie-authenticated call; prints the body.
  local method="$1" path="$2" body="${3:-}"
  local args=(-fsS -b "$jar" -c "$jar" -H "Origin: $ORIGIN" -H 'Content-Type: application/json' -X "$method")
  [ -n "$body" ] && args+=(--data "$body")
  curl "${args[@]}" "$API_URL$path"
}

log "signing in as $EMAIL (nothing is printed)"
api POST /api/v1/auth/login "$(printf '{"email":"%s","password":"%s"}' "$EMAIL" "$PASSWORD")" >/dev/null \
  || die "login failed for $EMAIL (set DEV_BOX_EMAIL / DEV_BOX_PASSWORD)" 5
ORG="$(api GET /api/v1/auth/me | jq -r .org_team_id)"
[ -n "$ORG" ] && [ "$ORG" != null ] || die "the signed-in user has no org" 5

if api GET "/admin/v1/orgs/$ORG/compute" 2>/dev/null | jq -e 'length > 0' >/dev/null 2>&1; then
  log "the org already has a compute grant"
else
  log "granting the org compute so its chats place on the box"
  expires="$(date -u -v+1y +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+1 year' +%Y-%m-%dT%H:%M:%SZ)"
  api PUT "/admin/v1/orgs/$ORG/compute" \
    "$(printf '{"ceiling":2,"rate_per_minute_nanos":0,"expires_at":"%s"}' "$expires")" >/dev/null \
    || die "the compute grant was refused" 5
fi

serving() {
  api GET /admin/v1/machines | jq -r '
    .items[]? | select(.provider == "localdev")
    | select(.state | IN("provisioning","bootstrapping","ready","draining"))
    | "\(.id) \(.state)"' | head -n1
}
existing="$(serving)"
if [ -n "$existing" ]; then
  log "a local box is already serving ($existing)"
  machine="${existing%% *}"
else
  log "provisioning a localdev box"
  machine="$(api POST /admin/v1/machines/provision \
    '{"provider":"localdev","machine_type_code":"local","storage_gb":10,"tenancy":"pool"}' \
    | jq -r .id)" || die "the provision was refused (see the backend log)" 5
fi

log "waiting for $machine to be ready (first boot: a few minutes)"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
while :; do
  state="$(api GET /admin/v1/machines | jq -r --arg id "$machine" '.items[]? | select(.id == $id) | .state')"
  case "$state" in
    ready) break ;;
    failed|released|lost) die "the box ended $state; see docker logs and the backend log" 6 ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] || die "the box is still $state after ${READY_TIMEOUT}s" 6
  sleep 5
done
container="$(api GET /admin/v1/machines | jq -r --arg id "$machine" '.items[]? | select(.id == $id) | .provider_machine_id')"
log "ready: $container. Logs: docker exec $container tail -f /var/log/alkera-node-bootstrap.log"
