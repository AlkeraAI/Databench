#!/usr/bin/env bash
# Model-check one TLA+ spec with TLC.
#
#     ops/scripts/tlc.sh <spec.tla> <spec.cfg> [workers] [extra TLC args...]
#
# Anything after the two required paths that is not the bare `workers` count is passed
# straight through to TLC, so a caller can ask for a simulation run
# (`-simulate file=.cache/tla/traces/tr,num=20 -depth 40`) without this script knowing
# what those flags mean. Relative paths in those arguments resolve against the caller's
# working directory under a local JRE and against the repo root (`-w /w`) under Docker,
# so a caller that runs this from the repo root gets the same file either way.
#
# The exit code is TLC's own: 0 when model checking completed with every invariant
# holding, non-zero when TLC reports a violation or fails to parse. That is what makes
# this usable as a gate -- `make files-specs` fails on the first red spec.
#
# Runtime: a real `java` when the host has one, otherwise the same JRE in Docker. The
# dev Macs here have no JDK installed, and macOS ships a /usr/bin/java *stub* that exists
# on PATH and exits non-zero telling you to install a runtime -- so the probe runs
# `java -version` rather than trusting `command -v java`, and a stub falls through to
# Docker instead of failing the whole target.
#
# Under Docker the repo root is bind-mounted at /w because the jar lives in the repo's
# own .cache/tla/. A spec outside the repo (a test writing a mutated copy into a tmp
# directory) gets its directory mounted as a second volume, so the same script checks
# both without the caller knowing which runtime it got.
#
# A Docker run never outlives this script. The container has a name of its own and a
# trap removes it on every exit, an interrupt and a termination included, so a caller
# that gives up (a test timeout, Ctrl-C) leaves nothing behind. Three bounds cover what
# a trap cannot: the run is stopped after TLC_WALL_SECONDS (exit 124), the container
# stops itself half a minute after that if this script was killed outright, and each
# run first removes any TLC container whose script is no longer alive.
#
# Environment:
#   TLC_RUNTIME       auto (default), java or docker. `docker` skips the local JRE.
#   TLC_WALL_SECONDS  wall-clock bound on a Docker run, default 3600.
set -euo pipefail

usage() {
  echo "usage: ops/scripts/tlc.sh <spec.tla> <spec.cfg> [workers] [extra TLC args...]" >&2
  exit 2
}

[ "$#" -ge 2 ] || usage

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TLA_TOOLS_VERSION="${TLA_TOOLS_VERSION:-1.8.0}"
JAR_REL=".cache/tla/tla2tools-${TLA_TOOLS_VERSION}.jar"
# The third positional stays the worker count only while it looks like one; a flag there
# is the first pass-through argument, which keeps every existing two- and three-argument
# call site working unchanged.
SPEC_ARG="$1"
CFG_ARG="$2"
WORKERS=4
if [ "$#" -ge 3 ] && [ -n "${3##*[!0-9]*}" ]; then
  WORKERS="$3"
  shift 3
else
  shift 2
fi
EXTRA_ARGS=("$@")

log() { echo "[tlc] $*" >&2; }

# An absolute path for a file that need not be under $PWD (macOS has no GNU
# `realpath --relative-to`, so this is done by hand).
abspath() {
  local dir base
  dir="$(cd "$(dirname "$1")" && pwd)"
  base="$(basename "$1")"
  printf '%s/%s\n' "$dir" "$base"
}

spec="$(abspath "$SPEC_ARG")"
cfg="$(abspath "$CFG_ARG")"
[ -f "$spec" ] || { log "no such spec: $SPEC_ARG"; exit 2; }
[ -f "$cfg" ] || { log "no such config: $CFG_ARG"; exit 2; }
[ -f "$REPO_ROOT/$JAR_REL" ] || {
  log "missing $JAR_REL -- run ops/scripts/ensure-tla-tools.sh first"
  exit 2
}

spec_dir="$(dirname "$spec")"

# TLC writes its metadata (the states directory) beside the spec by default, and on a
# violation it also drops a <spec>_TTrace_<ts>.tla/.bin trace-expression module there.
# Both are pushed elsewhere: -metadir into the repo cache, -noTE off entirely, so a RED
# run leaves the committed specs directory byte-identical instead of untracked-dirty
# (which would fail the drift gate every time a spec breaks).
meta_dir="$REPO_ROOT/.cache/tla/meta/$(basename "${spec%.tla}")"
mkdir -p "$meta_dir"

java_usable() {
  command -v java >/dev/null 2>&1 && java -version >/dev/null 2>&1
}

TLC_RUNTIME="${TLC_RUNTIME:-auto}"
case "$TLC_RUNTIME" in
  auto | java | docker) ;;
  *) log "TLC_RUNTIME must be auto, java or docker, not '$TLC_RUNTIME'"; exit 2 ;;
esac
if [ "$TLC_RUNTIME" = java ] && ! java_usable; then
  log "TLC_RUNTIME=java but no usable java on PATH"
  exit 2
fi

if [ "$TLC_RUNTIME" != docker ] && java_usable; then
  log "runtime: local java ($(java -version 2>&1 | head -n1))"
  exec java -XX:+UseParallelGC -jar "$REPO_ROOT/$JAR_REL" \
    -config "$cfg" -workers "$WORKERS" -cleanup -noTE -metadir "$meta_dir" \
    ${EXTRA_ARGS+"${EXTRA_ARGS[@]}"} "$spec"
