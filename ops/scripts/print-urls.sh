#!/usr/bin/env bash
# Print this worktree's dev URLs. Ports are not fixed (each git
# worktree gets its own), so this is the source of truth for "what do I open".
# Used by `make urls`, `make infra-up`, and the dev-all startup banner.

set -euo pipefail

cd "$(dirname "$0")/../.."
# PRINT_URLS_ROOT is a test-only seam: it points the two env files this banner
# reads at a throwaway tree, so the override behaviour can be exercised without
# staging a `.env.local` in a real checkout (which holds a developer's secrets
# and must never be moved aside by a test). Unset in production, where it is the
# repo root.
ROOT="${PRINT_URLS_ROOT:-$(pwd)}"

WORKSPACE_ENV_ROOT="$ROOT" bash ops/scripts/workspace-env.sh ensure >/dev/null 2>&1 || true
if [ -f "$ROOT/.env.workspace" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env.workspace"
  set +a
fi

# .env.local is loaded LAST by every other reader (the Makefile, pydantic-settings, Vite),
# so a port a developer pins there, a store this worktree shares with another stack's
# containers, say, is the one the apps actually use. The banner honours the same
# override, or it prints a URL nothing answers on. Only bare numbers are read, one key
# at a time; the file is never sourced (it holds secrets and values with spaces).
if [ -f "$ROOT/.env.local" ]; then
  for key in API_PORT GATEWAY_PORT WEB_PORT POSTGRES_PORT MAILPIT_UI_PORT TEMPORAL_PORT \
    TEMPORAL_UI_PORT ALKERA_WORKER_HEALTH_PORT SEAWEEDFS_S3_PORT SEAWEEDFS_UI_PORT; do
    value="$(sed -n "s/^${key}=\([0-9][0-9]*\)[[:space:]]*\$/\1/p" "$ROOT/.env.local" | tail -n1)"
    [ -n "$value" ] && export "${key}=${value}"
  done
  # The two Files values that are names rather than ports. A developer who points
  # FILES_* at a real bucket in .env.local must see THAT in the banner, or it
  # names a store nothing is writing to. Read one key at a time, bare values only
  # (no spaces, no quotes), the file is never sourced, and no credential is read.
  for key in FILES_STORE_BUCKET FILES_CONTENT_BASE_URL; do
    value="$(sed -n "s/^${key}=\([^[:space:]\"']*\)[[:space:]]*\$/\1/p" "$ROOT/.env.local" | tail -n1)"
    [ -n "$value" ] && export "${key}=${value}"
  done
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
proj="${COMPOSE_PROJECT_NAME:-alkera}"

cyan=$'\033[1;36m'; green=$'\033[1;32m'; bold=$'\033[1m'; reset=$'\033[0m'

printf '\n'
printf '%s┌─ Databench dev, workspace %s%s%s  (OrbStack group: %s)%s\n' "$cyan" "$bold" "$branch" "$reset$cyan" "$proj" "$reset"
printf '%s│%s  %s%s▶ Web (open this):%s  http://localhost:%s\n' "$cyan" "$reset" "$green" "$bold" "$reset" "${WEB_PORT:-5173}"
printf '%s│%s    API:              http://localhost:%s\n' "$cyan" "$reset" "${API_PORT:-8000}"
printf '%s│%s    Gateway:          http://localhost:%s\n' "$cyan" "$reset" "${GATEWAY_PORT:-8081}"
printf '%s│%s    Mailpit UI:       http://localhost:%s\n' "$cyan" "$reset" "${MAILPIT_UI_PORT:-8025}"
printf '%s│%s    Temporal UI:      http://localhost:%s\n' "$cyan" "$reset" "${TEMPORAL_UI_PORT:-8233}"
printf '%s│%s    S3 (SeaweedFS):   http://localhost:%s  ·  Files UI: http://localhost:%s\n' \
  "$cyan" "$reset" "${SEAWEEDFS_S3_PORT:-8333}" "${SEAWEEDFS_UI_PORT:-8888}"
printf '%s│%s    Files bucket:     %s    content origin: %s\n' "$cyan" "$reset" \
  "${FILES_STORE_BUCKET:-alkera-files}" "${FILES_CONTENT_BASE_URL:-http://files.localhost:${API_PORT:-8000}}"
printf '%s│%s    Postgres:         localhost:%s    Temporal: localhost:%s\n' "$cyan" "$reset" "${POSTGRES_PORT:-5432}" "${TEMPORAL_PORT:-7233}"
printf '%s│%s    Worker health:    http://localhost:%s/health/live\n' "$cyan" "$reset" "${ALKERA_WORKER_HEALTH_PORT:-9000}"
printf '%s└%s\n\n' "$cyan" "$reset"
