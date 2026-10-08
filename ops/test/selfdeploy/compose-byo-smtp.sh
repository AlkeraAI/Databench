#!/usr/bin/env bash
# Self-deploy smoke for the BYO-SMTP (email-ON) shape: bring the stack up against a
# real SMTP relay (Mailpit) with NO auth, exercising the common enterprise case of an
# internal, IP-allowlisted, anonymous relay, bootstrap an admin, trigger a
# password-reset, and ASSERT the branded email actually lands in the relay. Tears down.
# Runs on any Docker host.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
APP_VERSION="${APP_VERSION:-0.1.0-selftest}"
BRAND="${BRAND_PRODUCT_NAME:-Acme Data}"
ADMIN="admin@acme.example"
PW="Bootstrap123abc"
BASE="http://localhost"
MAILPIT="http://localhost:8025"

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
  (cd "$WORK" 2>/dev/null && docker compose -f compose.yml -f mailpit.yml down -v --remove-orphans >/dev/null 2>&1 || true)
  rm -rf "$WORK" || true
  if [ "$status" -eq 0 ] && [ "$SMOKE_COMPLETED" -ne 1 ]; then status=1; fi
  exit "$status"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; (cd "$WORK" && docker compose logs --tail=40 backend worker 2>&1 | tail -50) >&2 || true; exit 1; }
pass() { echo "PASS: $*"; }

cp "$REPO/deploy/docker/compose.prod.example.yml" "$WORK/compose.yml"
cat > "$WORK/mailpit.yml" <<'YAML'
services:
  mailpit:
    image: axllent/mailpit:latest
    restart: unless-stopped
    ports: ["8025:8025"]
YAML
cd "$WORK"

# Every secret is generated into a shell VARIABLE first and the .env is written from
# those variables, so the script and the file it hands compose always hold the same
# value and a caller may supply its own. A `NAME=$(openssl ...)` line inside the
# heredoc is a file line and nothing else -- the script would have no `$NAME`.
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
PUBLIC_BASE_URL=$BASE
EMAIL_ENABLED=true
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_USE_TLS=false
TURNSTILE_REQUIRED=false
GATEWAY_ASSUME_BEDROCK_IAM=true
BRAND_PRODUCT_NAME=$BRAND
ADMIN_BOOTSTRAP_EMAIL=$ADMIN
ADMIN_BOOTSTRAP_ORG_NAME=Acme
ADMIN_BOOTSTRAP_PASSWORD=$PW
EOF

# email ON + no SMTP_USERNAME/PASSWORD = anonymous relay; the prod validator must accept it.
docker compose -f compose.yml -f mailpit.yml config -q || fail "compose config invalid"
pass "compose valid: EMAIL_ENABLED=true, anonymous relay (no SMTP auth)"

docker compose -f compose.yml -f mailpit.yml up -d
for i in $(seq 1 30); do sleep 6; docker compose ps --format '{{.Service}}={{.Health}}' | grep -q 'backend=healthy' && break; [ "$i" = 30 ] && fail "backend never healthy"; done
pass "stack up with email ON + anonymous relay"

docker compose --profile bootstrap run --rm bootstrap >/dev/null 2>&1 || fail "bootstrap failed"
pass "admin bootstrapped"

# Trigger a password reset → backend sends the branded email through Mailpit.
curl -fsS -X POST "$BASE/api/v1/auth/password-reset/request" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN\"}" >/dev/null || fail "password-reset request failed"
pass "password-reset requested"

# Poll Mailpit for the delivered message.
for i in $(seq 1 15); do
  msg=$(curl -fsS "$MAILPIT/api/v1/messages" 2>/dev/null | jq -r --arg to "$ADMIN" '.messages[]? | select(.To[]?.Address==$to) | .Subject' | head -1)
  [ -n "$msg" ] && break; sleep 2
  [ "$i" = 15 ] && fail "no email delivered to $ADMIN in Mailpit (anonymous relay send failed)"
done
pass "email delivered to $ADMIN via the anonymous relay"

echo "$msg" | grep -qF "$BRAND" || fail "email subject not white-labeled: '$msg'"
pass "delivered subject is white-labeled: '$msg'"

echo "ALL BYO-SMTP CHECKS PASSED ✓"
SMOKE_COMPLETED=1
