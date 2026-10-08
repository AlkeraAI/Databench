#!/usr/bin/env bash
# Self-deploy smoke for the reference PRODUCTION Docker Compose stack.
#
# Brings the stack up exactly as a customer would (per deploy/INSTALL.md),
# runs the documented first-admin bootstrap, and ASSERTS the health / version /
# white-label / auth / no-email behaviours a self-hosted install relies on, then
# tears down. Runs on any Docker host (a laptop, a CI runner). Idempotent:
# always cleans up its own project + volumes on exit.
#
# Scenario is env-driven (all optional). Defaults = the on-prem AIR-GAPPED shape
# (no email, no Cloudflare) + a white-label brand, since that's the strictest case.
#   APP_VERSION                  image tag to deploy        (default 0.1.0-selftest)
#   EMAIL_ENABLED                false = no-email mode       (default false)
#   BRAND_PRODUCT_NAME           white-label assertion       (default "Acme Data")
#   BRAND_SUPPORT_EMAIL          white-label assertion       (default help@acme.example)
#   SMTP_HOST/USERNAME/PASSWORD  set for a BYO-relay scenario (default unset)
#   COMPOSE_FILE                 (default: the repo's compose.prod.example.yml)
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO/deploy/docker/compose.prod.example.yml}"
APP_VERSION="${APP_VERSION:-0.1.0-selftest}"
EMAIL_ENABLED="${EMAIL_ENABLED:-false}"
BRAND_PRODUCT_NAME="${BRAND_PRODUCT_NAME:-Acme Data}"
BRAND_SUPPORT_EMAIL="${BRAND_SUPPORT_EMAIL:-help@acme.example}"
ADMIN_EMAIL="${ADMIN_BOOTSTRAP_EMAIL:-admin@acme.example}"
ADMIN_ORG="${ADMIN_BOOTSTRAP_ORG_NAME:-Acme}"
BASE_URL="${PUBLIC_BASE_URL:-http://localhost}"

WORK="$(mktemp -d)"

# The EXIT trap below is the only way out, so it -- not its own last command -- has
# to decide what the caller reads back. It re-raises the status it was entered with,
# and falls back to the stamp this run sets just before its last line, because bash
# 3.2 (the /bin/bash macOS still ships) enters the trap with $? already 0 when `set
# -u` aborts on an unbound variable: on that shell a run that asserted nothing would
# otherwise report success. `|| true` keeps a best-effort teardown from aborting the
# trap under `set -e` before it reaches that exit.
SMOKE_COMPLETED=0
cleanup() {
  local status=$?
  (cd "$WORK" 2>/dev/null && docker compose down -v --remove-orphans >/dev/null 2>&1 || true)
  rm -rf "$WORK" || true
  if [ "$status" -eq 0 ] && [ "$SMOKE_COMPLETED" -ne 1 ]; then status=1; fi
  exit "$status"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; (cd "$WORK" && docker compose logs --tail=50 backend gateway worker temporal 2>&1 | tail -100) >&2 || true; exit 1; }
pass() { echo "PASS: $*"; }

cp "$COMPOSE_FILE" "$WORK/compose.yml"
cd "$WORK"

# Every secret is generated into a shell VARIABLE first and the .env is written from
# those variables. A `NAME=$(openssl ...)` line inside the heredoc produces a file line
# and nothing else: the script itself would then have no `$NAME` (an abort under `set
# -u` at the first use), and a caller that exported one would get a different value in
# .env from the one it holds -- so its `psql -U temporal` probe would use the wrong
# password. `${NAME:-...}` keeps a caller's own value when it supplies one.
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 16)}"
TEMPORAL_DB_PASSWORD="${TEMPORAL_DB_PASSWORD:-$(openssl rand -hex 16)}"
AUTH_JWT_SECRET="${AUTH_JWT_SECRET:-$(openssl rand -hex 32)}"
TOKEN_HASH_PEPPER="${TOKEN_HASH_PEPPER:-$(openssl rand -hex 32)}"

cat > .env <<EOF
APP_VERSION=$APP_VERSION
DB_PASSWORD=$DB_PASSWORD
TEMPORAL_DB_PASSWORD=$TEMPORAL_DB_PASSWORD
AUTH_JWT_SECRET=$AUTH_JWT_SECRET
TOKEN_HASH_PEPPER=$TOKEN_HASH_PEPPER
PUBLIC_BASE_URL=$BASE_URL
EMAIL_ENABLED=$EMAIL_ENABLED
TURNSTILE_REQUIRED=false
GATEWAY_ASSUME_BEDROCK_IAM=true
BRAND_PRODUCT_NAME=$BRAND_PRODUCT_NAME
BRAND_SUPPORT_EMAIL=$BRAND_SUPPORT_EMAIL
ADMIN_BOOTSTRAP_EMAIL=$ADMIN_EMAIL
ADMIN_BOOTSTRAP_ORG_NAME=$ADMIN_ORG
SMTP_HOST=${SMTP_HOST:-}
SMTP_USERNAME=${SMTP_USERNAME:-}
SMTP_PASSWORD=${SMTP_PASSWORD:-}
EOF

docker compose config -q || fail "compose config invalid"
pass "compose config valid (EMAIL_ENABLED=$EMAIL_ENABLED, SMTP_HOST='${SMTP_HOST:-}')"

docker compose up -d
for i in $(seq 1 30); do
  sleep 6
  docker compose ps --format '{{.Service}}={{.Health}}' | grep -q 'backend=healthy' && break
  [ "$i" = 30 ] && fail "backend never became healthy"
