#!/usr/bin/env bash
# Resolve (or fetch) the `temporal` CLI that the temporal-marked tests run as their local
# dev server (temporalio.testing.WorkflowEnvironment.start_local). The ONLY thing on stdout
# is one env line:
#
#     ALKERA_TEMPORAL_BIN=<absolute path to the temporal binary>
#
# so callers can append it straight to $GITHUB_ENV (CI) or `eval` it (local / Makefile).
# Both consumers must read the same value from that one line, which is why it is a
# bare `name=value` with no quoting: $GITHUB_ENV takes the value literally, `eval`
# re-parses it as shell, and only a value free of shell metacharacters reads the
# same both ways (the script refuses to emit anything else). All progress goes to
# stderr. Resolution order:
#
#   1. $ALKERA_TEMPORAL_BIN, when it points at an executable (an operator's own build);
#   2. a `temporal` already on PATH;
#   3. the pinned release below, downloaded once into
#      ${XDG_CACHE_HOME:-$HOME/.cache}/alkera/temporal-cli/<version>/ and reused after.
#
# The pin is deliberate: the dev server's behaviour (schedules, overlap policy, dynamic
# config flags) is part of what the suite exercises, so every developer and every CI
# runner must run the same one. Bump it on purpose, with a green suite.
#
# A download becomes an executable this repo then runs, so the bytes are pinned as
# well as the version: every (version, os, arch) has a SHA-256 recorded below, the
# archive is verified against it BEFORE anything is unpacked or made executable, and a
# mismatch refuses. There is no unverified path -- a version with no recorded digest is
# refused before the download rather than trusted.
set -euo pipefail

TEMPORAL_CLI_VERSION="${TEMPORAL_CLI_VERSION:-1.8.3}"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/alkera/temporal-cli"

log() { echo "[ensure-temporal-cli] $*" >&2; }

# SHA-256 of each published release archive, keyed <version>/<os>/<arch>. Recorded by
# downloading every asset of the pinned release once and hashing it
# (`curl -fsSL <asset> | shasum -a 256`), cross-checked against that release's own
# checksums.txt. Bumping TEMPORAL_CLI_VERSION means adding the new release's six lines
# here in the same change; nothing else will download.
pinned_sha256() {
  case "$1/$2/$3" in
    1.8.3/darwin/amd64) echo 0eed9a02008ba0d1c5417fc1aa706c9016166eae7216ae161ad95eccc6a775ca ;;
    1.8.3/darwin/arm64) echo 77c5bef1753ddfcdcaced2a2d44207aeced1c776e7bcbf94520c7911bd0c4080 ;;
    1.8.3/linux/amd64) echo 6f0afac1e9ddea71f480c43a49f5db5167a244c21db923707f069a79bcabdfea ;;
    1.8.3/linux/arm64) echo 5972ce781d7f28644b353e4177007e7da8e48a316b8458267054b24de2308e09 ;;
    1.8.3/windows/amd64) echo b29a65ba26ae519dc1f3c450addbf77e4676899530ac4061f851427da8d37b05 ;;
    1.8.3/windows/arm64) echo 540d284962080ca4b7919e84b7f3ac75e778aedf5fa6025cb3ad1ced3b9467b5 ;;
    *) return 1 ;;
  esac
}

# Whichever hasher the host has. No tool at all is a refusal, not a skip.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d" " -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d" " -f1
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$1" | sed "s/.*= *//"
  else
    return 1
  fi
}

emit() {
  local path="$1"
  # Under an MSYS shell (Git Bash on Windows) the path is POSIX-shaped
  # (/c/Users/...), and Python on Windows needs a native spelling. Hand out
  # the MIXED form -- drive letter, forward slashes (`cygpath -m`): Python
  # opens it, and unlike the backslashed `cygpath -w` form it survives the
  # `eval` the Makefile applies to this line (bash reads C:\Users\x as
  # C:Usersx). Idempotent on a value already in that form, which is what an
  # earlier run's $GITHUB_ENV export hands back in as the override.
  if command -v cygpath >/dev/null 2>&1; then
    path="$(cygpath -m "$path")"
  fi
  # A bare word is the only spelling both consumers read identically, so a
  # path the shell would re-interpret (a space, a quote, $, a backslash, a
  # glob character, ...) cannot be emitted at all: printf %q leaves a string
  # unchanged exactly when eval would.
  if [ "$(printf '%q' "$path")" != "$path" ]; then
    log "refusing to emit '$path': the Makefile evals this line, and that path contains a character the shell would re-interpret; point XDG_CACHE_HOME or ALKERA_TEMPORAL_BIN at a path without spaces or shell metacharacters"
    exit 1
  fi
  echo "ALKERA_TEMPORAL_BIN=$path"
}

# 1. An explicit override wins when it is real.
if [ -n "${ALKERA_TEMPORAL_BIN:-}" ]; then
  if [ -x "$ALKERA_TEMPORAL_BIN" ]; then
    log "using ALKERA_TEMPORAL_BIN=$ALKERA_TEMPORAL_BIN"
    emit "$ALKERA_TEMPORAL_BIN"
    exit 0
  fi
  log "ALKERA_TEMPORAL_BIN=$ALKERA_TEMPORAL_BIN is not executable; falling through"
fi

# 2. Whatever is already installed.
if found="$(command -v temporal 2>/dev/null)" && [ -n "$found" ]; then
  log "using temporal on PATH: $found ($("$found" --version 2>/dev/null | head -n1))"
  emit "$found"
  exit 0
fi

