#!/usr/bin/env bash
# Self-test for ops/scripts/workspace-env.sh port allocation.
#
# NOT part of `make test` (that tier is Python/TS only), run manually:
#   bash ops/scripts/workspace-env.test.sh
#
# Exercises the picker via its test seams (WORKSPACE_ENV_ROOT,
# WORKSPACE_ENV_PORT_*, WORKSPACE_ENV_BUSY_PORTS, WORKSPACE_ENV_BRANCH) against
# throwaway tmp trees, so it never touches the real worktree fleet and needs no
# bound sockets.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUT="$SCRIPT_DIR/workspace-env.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=1; }

# Run the picker for a fresh workspace dir, with all collision sources controlled.
# Args: <ws_dir> [extra env assignments...]. Prints the assigned API_PORT.
run_generate() {
  local ws="$1"; shift
  # CI="" so the slug isn't forced to the empty/main case; a tiny 3-slot range
  # (41000/41020/41040) makes exhaustion reachable with a handful of siblings.
  # The window sits inside the kernel's ephemeral range, so every port is
  # declared free through the seam (a later assignment names the busy ones) and
  # lsof never decides a case here.
  env CI="" \
    WORKSPACE_ENV_ROOT="$ws" \
    WORKSPACE_ENV_PORT_BASE_MIN=41000 \
    WORKSPACE_ENV_PORT_BLOCK_SLOTS=3 \
    WORKSPACE_ENV_PORT_BLOCK_STRIDE=20 \
    WORKSPACE_ENV_BUSY_PORTS="" \
    "$@" \
    bash "$SUT" generate >/dev/null 2>&1
  sed -n 's/^API_PORT=\([0-9][0-9]*\)$/\1/p' "$ws/.env.workspace" | head -n1
}

reserve() { printf 'API_PORT=%s\n' "$2" > "$1/.env.workspace"; }

# Read one key out of a generated file. Args: <ws_dir> <KEY>.
val() { sed -n "s/^$2=\\(.*\\)\$/\\1/p" "$1/.env.workspace" | head -n1; }

# ---------------------------------------------------------------------------
echo "test 1: avoids ports reserved by sibling workspaces (even when idle)"
T1="$(mktemp -d)"; trap 'rm -rf "$T1"' EXIT
mkdir -p "$T1/wsA" "$T1/wsB" "$T1/wsC"
reserve "$T1/wsA" 41000   # block 41000-41012
reserve "$T1/wsB" 41020   # block 41020-41032
got="$(run_generate "$T1/wsC")"
if [ "$got" = "41040" ]; then pass "picked 41040 (avoided A=41000, B=41020)"
else bad "expected 41040, got '$got'"; fi

# ---------------------------------------------------------------------------
echo "test 2: on exhaustion, reclaims the least-recently-used (oldest mtime) block"
T2="$(mktemp -d)"
mkdir -p "$T2/wsA" "$T2/wsB" "$T2/wsC" "$T2/wsD"
reserve "$T2/wsA" 41000; touch -t 202001010000 "$T2/wsA/.env.workspace"  # oldest
reserve "$T2/wsB" 41020; touch -t 202301010000 "$T2/wsB/.env.workspace"
reserve "$T2/wsC" 41040; touch -t 202601010000 "$T2/wsC/.env.workspace"  # newest
got="$(run_generate "$T2/wsD")"
if [ "$got" = "41000" ]; then pass "reclaimed 41000 (oldest mtime)"
else bad "expected 41000, got '$got'"; fi
rm -rf "$T2"

# ---------------------------------------------------------------------------
echo "test 3: LRU never steals a block whose stack is currently up"
T3="$(mktemp -d)"
mkdir -p "$T3/wsA" "$T3/wsB" "$T3/wsC" "$T3/wsD"
reserve "$T3/wsA" 41000; touch -t 202001010000 "$T3/wsA/.env.workspace"  # oldest, but BUSY
reserve "$T3/wsB" 41020; touch -t 202301010000 "$T3/wsB/.env.workspace"  # next oldest, free
reserve "$T3/wsC" 41040; touch -t 202601010000 "$T3/wsC/.env.workspace"
got="$(run_generate "$T3/wsD" WORKSPACE_ENV_BUSY_PORTS="41000")"
if [ "$got" = "41020" ]; then pass "skipped busy 41000, reclaimed 41020 (next-oldest free)"
else bad "expected 41020, got '$got'"; fi
rm -rf "$T3"

