"""Claude live_provider capstone for the Alkera MCP surface.

The mock-e2e can't drive Claude's tool use (the binary verifies model access), so
the Claude HALF of cross-backend parity is proven HERE against the REAL Anthropic
API: a real Opus turn connects to the SAME parent-hosted loopback MCP server the
OpenCode backend uses (Option A), calls ``call_tool`` → ``sql.query`` against a
real DuckDB connection, and gets 42 back. Pairs with the OpenCode live transport
e2e (test_alkera_mcp_e2e) — same server, both backends.

Opt-in (``-m live_provider``); skips when ``ANTHROPIC_API_KEY`` is unset.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import duckdb
import pytest
from _helpers.alkera_tools import alkera_tool_server
from alkera_cli.harness.adapter import PromptInput, SessionConfig
from alkera_cli.harness.adapters.claude_agent import ClaudeAgentAdapter
from alkera_cli.harness.claude_binary import resolve_claude_binary
from alkera_cli.harness.event_bus import EventBus
from alkera_cli.harness.runtime import _alkera_mcp_block
from alkera_cli.plugins.plugin_base.agent_result import AgentUsageStats, SubagentRunResult
from alkera_core.schemas.chat import Event, SessionStatusChanged, ToolCall, ToolCallUpdate

pytestmark = [pytest.mark.live_provider, pytest.mark.asyncio]

_SID = "22222222-2222-4222-8222-222222222222"


def _load_anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    for parent in Path(__file__).resolve().parents:
        env_local = parent / ".env.local"
        if env_local.is_file():
            for line in env_local.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("ANTHROPIC_API_KEY="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
            break
    return None


async def _drain(sub: Any, *, budget: float = 120.0) -> list[Event]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    out: list[Event] = []
    seen_running = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out
        try:
            async with asyncio.timeout(remaining):
                ev: Event = await anext(sub)
        except (TimeoutError, StopAsyncIteration):
            return out
        out.append(ev)
        if isinstance(ev, SessionStatusChanged):
            if ev.status == "running":
                seen_running = True
            elif ev.status in ("idle", "error") and seen_running:
                return out


async def test_live_claude_calls_alkera_mcp_sql_query(tmp_path: Path) -> None:
    key = _load_anthropic_key()
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set (and not in .env.local)")

    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()

    env = {
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_MODEL": "claude-opus-4-5",
        "ANTHROPIC_SMALL_FAST_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-4-5",
    }
    chat_dir = tmp_path / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)

    async with alkera_tool_server(tmp_path) as tools:
        config = SessionConfig(
            session_id=_SID,
            project_dir=tmp_path,
            chat_dir=chat_dir,
            harness_native={
                "claude_env": env,
                "alkera_mcp": _alkera_mcp_block(tools.url, tools.auth_headers(), claude=True),
            },
        )
        adapter = ClaudeAgentAdapter(config, binary=resolve_claude_binary(), event_bus=EventBus())
        await adapter.start()
        try:
            sub = adapter.subscribe()
            await adapter.send_prompt(
                PromptInput(
                    text=(
                        "Use the Alkera tools to query the 'warehouse' connection. "
                        "Call the call_tool tool with name='sql.query' and "
                        "args={mode:'sql', connection:'warehouse', sql:'SELECT 42 AS answer'}. "
                        "Then tell me the answer."
                    )
                )
            )
            events = await _drain(sub)
        finally:
            await adapter.stop()

    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert "error" not in statuses, f"turn errored (statuses={statuses})"

    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    assert any("call_tool" in tc.tool_name for tc in tool_calls), (
        f"call_tool never invoked (tools={[t.tool_name for t in tool_calls]})"
    )
    outputs = [
        json.dumps(e.output)
        for e in events
        if isinstance(e, ToolCallUpdate) and e.output is not None
    ]
    assert any("42" in o for o in outputs), f"sql.query result (42) missing from {outputs}"


async def test_live_claude_fans_out_to_multiple_spawn_agents(tmp_path: Path) -> None:
    """Claude fan-out SURFACING (live). A real Opus turn asked to spawn TWO Explore
    agents emits two ``spawn_agent`` tool calls — the surfacing works on the Claude
    backend.

    ⚠️ PARALLELISM IS NOT CLAIMED ON THIS BACKEND. opencode fires same-turn parallel
    tool_use as CONCURRENT MCP requests (proven by the wall-clock overlap test in
    test_alkera_mcp_e2e). The Claude Agent SDK, by contrast, was observed here to
    DISPATCH MCP tool calls SERIALLY: even when the model emits both tool_use blocks
    in ONE turn, the SDK awaits each tool's result before sending the next MCP request
    (with 1 s tool sleeps the two calls ran back-to-back with a ~7 ms gap — i.e. NO
    model round-trip between them, so same-turn emission, serial dispatch). Explore
    fan-out therefore runs SEQUENTIALLY on Claude. The serialization is in the vendor
    SDK's MCP client, NOT our dispatch path — our side is concurrent regardless (the
    unit gather-guard + the opencode overlap both prove ``ToolRegistry.dispatch`` never
    serializes). We assert only that the fan-out SURFACES (both calls fire)."""
    key = _load_anthropic_key()
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set (and not in .env.local)")

    spawn_calls: list[str] = []

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        spawn_calls.append(prompt)
        return SubagentRunResult(summary=f"explored {prompt}", stats=AgentUsageStats())

    env = {
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_MODEL": "claude-opus-4-5",
        "ANTHROPIC_SMALL_FAST_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-4-5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-4-5",
    }
    chat_dir = tmp_path / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)

    async with alkera_tool_server(tmp_path, spawn=_spawn) as tools:
        config = SessionConfig(
            session_id=_SID,
            project_dir=tmp_path,
            chat_dir=chat_dir,
            harness_native={
                "claude_env": env,
                "alkera_mcp": _alkera_mcp_block(tools.url, tools.auth_headers(), claude=True),
            },
        )
        adapter = ClaudeAgentAdapter(config, binary=resolve_claude_binary(), event_bus=EventBus())
        await adapter.start()
        try:
            sub = adapter.subscribe()
            await adapter.send_prompt(
                PromptInput(
                    text=(
                        "Call the spawn_agent tool TWICE in this response: once with "
                        "prompt='map the auth flow' and once with prompt='map the gateway'. "
                        "Then briefly confirm both were spawned."
                    )
                )
            )
            await _drain(sub)
        finally:
            await adapter.stop()

    assert len(spawn_calls) >= 2, (
        f"Claude did not fan out — got {len(spawn_calls)} spawn_agent call(s): {spawn_calls}"
    )