# 3. Download the pinned release.
case "$(uname -s)" in
  Darwin) os="darwin" ;;
  Linux) os="linux" ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT) os="windows" ;;
  *) log "unsupported OS: $(uname -s)"; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch="amd64" ;;
  arm64|aarch64) arch="arm64" ;;
  *) log "unsupported architecture: $(uname -m)"; exit 1 ;;
esac

exe="temporal"
archive_ext="tar.gz"
if [ "$os" = "windows" ]; then
  exe="temporal.exe"
  archive_ext="zip"
fi

dest_dir="$CACHE_ROOT/$TEMPORAL_CLI_VERSION"
dest="$dest_dir/$exe"

if [ ! -x "$dest" ]; then
  # What the archive must hash to. An explicit TEMPORAL_CLI_SHA256 is how a bump is
  # tried out before its digests are recorded; without one, an unrecorded
  # (version, os, arch) is refused here -- before the download, not after it.
  expected="${TEMPORAL_CLI_SHA256:-}"
  if [ -z "$expected" ] && ! expected="$(pinned_sha256 "$TEMPORAL_CLI_VERSION" "$os" "$arch")"; then
    log "refusing to download the temporal CLI: no pinned SHA-256 for ${TEMPORAL_CLI_VERSION}/${os}/${arch}. Record the release's digests in pinned_sha256() in this script (or pass TEMPORAL_CLI_SHA256=<digest> for a one-off)"
    exit 1
  fi

  archive="temporal_cli_${TEMPORAL_CLI_VERSION}_${os}_${arch}.${archive_ext}"
  url="https://github.com/temporalio/cli/releases/download/v${TEMPORAL_CLI_VERSION}/${archive}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  log "downloading $url"
  # --retry-all-errors: a TLS handshake that fails mid-way (curl 35) is not in curl's
  # default retry set. On Windows, schannel refuses the handshake outright when the
  # CA's revocation server is unreachable (CRYPT_E_REVOCATION_OFFLINE), which failed
  # gate shards; best-effort revocation lets it proceed. The archive's pinned SHA-256
  # below is what vouches for the bytes, so this does not weaken the check.
  curl_tls=()
  if [ "$os" = "windows" ]; then
    curl_tls=(--ssl-revoke-best-effort)
  fi
  curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors ${curl_tls[@]+"${curl_tls[@]}"} -o "$tmp/$archive" "$url"

  # Verify BEFORE unpacking: an archive that fails here is never extracted, never
  # written into the cache and never made executable, so a refusal leaves the host
  # exactly as it was.
  if ! actual="$(sha256_of "$tmp/$archive")"; then
    log "refusing $url: no sha256 tool on PATH (sha256sum, shasum or openssl) to verify the download against the pinned digest"
    exit 1
  fi
  if [ "$actual" != "$expected" ]; then
    log "refusing $url: sha256 $actual does not match the pinned $expected. The archive was tampered with, truncated, or the release was re-cut -- nothing was installed"
    exit 1
  fi
  log "sha256 verified: $expected"

  mkdir -p "$dest_dir"
  # Only Windows downloads a zip, and the gate's Windows shard runs it under an MSYS
  # shell that ships neither bsdtar nor unzip and whose `tar` is GNU tar -- which
  # cannot read a zip at all, so falling through to it is a failed extraction, not a
  # fallback. Windows' own bsdtar sits at System32\tar.exe on every Windows Server
  # 2019+ / Windows 10 1803+ image, so name that path instead of trusting PATH order
  # (the order that puts it first is cmd's and PowerShell's, not the MSYS shell's).
  # Then any unzip the host has, then PowerShell's Expand-Archive -- which unpacks the
  # whole archive rather than the one member, harmless in a cache directory we own. A
  # host with none of the three is a loud refusal naming everything that was tried.
  if [ "$archive_ext" = "zip" ]; then
    windows_root="${SYSTEMROOT:-${SystemRoot:-C:/Windows}}"
    system32_tar="${windows_root//\\//}/System32/tar.exe"
    if [ -x "$system32_tar" ]; then
      "$system32_tar" -xf "$tmp/$archive" -C "$dest_dir" "$exe"
    elif command -v unzip >/dev/null 2>&1; then
      unzip -o -q "$tmp/$archive" "$exe" -d "$dest_dir"
    elif command -v powershell >/dev/null 2>&1; then
      # Expand-Archive takes Windows spellings; under MSYS cygpath supplies them.
      ps_archive="$tmp/$archive"
      ps_dest="$dest_dir"
      if command -v cygpath >/dev/null 2>&1; then
        ps_archive="$(cygpath -w "$ps_archive")"
        ps_dest="$(cygpath -w "$ps_dest")"
      fi
      powershell -NoProfile -Command \
        "Expand-Archive -LiteralPath '$ps_archive' -DestinationPath '$ps_dest' -Force"
    else
      log "cannot unpack $archive: no zip reader on this host. Tried $system32_tar, unzip on PATH, and powershell Expand-Archive. Install one of them, or point ALKERA_TEMPORAL_BIN at a temporal binary you already have"
      exit 1
    fi
  else
    tar -xzf "$tmp/$archive" -C "$dest_dir" "$exe"
  fi
  chmod +x "$dest"
fi

# Verify the binary runs before handing it out (a truncated download would otherwise
# surface as an opaque dev-server start failure inside pytest).
version_line="$("$dest" --version 2>/dev/null | head -n1 || true)"
case "$version_line" in
  *"$TEMPORAL_CLI_VERSION"*) ;;
  *) log "binary at $dest did not report version $TEMPORAL_CLI_VERSION: '$version_line'"; exit 1 ;;
esac

log "using $dest ($version_line)"
emit "$dest"