# ---------------------------------------------------------------------------
echo "test 4: ensure bumps the mtime so recency reflects last use"
T4="$(mktemp -d)"
mkdir -p "$T4/ws"
reserve "$T4/ws" 41000; touch -t 202001010000 "$T4/ws/.env.workspace"
before="$(stat -f %m "$T4/ws/.env.workspace" 2>/dev/null || stat -c %Y "$T4/ws/.env.workspace")"
env CI="" WORKSPACE_ENV_ROOT="$T4/ws" bash "$SUT" ensure >/dev/null 2>&1
after="$(stat -f %m "$T4/ws/.env.workspace" 2>/dev/null || stat -c %Y "$T4/ws/.env.workspace")"
if [ "$after" -gt "$before" ]; then pass "mtime advanced on ensure ($before -> $after)"
else bad "mtime did not advance (before=$before after=$after)"; fi
rm -rf "$T4"

# ---------------------------------------------------------------------------
echo "test 5: writes a per-worktree test-Postgres at base+7 (+ its fixed config)"
T5="$(mktemp -d)"
mkdir -p "$T5/ws"
api="$(run_generate "$T5/ws")"
pgt="$(sed -n 's/^POSTGRES_TEST_PORT=\([0-9][0-9]*\)$/\1/p' "$T5/ws/.env.workspace" | head -n1)"
db="$(sed -n 's/^POSTGRES_TEST_DB=\(.*\)$/\1/p' "$T5/ws/.env.workspace" | head -n1)"
myt="$(sed -n 's/^MYSQL_TEST_PORT=\([0-9][0-9]*\)$/\1/p' "$T5/ws/.env.workspace" | head -n1)"
cht="$(sed -n 's/^CLICKHOUSE_TEST_PORT=\([0-9][0-9]*\)$/\1/p' "$T5/ws/.env.workspace" | head -n1)"
if [ "$pgt" = "$((api + 7))" ] && [ "$myt" = "$((api + 8))" ] && [ "$cht" = "$((api + 9))" ] \
  && [ "$db" = "alkera_test" ]; then
  pass "POSTGRES_TEST_PORT=$pgt (+7), MYSQL_TEST_PORT=$myt (+8), CLICKHOUSE_TEST_PORT=$cht (+9), DB=$db"
else
  bad "expected pg $((api + 7)) + mysql $((api + 8)) + clickhouse $((api + 9)) + DB alkera_test, got pg='$pgt' mysql='$myt' ch='$cht' db='$db'"
fi
rm -rf "$T5"

# ---------------------------------------------------------------------------
echo "test 6: writes the Temporal server, its UI and the worker health port at base+10..+12, and no broker keys"
T6="$(mktemp -d)"
mkdir -p "$T6/ws"
api="$(run_generate "$T6/ws")"
tp="$(sed -n 's/^TEMPORAL_PORT=\([0-9][0-9]*\)$/\1/p' "$T6/ws/.env.workspace" | head -n1)"
tui="$(sed -n 's/^TEMPORAL_UI_PORT=\([0-9][0-9]*\)$/\1/p' "$T6/ws/.env.workspace" | head -n1)"
wh="$(sed -n 's/^ALKERA_WORKER_HEALTH_PORT=\([0-9][0-9]*\)$/\1/p' "$T6/ws/.env.workspace" | head -n1)"
addr="$(sed -n 's/^TEMPORAL_ADDRESS=\(.*\)$/\1/p' "$T6/ws/.env.workspace" | head -n1)"
broker_keys="$(grep -c -E '^(REDIS_|CELERY_|SQS_)' "$T6/ws/.env.workspace" || true)"
if [ "$tp" = "$((api + 10))" ] && [ "$tui" = "$((api + 11))" ] && [ "$wh" = "$((api + 12))" ] \
  && [ "$addr" = "localhost:$((api + 10))" ] && [ "$broker_keys" = "0" ]; then
  pass "TEMPORAL_PORT=$tp (+10), TEMPORAL_UI_PORT=$tui (+11), ALKERA_WORKER_HEALTH_PORT=$wh (+12), TEMPORAL_ADDRESS=$addr, no broker keys"
