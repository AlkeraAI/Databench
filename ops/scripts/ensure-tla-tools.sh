#!/usr/bin/env bash
# Resolve (or fetch) the TLA+ tools jar that `make files-specs` runs TLC from. The ONLY
# thing on stdout is one env line:
#
#     TLA_TOOLS_JAR=<absolute path to tla2tools.jar>
#
# so callers can append it straight to $GITHUB_ENV (CI) or `eval` it (local / Makefile).
# Both consumers must read the same value from that one line, which is why it is a bare
# `name=value` with no quoting: $GITHUB_ENV takes the value literally, `eval` re-parses
# it as shell, and only a value free of shell metacharacters reads the same both ways
# (the script refuses to emit anything else). All progress goes to stderr.
#
# The jar is cached inside the repo at .cache/tla/ rather than under $HOME, because the
# Docker fallback in tlc.sh bind-mounts the repo root and nothing else: a jar outside the
# checkout would be invisible to the container that has to run it.
#
# A download becomes code this repo then executes, so the bytes are pinned as well as the
# version: the release's SHA-256 is recorded below, the jar is verified against it after
# every install AND on every reuse from cache, and a mismatch refuses (and removes the
# bad file) rather than running it. There is no unverified path.
#
# Why a jar is VENDORED under ops/tools/tla/ instead of always downloaded
# ----------------------------------------------------------------------
# Pinning bytes only works when the bytes upstream are immutable. tlaplus' v1.8.0 is a
# ROLLING pre-release: its CI re-cuts the tag and re-uploads tla2tools.jar, so the asset
# behind that URL changes without the version changing, and a byte pin against it breaks
# on somebody else's build. The last NON-rolling tagged release, v1.7.4, ships TLC 2.19,
# which cannot drive these specs: it rejects `-noTE` (harmless there -- 2.19 writes no
# trace-expression module to suppress), but it also writes NO action header
# (`\* <leases_acquired(h2) line 165, ... of module lease_fencing>`) above each state of
# a `-simulate` dump. The trace-conformance replays read the action name and its
# parameter out of exactly that header, so on 2.19 every dump parses to zero actions and
# the replays cannot run at all.
#
# So the 1.8.0 build the specs were developed against is committed to this repo and is
# the source of truth for that version. It is still verified against the pin on every
# run -- vendoring changes where the bytes come from, not whether they are checked. A
# version with no vendored copy still downloads, so bumping to a future immutable
# release is a one-line change to the two tables below and nothing else.
set -euo pipefail

TLA_TOOLS_VERSION="${TLA_TOOLS_VERSION:-1.8.0}"

# SHA-256 of the tla2tools.jar this repo runs for the pinned version. For a vendored
# version this is the digest of the committed jar; for a downloaded one it is the release
# asset's, recorded by hashing it once (`curl -fsSL <asset> | shasum -a 256`). Bumping
# TLA_TOOLS_VERSION means recording the new digest here in the same change; without one,
# nothing runs.
pinned_sha256() {
  case "$1" in
    1.8.0) echo 20322939d1b55bb0a3f674ab34bb69b87c711a6b35559d32445cb7d7f6d3bb58 ;;
    *) return 1 ;;
  esac
}

# The committed jar for a version, repo-relative, or non-zero for a version that has no
# vendored copy and must be downloaded. Registering a version here is what takes it off
# the network; the filename carries the upstream build date because a rolling tag's
# "1.8.0" alone does not identify which build these bytes are.
vendored_jar() {
  case "$1" in
    1.8.0) echo ops/tools/tla/tla2tools-1.8.0-2026-09-15.jar ;;
    *) return 1 ;;
  esac
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CACHE_DIR="$REPO_ROOT/.cache/tla"
JAR="$CACHE_DIR/tla2tools-${TLA_TOOLS_VERSION}.jar"

log() { echo "[ensure-tla-tools] $*" >&2; }

# Whichever hasher the host has. No tool at all is a refusal, not a skip.
sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d" " -f1
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d" " -f1
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$1" | sed "s/.*= *//"
  else
    return 1
  fi
}

