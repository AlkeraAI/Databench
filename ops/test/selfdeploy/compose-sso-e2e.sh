#!/usr/bin/env bash
# Live END-TO-END test of single sign-on on the reference self-hosted stack,
# against a REAL external identity provider (Keycloak), not a mock.
#
# It brings the prod Compose stack up exactly as an operator would, adds a
# Keycloak IdP, and then drives, over real HTTP: OIDC and SAML sign-in with JIT
# provisioning into the org, the IdpScope gate refusing an out-of-domain
# identity, group mapping, SCIM provisioning, SSO enforcement, deprovisioning
# and the login hardening.
#
# Idempotent: always tears down its own project + volumes on exit. Runs on any
# Docker host (a laptop, a CI runner). Requires the prod images to exist
# (make docker-images IMAGE_VERSION=$APP_VERSION).
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
APP_VERSION="${APP_VERSION:-0.1.0-selftest}"
# Authoritative for compose interpolation, a shell-exported APP_VERSION would
# otherwise shadow the .env value and pull a non-existent image tag.
export APP_VERSION
BASE_URL="${PUBLIC_BASE_URL:-http://localhost}"
KC_URL="${KC_URL:-http://localhost:8088}"
ADMIN_EMAIL="${ADMIN_BOOTSTRAP_EMAIL:-admin@acme.example}"
ADMIN_ORG="${ADMIN_BOOTSTRAP_ORG_NAME:-Acme}"
PROJECT="alkera-sso-e2e"
DRIVER="$REPO/ops/test/selfdeploy/sso_e2e_driver.py"

# Pinned in the realm import (ops/test/selfdeploy/keycloak/realm-acme.json).
OIDC_ISSUER="http://keycloak:8080/realms/acme"
OIDC_CLIENT_ID="alkera-portal"
OIDC_CLIENT_SECRET="alkera-portal-secret"
ALLOWED_DOMAINS="acme.example"
ALICE_EMAIL="alice@acme.example"; ALICE_PASSWORD="Wonderland123!"
MALLORY_EMAIL="mallory@evil.example"; MALLORY_PASSWORD="Outsider123!"

WORK="$(mktemp -d)"

# Every secret is generated into a shell VARIABLE first and the .env is written from
# those variables, so the script and the file it hands compose always hold the same
# value and a caller may supply its own. A `NAME=$(openssl ...)` line inside the
# heredoc is a file line and nothing else -- the script would have no `$NAME`.
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 16)}"
TEMPORAL_DB_PASSWORD="${TEMPORAL_DB_PASSWORD:-$(openssl rand -hex 16)}"
AUTH_JWT_SECRET="${AUTH_JWT_SECRET:-$(openssl rand -hex 32)}"
TOKEN_HASH_PEPPER="${TOKEN_HASH_PEPPER:-$(openssl rand -hex 32)}"

dc() {
  docker compose -p "$PROJECT" \
    --project-directory "$REPO/deploy/docker" \
    --env-file "$WORK/.env" \
    -f "$REPO/deploy/docker/compose.prod.example.yml" \
    -f "$REPO/deploy/docker/compose.sso-e2e.yml" "$@"
}
# The EXIT trap below is the only way out, so it -- not its own last command -- has
# to decide what the caller reads back. It re-raises the status it was entered with,
# and falls back to the stamp this run sets just before its last line, because bash
# 3.2 (the /bin/bash macOS still ships) enters the trap with $? already 0 when `set
# -u` aborts on an unbound variable: on that shell a run that asserted nothing would
# otherwise report success. KEEP=1 leaves the stack up but still reports the run.
SMOKE_COMPLETED=0
cleanup() {
  local status=$?
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: leaving the stack up (project '$PROJECT', env $WORK/.env). 'docker compose -p $PROJECT down -v' to clean up." >&2
  else
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK" || true
  fi
  if [ "$status" -eq 0 ] && [ "$SMOKE_COMPLETED" -ne 1 ]; then status=1; fi
  exit "$status"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; dc logs --tail=60 backend gateway keycloak 2>&1 | tail -120 >&2 || true; exit 1; }
pass() { echo "PASS: $*"; }