else
  bad "expected temporal $((api + 10)) + ui $((api + 11)) + health $((api + 12)) + TEMPORAL_ADDRESS=localhost:$((api + 10)) + 0 broker keys, got temporal='$tp' ui='$tui' health='$wh' addr='$addr' broker_keys='$broker_keys'"
fi
rm -rf "$T6"

# ---------------------------------------------------------------------------
echo "test 7: main keeps the classic ports, including Temporal's"
T7="$(mktemp -d)"
mkdir -p "$T7/ws"
env CI="1" WORKSPACE_ENV_ROOT="$T7/ws" bash "$SUT" generate >/dev/null 2>&1
main_tp="$(sed -n 's/^TEMPORAL_PORT=\([0-9][0-9]*\)$/\1/p' "$T7/ws/.env.workspace" | head -n1)"
main_tui="$(sed -n 's/^TEMPORAL_UI_PORT=\([0-9][0-9]*\)$/\1/p' "$T7/ws/.env.workspace" | head -n1)"
main_wh="$(sed -n 's/^ALKERA_WORKER_HEALTH_PORT=\([0-9][0-9]*\)$/\1/p' "$T7/ws/.env.workspace" | head -n1)"
main_pg="$(sed -n 's/^POSTGRES_PORT=\([0-9][0-9]*\)$/\1/p' "$T7/ws/.env.workspace" | head -n1)"
if [ "$main_tp" = "7233" ] && [ "$main_tui" = "8233" ] && [ "$main_wh" = "9000" ] && [ "$main_pg" = "5432" ]; then
  pass "main/CI: TEMPORAL_PORT=7233 TEMPORAL_UI_PORT=8233 ALKERA_WORKER_HEALTH_PORT=9000 POSTGRES_PORT=5432"
else
  bad "expected the classic ports on main/CI, got temporal='$main_tp' ui='$main_tui' health='$main_wh' pg='$main_pg'"
fi
rm -rf "$T7"

# ---------------------------------------------------------------------------
echo "test 8: writes the SeaweedFS S3 + filer-UI ports at base+18..+19 and derives FILES_STORE_ENDPOINT"
T8="$(mktemp -d)"
mkdir -p "$T8/ws"
api="$(run_generate "$T8/ws")"
s3p="$(sed -n 's/^SEAWEEDFS_S3_PORT=\([0-9][0-9]*\)$/\1/p' "$T8/ws/.env.workspace" | head -n1)"
uip="$(sed -n 's/^SEAWEEDFS_UI_PORT=\([0-9][0-9]*\)$/\1/p' "$T8/ws/.env.workspace" | head -n1)"
endpoint="$(sed -n 's/^FILES_STORE_ENDPOINT=\(.*\)$/\1/p' "$T8/ws/.env.workspace" | head -n1)"
if [ "$s3p" = "$((api + 18))" ] && [ "$uip" = "$((api + 19))" ] \
  && [ "$endpoint" = "http://localhost:$((api + 18))" ]; then
  pass "SEAWEEDFS_S3_PORT=$s3p (+18), SEAWEEDFS_UI_PORT=$uip (+19), FILES_STORE_ENDPOINT=$endpoint"
else
  bad "expected s3 $((api + 18)) + ui $((api + 19)) + endpoint http://localhost:$((api + 18)), got s3='$s3p' ui='$uip' endpoint='$endpoint'"
fi
rm -rf "$T8"