emit() {
  local jar_path="$1"
  # A bare word is the only spelling both consumers read identically, so a path the shell
  # would re-interpret (a space, a quote, $, a backslash, a glob character, ...) cannot be
  # emitted at all: printf %q leaves a string unchanged exactly when eval would.
  if [ "$(printf '%q' "$jar_path")" != "$jar_path" ]; then
    log "refusing to emit '$jar_path': the Makefile evals this line, and that path contains a character the shell would re-interpret; check out the repo at a path without spaces or shell metacharacters"
    exit 1
  fi
  echo "TLA_TOOLS_JAR=$jar_path"
}

expected="${TLA_TOOLS_SHA256:-}"
if [ -z "$expected" ] && ! expected="$(pinned_sha256 "$TLA_TOOLS_VERSION")"; then
  log "refusing to use the TLA+ tools: no pinned SHA-256 for ${TLA_TOOLS_VERSION}. Record the release's digest in pinned_sha256() in this script (or pass TLA_TOOLS_SHA256=<digest> for a one-off)"
  exit 1
fi

# Where the bytes in $JAR came from, so a digest mismatch below can tell the operator
# which failure they are looking at -- a re-cut upstream release and an edited file on
# disk want completely different responses.
origin="cache"
origin_detail="$JAR"

if [ ! -f "$JAR" ]; then
  mkdir -p "$CACHE_DIR"
  if vendored_rel="$(vendored_jar "$TLA_TOOLS_VERSION")"; then
    origin="vendored"
    origin_detail="$vendored_rel"
    if [ ! -f "$REPO_ROOT/$vendored_rel" ]; then
      log "refusing to use the TLA+ tools: ${TLA_TOOLS_VERSION} is registered as vendored in this script but $vendored_rel is not in the checkout. Restore it (it is committed), or unregister the version from vendored_jar() to fetch it from the release instead"
      exit 1
    fi
    log "installing the vendored $vendored_rel"
    # Copied beside the jar, then renamed into place: parallel test workers run
    # this at once, and a reader must never see a half-written jar (it would fail
    # the digest check below and delete the jar under the others).
    part="$(mktemp "$CACHE_DIR/.tla2tools.XXXXXX")"
    trap 'rm -f "$part"' EXIT
    cp "$REPO_ROOT/$vendored_rel" "$part"
    mv -f "$part" "$JAR"
  else
    origin="download"
    origin_detail="https://github.com/tlaplus/tlaplus/releases/download/v${TLA_TOOLS_VERSION}/tla2tools.jar"
    # Beside the jar, so the rename below is atomic (see the vendored branch).
    tmp="$(mktemp -d "$CACHE_DIR/.download.XXXXXX")"
    trap 'rm -rf "$tmp"' EXIT
    log "downloading $origin_detail"
    curl -fsSL --retry 3 --retry-delay 2 -o "$tmp/tla2tools.jar" "$origin_detail"
    mv -f "$tmp/tla2tools.jar" "$JAR"
  fi
fi

# Verified on EVERY run, not only after an install: the cache is a plain directory a
# developer (or another tool) can overwrite, and this is the one gate before the jar is
# handed to a JVM.
if ! actual="$(sha256_of "$JAR")"; then
  log "refusing $JAR: no sha256 tool on PATH (shasum, sha256sum or openssl) to verify it against the pinned digest"
  exit 1
fi
if [ "$actual" != "$expected" ]; then
  rm -f "$JAR"
  log "refusing $JAR: sha256 $actual does not match the pinned $expected."
  case "$origin" in
    download)
      log "  those bytes came from $origin_detail. Either upstream re-cut that release under the same tag (tlaplus' v1.8.0 is a rolling pre-release its CI rebuilds -- which is why the 1.8.0 build this repo runs is vendored under ops/tools/tla/ instead), or the download was tampered with in transit. Check the release's asset before re-pinning anything: a digest that changed while the version did not is not automatically safe to adopt."
      ;;
    vendored)
      log "  those bytes were copied from the committed $origin_detail, so nothing was downloaded and the network is not involved: the vendored jar and pinned_sha256() in this script disagree. One of the two was changed without the other -- restore the committed jar (git checkout) or fix the pin, and do not run the jar until they agree."
      ;;
    *)
      log "  those bytes were already in the cache from an earlier run. .cache/tla/ is a plain gitignored directory, so the file was overwritten, truncated or replaced since it was verified. It has been removed; re-run to reinstall it from the vendored copy (or the release)."
      ;;
  esac
  exit 1
fi

log "sha256 verified: $expected ($origin)"
log "using $JAR"
emit "$JAR"