cat > "$WORK/.env" <<EOF
APP_VERSION=$APP_VERSION
DB_PASSWORD=$DB_PASSWORD
TEMPORAL_DB_PASSWORD=$TEMPORAL_DB_PASSWORD
AUTH_JWT_SECRET=$AUTH_JWT_SECRET
TOKEN_HASH_PEPPER=$TOKEN_HASH_PEPPER
PUBLIC_BASE_URL=$BASE_URL
EMAIL_ENABLED=false
TURNSTILE_REQUIRED=false
GATEWAY_ASSUME_BEDROCK_IAM=true
BRAND_PRODUCT_NAME=Acme Data
BRAND_SUPPORT_EMAIL=help@acme.example
ADMIN_BOOTSTRAP_EMAIL=$ADMIN_EMAIL
ADMIN_BOOTSTRAP_ORG_NAME=$ADMIN_ORG
SMTP_HOST=
SMTP_USERNAME=
SMTP_PASSWORD=
EOF

dc config -q || fail "compose config invalid"
pass "compose config valid (prod stack + Keycloak overlay)"

for img in backend gateway web worker; do
  docker image inspect "alkera/$img:$APP_VERSION" >/dev/null 2>&1 \
    || fail "image alkera/$img:$APP_VERSION not found; run 'make docker-images IMAGE_VERSION=$APP_VERSION'"
done
pass "prod images present (alkera/{backend,gateway,web,worker}:$APP_VERSION)"

dc up -d
for i in $(seq 1 40); do
  sleep 6
  dc ps --format '{{.Service}}={{.Health}}' | grep -q 'backend=healthy' && break
  [ "$i" = 40 ] && fail "backend never became healthy"
done
pass "stack up, backend healthy"

# Keycloak readiness: poll its discovery doc (published on the host for this poll only).
for i in $(seq 1 40); do
  curl -fsS "$KC_URL/realms/acme/.well-known/openid-configuration" >/dev/null 2>&1 && break
  sleep 3
  [ "$i" = 40 ] && fail "Keycloak realm never came up"
done
pass "Keycloak IdP up (realm 'acme' discovery reachable)"

cfg=$(curl -fsS "$BASE_URL/api/v1/config") || fail "/api/v1/config unreachable"
echo "$cfg" | grep -q '"telemetry_enabled":false' || fail "self-host telemetry must be off: $cfg"
pass "self-host config: telemetry off, brand applied"

OUT=$(dc --profile bootstrap run --rm bootstrap 2>&1)
echo "$OUT" | grep -q "created: org='$ADMIN_ORG'" || fail "bootstrap did not create admin: $OUT"
ADMIN_PW=$(echo "$OUT" | grep -A1 'GENERATED ADMIN PASSWORD' | tail -1 | tr -d '[:space:]')
[ -n "$ADMIN_PW" ] || fail "bootstrap printed no generated password"
pass "first-admin bootstrap created $ADMIN_EMAIL"

run_driver() {  # $1 = PHASE
  dc run --rm --no-deps -T \
    --entrypoint python \
    -v "$DRIVER:/driver.py:ro" \
    -e PHASE="$1" \
    -e BACKEND="http://backend:8000" \
    -e GATEWAY="http://gateway:8081" \
    -e ADMIN_EMAIL="$ADMIN_EMAIL" -e ADMIN_PASSWORD="$ADMIN_PW" \
    -e OIDC_ISSUER="$OIDC_ISSUER" -e OIDC_CLIENT_ID="$OIDC_CLIENT_ID" \
    -e OIDC_CLIENT_SECRET="$OIDC_CLIENT_SECRET" -e ALLOWED_DOMAINS="$ALLOWED_DOMAINS" \
    -e ALICE_EMAIL="$ALICE_EMAIL" -e ALICE_PASSWORD="$ALICE_PASSWORD" \
    -e MALLORY_EMAIL="$MALLORY_EMAIL" -e MALLORY_PASSWORD="$MALLORY_PASSWORD" \
    -e LOCAL_EMAIL="$LOCAL_EMAIL" -e LOCAL_PASSWORD="$LOCAL_PASSWORD" \
    -e HARDEN_EMAIL="$HARDEN_EMAIL" -e HARDEN_PASSWORD="$HARDEN_PASSWORD" \
    backend /driver.py
}

LOCAL_EMAIL="local@acme.example"; LOCAL_PASSWORD="LocalPass123!"
HARDEN_EMAIL="harden@acme.example"; HARDEN_PASSWORD="HardenPass123!"