# ---------------------------------------------------------------------------
echo "test 9: main keeps SeaweedFS's own defaults (8333 / 8888)"
T9="$(mktemp -d)"
mkdir -p "$T9/ws"
env CI="1" WORKSPACE_ENV_ROOT="$T9/ws" bash "$SUT" generate >/dev/null 2>&1
main_s3="$(sed -n 's/^SEAWEEDFS_S3_PORT=\([0-9][0-9]*\)$/\1/p' "$T9/ws/.env.workspace" | head -n1)"
main_ui="$(sed -n 's/^SEAWEEDFS_UI_PORT=\([0-9][0-9]*\)$/\1/p' "$T9/ws/.env.workspace" | head -n1)"
if [ "$main_s3" = "8333" ] && [ "$main_ui" = "8888" ]; then
  pass "main/CI: SEAWEEDFS_S3_PORT=8333 SEAWEEDFS_UI_PORT=8888"
else
  bad "expected main/CI 8333 + 8888, got s3='$main_s3' ui='$main_ui'"
fi
rm -rf "$T9"

# ---------------------------------------------------------------------------
echo "test 10: a busy SeaweedFS UI port (base+19) disqualifies the whole block"
T10="$(mktemp -d)"
mkdir -p "$T10/ws"
# 41019 is the filer-UI slot of the first block. A picker that only probes the
# first 18 slots would hand this block out anyway and the new stack's SeaweedFS
# would fail to bind, so the skip to 41020 is the proof the block is 20 wide.
got="$(run_generate "$T10/ws" WORKSPACE_ENV_BUSY_PORTS="41019")"
if [ "$got" = "41020" ]; then
  pass "picked 41020 (41000's block rejected because its base+19 is busy)"
else
  bad "expected 41020, got '$got'"
fi
rm -rf "$T10"

# ---------------------------------------------------------------------------
echo "test 11: a non-main worktree gets a complete, self-consistent Files block"
T11="$(mktemp -d)"
mkdir -p "$T11/ws"
api="$(run_generate "$T11/ws" WORKSPACE_ENV_BRANCH="feature/Pretty-Name")"
enabled="$(val "$T11/ws" FILES_ENABLED)"
provider="$(val "$T11/ws" FILES_STORE_PROVIDER)"
endpoint="$(val "$T11/ws" FILES_STORE_ENDPOINT)"
bucket="$(val "$T11/ws" FILES_STORE_BUCKET)"
content="$(val "$T11/ws" FILES_CONTENT_BASE_URL)"
inline="$(val "$T11/ws" FILES_INLINE_OPERATIONS)"
akey="$(val "$T11/ws" FILES_STORE_ACCESS_KEY)"
# The store endpoint must be THIS block's S3 port and the content origin THIS
# block's API port, a Files block wired to another worktree's ports is exactly
# the hand-editing this generation exists to remove. The bucket must be derived
# from the slug (never the bare shared name) and DNS-shaped, since SeaweedFS and
# every S3 endpoint refuse anything else.
if [ "$enabled" = "true" ] && [ "$provider" = "s3_compatible" ] \
  && [ "$endpoint" = "http://localhost:$((api + 18))" ] \
  && [ "$content" = "http://files.localhost:$api" ] \
  && [ "$bucket" = "alkera-files-feature-pretty-name-$(printf 'feature-pretty-name' | cksum | cut -d' ' -f1)" ] \
  && [ "$inline" = "true" ] && [ "$akey" = "alkera" ] \
  && printf '%s' "$bucket" | grep -qE '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'; then
  pass "FILES_STORE_ENDPOINT=$endpoint, FILES_CONTENT_BASE_URL=$content, bucket=$bucket"
else
  bad "unexpected Files block: enabled='$enabled' provider='$provider' endpoint='$endpoint' content='$content' bucket='$bucket' inline='$inline' key='$akey'"
fi
rm -rf "$T11"