fi

command -v docker >/dev/null 2>&1 || {
  log "no usable java and no docker: install a JRE (or Docker) to model-check specs"
  exit 2
}

# Run a command for at most <seconds>, returning its status, or 124 once it is killed
# for running over. macOS has no `timeout`, and the Docker CLI is the thing that hangs.
# The watchdog sleeps a second at a time so nothing lingers once the command returns.
bounded() {
  local seconds="$1" pid dog status=0
  shift
  "$@" &
  pid=$!
  (
    exec >/dev/null 2>&1
    waited=0
    while [ "$waited" -lt "$seconds" ]; do
      sleep 1
      kill -0 "$pid" 2>/dev/null || exit 0
      waited=$((waited + 1))
    done
    kill -KILL "$pid"
  ) &
  dog=$!
  wait "$pid" 2>/dev/null || status=$?
  if kill "$dog" 2>/dev/null; then
    wait "$dog" 2>/dev/null || true
  else
    status=124
  fi
  return "$status"
}

DOCKER_PROBE_SECONDS=15
DOCKER_REMOVE_SECONDS=30
CONTAINER_GRACE_SECONDS=30

bounded "$DOCKER_PROBE_SECONDS" docker info >/dev/null 2>&1 || {
  log "docker did not answer within ${DOCKER_PROBE_SECONDS}s: start it, or install a JRE"
  exit 2
}

WALL_SECONDS="${TLC_WALL_SECONDS:-3600}"
[ -n "${WALL_SECONDS##*[!0-9]*}" ] && [ "$WALL_SECONDS" -gt 0 ] || {
  log "TLC_WALL_SECONDS must be a positive whole number of seconds, not '$WALL_SECONDS'"
  exit 2
}

OWNER_LABEL="alkera.tlc.owner"
container="alkera-tlc-$$-${RANDOM}${RANDOM}"

# A container whose script is gone has nobody reading its verdict. That is what a run
# killed outright, or killed while Docker was still creating its container, leaves.
sweep_orphans() {
  local id owner
  for id in $(bounded "$DOCKER_PROBE_SECONDS" docker ps -aq --filter "label=$OWNER_LABEL" 2>/dev/null || true); do
    owner="$(bounded "$DOCKER_PROBE_SECONDS" docker inspect \
      --format "{{ index .Config.Labels \"$OWNER_LABEL\" }}" "$id" 2>/dev/null || true)"
    [ -n "$owner" ] && [ -n "${owner##*[!0-9]*}" ] || continue
    kill -0 "$owner" 2>/dev/null && continue
    log "removing a TLC container left by a run that is gone (pid $owner)"
    bounded "$DOCKER_REMOVE_SECONDS" docker rm -f "$id" >/dev/null 2>&1 || true
  done
}
sweep_orphans

log "runtime: docker eclipse-temurin:21-jre (no usable local java)"

# Paths as the container sees them. Everything under the repo root is already mounted at
# /w; anything else needs its own mount.
mounts=(-v "$REPO_ROOT":/w)
container_path() {
  local p="$1"
  case "$p" in
    "$REPO_ROOT"/*) printf '/w/%s\n' "${p#"$REPO_ROOT"/}" ;;
    *) printf '/outside/%s\n' "$(basename "$p")" ;;
  esac
}
case "$spec_dir" in
  "$REPO_ROOT" | "$REPO_ROOT"/*) ;;
  *) mounts+=(-v "$spec_dir":/outside) ;;
esac

run_pid=""
dog_pid=""
cleanup() {
  trap - EXIT INT TERM USR1
  [ -z "$dog_pid" ] || kill "$dog_pid" 2>/dev/null || true
  if [ -n "$run_pid" ]; then
    # The CLI goes first: one still creating the container would start it after the
    # removal below had found nothing to remove.
    kill -KILL "$run_pid" 2>/dev/null || true
    wait "$run_pid" 2>/dev/null || true
  fi
  bounded "$DOCKER_REMOVE_SECONDS" docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'log "stopped after ${WALL_SECONDS}s (TLC_WALL_SECONDS)"; exit 124' USR1

# Not `exec`: this shell has to outlive the CLI to remove the container. In the
# container `timeout` is the second bound, for a script that was killed outright. It
# is a little longer than the script's own, so a run that is merely over time is
# reported by the script (exit 124) and not as a killed JVM.
docker run --rm --name "$container" --label "$OWNER_LABEL=$$" --stop-timeout 10 \
  "${mounts[@]}" -w /w eclipse-temurin:21-jre \
  timeout --signal=KILL "$((WALL_SECONDS + CONTAINER_GRACE_SECONDS))" \
  java -XX:+UseParallelGC -jar "/w/$JAR_REL" \
  -config "$(container_path "$cfg")" -workers "$WORKERS" -cleanup -noTE \
  -metadir "$(container_path "$meta_dir")" ${EXTRA_ARGS+"${EXTRA_ARGS[@]}"} \
  "$(container_path "$spec")" &
run_pid=$!

(
  exec >/dev/null 2>&1
  waited=0
  while [ "$waited" -lt "$WALL_SECONDS" ]; do
    sleep 1
    # A script killed outright runs no trap; its watchdog must not outlive it.
    kill -0 $$ 2>/dev/null || exit 0
    waited=$((waited + 1))
  done
  kill -USR1 $$
) &
dog_pid=$!

status=0
wait "$run_pid" || status=$?
run_pid=""
exit "$status"
