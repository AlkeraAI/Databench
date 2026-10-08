#!/usr/bin/env bash
# Self-deploy BACKUP -> DESTROY -> RESTORE round-trip for the reference Compose stack.
#
# Proves the backup and restore procedure actually RECOVERS (not just that a dump is produced):
# bring the stack up, bootstrap an admin, dump the DB, WIPE the schema, restore the dump,
# bring the app back, and confirm the admin can still log in + row counts match. Runs on
# any Docker host (a laptop, a CI runner). Idempotent: cleans up its project +
# volumes on exit.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO/deploy/docker/compose.prod.example.yml}"
APP_VERSION="${APP_VERSION:-0.1.0-selftest}"
ADMIN_EMAIL="${ADMIN_BOOTSTRAP_EMAIL:-admin@acme.example}"
ADMIN_ORG="${ADMIN_BOOTSTRAP_ORG_NAME:-Acme}"
BASE_URL="${PUBLIC_BASE_URL:-http://localhost}"
PGU=alkera
PGDB=alkera

WORK="$(mktemp -d)"

# The EXIT trap below is the only way out, so it -- not its own last command -- has
# to decide what the caller reads back. It re-raises the status it was entered with,
# and falls back to the stamp this run sets just before its last line, because bash
# 3.2 (the /bin/bash macOS still ships) enters the trap with $? already 0 when `set
# -u` aborts on an unbound variable: on that shell a round-trip that restored nothing
# would otherwise report success. `|| true` keeps a best-effort teardown from aborting
# the trap under `set -e` before it reaches that exit.
SMOKE_COMPLETED=0
cleanup() {
  local status=$?
  (cd "$WORK" 2>/dev/null && docker compose down -v --remove-orphans >/dev/null 2>&1 || true)
  rm -rf "$WORK" || true
  if [ "$status" -eq 0 ] && [ "$SMOKE_COMPLETED" -ne 1 ]; then status=1; fi
  exit "$status"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; (cd "$WORK" && docker compose logs --tail=50 backend 2>&1 | tail -60) >&2 || true; exit 1; }
pass() { echo "PASS: $*"; }
psqlc() { docker compose exec -T postgres psql -qtAX -U "$PGU" -d "$PGDB" -c "$1"; }
wait_healthy() {
  for _ in $(seq 1 30); do
    sleep 6
    docker compose ps --format '{{.Service}}={{.Health}}' | grep -q 'backend=healthy' && return 0
  done
  fail "backend never became healthy ($1)"
}
login() {
  curl -sS -o /tmp/br_login.json -w '%{http_code}' -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$PW\"}"
}

cp "$COMPOSE_FILE" "$WORK/compose.yml"
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
PUBLIC_BASE_URL=$BASE_URL
EMAIL_ENABLED=false
TURNSTILE_REQUIRED=false
GATEWAY_ASSUME_BEDROCK_IAM=true
ADMIN_BOOTSTRAP_EMAIL=$ADMIN_EMAIL
ADMIN_BOOTSTRAP_ORG_NAME=$ADMIN_ORG
EOF

docker compose up -d
wait_healthy "initial boot"
pass "stack up, backend healthy"

OUT=$(docker compose --profile bootstrap run --rm bootstrap 2>&1)
PW=$(echo "$OUT" | grep -A1 'GENERATED ADMIN PASSWORD' | tail -1 | tr -d '[:space:]')
[ -n "$PW" ] || fail "bootstrap printed no generated password"
[ "$(login)" = 200 ] || fail "pre-backup login failed: $(cat /tmp/br_login.json)"
USERS_BEFORE=$(psqlc "select count(*) from users;")
[ "$USERS_BEFORE" -ge 1 ] || fail "expected >=1 user before backup, got '$USERS_BEFORE'"
pass "admin bootstrapped; login 200; users=$USERS_BEFORE"

# 1. BACKUP (plain SQL, restorable with psql after a wipe).
docker compose exec -T postgres pg_dump --no-owner -U "$PGU" "$PGDB" > "$WORK/db.sql"
[ -s "$WORK/db.sql" ] || fail "pg_dump produced an empty file"
pass "pg_dump wrote $(wc -c < "$WORK/db.sql") bytes"

# 2. DESTROY: stop the app, then drop everything. Temporal holds connections to
# the same Postgres instance; its own two databases are untouched by the `alkera`
# dump/wipe and are disposable (schedules re-register at the next worker boot).
docker compose stop backend worker temporal >/dev/null
psqlc "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
TABLES=$(psqlc "select count(*) from information_schema.tables where table_schema='public';")
[ "$TABLES" = "0" ] || fail "schema not wiped (tables=$TABLES)"
pass "data destroyed (public schema dropped + recreated empty)"

# 3. RESTORE from the dump.
docker compose exec -T postgres psql -qX -U "$PGU" -d "$PGDB" < "$WORK/db.sql" >/dev/null 2>&1
USERS_AFTER=$(psqlc "select count(*) from users;")
[ "$USERS_AFTER" = "$USERS_BEFORE" ] || fail "user count $USERS_AFTER != $USERS_BEFORE after restore"
pass "restored from dump; users=$USERS_AFTER (matches)"

# 4. App back up + schema at head + the admin can still log in (= data recovered).
docker compose start backend temporal worker >/dev/null
wait_healthy "post-restore"
curl -fsS "$BASE_URL/health/ready" | grep -q '"db":"ok"' || fail "/health/ready db not ok after restore"
[ "$(login)" = 200 ] || fail "post-restore login failed, data not recovered: $(cat /tmp/br_login.json)"
pass "post-restore: /health/ready db ok + admin login 200"

echo "BACKUP -> DESTROY -> RESTORE ROUND-TRIP PASSED ✓"
SMOKE_COMPLETED=1