echo "--- OIDC SSO end-to-end ---"
out=$(run_driver sso 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'OIDC-E2E-OK' || fail "OIDC SSO end-to-end failed (see above)"
pass "OIDC SSO end-to-end PASSED (real Keycloak, JIT provisioning, IdpScope gate)"

echo "--- SAML SSO end-to-end ---"
out=$(run_driver saml 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'SAML-E2E-OK' || fail "SAML SSO end-to-end failed (see above)"
pass "SAML SSO end-to-end PASSED (real Keycloak, signed assertion, POST binding)"

echo "--- IdP group → role mapping end-to-end ---"
out=$(run_driver groups 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'GROUPS-E2E-OK' || fail "group→role mapping end-to-end failed (see above)"
pass "IdP group→role mapping PASSED (admin-group user → org admin, others → member)"

echo "--- SCIM 2.0 provisioning end-to-end ---"
out=$(run_driver scim 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'SCIM-E2E-OK' || fail "SCIM 2.0 end-to-end failed (see above)"
pass "SCIM 2.0 provisioning PASSED (mint token, create/filter/dedup, PATCH deprovision)"

# Server-side SSO enforcement: create a NON-exempt local (password) member, SSO
# users have no password, then prove enforced SSO rejects their password login.
echo "--- Server-side SSO enforcement end-to-end ---"
dc exec -T backend python -c "
import asyncio
from datetime import UTC, datetime
from sqlalchemy import select
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team
from backend.services import user_service
async def main():
    async with AsyncSessionLocal() as db:
        org = (await db.execute(select(Team).where(Team.is_root.is_(True)))).scalars().first()
        u = await user_service.create_user(db, org_team_id=org.id, email='$LOCAL_EMAIL',
            first_name='Local', last_name='User', password='$LOCAL_PASSWORD')
        u.email_verified_at = datetime.now(UTC)
        await db.commit()
        print('member', u.email)
asyncio.run(main())
" 2>&1 | grep -q "member $LOCAL_EMAIL" || fail "could not create the local (password) member"
out=$(run_driver enforce 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'ENFORCE-E2E-OK' || fail "SSO enforcement end-to-end failed (see above)"
pass "server-side SSO enforcement PASSED (non-exempt blocked, break-glass admin allowed)"

echo "--- User deprovisioning (offboarding) end-to-end ---"
out=$(run_driver deprovision 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'DEPROVISION-E2E-OK' || fail "deprovisioning end-to-end failed (see above)"
pass "user deprovisioning PASSED (deactivated SSO user refused, reactivation restores)"

# Login hardening: a local (password) user for the MFA + lockout end-to-end.
echo "--- Login hardening (MFA + brute-force lockout) end-to-end ---"
dc exec -T backend python -c "
import asyncio
from datetime import UTC, datetime
from sqlalchemy import select
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team
from backend.services import user_service
async def main():
    async with AsyncSessionLocal() as db:
        org = (await db.execute(select(Team).where(Team.is_root.is_(True)))).scalars().first()
        u = await user_service.create_user(db, org_team_id=org.id, email='$HARDEN_EMAIL',
            first_name='Harden', last_name='Test', password='$HARDEN_PASSWORD')
        u.email_verified_at = datetime.now(UTC)
        await db.commit()
        print('member', u.email)
asyncio.run(main())
" 2>&1 | grep -q "member $HARDEN_EMAIL" || fail "could not create the hardening test member"
out=$(run_driver hardening 2>&1) || true
echo "$out" | sed 's/^/  /'
echo "$out" | grep -q 'HARDENING-E2E-OK' || fail "login hardening end-to-end failed (see above)"
pass "login hardening PASSED (TOTP MFA challenge + brute-force lockout)"

# A distribution adds phases of its own as scripts in compose-sso-e2e.d/. Each
# one is sourced here with dc, run_driver, DRIVER, the admin's credentials, pass
# and fail in scope.
for extra in "$REPO"/ops/test/selfdeploy/compose-sso-e2e.d/*.sh; do
  [ -e "$extra" ] || continue
  # shellcheck source=/dev/null
  . "$extra"
done

echo "ALL SSO E2E CHECKS PASSED ✓"
SMOKE_COMPLETED=1
