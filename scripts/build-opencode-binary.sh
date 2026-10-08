#!/usr/bin/env bash
# Build the opencode standalone binary using Bun.
#
# Output: vendor/opencode/dist/opencode-<target>/bin/opencode
# Plus:   apps/cli/dist/opencode/{alkera-agent, .alkera-sha}  (staged for a daemon build to bundle)
# Plus:   apps/cli/dist/opencode/{rg, .alkera-rg-version}     (bundled ripgrep, so the
#         harness never downloads rg from github.com at runtime, see stage_ripgrep)
#
# Usage:
#   scripts/build-opencode-binary.sh                  # build opencode + stage rg
#   scripts/build-opencode-binary.sh --ripgrep-only   # stage only bundled rg
#
# NOTE: the staged executable is named `alkera-agent`, the product's name for
# its agent runtime (our build of opencode). The dev-side stage dir and the
# vendor build output keep their upstream `opencode` names.
#
# Requires: `bun` on PATH for the full opencode build
# (see https://bun.sh/docs/installation). `--ripgrep-only` does not require bun.
#
# This script is idempotent, re-running with the same subtree SHA
# will skip the heavy bun-install/build if the staged artifact is
# already current.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
Usage: scripts/build-opencode-binary.sh [--ripgrep-only]

Build the vendored opencode standalone binary and stage the pinned ripgrep.

Options:
  --ripgrep-only  Stage only apps/cli/dist/opencode/{rg,.alkera-rg-version}
                  for tests and CI; skip bun install and opencode compilation.
  -h, --help      Show this help.
EOF
}

RIPGREP_ONLY=0
while (($#)); do
  case "$1" in
    --ripgrep-only)
      RIPGREP_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

# `bun install` (and the installs build.ts runs internally) fail intermittently
# in CI, most painfully on Windows runners, where bun hits transient EPERM
# moving/reading files in its package store (NtSetInformationFile / file-handle
# races; see oven-sh/bun#26879 and anomalyco/opencode#7685). bun has no built-in
# install retry yet, and the failure is NOT deterministic: the same step often
# succeeds on a re-run. Windows Defender exclusions were tested upstream and did
# NOT help (#7685), so the accepted mitigation is simply to retry. Retries are
# safe/idempotent here: `bun install` reconciles to the lockfile, and build.ts
# does `rm -rf dist` before rebuilding. On platforms that don't flake this is a
# no-op (the command runs once and returns 0).
retry() {
  local max="$1"; shift
  local attempt=1
  local status
  while true; do
    # `|| status=$?` captures the real exit code (and keeps set -e happy, a
    # failure on the left of || doesn't trip it). Using `if "$@"` instead would
    # capture the if-statement's status (always 0), masking the failure.
    status=0
    "$@" || status=$?
    if [ "$status" -eq 0 ]; then return 0; fi
    if [ "$attempt" -ge "$max" ]; then
      echo "✗ still failing after $attempt attempt(s) (exit $status): $*" >&2
      return "$status"
    fi
    echo "⚠ attempt $attempt/$max failed (exit $status); retrying in $((attempt * 10))s: $*" >&2
    sleep "$((attempt * 10))"
    attempt=$((attempt + 1))
  done
}

# On Windows (git-bash/MSYS), Bun compiles to `opencode.exe`; everywhere
# else it's `opencode`. We search the build output under that name, but
# stage it under the user-facing `alkera-agent` name.
EXE=""
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) EXE=".exe" ;; esac

# User-facing name of the staged/bundled harness executable (kept in sync with
# `_opencode_filename()` in apps/cli/alkera_cli/harness/opencode_binary.py).
AGENT_BIN_NAME="alkera-agent"

OPENCODE_DIR="$REPO_ROOT/vendor/opencode"
STAGE_DIR="$REPO_ROOT/apps/cli/dist/opencode"
mkdir -p "$STAGE_DIR"

