#!/usr/bin/env bash
# Per-workspace local-dev environment.
#
# When every git branch has its own worktree, one global dev stack (Docker
# project, ports, DB/Temporal/Mailpit) makes two worktrees running
# `make dev-all` collide on ports and share one Postgres volume, so one
# branch's migrations break the other's database. This script gives each worktree an
# isolated stack by deriving a slug from the branch, allocating a free block of
# host ports, and materializing them into a gitignored `.env.workspace` at the
# repo root. Every downstream loader (Makefile, dev scripts, pydantic settings,
# vite) reads that file, so the right URLs/ports flow everywhere automatically.
#
# `main` is special: it keeps today's classic ports (8000/5173/5432/…) and the
# plain `alkera` project name, so the primary checkout is unchanged.
#
# Subcommands:
#   project-name   print the compose project name (alkera | alkera-<slug>)
#   slug           print the sanitized branch slug ("" on main)
#   generate       (re)write .env.workspace, ports stay stable (reused), only the
#                  derived keys refresh between runs. The Makefile rule lists this
#                  script as the file's prerequisite, so an upgraded generator
#                  rewrites a stale .env.workspace on the next `make`.
#   ensure         alias for generate
#   path           print the absolute path to .env.workspace
#
# `.env.workspace` writes ONLY infra keys: ports, the URLs derived from them, and
# the committed DEV-ONLY credentials of the local containers those URLs point at
# (the SeaweedFS pair in deploy/docker/seaweedfs-s3.json). Never a real secret. It
# is loaded after `.env` (overriding the classic defaults) but BEFORE `.env.local`,
# so the developer's real secrets in `.env.local` always win.

set -euo pipefail

cd "$(dirname "$0")/../.."
# ROOT is the worktree root. WORKSPACE_ENV_ROOT overrides it so a test can point the
# whole script (including the sibling scan, which keys off `dirname "$ROOT"`) at a
# throwaway tmp tree without touching the real fleet.
ROOT="${WORKSPACE_ENV_ROOT:-$(pwd)}"
ENV_FILE="$ROOT/.env.workspace"
SIBLING_BLOCKS=""

# Default ports for `main`, must match deploy/docker/compose.local.yml fallbacks and
# the committed .env so the primary checkout behaves exactly as before.
readonly MAIN_API=8000
readonly MAIN_GATEWAY=8081
readonly MAIN_WEB=5173
readonly MAIN_POSTGRES=5432
readonly MAIN_SMTP=1025
readonly MAIN_MAILPIT=8025
readonly MAIN_TEMPORAL=7233
readonly MAIN_TEMPORAL_UI=8233
readonly MAIN_WORKER_HEALTH=9000
# Dedicated throwaway DBs for warehouse-connector tests, SEPARATE from the app DB
# (MAIN_POSTGRES) so a live test's CREATE/DROP SCHEMA can never touch dev data.
readonly MAIN_POSTGRES_TEST=5433
readonly MAIN_MYSQL_TEST=3307
readonly MAIN_CLICKHOUSE_TEST=8124
readonly MAIN_KAFKA_TEST=9094
readonly MAIN_SCHEMA_REGISTRY_TEST=8085
readonly MAIN_MONGODB_TEST=27018
readonly MAIN_ELASTICSEARCH_TEST=9201
readonly MAIN_DRUID_TEST=8888
# The local S3-compatible object store for Files (SeaweedFS): the S3 API and
# the filer web UI, which is the browsable explorer over every bucket. On `main` the
# UI keeps SeaweedFS's own default 8888, which is also the Druid-test default above,
# Druid is a profile-gated service that is not in compose.local.yml, so the two are
# never published at once; every other worktree derives both from its own block.
readonly MAIN_SEAWEEDFS_S3=8333
readonly MAIN_SEAWEEDFS_UI=8888
# The SeaweedFS master's own HTTP API. Published because removing a bucket over S3
# leaves the collection behind it (and its volume files) standing: only the master
# drops a collection, and the test suite's own bucket is a collection per run per
# worker. Unpublished, every run leaked volume files onto the dev store until it
# filled the disk.
readonly MAIN_SEAWEEDFS_MASTER=9333
# The bucket Files writes objects into. `main` keeps the plain name the
# committed .env and the docs have always used; every other worktree derives its
# own (see files_bucket) so two stacks that ever point at one store cannot GC
# each other's objects.
readonly MAIN_FILES_BUCKET=alkera-files

