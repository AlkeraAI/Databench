"""The workspace pins a node-gyp the repo's Node can run.

vendor/opencode's native postinstalls (tree-sitter grammars) go through
node-gyp-build, which does ``require("node-gyp")`` and, when that finds
nothing, runs ``node-gyp`` through bun, which fetches the latest release.
node-gyp 13 needs Node 22.22 or newer; this repo runs Node 20 (``.nvmrc``),
so on a fresh clone ``make e2e`` and ``make opencode-binary`` failed in
``bun install`` with "webidl.util.markAsUncloneable is not a function".
Node resolves ``require`` up the directory tree, so the node-gyp pinned in the
repo root's ``package.json`` (installed by ``make bootstrap``) is the one the
grammars build with. Nothing imports it, so it reads as unused; this test is
what keeps it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The newest node-gyp major that still runs on Node 20.
NODE_20_NODE_GYP_MAJOR = 11


def _node_major() -> int:
    match = re.match(r"v?(\d+)", (REPO_ROOT / ".nvmrc").read_text(encoding="utf-8").strip())
    assert match, ".nvmrc names no Node version"
    return int(match.group(1))


def test_the_root_pins_a_node_gyp_the_pinned_node_can_run() -> None:
    root = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    pinned = (root.get("devDependencies") or {}).get("node-gyp")
    if _node_major() >= 22:
        return  # node-gyp's latest runs here; the pin is no longer load-bearing
    assert pinned, (
        "the root package.json no longer pins node-gyp, so vendor/opencode's native "
        "builds fetch the latest, which this repo's Node cannot run"
    )
    match = re.fullmatch(r"(\d+)\.\d+\.\d+", pinned)
    # An exact version: a range would let the next install drift to a major this
    # Node cannot run, which is the failure being pinned against.
    assert match, f"node-gyp should be pinned to an exact version, not {pinned!r}"
    assert int(match.group(1)) <= NODE_20_NODE_GYP_MAJOR, pinned