# macOS code signing. macOS ONLY, a complete no-op on Linux/Windows (there is no
# code-signing concept there), guarded on the very first line. Two macOS modes:
#   - Signed (ALKERA_CODESIGN_IDENTITY set): sign with that Developer ID before
#     a daemon build bundles the binary. The signature lives in the Mach-O
#     bytes, so the copy the daemon extracts to ~/.alkera/cache/ at runtime
#     stays signed.
#   - Local dev (no identity): ADHOC re-sign. Bun's `--compile` adhoc signature
#     on arm64 can stage INVALID ("code or signature have been modified"), and
#     macOS SIGKILLs (-9) an invalidly-signed binary the instant it execs, so a
#     fresh adhoc sign here guarantees `make opencode-binary` stages a binary
#     that actually runs (rg is already validly signed; an adhoc refresh is a
#     harmless no-op for it).
# Hardened runtime (`--options runtime`) is intentionally omitted: opencode is a
# Bun binary that JITs, which needs the com.apple.security.cs.allow-jit
# entitlement under hardened runtime; we add that together with notarization
# when shipping .app/.pkg installers.
codesign_if_configured() {
  local bin="$1"
  local label="${2:-binary}"
  # macOS only; nothing to (re)sign elsewhere.
  [ "$(uname -s)" = "Darwin" ] || return 0
  if [ -n "${ALKERA_CODESIGN_IDENTITY:-}" ]; then
    echo "==> Code-signing $label with: $ALKERA_CODESIGN_IDENTITY"
    codesign --force --timestamp --sign "$ALKERA_CODESIGN_IDENTITY" "$bin"
    codesign --verify --strict --verbose=2 "$bin"
    return 0
  fi
  # Local dev: adhoc re-sign so a Bun-invalid signature can't SIGKILL the binary.
  echo "==> Ad-hoc re-signing $label (local macOS dev, no signing identity)"
  codesign --force --sign - "$bin"
}

# ---------------------------------------------------------------------------
# ripgrep staging (replaces opencode's RUNTIME github download)
# ---------------------------------------------------------------------------
# opencode normally fetches `rg` from github.com/BurntSushi on first use, into a
# per-chat sandbox cache, every fresh sandbox without a system rg re-downloads.
# We move that to build time: download the host platform's rg ONCE here, stage it
# next to the harness binary, bundle it with the daemon, and the adapter
# puts it on PATH + sets OPENCODE_DISABLE_RIPGREP_DOWNLOAD so the runtime never
# phones home. Version + platform map MUST stay in sync with the PLATFORM table
# in vendor/opencode/packages/opencode/src/file/ripgrep.ts.
RIPGREP_VERSION="15.1.0"
STAGED_RG="$STAGE_DIR/rg$EXE"
STAGED_RG_VERSION_FILE="$STAGE_DIR/.alkera-rg-version"

# Echo "<rust-triple> <archive-ext>" for the build host; return 1 if unsupported.
ripgrep_target_triple() {
  local os arch
  if [[ -n "${ALKERA_TARGET:-}" ]]; then
    # CI-authoritative target (see the ALKERA_TARGET note in current-target.sh),
    # trustworthy on Windows ARM64 where an x64 git-bash's `uname -m` misreports.
    os="${ALKERA_TARGET%-*}"; arch="${ALKERA_TARGET#*-}"
  else
    case "$(uname -s)" in
      Darwin) os=darwin ;;
      Linux) os=linux ;;
      MINGW*|MSYS*|CYGWIN*) os=win32 ;;
      *) return 1 ;;
    esac
    case "$(uname -m)" in
      arm64|aarch64) arch=arm64 ;;
      x86_64|amd64) arch=x64 ;;
      i686|i386) arch=ia32 ;;
      *) return 1 ;;
    esac
  fi
  case "$arch-$os" in
    arm64-darwin) echo "aarch64-apple-darwin tar.gz" ;;
    x64-darwin)   echo "x86_64-apple-darwin tar.gz" ;;
    arm64-linux)  echo "aarch64-unknown-linux-gnu tar.gz" ;;
    x64-linux)    echo "x86_64-unknown-linux-musl tar.gz" ;;
    arm64-win32)  echo "aarch64-pc-windows-msvc zip" ;;
    ia32-win32)   echo "i686-pc-windows-msvc zip" ;;
    x64-win32)    echo "x86_64-pc-windows-msvc zip" ;;
    *) return 1 ;;
  esac
}