# ---------------------------------------------------------------------------
echo "test 12: two long branches sharing a prefix get DIFFERENT buckets"
T12="$(mktemp -d)"
mkdir -p "$T12/wsA" "$T12/wsB"
# Both slugs agree well past the 24 characters the readable part keeps, so a
# name built by truncation alone would hand them ONE bucket, and one worktree's
# Files GC would delete the other's objects.
run_generate "$T12/wsA" WORKSPACE_ENV_BRANCH="feature/a-very-long-branch-name-alpha" >/dev/null
run_generate "$T12/wsB" WORKSPACE_ENV_BRANCH="feature/a-very-long-branch-name-beta" >/dev/null
bA="$(val "$T12/wsA" FILES_STORE_BUCKET)"
bB="$(val "$T12/wsB" FILES_STORE_BUCKET)"
if [ -n "$bA" ] && [ "$bA" != "$bB" ] && [ "${#bA}" -le 63 ] && [ "${#bB}" -le 63 ]; then
  pass "distinct buckets under 63 chars ($bA vs $bB)"
else
  bad "expected two distinct buckets <= 63 chars, got '$bA' and '$bB'"
fi
rm -rf "$T12"

# ---------------------------------------------------------------------------
echo "test 13: main/CI keeps the default store: plain bucket, content origin on :8000"
T13="$(mktemp -d)"
mkdir -p "$T13/ws"
env CI="1" WORKSPACE_ENV_ROOT="$T13/ws" bash "$SUT" generate >/dev/null 2>&1
main_bucket="$(val "$T13/ws" FILES_STORE_BUCKET)"
main_endpoint="$(val "$T13/ws" FILES_STORE_ENDPOINT)"
main_content="$(val "$T13/ws" FILES_CONTENT_BASE_URL)"
if [ "$main_bucket" = "alkera-files" ] && [ "$main_endpoint" = "http://localhost:8333" ] \
  && [ "$main_content" = "http://files.localhost:8000" ]; then
  pass "main/CI: bucket=$main_bucket endpoint=$main_endpoint content=$main_content"
else
  bad "expected the classic Files values on main/CI, got bucket='$main_bucket' endpoint='$main_endpoint' content='$main_content'"
fi
rm -rf "$T13"

# ---------------------------------------------------------------------------
echo "test 14: regenerating is idempotent: the Files block does not move"
T14="$(mktemp -d)"
mkdir -p "$T14/ws"
run_generate "$T14/ws" WORKSPACE_ENV_BRANCH="feature/idem" >/dev/null
first="$(grep '^FILES_' "$T14/ws/.env.workspace")"
run_generate "$T14/ws" WORKSPACE_ENV_BRANCH="feature/idem" >/dev/null
second="$(grep '^FILES_' "$T14/ws/.env.workspace")"
if [ "$first" = "$second" ] && [ -n "$first" ]; then
  pass "second generate produced the identical Files block"
else
  bad "Files block changed between runs:"$'\n'"$first"$'\n'"--- vs ---"$'\n'"$second"
fi
rm -rf "$T14"

# ---------------------------------------------------------------------------
echo "test 15: .env.local still wins: a pinned bucket is what the banner reports"
T15="$(mktemp -d)"
mkdir -p "$T15/ws"
# .env.workspace names the generated bucket; .env.local pins a different one. The
# load order says the developer's file wins, so the banner must report THAT, a
# banner naming the generated bucket would send them to a store nothing writes to.
run_generate "$T15/ws" WORKSPACE_ENV_BRANCH="feature/override" >/dev/null
generated_bucket="$(val "$T15/ws" FILES_STORE_BUCKET)"
printf 'FILES_STORE_BUCKET=a-real-bucket\n' > "$T15/ws/.env.local"
banner_bucket="$(PRINT_URLS_ROOT="$T15/ws" bash "$SCRIPT_DIR/print-urls.sh" \
  | sed -n 's/.*Files bucket: *\([^ ]*\).*/\1/p')"
if [ "$banner_bucket" = "a-real-bucket" ] && [ "$generated_bucket" != "a-real-bucket" ]; then
  pass "banner honoured .env.local (a-real-bucket, not the generated $generated_bucket)"