done
pass "stack up, backend healthy"

# The worker's health check is its own /health/live (200 only while its Temporal
# pollers run), so worker=healthy proves the boot connect to the bundled Temporal
# succeeded and the schedules were registered, not just that a process started.
for i in $(seq 1 30); do
  sleep 6
  health=$(docker compose ps --format '{{.Service}}={{.Health}}')
  echo "$health" | grep -q 'worker=healthy' && echo "$health" | grep -q 'temporal=healthy' && break
  [ "$i" = 30 ] && fail "worker/temporal never became healthy: $health"
done
pass "worker + temporal healthy: Temporal reachable, pollers running"

# temporal-db-init is a one-shot: it creates Temporal's dedicated Postgres role and
# the two databases it owns, then exits 0. Prove it succeeded, then exclude it from
# the "nothing exited" check that catches services crash-looping on missing env.
init_status=$(docker compose ps -a --format '{{.Service}} {{.Status}}' | grep '^temporal-db-init ')
echo "$init_status" | grep -q 'Exited (0)' || fail "temporal-db-init did not succeed: $init_status"
pass "temporal-db-init exited 0: Temporal's own DB role and databases are in place"

docker compose ps -a --format '{{.Service}} {{.Status}}' | grep -v '^temporal-db-init ' \
  | grep -iqE 'restart|exited' \
  && fail "a service is restarting/exited (anchor likely missing env)" || true
pass "no restarts/exits: every service booted under the prod validator"

# The bundled Temporal must be running under its own least-privileged role, not the
# application database's superuser: prove the role owns its two databases and that its
# credential reads nothing of the app's.
owners=$(docker compose exec -T postgres psql -U alkera -d postgres -tAc \
  "SELECT string_agg(datname || '=' || pg_get_userbyid(datdba), ',' ORDER BY datname) \
   FROM pg_database WHERE datname IN ('temporal','temporal_visibility')")
[ "$owners" = "temporal=temporal,temporal_visibility=temporal" ] \
  || fail "Temporal's databases are not owned by the temporal role: $owners"
docker compose exec -T -e PGPASSWORD="$TEMPORAL_DB_PASSWORD" postgres \
  psql -U temporal -h 127.0.0.1 -d alkera -tAc "SELECT * FROM users LIMIT 1" >/dev/null 2>&1 \
  && fail "the temporal role can read the application's users table" || true
pass "Temporal runs as its own role: owns its two databases, reads no app table"

info=$(curl -fsS "$BASE_URL/health/info") || fail "/health/info unreachable"
echo "$info" | grep -q "\"version\":\"$APP_VERSION\"" || fail "version mismatch: $info"
pass "/health/info reports version $APP_VERSION (Dockerfile ARG fix)"

curl -fsS "$BASE_URL/health/ready" | grep -q '"db":"ok"' || fail "/health/ready db not ok"
pass "/health/ready: db ok"

cfg=$(curl -fsS "$BASE_URL/api/v1/config") || fail "/api/v1/config unreachable"
echo "$cfg" | grep -qF "$BRAND_PRODUCT_NAME" || fail "white-label product_name missing: $cfg"
echo "$cfg" | grep -qF "$BRAND_SUPPORT_EMAIL" || fail "white-label support_email missing: $cfg"
pass "/api/v1/config white-label = '$BRAND_PRODUCT_NAME' / $BRAND_SUPPORT_EMAIL"

# A self-hosted install turns browser telemetry off: the SPA never reports
# anywhere, even when a Sentry DSN was baked into the web image.
echo "$cfg" | grep -q '"telemetry_enabled":false' || fail "self-host must disable telemetry: $cfg"
pass "/api/v1/config telemetry_enabled=false (no Sentry on a self-host)"

OUT=$(docker compose --profile bootstrap run --rm bootstrap 2>&1)
echo "$OUT" | grep -q "created: org='$ADMIN_ORG'" || fail "bootstrap did not create admin: $OUT"
PW=$(echo "$OUT" | grep -A1 'GENERATED ADMIN PASSWORD' | tail -1 | tr -d '[:space:]')
[ -n "$PW" ] || fail "bootstrap printed no generated password"
pass "first-admin bootstrap created $ADMIN_EMAIL"

code=$(curl -sS -c "$WORK/cookies.txt" -o /tmp/sd_login.json -w '%{http_code}' \
  -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$PW\"}")
[ "$code" = 200 ] || fail "login HTTP $code: $(cat /tmp/sd_login.json)"
grep -q "\"$ADMIN_EMAIL\"" /tmp/sd_login.json || fail "login body missing admin email"
pass "login HTTP 200 as the bootstrapped admin (no captcha, Turnstile off)"

# A distribution adds checks of its own as scripts in compose-smoke.d/. Each one
# is sourced here, signed in as the admin, with BASE_URL, WORK (cookies.txt),
# pass and fail in scope.
for extra in "$REPO"/ops/test/selfdeploy/compose-smoke.d/*.sh; do
  [ -e "$extra" ] || continue
  # shellcheck source=/dev/null
  . "$extra"
done

docker compose --profile bootstrap run --rm bootstrap 2>&1 \
  | grep -q 'skipped: instance already initialized' || fail "bootstrap NOT idempotent"
pass "bootstrap re-run is an idempotent no-op (can't mint a second admin)"

echo "ALL SELF-DEPLOY CHECKS PASSED ✓ (EMAIL_ENABLED=$EMAIL_ENABLED)"
SMOKE_COMPLETED=1