stage_ripgrep() {
  # Idempotent: re-use the staged rg when it already matches the pinned version.
  if [[ -x "$STAGED_RG" && -f "$STAGED_RG_VERSION_FILE" ]] \
     && [[ "$(cat "$STAGED_RG_VERSION_FILE")" == "$RIPGREP_VERSION" ]]; then
    codesign_if_configured "$STAGED_RG" "ripgrep"
    echo "✓ ripgrep $RIPGREP_VERSION already staged"
    return 0
  fi

  local spec triple ext
  spec="$(ripgrep_target_triple)" || {
    echo "✗ unsupported platform for ripgrep: $(uname -s)/$(uname -m)" >&2
    return 1
  }
  triple="${spec% *}"
  ext="${spec#* }"

  local name="ripgrep-${RIPGREP_VERSION}-${triple}"
  local archive_name="${name}.${ext}"
  local url="https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/${archive_name}"
  local workdir
  workdir="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$workdir'" RETURN
  local archive="$workdir/$archive_name"

  echo "==> Downloading ripgrep $RIPGREP_VERSION ($triple)…"
  retry 3 curl -fsSL -o "$archive" "$url"

  if [[ "$ext" == "tar.gz" ]]; then
    tar -xzf "$archive" -C "$workdir"
  elif command -v unzip >/dev/null 2>&1; then
    unzip -oq "$archive" -d "$workdir"
  else
    # Windows git-bash without unzip, mirror ripgrep.ts's Expand-Archive path.
    powershell.exe -NoProfile -NonInteractive -Command \
      "\$ProgressPreference='SilentlyContinue'; Expand-Archive -LiteralPath '$archive' -DestinationPath '$workdir' -Force"
  fi

  local extracted="$workdir/$name/rg$EXE"
  if [[ ! -f "$extracted" ]]; then
    echo "✗ ripgrep archive did not contain $extracted" >&2
    find "$workdir" -maxdepth 2 -type f 2>/dev/null | sed 's|^|  |' >&2
    return 1
  fi

  cp -f "$extracted" "$STAGED_RG"
  chmod +x "$STAGED_RG"
  echo "$RIPGREP_VERSION" > "$STAGED_RG_VERSION_FILE"
  codesign_if_configured "$STAGED_RG" "ripgrep"

  local rg_size
  rg_size="$(du -m "$STAGED_RG" | cut -f1)"
  echo "✓ ripgrep $RIPGREP_VERSION staged at $STAGED_RG (${rg_size}MB, $triple)"
}

# `make e2e` runs opencode from source (bun-dev) and only needs a staged rg so
# the airgap tests exercise our bundled-ripgrep path without building opencode.
if [[ "$RIPGREP_ONLY" == "1" ]]; then
  stage_ripgrep
  exit 0
fi

# vendor/opencode is a git subtree, NOT a submodule. It's just a normal
# directory in this repository; check for the package.json that opencode's source ships.
if [[ ! -f "$OPENCODE_DIR/packages/opencode/package.json" ]]; then
    echo "✗ vendor/opencode source not found at $OPENCODE_DIR."
    echo "  The subtree may have been removed accidentally. Run:"
    echo "    make opencode-fetch-upstream"
    echo "  then re-pull via:"
    echo "    make opencode-bump"
    exit 1
fi

if ! command -v bun >/dev/null 2>&1; then
    echo "✗ \`bun\` is not on PATH."
    echo "  Install: curl -fsSL https://bun.sh/install | bash"
    exit 1
fi

# Stage ripgrep first, and unconditionally; it's independent of the opencode
# subtree SHA, so it must run even on the opencode cache-hit fast path below
# (otherwise an older stage dir built before this change would have no rg and the
# daemon bundle step would fail).
stage_ripgrep

# Post-subtree: the "version" of opencode is the vendor TREE hash, a content
# address, so two branches or worktrees with identical vendor/opencode key the
# same regardless of history (a rebase never forces a rebuild), matching the
# key CI's pr-gate cache uses. Committed content only: opencode's own build
# dirties bun.lock in every worktree that has built, so a dirt-sensitive key
# would never match anything.
#
# Outside a git checkout (an image build copies the sources without .git) the
# caller names the content in ALKERA_OPENCODE_SHA instead; nothing is staged
# there before, so the fingerprint only has to be stable within the build.
OPENCODE_SHA="${ALKERA_OPENCODE_SHA:-$(git -C "$REPO_ROOT" rev-parse "HEAD:./vendor/opencode" 2>/dev/null || echo unknown)}"
if [[ "$OPENCODE_SHA" == "unknown" || -z "$OPENCODE_SHA" ]]; then
    echo "✗ Could not determine the vendor/opencode tree hash."
    echo "  Run inside a git checkout, or set ALKERA_OPENCODE_SHA."
    exit 1
