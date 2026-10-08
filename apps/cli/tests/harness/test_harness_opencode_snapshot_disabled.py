"""OpenCode adapter: filesystem snapshot tracking is disabled.

opencode's snapshot feature (snapshot/index.ts) inits a git repo whose work-tree
is the user's project root and `git add --all`s every tracked + untracked file
into its object store on the first turn — thousands of loose git objects for a
monorepo, recreated per chat because each chat has its own isolated
XDG_DATA_HOME. Alkera never drives the snapshot-backed revert, so the config we
inject must turn it off (`snapshot: false`). These tests pin that through the
real injection seam (`_build_env` → ALKERA_CONFIG_CONTENT) and the override merge.
"""

from __future__ import annotations

import json
from pathlib import Path

from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary


def _adapter(tmp_path: Path, *, harness_native: dict | None = None) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native=harness_native or {},
    )
    binary = ResolvedOpencodeBinary(
        path=Path("/usr/bin/true"), prefix_args=(), source="path", ripgrep_path=None
    )
    adapter = OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())
    adapter._harness_dir.mkdir(parents=True, exist_ok=True)
    return adapter


def _injected_config(adapter: OpencodeHttpAdapter) -> dict:
    return json.loads(adapter._build_env("test-password").secrets["ALKERA_CONFIG_CONTENT"])


def test_default_config_disables_snapshot(tmp_path: Path) -> None:
    config = _injected_config(_adapter(tmp_path))
    assert config["snapshot"] is False


def test_provider_override_does_not_clobber_disabled_snapshot(tmp_path: Path) -> None:
    """The real backend passes an `agent_config` override carrying provider + model
    (build_alkera_opencode_config) but no `snapshot` key. The shallow merge must
    keep snapshot disabled — otherwise every authenticated chat re-enables it."""
    override = {"provider": {"alkera-claude": {"npm": "@ai-sdk/anthropic"}}, "model": "x/y"}
    config = _injected_config(_adapter(tmp_path, harness_native={"agent_config": override}))
    assert config["snapshot"] is False
    assert config["model"] == "x/y"  # the override still wins on the keys it sets


def test_explicit_snapshot_override_wins(tmp_path: Path) -> None:
    """`snapshot: false` is a DEFAULT, not a hard-coded clamp: a caller that
    deliberately passes `agent_config={"snapshot": True}` must get snapshot ON.
    Pins the documented "override keys win" contract of `_opencode_config` and
    kills the wrong-but-passes impl that re-forces `merged["snapshot"] = False`
    after the shallow merge."""
    adapter = _adapter(tmp_path, harness_native={"agent_config": {"snapshot": True}})
    config = _injected_config(adapter)
    assert config["snapshot"] is True


def test_revert_capability_not_advertised(tmp_path: Path) -> None:
    """Snapshots are off, so opencode's snapshot-backed revert cannot undo file
    changes — the adapter must not claim the capability."""
    assert "revert" not in OpencodeHttpAdapter.capabilities


def test_dropping_revert_did_not_drop_the_other_capabilities(tmp_path: Path) -> None:
    """Removing `revert` must be surgical: every capability a caller still relies
    on must remain. Guards against an over-eager edit that thinned the frozenset
    (a wrong impl that emptied it would also pass the `"revert" not in` test).
    Uses subset (not equality) so a legitimate future capability addition survives."""
    caps = OpencodeHttpAdapter.capabilities
    relied_on = {
        "fork",
        "resume",
        "summarize",
        "clear",
        "mcp_dynamic",
        "subagents",
        "permission_runtime",
    }
    assert relied_on <= caps
    # `revert` and `share` are the two intentionally-absent capabilities (both
    # gated on the snapshot/share features Alkera disables).
    assert "share" not in caps