# Non-main allocation lives in an obviously-ephemeral high range, far from the
# normal dev ports, in non-overlapping blocks of 20 (19 used: the stack at
# +0..+6, +4 is the slot the retired cache held, now the SeaweedFS master's
# HTTP API, the one surface that drops a collection, the test-Postgres at +7,
# test-MySQL at +8,
# test-ClickHouse at +9, the Temporal server at +10, its UI at +11, the
# worker's health port at +12, the five connector-test stores at +13..+17, and
# the SeaweedFS S3 API + filer UI at +18..+19, which fills the block). The
# WORKSPACE_ENV_PORT_* overrides exist only so a test can shrink the range to make
# exhaustion (and the LRU-reclaim fallback) reachable; production uses the defaults.
readonly PORT_BASE_MIN="${WORKSPACE_ENV_PORT_BASE_MIN:-20000}"
readonly PORT_BLOCK_SLOTS="${WORKSPACE_ENV_PORT_BLOCK_SLOTS:-950}"
readonly PORT_BLOCK_STRIDE="${WORKSPACE_ENV_PORT_BLOCK_STRIDE:-20}"

# The branch this worktree is on. WORKSPACE_ENV_BRANCH is a test-only seam: the
# script always runs from the repo root, so a test cannot vary the branch any
# other way, and the slug is what the project name, the port block and the Files
# bucket are all derived from. Unset in production, where git answers.
raw_branch() {
  if [ -n "${WORKSPACE_ENV_BRANCH:-}" ]; then
    printf '%s' "$WORKSPACE_ENV_BRANCH"
    return 0
  fi
  git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown"
}

# Lowercase; collapse any run of non-[a-z0-9_-] into a single dash; trim dashes.
sanitize() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9_-]+/-/g; s/-{2,}/-/g; s/^-+//; s/-+$//'
}

slug() {
  local branch
  branch="$(raw_branch)"
  # CI is treated like `main`: classic ports + the plain `alkera` project, so the
  # pr-gate Postgres service on :5432 (and the code defaults) keep working. CI
  # never runs two stacks in parallel, so it needs no isolation.
  if [ "$branch" = "main" ] || [ -n "${CI:-}" ]; then
    printf ''
  else
    sanitize "$branch"
  fi
}

# The S3 bucket Files uses in this worktree, derived from the slug.
#
# Bucket names are DNS-shaped: lowercase, 3-63 characters, no underscores. Branch
# slugs are neither bounded in length nor free of underscores, so the readable
# part is translated and truncated, and a checksum of the WHOLE slug is
# appended, because truncation alone would collapse two long branches sharing a
# prefix onto one bucket, and a shared bucket means one worktree's GC deleting
# the other's objects.
files_bucket() {
  local s="$1" readable sum
  readable="$(printf '%s' "$s" | tr '_' '-' | cut -c1-24 | sed -E 's/-+$//')"
  sum="$(printf '%s' "$s" | cksum | cut -d' ' -f1)"
  if [ -n "$readable" ]; then
    printf '%s-%s-%s' "$MAIN_FILES_BUCKET" "$readable" "$sum"
  else
    printf '%s-%s' "$MAIN_FILES_BUCKET" "$sum"
  fi
}

project_name() {
  local s
  s="$(slug)"
  if [ -z "$s" ]; then
    printf 'alkera'
  else
    printf 'alkera-%s' "$s"
  fi
}

# 0 if nothing is listening on the given TCP port.
#
# WORKSPACE_ENV_BUSY_PORTS is a test-only seam. When it is set, the space-separated
# list it holds is the whole truth about what is bound, those ports are busy and
# every other port is free, and lsof is never consulted, so a test can drive the
# picker without binding a socket and gets the same answer on a quiet laptop as on
# a runner whose ephemeral range is full of other workers' sockets. (Consulting
# lsof as well made the answer depend on the host: a stray socket in the window
# moved the picker, and each probe was a process the loaded box had to schedule.)
# Unset in production, where lsof answers.
port_free() {
  if [ -n "${WORKSPACE_ENV_BUSY_PORTS+set}" ]; then
    case " $WORKSPACE_ENV_BUSY_PORTS " in
      *" $1 "*) return 1 ;;
    esac
    return 0
  fi
  ! lsof -ti "tcp:$1" >/dev/null 2>&1
}

# 0 if all 20 ports starting at $1 are free (the stack + the warehouse test stores +
# Temporal, its UI and the worker health port + the connector test stores + the
# SeaweedFS object store, which together fill the block's stride of 20).
block_free() {
  local base="$1" i
  for i in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19; do
    port_free "$((base + i))" || return 1
  done
  return 0
}