fi
STAGED_SHA_FILE="$STAGE_DIR/.alkera-sha"
STAGED_BIN="$STAGE_DIR/${AGENT_BIN_NAME}${EXE}"

# The stamp is an OPAQUE fingerprint of that hash (see the staging step for
# why it is never the raw hash). Computed here because the reuse decisions
# below key on it.
if command -v sha256sum >/dev/null 2>&1; then
  OPENCODE_FP="$(printf '%s' "alkera-agent:$OPENCODE_SHA" | sha256sum | cut -c1-16)"
elif command -v shasum >/dev/null 2>&1; then
  OPENCODE_FP="$(printf '%s' "alkera-agent:$OPENCODE_SHA" | shasum -a 256 | cut -c1-16)"
else
  OPENCODE_FP="$OPENCODE_SHA"
fi

# Never rebuild a binary that already exists for this vendor tree: a current
# local stage wins, then a sibling worktree's. ALKERA_AGENT_REBUILD=1 forces
# the build, the escape for uncommitted vendor/opencode edits, which the
# committed-tree key cannot see.
if [[ "${ALKERA_AGENT_REBUILD:-0}" != "1" ]]; then
  if [[ -f "$STAGED_SHA_FILE" && -x "$STAGED_BIN" ]] \
     && [[ "$(cat "$STAGED_SHA_FILE")" == "$OPENCODE_FP" ]]; then
    # Re-sign on the hit path too: a prior unsigned build may have staged this
    # binary before signing was configured; codesign --force is idempotent.
    codesign_if_configured "$STAGED_BIN" "opencode"
    echo "✓ opencode binary up to date (fp=$OPENCODE_FP)"
    exit 0
  fi
  # Sibling checkouts: every git worktree of this repo, plus any extra roots
  # in ALKERA_AGENT_REUSE_FROM (colon-separated, for separate clones). COPY,
  # never symlink: a later rebuild or codesign --force in this worktree would
  # write THROUGH a link and corrupt the donor's binary under its own stamp.
  # Outside a git checkout there are no siblings (git fails, and under
  # pipefail that would end the script).
  PEERS="$(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' || true)"
  if [[ -n "${ALKERA_AGENT_REUSE_FROM:-}" ]]; then
    PEERS="$PEERS"$'\n'"$(printf '%s' "$ALKERA_AGENT_REUSE_FROM" | tr ':' '\n')"
  fi
  while IFS= read -r peer; do
    [[ -z "$peer" || "$peer" == "$REPO_ROOT" ]] && continue
    peer_dir="$peer/apps/cli/dist/opencode"
    peer_bin="$peer_dir/${AGENT_BIN_NAME}${EXE}"
    [[ -f "$peer_dir/.alkera-sha" && -x "$peer_bin" ]] || continue
    [[ "$(cat "$peer_dir/.alkera-sha" 2>/dev/null)" == "$OPENCODE_FP" ]] || continue
    echo "==> Reusing the harness binary from $peer (same vendor tree)"
    TMP_BIN="$STAGE_DIR/.${AGENT_BIN_NAME}.tmp.$$"
    if cp "$peer_bin" "$TMP_BIN" 2>/dev/null; then
      chmod +x "$TMP_BIN"
      rm -f "$STAGED_BIN"
      mv "$TMP_BIN" "$STAGED_BIN"
      echo "$OPENCODE_FP" > "$STAGED_SHA_FILE"
      codesign_if_configured "$STAGED_BIN" "opencode"
      SIZE_MB="$(du -m "$STAGED_BIN" | cut -f1)"
      echo "✓ harness binary reused from $peer (${SIZE_MB}MB, fp=$OPENCODE_FP)"
      exit 0
    fi
    rm -f "$TMP_BIN"
  done <<< "$PEERS"
fi

echo "==> Installing opencode dependencies (bun install)…"
retry 3 bun install --cwd "$OPENCODE_DIR" --frozen-lockfile

