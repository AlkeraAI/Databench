"""The vendor fetch tool is dropped exactly when OUR web tools are mounted.

Leaving the native `webfetch` advertised beside our `web_fetch` costs the model a
turn: in `read_only`/`plan` (where a cloud chat starts) our classifier marks the
vendor one EGRESS and the policy refuses it outright. A permission rule is not
what keeps it out — a bare `pattern:"*"` deny is subtracted before the request
(`Permission.disabled` → `resolveTools`), but only while our ruleset is the last
word on the tool. The gate that cannot be outranked is the vendor-side filter in
`tool/registry.ts`, and this is the env flag that drives it.

These tests pin the Python half: that the flag is derived from the same
`alkera_mcp` map the subprocess is configured from, that it is set in both
directions (so an inherited value can't decide it), and that it is genuinely
independent of the parent-shell flag it is modeled on.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.opencode_http import (
    _OPENCODE_PARENT_SHELL_FLAG,
    _OPENCODE_PARENT_WEB_FETCH_FLAG,
    OpencodeHttpAdapter,
)
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.mcp_server import WEB_MCP_MOUNT
from alkera_cli.harness.opencode_binary import ResolvedOpencodeBinary
from alkera_cli.harness.runtime import _alkera_mcp_block

_GATEWAY_CONFIG: dict[str, Any] = {
    "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://x"}}},
    "model": "mock/mock-model",
}

#: What the runtime hands the adapter when the org's web toggle registered the
#: web tools — the `alkera` mount plus the dedicated `web` one.
_MCP_WITH_WEB: dict[str, Any] = {
    "alkera": {"type": "remote", "url": "http://127.0.0.1:1/mcp"},
    WEB_MCP_MOUNT: {"type": "remote", "url": "http://127.0.0.1:1/mcp-web"},
}
#: ...and what it hands over when the toggle is off: no web mount at all.
_MCP_WITHOUT_WEB: dict[str, Any] = {
    "alkera": {"type": "remote", "url": "http://127.0.0.1:1/mcp"},
}


def _adapter(tmp_path: Path, harness_native: dict[str, Any]) -> OpencodeHttpAdapter:
    config = SessionConfig(
        session_id="our-sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native={"agent_config": dict(_GATEWAY_CONFIG), **harness_native},
    )
    binary = ResolvedOpencodeBinary(path=Path("/usr/bin/true"), prefix_args=(), source="staged")
    return OpencodeHttpAdapter(config, binary=binary, event_bus=EventBus())


@pytest.mark.parametrize(
    ("harness_native", "expected"),
    [
        pytest.param({"alkera_mcp": _MCP_WITH_WEB}, True, id="web-mount-present"),
        pytest.param({"alkera_mcp": _MCP_WITHOUT_WEB}, False, id="web-mount-absent"),
        pytest.param({}, False, id="no-alkera-mcp-at-all"),
        pytest.param({"alkera_mcp": {}}, False, id="empty-mcp-map"),
        # A test that pins its own `tool_registry` instead of a real MCP server
        # gets no mount and must keep the vendor fetch.
        pytest.param({"tool_registry": object()}, False, id="in-process-registry-fallback"),
        # Not a mapping: the runtime never writes this, but a malformed native
        # blob must fall to "no mount" rather than raise mid-spawn.
        pytest.param({"alkera_mcp": "web"}, False, id="malformed-mcp-blob"),
    ],
)
def test_flag_tracks_the_web_mount(
    tmp_path: Path, harness_native: dict[str, Any], expected: bool
) -> None:
    """The env the subprocess is spawned with carries the drop-fetch flag exactly
    when the session serves `web_fetch` itself."""
    env = _adapter(tmp_path, harness_native)._build_env("pw").env
    assert (env.get(_OPENCODE_PARENT_WEB_FETCH_FLAG) == "true") is expected, (
        f"flag={env.get(_OPENCODE_PARENT_WEB_FETCH_FLAG)!r} for {harness_native!r}"
    )


def test_an_inherited_flag_cannot_drop_the_only_fetch_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray `ALKERA_PARENT_WEB_FETCH` in the user's own shell must not reach the
    child when the web tools are NOT mounted — that would leave the agent with no
    fetch tool at all, which is the one outcome this change must never produce."""
    monkeypatch.setenv(_OPENCODE_PARENT_WEB_FETCH_FLAG, "true")
    env = _adapter(tmp_path, {"alkera_mcp": _MCP_WITHOUT_WEB})._build_env("pw").env
    assert _OPENCODE_PARENT_WEB_FETCH_FLAG not in env


def test_the_two_flags_are_independent(tmp_path: Path) -> None:
    """The parent-hosted shell and the parent-hosted fetch are separate decisions:
    a session can host the shell and not the web tools. Overloading one flag for
    both would make the shell's POSIX-only rule silently govern the fetch too."""
    env = _adapter(tmp_path, {"alkera_mcp": _MCP_WITHOUT_WEB})._build_env("pw").env
    assert _OPENCODE_PARENT_WEB_FETCH_FLAG not in env
    if os.name == "posix":
        assert env.get(_OPENCODE_PARENT_SHELL_FLAG) == "true"


def test_the_runtime_writes_the_mount_under_the_name_the_adapter_reads(
    tmp_path: Path,
) -> None:
    """The producer and the consumer of the mount name, driven for real.

    `_alkera_mcp_block` is what the runtime puts in `harness_native`; the adapter
    reads it back. Spelling them independently is how the flag would silently stop
    firing, so build the real block and feed it to the real adapter."""
    headers = {"Authorization": "Bearer t"}
    with_web = _alkera_mcp_block(
        "http://127.0.0.1:1/mcp", headers, claude=False, web_url="http://127.0.0.1:1/mcp-web"
    )
    without_web = _alkera_mcp_block("http://127.0.0.1:1/mcp", headers, claude=False)

    on = _adapter(tmp_path, {"alkera_mcp": with_web})._build_env("pw").env
    off = _adapter(tmp_path, {"alkera_mcp": without_web})._build_env("pw").env

    assert on.get(_OPENCODE_PARENT_WEB_FETCH_FLAG) == "true"
    assert _OPENCODE_PARENT_WEB_FETCH_FLAG not in off


def test_claude_sessions_never_drop_the_fetch_tool(tmp_path: Path) -> None:
    """Claude gets no `web` mount (its native WebFetch already covers the web,
    broker-gated), so a Claude-shaped block must leave the flag off — the flag is
    about what the OPENCODE subprocess advertises."""
    claude_block = _alkera_mcp_block(
        "http://127.0.0.1:1/mcp",
        {"Authorization": "Bearer t"},
        claude=True,
        web_url="http://127.0.0.1:1/mcp-web",
    )
    assert WEB_MCP_MOUNT not in claude_block
    env = _adapter(tmp_path, {"alkera_mcp": claude_block})._build_env("pw").env
    assert _OPENCODE_PARENT_WEB_FETCH_FLAG not in env
