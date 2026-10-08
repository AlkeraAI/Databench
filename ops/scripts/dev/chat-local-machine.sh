#!/usr/bin/env bash
# Register THIS laptop as a cloud chat machine for the local stack and serve
# its chats in the shape of a provisioned box,
# without the box: the same publisher (`alkera cloud-mirror run`), the same
# credential file, the same registration facts, all on one machine.
#
# What it does, idempotently:
#   1. reads this worktree's ports from .env.workspace (never assumes :8000);
#   2. mints a CLI credential for the machine's user through the device flow
#      (a browser login cookie approves the device code, no token is typed,
#      passed on a command line or printed) into a PRIVATE ALKERA_HOME under
#      .alkera-dev/chat-machine/, so ~/.alkera and any other stack are untouched;
#      an unexpired credential already there is reused;
#   3. starts the mirror in the foreground against a throwaway workspace
#      (.alkera-dev/chat-machine/workspace by default), Ctrl-C stops it.
#
# Environment (all optional):
#   CHAT_MACHINE_EMAIL / CHAT_MACHINE_PASSWORD   the user the machine acts as
#                                               (default: the seeded admin)
#   CHAT_MACHINE_NAME                           the machine's name in the portal
#   CHAT_MACHINE_TYPE_CODE                      catalog code (default: self-hosted, seeded)
#   CHAT_MACHINE_WORKSPACE                      the folder the box's tools work in
#   CHAT_MACHINE_LOG_LEVEL                      debug|info|warning|error
#   CHAT_MACHINE_STATE_DIR                      where the credential and the default
#                                               workspace live (default .alkera-dev/chat-machine)
#   ALKERA_API_URL + CHAT_MACHINE_WEB_ORIGIN    the API and the portal origin, given
#                                               together in place of .env.workspace (the
#                                               box image runs this script that way)
#   CHAT_MACHINE_PYTHON / CHAT_MACHINE_ALKERA   how Python and the CLI run (default
#                                               `uv run --frozen python` / `... alkera`)
#   --mint-only                                 stop after the credential exists
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

MINT_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --mint-only) MINT_ONLY=1 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[36m[chat-machine]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[chat-machine] ✗\033[0m %s\n' "$*" >&2; exit "${2:-1}"; }

read -r -a PY <<< "${CHAT_MACHINE_PYTHON:-uv run --frozen python}"
read -r -a ALKERA <<< "${CHAT_MACHINE_ALKERA:-uv run --frozen alkera}"

# --- 1. this worktree's ports --------------------------------------------------
if [ -n "${ALKERA_API_URL:-}" ] && [ -n "${CHAT_MACHINE_WEB_ORIGIN:-}" ]; then
  API_URL="$ALKERA_API_URL"
  WEB_PORT=""
else
[ -f .env.workspace ] || die ".env.workspace is missing; run any make target once (make urls)" 3
env_key() { grep -E "^$1=" .env.workspace | tail -1 | cut -d= -f2- | tr -d '"' ; }
API_PORT="$(env_key API_PORT)"
WEB_PORT="$(env_key WEB_PORT)"
[ -n "$API_PORT" ] || die "API_PORT is not in .env.workspace" 3
API_URL="${ALKERA_API_URL:-http://127.0.0.1:$API_PORT}"
fi
# The portal's origin, as the backend reads it (.env, then .env.workspace, then
# .env.local, last one wins). The CSRF guard refuses a cookie-authenticated
# request that names no trusted origin, so the approve call states this one.
frontend_base_url() {
  local f value found=""
  for f in .env .env.workspace .env.local; do
    [ -f "$f" ] || continue
    value="$(grep -E '^FRONTEND_BASE_URL=' "$f" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    if [ -n "$value" ]; then found="$value"; fi
  done
  printf '%s' "${found:-http://localhost:$WEB_PORT}"
}
WEB_ORIGIN="${CHAT_MACHINE_WEB_ORIGIN:-$(frontend_base_url)}"
WEB_ORIGIN="${WEB_ORIGIN%/}"

STATE_DIR="${CHAT_MACHINE_STATE_DIR:-$ROOT/.alkera-dev/chat-machine}"
HOME_DIR="$STATE_DIR/home"
WORKSPACE="${CHAT_MACHINE_WORKSPACE:-$STATE_DIR/workspace}"
mkdir -p "$HOME_DIR" "$WORKSPACE"
chmod 700 "$HOME_DIR"
AUTH_FILE="$HOME_DIR/auth.yml"

curl -fsS --max-time 5 "$API_URL/health/live" >/dev/null 2>&1 \
  || die "no backend on $API_URL; start the stack first (make dev-all)" 4

# --- 2. the credential -------------------------------------------------------------
credential_is_fresh() {
  [ -f "$AUTH_FILE" ] || return 1
  local expires
  expires="$(grep -E '^expires_at:' "$AUTH_FILE" | head -1 | sed -E "s/^expires_at: *'?([^']*)'?$/\1/")"
  [ -n "$expires" ] || return 1
  "${PY[@]}" - "$expires" <<'PY' >/dev/null 2>&1
import sys
from datetime import datetime, timedelta, timezone
at = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
if at.tzinfo is None:
    at = at.replace(tzinfo=timezone.utc)
sys.exit(0 if at - datetime.now(timezone.utc) > timedelta(hours=1) else 1)
PY
}