# We invoke opencode's OWN `script/build.ts` rather than a hand-rolled
# `bun build --compile` because the project requires the Solid JSX
# transform plugin (`@opentui/solid/bun-plugin`), embedded migration
# SQL, generated model registry, and a per-platform packaging dance,
# all wired up only inside that script. The `--single` flag restricts
# the build to the current platform (without it, the script builds
# all 12 target permutations). `--skip-embed-web-ui` opts out of
# bundling the optional web UI (we don't need it for our integration
# and it requires a separate Vite build).
echo "==> Building opencode (single platform via opencode's build.ts)…"
# Wrap the whole build.ts run too; it does its own `bun install` internally
# (for the cross-platform @opentui/core + @parcel/watcher binaries), so the same
# transient bun EPERM can strike here. build.ts `rm -rf dist` first, so a retry
# starts clean.
(
    cd "$OPENCODE_DIR/packages/opencode"
    # --baseline: on x64 hosts, build Bun's non-AVX2 "baseline" target so the
    # shipped agent runs on CPUs/VMs WITHOUT AVX2 (e.g. QEMU's default vCPU),
    # which otherwise fault with an illegal instruction at load. No-op on arm64
    # (no baseline variant). See build.ts's ALKERA-patched --single filter, which
    # makes --single --baseline emit exactly one (baseline) binary on x64.
    retry 3 bun run script/build.ts --single --skip-embed-web-ui --baseline
)

# opencode's build emits dist/<package-target>/bin/opencode. Pick
# whichever target it produced (there's exactly one with --single).
BUILT_BIN="$(find "$OPENCODE_DIR/packages/opencode/dist" -maxdepth 3 -type f -name "opencode$EXE" | head -n1)"
if [[ -z "$BUILT_BIN" || ! -x "$BUILT_BIN" ]]; then
    echo "✗ opencode build didn't produce a usable binary under packages/opencode/dist/"
    find "$OPENCODE_DIR/packages/opencode/dist" -maxdepth 3 -type f 2>/dev/null | sed 's|^|  |'
    exit 1
fi

# On x64 we MUST ship the baseline (non-AVX2) build: a modern x64 agent faults
# with an illegal instruction (0xC000001D, surfacing as a 0xC0000409 abort) on
# CPUs/VMs without AVX2, e.g. QEMU's default vCPU. Baseline builds land in a
# dist dir named "…-x64-baseline". Fail loudly if a modern x64 binary slipped
# through (e.g. the build.ts --baseline filter regressed), so we never silently
# reship an AVX2-only agent. arm64 has no baseline variant and is exempt.
# arch: CI-authoritative ALKERA_TARGET when set (x64 git-bash on Windows ARM64
# misreports `uname -m` as x86_64, which would wrongly demand a baseline of the
# native arm64 build), else the host.
if [[ -n "${ALKERA_TARGET:-}" ]]; then
    oc_arch="${ALKERA_TARGET#*-}"
else
    case "$(uname -m)" in arm64 | aarch64) oc_arch=arm64 ;; x86_64 | amd64) oc_arch=x64 ;; *) oc_arch=other ;; esac
fi
case "$oc_arch" in
    x64)
        if [[ "$BUILT_BIN" != *baseline* ]]; then
            echo "✗ expected a BASELINE (non-AVX2) x64 opencode build, got: $BUILT_BIN"
            echo "  the agent must run on non-AVX2 CPUs; check build.ts --baseline handling"
            exit 1
        fi
        echo "✓ baseline (non-AVX2) x64 opencode build confirmed: $BUILT_BIN"
        ;;
esac

# Remove-then-copy so a hand-made symlink is never written through into
# another worktree's staged binary.
rm -f "$STAGED_BIN"
cp -f "$BUILT_BIN" "$STAGED_BIN"
chmod +x "$STAGED_BIN"

# .alkera-sha gets the OPAQUE fingerprint computed above (never the raw hash)
# so the shipped binary's bundled runtime/.alkera-sha carries no correlatable
# git object. The value is only a cache-dir key (runtime-<fp>/) + dedup token;
# traceability lives in this build log + git history + the daemon's BUILD_ID.
# The no-hasher fallback keeps the raw hash rather than ever writing an empty
# file (an empty .alkera-sha makes the bundled resolver return None).
echo "$OPENCODE_FP" > "$STAGED_SHA_FILE"

# Sign before a daemon build bundles this.
codesign_if_configured "$STAGED_BIN" "opencode"

SIZE_MB="$(du -m "$STAGED_BIN" | cut -f1)"
echo "✓ harness binary staged at $STAGED_BIN (${SIZE_MB}MB, sha=${OPENCODE_SHA:0:12} fp=${OPENCODE_FP})"