# Seconds-since-epoch mtime, in whichever stat dialect this machine speaks.
#
# `stat -c %Y` (GNU) and `stat -f %m` (BSD) ask the same question two ways, and the
# wrong one does not merely fail. GNU's `-f` is --file-system and takes no format, so
# it reads `%m` as a second FILE, and it still prints a whole filesystem report for
# the real file (an `ID:` line, block sizes, counts) to STDOUT before exiting
# non-zero. A plain `a || b` chain therefore captures that report AND the fallback's
# answer, and the caller's arithmetic then evaluates the word `ID` as a variable,
# which is fatal under `set -u`. So: accept a dialect's output only when it is the
# bare run of digits it promised.
file_mtime() {
  local f="$1" out
  out="$(stat -c %Y "$f" 2>/dev/null || true)"
  case "$out" in '' | *[!0-9]*) out="$(stat -f %m "$f" 2>/dev/null || true)" ;; esac
  case "$out" in '' | *[!0-9]*) out=0 ;; esac
  printf '%s' "$out"
}

# "<base> <mtime>" for every OTHER workspace's .env.workspace under the worktrees
# parent dir (self excluded). lsof only sees what is bound right now; a sibling whose
# stack is stopped still OWNS its block, so we treat these as reserved when picking,
# otherwise two idle worktrees get assigned the same ports and collide once both boot.
# mtime (bumped on each `ensure`, see generate) is the recency signal for LRU reclaim.
sibling_blocks() {
  local parent f base
  parent="$(dirname "$ROOT")"
  [ -d "$parent" ] || return 0
  for f in "$parent"/*/.env.workspace; do
    [ -f "$f" ] || continue
    [ "$f" = "$ENV_FILE" ] && continue
    base="$(sed -n 's/^API_PORT=\([0-9][0-9]*\)$/\1/p' "$f" | head -n1)"
    [ -n "$base" ] || continue
    printf '%s %s\n' "$base" "$(file_mtime "$f")"
  done
}

# 0 if the block [base..base+19] overlaps any sibling's reserved block. The overlap
# test (not just base equality) keeps it correct even against misaligned/classic ports.
block_reserved() {
  local base="$1" sbase
  while read -r sbase _; do
    # Only a plain number may reach the arithmetic below: bash re-evaluates a
    # non-numeric operand as a variable name, which `set -u` turns into a hard exit.
    case "$sbase" in '' | *[!0-9]*) continue ;; esac
    if [ "$base" -le "$((sbase + 19))" ] && [ "$sbase" -le "$((base + 19))" ]; then
      return 0
    fi
  done <<< "$SIBLING_BLOCKS"
  return 1
}

# Last resort when the whole range is reserved/taken: reuse the block of the
# least-recently-used sibling whose stack is currently DOWN (never steal a running
# one). Prints the base; returns 1 if every sibling block is also live.
lru_reclaim_base() {
  local best="" best_mtime="" sbase smtime
  while read -r sbase smtime; do
    case "$sbase" in '' | *[!0-9]*) continue ;; esac
    case "$smtime" in '' | *[!0-9]*) smtime=0 ;; esac
    block_free "$sbase" || continue
    if [ -z "$best_mtime" ] || [ "$smtime" -lt "$best_mtime" ]; then
      best="$sbase"; best_mtime="$smtime"
    fi
  done <<< "$SIBLING_BLOCKS"
  [ -n "$best" ] || return 1
  echo "workspace-env: port range exhausted; reclaiming least-recently-used block $best" >&2
  printf '%s' "$best"
}

# Deterministic starting base from the slug, then probe upward until a block is BOTH
# unreserved by a sibling AND lsof-free. Persisting the result (below) keeps ports
# stable across nukes; the hash start means a re-roll tends to reclaim the same block.
allocate_base() {
  local s="$1" n base tries=0 max
  SIBLING_BLOCKS="$(sibling_blocks)"
  n="$(printf '%s' "$s" | cksum | cut -d' ' -f1)"
  base=$(( PORT_BASE_MIN + (n % PORT_BLOCK_SLOTS) * PORT_BLOCK_STRIDE ))
  max=$(( PORT_BASE_MIN + (PORT_BLOCK_SLOTS - 1) * PORT_BLOCK_STRIDE ))
  while block_reserved "$base" || ! block_free "$base"; do
    base=$(( base + PORT_BLOCK_STRIDE ))
    [ "$base" -gt "$max" ] && base="$PORT_BASE_MIN"
    tries=$(( tries + 1 ))
    if [ "$tries" -ge "$PORT_BLOCK_SLOTS" ]; then
      base="$(lru_reclaim_base)" || {
        echo "workspace-env: no free port block and no reclaimable sibling block" >&2
        exit 1
      }
      break
    fi
  done
  printf '%s' "$base"
}

write_env_file() {
  local proj="$1" api="$2" gw="$3" web="$4" pg="$5" smtp="$6" mailpit="$7"
  local pg_test="$8" mysql_test="$9" ch_test="${10}"
  local temporal="${11}" temporal_ui="${12}" worker_health="${13}"
  local kafka_test="${14}" schema_registry_test="${15}"
  local mongodb_test="${16}" elasticsearch_test="${17}"
  local druid_test="${18}"
  local seaweedfs_s3="${19}" seaweedfs_ui="${20}" seaweedfs_master="${21}"
  local files_bucket="${22}"
  # BOTH frontend URLs derive from WEB_PORT, the backend's FRONTEND_BASE_URL (login /device page,
  # email links, SSO/OAuth + Stripe returns) AND the CLI/daemon's ALKERA_FRONTEND_URL (the webview
  # "open web app" button), so they can never split.
  # The same rule for the gateway: the CLI/daemon's ALKERA_GATEWAY_URL *and* the backend's
  # GATEWAY_BASE_URL (the setting behind the chat-model catalog hop and the deployment health
  # check) both derive from GATEWAY_PORT. Emitting only one left the backend on the `main`
  # default :8081, so /me/chat-models answered empty on every other worktree.
  # The whole Alkera Files block is derived here for the same reason Postgres is:
  # a developer should get a working object store from `make infra-up` without
  # hand-editing .env.local. The store is this worktree's SeaweedFS (endpoint from
  # SEAWEEDFS_S3_PORT, bucket from the slug), the credentials are the DEV-ONLY pair
  # committed in deploy/docker/seaweedfs-s3.json, and the content origin is
  # `files.localhost` on the API port, a different hostname to the SPA and the API
  # so a downloaded file can never run on the origin holding the session cookie.
  # Queued work runs inline because a developer running only `make dev-backend` has
  # no Temporal worker to hand an upload promote to; with `make dev-all` the worker
  # is up and the flag only moves WHICH process runs it, never runs it twice.
  cat > "$ENV_FILE" <<EOF
# AUTO-GENERATED per-workspace dev env, DO NOT COMMIT, DO NOT HAND-EDIT.
# Created by ops/scripts/workspace-env.sh, and rewritten automatically by \`make\` when
# that script changes. Ports are kept. Rewrite by hand: make workspace-env-refresh
# Loaded after .env and BEFORE .env.local, so your .env.local secrets still win.
COMPOSE_PROJECT_NAME=$proj
API_PORT=$api
GATEWAY_PORT=$gw
WEB_PORT=$web
POSTGRES_PORT=$pg
SMTP_PORT=$smtp
MAILPIT_UI_PORT=$mailpit
TEMPORAL_PORT=$temporal
TEMPORAL_UI_PORT=$temporal_ui
ALKERA_WORKER_HEALTH_PORT=$worker_health
POSTGRES_TEST_PORT=$pg_test
POSTGRES_TEST_HOST=localhost
POSTGRES_TEST_DB=alkera_test
POSTGRES_TEST_USER=alkera
POSTGRES_TEST_PASSWORD=alkera
MYSQL_TEST_PORT=$mysql_test
MYSQL_TEST_HOST=127.0.0.1
MYSQL_TEST_DB=alkera_test
MYSQL_TEST_USER=alkera
MYSQL_TEST_PASSWORD=alkera
CLICKHOUSE_TEST_PORT=$ch_test
CLICKHOUSE_TEST_HOST=127.0.0.1
CLICKHOUSE_TEST_DB=alkera_test
CLICKHOUSE_TEST_USER=alkera
CLICKHOUSE_TEST_PASSWORD=alkera
KAFKA_TEST_PORT=$kafka_test
KAFKA_TEST_BOOTSTRAP_SERVERS=localhost:$kafka_test
SCHEMA_REGISTRY_TEST_PORT=$schema_registry_test
SCHEMA_REGISTRY_TEST_URL=http://localhost:$schema_registry_test
MONGODB_TEST_PORT=$mongodb_test
MONGODB_TEST_URI=mongodb://alkera:alkera@localhost:$mongodb_test/?authSource=admin
ELASTICSEARCH_TEST_PORT=$elasticsearch_test
ELASTICSEARCH_TEST_URL=http://localhost:$elasticsearch_test
DRUID_TEST_PORT=$druid_test
DRUID_TEST_URL=http://localhost:$druid_test
SEAWEEDFS_S3_PORT=$seaweedfs_s3
SEAWEEDFS_UI_PORT=$seaweedfs_ui
SEAWEEDFS_MASTER_PORT=$seaweedfs_master
FILES_ENABLED=true
FILES_STORE_PROVIDER=s3_compatible
FILES_STORE_ENDPOINT=http://localhost:$seaweedfs_s3
FILES_STORE_ADMIN_ENDPOINT=http://localhost:$seaweedfs_master
FILES_STORE_BUCKET=$files_bucket
FILES_STORE_REGION=us-east-1
FILES_STORE_ACCESS_KEY=alkera
FILES_STORE_SECRET_KEY=alkera-dev-secret
FILES_INLINE_OPERATIONS=true
FILES_CONTENT_BASE_URL=http://files.localhost:$api
DATABASE_URL=postgresql+asyncpg://alkera:alkera@localhost:$pg/alkera
DATABASE_URL_SYNC=postgresql+psycopg://alkera:alkera@localhost:$pg/alkera
TEMPORAL_ADDRESS=localhost:$temporal
SMTP_HOST=localhost
API_CORS_ORIGINS=http://localhost:$web
FRONTEND_BASE_URL=http://localhost:$web
API_PUBLIC_BASE_URL=http://localhost:$api
ALKERA_API_URL=http://localhost:$api
ALKERA_FRONTEND_URL=http://localhost:$web
ALKERA_GATEWAY_URL=http://localhost:$gw
GATEWAY_BASE_URL=http://localhost:$gw
VITE_API_PROXY_TARGET=http://localhost:$api
ALKERA_NODE_API_URL=http://host.docker.internal:$api
ALKERA_NODE_GATEWAY_URL=http://host.docker.internal:$gw
EOF
}

generate() {
  # Ports stay stable for the life of the worktree. This REWRITES the file every call (reusing
  # the already-allocated base when it exists) rather than no-op'ing, so derived URLs refresh
  # WITHOUT re-rolling ports (re-rolling would move Postgres/API and tear down running services).
  # The rewrite also advances the mtime, which is the recency signal lru_reclaim_base ranks on.
  local s base
  s="$(slug)"
  if [ -z "$s" ]; then
    # main / CI: the fixed classic ports.
    write_env_file "alkera" \
      "$MAIN_API" "$MAIN_GATEWAY" "$MAIN_WEB" \
      "$MAIN_POSTGRES" "$MAIN_SMTP" "$MAIN_MAILPIT" \
      "$MAIN_POSTGRES_TEST" "$MAIN_MYSQL_TEST" "$MAIN_CLICKHOUSE_TEST" \
      "$MAIN_TEMPORAL" "$MAIN_TEMPORAL_UI" "$MAIN_WORKER_HEALTH" \
      "$MAIN_KAFKA_TEST" "$MAIN_SCHEMA_REGISTRY_TEST" \
      "$MAIN_MONGODB_TEST" "$MAIN_ELASTICSEARCH_TEST" "$MAIN_DRUID_TEST" \
      "$MAIN_SEAWEEDFS_S3" "$MAIN_SEAWEEDFS_UI" "$MAIN_SEAWEEDFS_MASTER" \
      "$MAIN_FILES_BUCKET"
    return 0
  fi
  # Reuse this worktree's existing base; allocate a fresh block only on first generation.
  if [ -f "$ENV_FILE" ]; then
    base="$(sed -n 's/^API_PORT=\([0-9][0-9]*\)$/\1/p' "$ENV_FILE" | head -n1)"
  fi
  [ -n "${base:-}" ] || base="$(allocate_base "$s")"
  write_env_file "alkera-$s" \
    "$((base + 0))" "$((base + 1))" "$((base + 2))" \
    "$((base + 3))" "$((base + 5))" "$((base + 6))" \
    "$((base + 7))" "$((base + 8))" "$((base + 9))" \
    "$((base + 10))" "$((base + 11))" "$((base + 12))" \
    "$((base + 13))" "$((base + 14))" "$((base + 15))" "$((base + 16))" \
    "$((base + 17))" "$((base + 18))" "$((base + 19))" "$((base + 4))" \
    "$(files_bucket "$s")"
}

case "${1:-ensure}" in
  project-name) project_name ;;
  slug)         slug ;;
  generate)     generate ;;
  ensure)       generate ;;
  path)         printf '%s' "$ENV_FILE" ;;
  *)
    echo "usage: workspace-env.sh {project-name|slug|generate|ensure|path}" >&2
    exit 2
    ;;
esac