else
  bad "expected 'a-real-bucket' from .env.local, got '$banner_bucket' (generated was '$generated_bucket')"
fi
rm -rf "$T15"

# ---------------------------------------------------------------------------
# The refresh mechanism: the Makefile declares the generator as .env.workspace's
# prerequisite, so GNU make re-makes the included file whenever the script that
# produced it is newer. These four drive REAL make against a scratch tree, with
# the prerequisite list read out of the repo's own Makefile, delete it there and
# tests 16, 18 and 19 go red.
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MAKE_PREREQS="$(sed -n 's/^\.env\.workspace:[[:space:]]*\(.*\)$/\1/p' "$REPO_ROOT/Makefile" | head -n1)"

# A scratch Makefile that mirrors the real load order (.env.workspace then
# .env.local) and carries the real prerequisite list, rebased onto $REPO_ROOT so
# the mtime compared is the actual generator's. Args: <ws_dir> <branch>.
write_probe_makefile() {
  local ws="$1" branch="$2" prereqs="" p
  for p in $MAKE_PREREQS; do prereqs="$prereqs $REPO_ROOT/$p"; done
  {
    printf -- '-include .env.workspace\n'
    printf -- '-include .env.local\n'
    printf -- '.env.workspace:%s\n' "$prereqs"
    # The recipe logs every run, so a test can assert the generator fired exactly
    # as often as the mtime comparison should have made it fire.
    # shellcheck disable=SC2016  # $(CURDIR) is make's variable, not the shell's
    printf -- '\t@echo ran >> $(CURDIR)/.gen-runs && CI="" WORKSPACE_ENV_ROOT=$(CURDIR) WORKSPACE_ENV_BRANCH=%s bash %s/ops/scripts/workspace-env.sh generate\n' \
      "$branch" "$REPO_ROOT"
    printf -- '.PHONY: probe\n'
    printf -- 'probe:\n'
    # shellcheck disable=SC2016  # ditto: make expands FILES_STORE_BUCKET, not bash
    printf -- '\t@echo "bucket=$(FILES_STORE_BUCKET)"\n'
  } > "$ws/Makefile"
}

mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1"; }

# A workspace file in the shape that predates the Files block, aged well before
# the generator so make sees it as out of date. Args: <ws_dir> <api_port>.
stale_workspace_file() {
  printf 'COMPOSE_PROJECT_NAME=alkera-probe\nAPI_PORT=%s\nWEB_PORT=%s\n' "$2" "$(( $2 + 2 ))" \
    > "$1/.env.workspace"
  touch -t 202001010000 "$1/.env.workspace"
}

echo "test 16: a workspace file older than the generator refreshes on the next make, keeping its ports"
T16="$(mktemp -d)"
mkdir -p "$T16/ws"
write_probe_makefile "$T16/ws" "feature/stale-refresh"
stale_workspace_file "$T16/ws" 41000
make -C "$T16/ws" probe >/dev/null 2>&1 || true
refreshed_api="$(val "$T16/ws" API_PORT)"
refreshed_bucket="$(val "$T16/ws" FILES_STORE_BUCKET)"
refreshed_enabled="$(val "$T16/ws" FILES_ENABLED)"
# The whole point: the developer keeps the port block their containers are bound
# to AND gains the keys the upgraded generator writes. Moving the ports here would
# tear down a running stack; not gaining the keys is the bug this rule fixes.
if [ "$refreshed_api" = "41000" ] && [ "$refreshed_enabled" = "true" ] \
  && [ -n "$refreshed_bucket" ] && [ "$refreshed_bucket" != "alkera-files" ]; then
  pass "ports kept (API_PORT=$refreshed_api) and the Files block arrived (bucket=$refreshed_bucket)"
else
  bad "expected API_PORT 41000 plus a derived Files block, got api='$refreshed_api' enabled='$refreshed_enabled' bucket='$refreshed_bucket'"
fi
rm -rf "$T16"

