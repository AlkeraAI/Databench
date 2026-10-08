#!/usr/bin/env bash
# Build the node bundle from source: the alkera CLI with its locked dependencies
# on a relocatable CPython, plus the opencode harness and ripgrep, packed as
# alkera-<target>.tar.gz in the layout the node bootstrap installs
# (alkera.dist/{alkera,python/,runtime/,VERSION}). No Nuitka.
#
# Builds for the machine it runs on: linux-x64 or linux-arm64. Run it on each.
#
#   scripts/build-node-bundle.sh --out dist/node-bundle [--version 1.2.3]
#
# Writes into --out: alkera-<target>.tar.gz, its .sha256, and manifest.json
# (merged, so two runs into one directory describe both targets).
#
# Needs: uv, bun (for the harness), curl, tar, sha256sum.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OUT=""
VERSION="${ALKERA_NODE_BUNDLE_VERSION:-}"
#: The CPython the bundle carries. uv installs a python-build-standalone build of
#: it, which runs from any directory.
PYTHON_VERSION="${ALKERA_NODE_BUNDLE_PYTHON:-3.13}"

while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift ;;
    --version) VERSION="$2"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$OUT" ]] || { echo "--out is required" >&2; exit 2; }
[[ "$(uname -s)" == "Linux" ]] || { echo "the node bundle builds on Linux only" >&2; exit 2; }
case "$(uname -m)" in
  x86_64|amd64) TARGET=linux-x64 ;;
  aarch64|arm64) TARGET=linux-arm64 ;;
  *) echo "unsupported machine $(uname -m)" >&2; exit 2 ;;
esac
if [[ -z "$VERSION" ]]; then
  VERSION="$(cat VERSION.txt 2>/dev/null || echo 0.0.0)+$(git rev-parse --short=12 HEAD 2>/dev/null || echo source)"
fi
case "$VERSION" in *[!0-9A-Za-z.+-]*|"") echo "refusing version $VERSION" >&2; exit 2 ;; esac

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DIST="$WORK/alkera.dist"
mkdir -p "$DIST/runtime"

echo "==> CPython $PYTHON_VERSION (python-build-standalone) for $TARGET"
UV_PYTHON_INSTALL_DIR="$WORK/pythons" UV_PYTHON_BIN_DIR="$WORK/pybin" \
  uv python install "$PYTHON_VERSION"
PY_HOME="$(find "$WORK/pythons" -mindepth 1 -maxdepth 1 -type d -name 'cpython-*' | head -n1)"
[[ -x "$PY_HOME/bin/python3" ]] || { echo "no python under $WORK/pythons" >&2; exit 1; }
cp -a "$PY_HOME" "$DIST/python"
PY="$DIST/python/bin/python3"
# A managed interpreter refuses installs into itself without this marker gone.
find "$DIST/python/lib" -maxdepth 2 -name EXTERNALLY-MANAGED -delete

echo "==> alkera-cli and its locked dependencies"
uv export --frozen --no-dev --package alkera-cli --no-emit-workspace \
  --output-file "$WORK/requirements.txt" >/dev/null
# The export is the whole locked closure (overrides applied), so it installs as is.
uv pip install --python "$PY" --no-config --no-deps --require-hashes -r "$WORK/requirements.txt"
# The workspace members alkera-cli depends on, installed from this tree, not
# editable: the bundle carries their code.
uv export --frozen --no-dev --package alkera-cli --no-hashes \
  --output-file "$WORK/members.txt" >/dev/null
members=()
while IFS= read -r line; do
  case "$line" in
    -e\ *) members+=("${line#-e }") ;;
  esac
done <"$WORK/members.txt"
[[ ${#members[@]} -gt 0 ]] || { echo "no workspace members to install" >&2; exit 1; }
uv pip install --python "$PY" --no-config --no-deps "${members[@]}"
# The launcher below runs the `alkera` console script the installed CLI
# declares, so the open tree and a product build each launch their own entry.
"$PY" -I -c 'from importlib.metadata import entry_points; (script,) = entry_points(group="console_scripts", name="alkera"); script.load()' \
  || { echo "the bundle cannot load the alkera console script" >&2; exit 1; }
# A box stages the kernel's platform files from the installed packages; without
# them no notebook kernel starts on the node.
"$PY" -I -c "import sys; from pathlib import Path; from alkera_cli.notebooks.kernel_mount import bundle; bundle(Path(sys.argv[1]))" "$WORK/kernel-mount-check" \
  || { echo "the bundle cannot stage the notebook kernel's platform files" >&2; exit 1; }

echo "==> the opencode harness and ripgrep"
bash scripts/build-opencode-binary.sh
STAGE="apps/cli/dist/opencode"
install -m 0755 "$STAGE/alkera-agent" "$DIST/runtime/alkera-agent"
install -m 0755 "$STAGE/rg" "$DIST/runtime/rg"
install -m 0644 "$STAGE/.alkera-sha" "$DIST/runtime/.alkera-sha"

cat >"$DIST/alkera" <<'LAUNCHER'
#!/bin/sh
# The node daemon: the bundle's own CPython, isolated from the host's (-I).
here="$(dirname "$(readlink -f "$0")")"
exec "$here/python/bin/python3" -I -c 'import sys; from importlib.metadata import entry_points; (script,) = entry_points(group="console_scripts", name="alkera"); sys.argv[0] = "alkera"; sys.exit(script.load()())' "$@"
LAUNCHER
chmod 0755 "$DIST/alkera"
printf '%s\n' "$VERSION" >"$DIST/VERSION"
"$DIST/alkera" cloud-mirror supervise --help >/dev/null \
  || { echo "the bundled daemon does not run" >&2; exit 1; }

echo "==> packing"
NAME="alkera-$TARGET.tar.gz"
tar -czf "$OUT/$NAME" -C "$WORK" alkera.dist
SHA="$(sha256sum "$OUT/$NAME" | cut -d' ' -f1)"
printf '%s  %s\n' "$SHA" "$NAME" >"$OUT/$NAME.sha256"
SIZE="$(stat -c %s "$OUT/$NAME")"
"$PY" -I - "$OUT/manifest.json" "$VERSION" "$TARGET" "$NAME" "$SHA" "$SIZE" <<'MERGE'
import json, sys
from pathlib import Path
path, version, target, name, sha, size = sys.argv[1:]
p = Path(path)
manifest = json.loads(p.read_text()) if p.exists() else {}
if manifest.get("version") not in (None, version):
    sys.exit(f"{path} holds version {manifest['version']}, not {version}")
manifest["version"] = version
manifest.setdefault("targets", {})[target] = {"file": name, "sha256": sha, "size": int(size)}
p.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
MERGE
echo "✓ $OUT/$NAME ($((SIZE / 1048576)) MB, sha256 $SHA, version $VERSION)"
