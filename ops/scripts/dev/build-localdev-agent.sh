#!/usr/bin/env bash
#
# Build the opencode harness for a local developer box: a Linux build of the
# vendored opencode, for the architecture this machine's Docker runs, staged at
# apps/cli/dist/localdev-agent/ with the pinned ripgrep beside it.
#
# `make opencode-binary` builds for the HOST (a macOS binary on a Mac), which a
# box, a Linux container, cannot run. This builds the same thing, through
# opencode's own script/build.ts, inside a Linux bun container. It copies
# vendor/opencode into the container without node_modules, so the Linux
# `bun install` never touches the host's node_modules (the host's harness and
# e2e keep their macOS native modules).
#
# Rebuilds only when vendor/opencode differs from what was last built (its
# committed tree id, or "dirty" when it has local edits). Called by
# `make dev-box`; safe to run by hand.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
OUT="apps/cli/dist/localdev-agent"
log() { printf '\033[36m[localdev-agent]\033[0m %s\n' "$*"; }

if git diff --quiet HEAD -- vendor/opencode 2>/dev/null; then
  fingerprint="$(git rev-parse HEAD:./vendor/opencode)"
else
  fingerprint="dirty-$(git diff HEAD -- vendor/opencode | shasum -a 256 | cut -c1-16)"
fi
if [ -x "$OUT/opencode" ] && [ -x "$OUT/rg" ] && [ "$(cat "$OUT/.alkera-sha" 2>/dev/null)" = "$fingerprint" ]; then
  log "up to date ($fingerprint)"
  exit 0
fi

bun_version="$(jq -r .packageManager vendor/opencode/package.json | sed 's/^bun@//')"
rg_version="$(sed -n 's/^RIPGREP_VERSION="\(.*\)"$/\1/p' scripts/build-opencode-binary.sh)"
case "$(docker info --format '{{.Architecture}}')" in
  aarch64 | arm64) rg_triple=aarch64-unknown-linux-gnu ;;
  x86_64 | amd64) rg_triple=x86_64-unknown-linux-musl ;;
  *) echo "no localdev agent build for this Docker architecture" >&2; exit 1 ;;
esac
mkdir -p "$OUT"

log "building opencode $fingerprint for Linux with bun $bun_version (a few minutes the first time)"
docker run --rm \
  -v "$ROOT/vendor/opencode:/src:ro" \
  -v "$ROOT/$OUT:/out" \
  -v alkera-localdev-bun-cache:/root/.bun/install/cache \
  -e OPENCODE_CHANNEL=localdev \
  "oven/bun:${bun_version}-debian" bash -euo pipefail -c '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends git python3 make g++ >/dev/null
    # Native addons (tree-sitter grammars) build through node-gyp.
    bun install -g node-gyp >/dev/null
    export PATH="/root/.bun/bin:$PATH"
    mkdir -p /build
    cd /src
    tar --exclude="./node_modules" --exclude="./packages/*/node_modules" \
        --exclude="./packages/opencode/dist" -cf - . | tar -xf - -C /build
    cd /build
    bun install --frozen-lockfile
    cd packages/opencode
    bun run script/build.ts --single --skip-embed-web-ui --baseline
    built="$(find dist -maxdepth 3 -type f -name opencode | head -n1)"
    [ -x "$built" ] || { echo "the build produced no opencode binary" >&2; exit 1; }
    cp -f "$built" /out/opencode
    chmod +x /out/opencode
  '

log "staging ripgrep $rg_version ($rg_triple)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 3 -o "$work/rg.tar.gz" \
  "https://github.com/BurntSushi/ripgrep/releases/download/${rg_version}/ripgrep-${rg_version}-${rg_triple}.tar.gz"
tar -xzf "$work/rg.tar.gz" -C "$work"
cp -f "$work/ripgrep-${rg_version}-${rg_triple}/rg" "$OUT/rg"
chmod +x "$OUT/rg"

echo "$fingerprint" >"$OUT/.alkera-sha"
log "staged at $OUT"