# ---------------------------------------------------------------------------
echo "test 17: a workspace file newer than the generator is left untouched"
T17="$(mktemp -d)"
mkdir -p "$T17/ws"
write_probe_makefile "$T17/ws" "feature/current-file"
stale_workspace_file "$T17/ws" 41000
make -C "$T17/ws" probe >/dev/null 2>&1 || true   # first call refreshes it
runs_after_first="$(wc -l < "$T17/ws/.gen-runs" | tr -d ' ')"
before_mtime="$(mtime_of "$T17/ws/.env.workspace")"
make -C "$T17/ws" probe >/dev/null 2>&1 || true   # the next two must be no-ops
make -C "$T17/ws" probe >/dev/null 2>&1 || true
runs_total="$(wc -l < "$T17/ws/.gen-runs" | tr -d ' ')"
after_mtime="$(mtime_of "$T17/ws/.env.workspace")"
# Counting the generator's runs, not just comparing bytes: a rule that fired on
# every make would produce identical content and still be wrong; it would race a
# `make dev-all` that is reading the file, and rewriting it on every target is
# exactly what the mtime comparison exists to avoid.
if [ "$runs_after_first" = "1" ] && [ "$runs_total" = "1" ] && [ "$before_mtime" = "$after_mtime" ]; then
  pass "the generator ran once for the stale file and not again (mtime $after_mtime unchanged)"
else
  bad "expected exactly 1 generator run, got $runs_total (mtime $before_mtime -> $after_mtime)"
fi
rm -rf "$T17"

# ---------------------------------------------------------------------------
echo "test 18: a refresh is idempotent: same ports, same bytes, however often it fires"
T18="$(mktemp -d)"
mkdir -p "$T18/ws"
write_probe_makefile "$T18/ws" "feature/idempotent-refresh"
stale_workspace_file "$T18/ws" 41000
make -C "$T18/ws" probe >/dev/null 2>&1 || true
first="$(cat "$T18/ws/.env.workspace")"
# Age it again: the generator is newer once more, so make refreshes a second time.
touch -t 202001010000 "$T18/ws/.env.workspace"
make -C "$T18/ws" probe >/dev/null 2>&1 || true
second="$(cat "$T18/ws/.env.workspace")"
if [ -n "$first" ] && [ "$first" = "$second" ] \
  && printf '%s' "$first" | grep -q '^FILES_STORE_BUCKET='; then
  pass "a repeated refresh produced byte-identical content"
else
  bad "refresh is not idempotent:"$'\n'"$first"$'\n'"--- vs ---"$'\n'"$second"
fi
rm -rf "$T18"

# ---------------------------------------------------------------------------
echo "test 19: .env.local still wins after a refresh"
T19="$(mktemp -d)"
mkdir -p "$T19/ws"
write_probe_makefile "$T19/ws" "feature/local-survives"
stale_workspace_file "$T19/ws" 41000
# The refresh writes a generated bucket into .env.workspace; .env.local is included
# after it, so the value make resolves must stay the developer's. A refresh that
# clobbered .env.local (or that were included after it) would silently point a
# developer's stack at a store nothing writes to.
printf 'FILES_STORE_BUCKET=a-real-bucket\n' > "$T19/ws/.env.local"
resolved="$(make -C "$T19/ws" probe 2>/dev/null | sed -n 's/^bucket=//p')"
generated="$(val "$T19/ws" FILES_STORE_BUCKET)"
local_still_there="$(cat "$T19/ws/.env.local")"
if [ "$resolved" = "a-real-bucket" ] && [ -n "$generated" ] && [ "$generated" != "a-real-bucket" ] \
  && [ "$local_still_there" = "FILES_STORE_BUCKET=a-real-bucket" ]; then
  pass "make resolved the .env.local bucket (a-real-bucket) over the generated $generated"
else
  bad "expected .env.local to win, got resolved='$resolved' generated='$generated' local='$local_still_there'"
fi
rm -rf "$T19"

# ---------------------------------------------------------------------------
if [ "$fail" -eq 0 ]; then
  echo "ALL PASS"
else
  echo "FAILURES"; exit 1
fi
