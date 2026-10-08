"""Human-wait paths never time out.

A tool call legitimately blocks for as long as a human sits on a permission
prompt or clarifier question (gated writes dispatch in-parent and await the
broker), and a subagent can run for many minutes — the user must be able to
walk away indefinitely. Two distinct regressions are pinned here: the
MCP-PROTOCOL timeout (`-32001 Request timed out`) AND the HTTP-TRANSPORT timeout
(`The operation timed out.`) — different layers, both of which aborted a tool
call held open under a permission prompt.

Layers, each pinned here:
- opencode MCP-protocol: the config block carries an effectively-infinite
  per-server request timeout (no value = SDK 60s → `-32001`);
- opencode HTTP transport: bun's native ~300s fetch timeout aborts the held-open
  request separately and is NOT reachable from config — a vendored patch passes
  `timeout: false` on the MCP transport requestInit;
- Claude: the adapter env sets MCP_TOOL_TIMEOUT (per-call protocol) AND
  MCP_TIMEOUT / MCP_CONNECT_TIMEOUT_MS (the HTTP transport request/connect
  timeouts) — Claude Code has no per-server field; the env covers the loopback
  alkera server AND the in-process SDK-MCP present_plan/present_question tools;
- the Permission/Question brokers default to NO timeout (timeouts are opt-in
  for non-interactive resolvers; every interactive call site passes None).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter, ResolvedClaudeBinary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.permission_broker import PermissionBroker
from alkera_cli.harness.question_broker import QuestionBroker, QuestionResolution
from alkera_cli.harness.runtime import MCP_NEVER_TIMEOUT_MS, _alkera_mcp_block
from alkera_core.schemas.chat import PermissionOption, PermissionRequest, QuestionRequest

# Node's setTimeout ceiling — one ms more overflows and fires IMMEDIATELY, so
# this exact value is load-bearing, not a style choice.
NODE_SETTIMEOUT_MAX_MS = 2**31 - 1


# ---------------------------------------------------------------------------
# OpenCode: the per-server MCP request timeout in the config block
# ---------------------------------------------------------------------------


def test_never_timeout_is_exactly_the_node_ceiling() -> None:
    assert MCP_NEVER_TIMEOUT_MS == NODE_SETTIMEOUT_MAX_MS


def test_opencode_mcp_block_never_times_out() -> None:
    block = _alkera_mcp_block("http://127.0.0.1:1/mcp", {"Authorization": "Bearer x"}, claude=False)
    assert block["alkera"]["timeout"] == MCP_NEVER_TIMEOUT_MS


def test_vendored_opencode_disables_bun_fetch_timeout() -> None:
    # The per-server `timeout` above only covers opencode's MCP-PROTOCOL timer.
    # bun's native ~300s fetch timeout aborts the held-open request separately
    # ("The operation timed out."), and it's NOT reachable from opencode config —
    # so a vendored patch passes `timeout: false` on the MCP transport requestInit.
    # Pin that the patch survives a subtree update (the only guard, since bun's
    # fetch timeout can't be exercised from Python).
    repo_root = Path(__file__).resolve().parents[4]
    src = (repo_root / "vendor/opencode/packages/opencode/src/mcp/index.ts").read_text(
        encoding="utf-8"
    )
    assert "ALKERA EDIT" in src
    assert "timeout: false" in src
    # ...applied to BOTH remote transports (StreamableHTTP + the SSE fallback).
    assert src.count("requestInit: alkeraRequestInit") == 2


def test_claude_mcp_block_has_no_timeout_field() -> None:
    # Claude Code's McpHttpServerConfig has no per-server timeout — its knob is
    # the MCP_TOOL_TIMEOUT env (next section). An unknown field here would be
    # schema noise at best.
    block = _alkera_mcp_block("http://127.0.0.1:1/mcp", {"Authorization": "Bearer x"}, claude=True)
    assert "timeout" not in block["alkera"]


# ---------------------------------------------------------------------------
# Claude: MCP_TOOL_TIMEOUT in the spawn env
# ---------------------------------------------------------------------------


def _adapter(tmp_path: Path, *, harness_native: dict[str, Any] | None = None) -> ClaudeAgentAdapter:
    config = SessionConfig(
        session_id="sid",
        project_dir=tmp_path,
        chat_dir=tmp_path / "chat",
        harness_native=harness_native or {},
    )
    return ClaudeAgentAdapter(
        config,
        binary=ResolvedClaudeBinary(path=Path("/usr/bin/true"), source="path", version="2.1.0"),
        event_bus=EventBus(),
    )


def test_claude_env_sets_all_three_mcp_timeouts(tmp_path: Path) -> None:
    # Three DISTINCT knobs in the bundled binary: the per-call protocol timeout
    # (MCP_TOOL_TIMEOUT) AND the HTTP transport request/connect timeouts
    # (MCP_TIMEOUT default 30s / MCP_CONNECT_TIMEOUT_MS default 5s) — the latter
    # two are what abort a request held open under a permission prompt ("The
    # operation timed out."), and MCP_TOOL_TIMEOUT does NOT cover them.
    env = _adapter(tmp_path)._build_env()
    assert env["MCP_TOOL_TIMEOUT"] == str(NODE_SETTIMEOUT_MAX_MS)
    assert env["MCP_TIMEOUT"] == str(NODE_SETTIMEOUT_MAX_MS)
    assert env["MCP_CONNECT_TIMEOUT_MS"] == str(NODE_SETTIMEOUT_MAX_MS)


def test_claude_env_override_can_still_tighten_it(tmp_path: Path) -> None:
    # The claude_env seam stays the last word (same contract as every other key).
    env = _adapter(
        tmp_path, harness_native={"claude_env": {"MCP_TOOL_TIMEOUT": "5000"}}
    )._build_env()
    assert env["MCP_TOOL_TIMEOUT"] == "5000"


# ---------------------------------------------------------------------------
# Brokers: no default timeout — the human may have walked away
# ---------------------------------------------------------------------------


def _perm_request() -> PermissionRequest:
    return PermissionRequest(
        event_id="ev_r1",
        time=datetime(2026, 6, 10, tzinfo=UTC),
        session_id="s1",
        request_id="r1",
        permission_kind="run",
        options=[
            PermissionOption(option_id="allow_once", name="Allow once"),
            PermissionOption(option_id="reject_once", name="Reject once"),
        ],
    )


def test_permission_broker_defaults_to_no_timeout() -> None:
    broker = PermissionBroker(lambda req: asyncio.sleep(0, "allow_once"))
    assert broker.default_timeout_seconds is None


def test_question_broker_defaults_to_no_timeout() -> None:
    async def answer(req: QuestionRequest) -> QuestionResolution:
        return ("answer", [])

    assert QuestionBroker(answer).default_timeout_seconds is None


async def test_permission_broker_default_waits_out_a_slow_human() -> None:
    async def slow_human(req: PermissionRequest) -> Any:
        await asyncio.sleep(0.2)
        return "allow_once"

    broker = PermissionBroker(slow_human)  # no timeout kwarg — the default
    assert await broker.resolve(_perm_request()) == "allow_once"


async def test_permission_broker_explicit_timeout_still_works() -> None:
    # Timeouts remain available as an OPT-IN for non-interactive resolvers.
    async def never(req: PermissionRequest) -> Any:
        await asyncio.sleep(60)
        return "allow_once"

    broker = PermissionBroker(never, default_timeout_seconds=0.05)
    assert await broker.resolve(_perm_request()) == "reject_once"