# A machine registers only under a compute grant (a fresh stack's org holds
# none, so registration answers 429 no_compute_grant). When the signed-in user
# is a platform admin and the org holds no grant, give it one for local
# machines: two at a time, no rate. Anyone else's org needs an admin's grant.
ensure_compute_grant() {
  local jar="$1" org grants expires
  org="$(curl -fsS -b "$jar" "$API_URL/api/v1/auth/me" \
    | "${PY[@]}" -c 'import json,sys;print(json.load(sys.stdin).get("org_team_id") or "")')" || return 0
  [ -n "$org" ] || return 0
  if ! grants="$(curl -fsS -b "$jar" "$API_URL/admin/v1/orgs/$org/compute" 2>/dev/null)"; then
    log "no compute grant could be read for this org; a platform admin must grant one"
    return 0
  fi
  [ "$grants" = "[]" ] || return 0
  expires="$("${PY[@]}" -c 'from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(days=3650)).isoformat())')"
  curl -fsS -b "$jar" -X PUT -H 'Content-Type: application/json' -H "Origin: $WEB_ORIGIN" \
    --data "$(printf '{"ceiling":2,"rate_per_minute_nanos":0,"expires_at":"%s","note":"local chat machine"}' "$expires")" \
    "$API_URL/admin/v1/orgs/$org/compute" >/dev/null \
    || die "granting this org compute for its local machine failed" 5
  log "granted this org compute for its local machine"
}

mint_credential() {
  local email="${CHAT_MACHINE_EMAIL:-${AUTH_DEV_ADMIN_EMAIL:-admin@example.com}}"
  local password="${CHAT_MACHINE_PASSWORD:-admin}"
  local jar code device_code user_code token_json
  jar="$(mktemp)"
  code="$(mktemp)"
  token_json="$(mktemp)"
  trap 'rm -f "$jar" "$code" "$token_json"' RETURN

  log "signing in as $email to approve a device code (nothing is printed)"
  curl -fsS -c "$jar" -H 'Content-Type: application/json' \
    --data "$(printf '{"email":"%s","password":"%s"}' "$email" "$password")" \
    "$API_URL/api/v1/auth/login" >/dev/null \
    || die "login failed for $email (set CHAT_MACHINE_EMAIL / CHAT_MACHINE_PASSWORD)" 5
  ensure_compute_grant "$jar"

  curl -fsS --data-urlencode 'client_id=alkera-cli' "$API_URL/api/v1/auth/device/code" >"$code" \
    || die "the device-code endpoint refused" 5
  device_code="$("${PY[@]}" -c 'import json,sys;print(json.load(open(sys.argv[1]))["device_code"])' "$code")"
  user_code="$("${PY[@]}" -c 'import json,sys;print(json.load(open(sys.argv[1]))["user_code"])' "$code")"

  curl -fsS -b "$jar" -H 'Content-Type: application/json' -H "Origin: $WEB_ORIGIN" \
    --data "$(printf '{"user_code":"%s"}' "$user_code")" \
    "$API_URL/api/v1/auth/device/approve" >/dev/null \
    || die "approving the device code failed" 5

  curl -fsS --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:device_code' \
    --data-urlencode "device_code=$device_code" --data-urlencode 'client_id=alkera-cli' \
    "$API_URL/api/v1/auth/device/token" >"$token_json" \
    || die "the token grant failed" 5

  # Written by the CLI's own writer so the shape and the 0600 mode are its own.
  ALKERA_HOME="$HOME_DIR" "${PY[@]}" - "$token_json" "$API_URL" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
from alkera_cli.account.auth_file import StoredAuth, save_auth
grant = json.load(open(sys.argv[1]))
expires = datetime.now(timezone.utc) + timedelta(seconds=int(grant.get("expires_in", 86400)))
save_auth(StoredAuth(api_url=sys.argv[2], token=grant["access_token"], expires_at=expires))
PY
  log "credential written to $AUTH_FILE (0600)"
}

if credential_is_fresh; then
  log "reusing the credential in $AUTH_FILE"
else
  mint_credential
fi
[ "$MINT_ONLY" = 1 ] && exit 0

# --- 3. the mirror ---------------------------------------------------------------------
MACHINE_NAME="${CHAT_MACHINE_NAME:-$(hostname -s)-local}"
log "serving cloud chats as machine '$MACHINE_NAME' from $WORKSPACE"
log "portal: http://localhost:${WEB_PORT:-?}  api: $API_URL  (Ctrl-C stops the mirror)"
exec env ALKERA_HOME="$HOME_DIR" ALKERA_API_URL="$API_URL" \
  "${ALKERA[@]}" cloud-mirror run \
    --api-url "$API_URL" \
    --project "$WORKSPACE" \
    --machine-name "$MACHINE_NAME" \
    --provider self_hosted \
    --provider-pod-id "local-$(hostname -s)" \
    --machine-type-code "${CHAT_MACHINE_TYPE_CODE:-self-hosted}" \
    --log-level "${CHAT_MACHINE_LOG_LEVEL:-info}"
