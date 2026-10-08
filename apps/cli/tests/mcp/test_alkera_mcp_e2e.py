"""Live MCP-transport e2e for the Alkera plugin tool surface.

Spawns a REAL opencode subprocess (bun-dev) wired to the mock provider, points it
at the PARENT-HOSTED loopback MCP server (Option A — the SAME server the Claude
backend uses), and proves the WHOLE OpenCode transport end-to-end: opencode
connects to the remote MCP server, the model calls `alkera_call_tool`, the server
dispatches `sql.query` against a real DuckDB connection IN THE PARENT, and the
result (42) flows back. This is the OpenCode half of the cross-backend parity
guarantee (the deterministic contract parity is pinned in
apps/cli/tests/plugins/test_mcp_parity.py; the Claude half is test_alkera_mcp_claude_e2e).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from _helpers.alkera_tools import alkera_tool_server
from _helpers.opencode_runner import opencode_e2e_adapter
from _mocks.mock_openai_server import parallel_tool_call_chunks, text_chunks, tool_call_chunks
from alkera_cli.harness.adapter import PromptInput
from alkera_cli.harness.runtime import _alkera_mcp_block
from alkera_cli.plugins.plugin_base.agent_result import AgentUsageStats, SubagentRunResult
from alkera_cli.plugins.plugin_base.permissions.refusal_words import NOT_GRANTED
from alkera_core.schemas.chat import (
    Event,
    SessionStatusChanged,
    ToolCall,
    ToolCallUpdate,
)

pytestmark = pytest.mark.opencode_e2e


async def _drain(sub: Any, *, budget_seconds: float = 90.0) -> list[Event]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
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


async def test_opencode_calls_alkera_mcp_sql_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a DuckDB file so the `alkera mcp` child activates duckdb_local and
    # discovers a "warehouse" connection.
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()

    prompt = "query the warehouse with sql"
    script = {
        prompt: tool_call_chunks(
            "alkera_call_tool",
            {
                "name": "sql.query",
                "args": {
                    "mode": "sql",
                    "connection": "warehouse",
                    "sql": "SELECT 42 AS answer",
                },
            },
        ),
        "*": text_chunks("The answer is 42."),
    }

    async with alkera_tool_server(tmp_path) as tools:
        block = _alkera_mcp_block(tools.url, tools.auth_headers(), claude=False)
        async with opencode_e2e_adapter(
            tmp_path,
            monkeypatch,
            mock_script=script,
            extra_config={"mcp": block},
        ) as (adapter, _server):
            sub = adapter.subscribe()
            await adapter.send_prompt(PromptInput(text=prompt))
            events = await _drain(sub)

    statuses = [e.status for e in events if isinstance(e, SessionStatusChanged)]
    assert "idle" in statuses, f"turn never completed (statuses={statuses})"
    assert "error" not in statuses, f"turn errored (statuses={statuses})"

    # opencode loaded the local-MCP server + invoked our call_tool meta-tool.
    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    assert any("call_tool" in tc.tool_name for tc in tool_calls), (
        f"alkera_call_tool was never invoked (tools={[t.tool_name for t in tool_calls]})"
    )

    # The dispatched sql.query ran against DuckDB and returned 42.
    outputs = [
        json.dumps(e.output)
        for e in events
        if isinstance(e, ToolCallUpdate) and e.output is not None
    ]
    assert any("42" in o for o in outputs), f"sql.query result (42) missing from {outputs}"


class _FakeBroker:
    """Records prompts; returns a fixed option (the Layer-A human decision)."""

    def __init__(self, option: str) -> None:
        self.option = option
        self.requests: list[Any] = []

    async def resolve(self, request: Any) -> str:
        self.requests.append(request)
        return self.option


async def test_opencode_destructive_sql_prompts_under_auto_and_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DROP routed through a REAL opencode subprocess hits the parent-hosted
    server's write gate: under ``auto`` the destructive floor PROMPTS the session
    broker (Layer A), and a rejection refuses the statement — proven across the live
    OpenCode transport (Claude's identical server path is unit-proven in
    test_mcp_server). (``bypass`` is a total override that waives the floor and runs
    the DROP without a prompt — proven at the gate in test_permissions_gate.)"""
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()

    prompt = "drop the table"
    script = {
        prompt: tool_call_chunks(
            "alkera_call_tool",
            {
                "name": "sql.query",
                "args": {"mode": "sql", "connection": "warehouse", "sql": "DROP TABLE t"},
            },
        ),
        "*": text_chunks("Done."),
    }

    broker = _FakeBroker("reject_once")
    async with alkera_tool_server(tmp_path, broker=broker, permission_mode="auto") as tools:
        block = _alkera_mcp_block(tools.url, tools.auth_headers(), claude=False)
        async with opencode_e2e_adapter(
            tmp_path,
            monkeypatch,
            mock_script=script,
            extra_config={"mcp": block},
        ) as (adapter, _server):
            sub = adapter.subscribe()
            await adapter.send_prompt(PromptInput(text=prompt))
            events = await _drain(sub)

    # The destructive floor forced a broker prompt under auto mode.
    assert broker.requests, "DROP did not prompt the broker under auto (Layer A floor)"
    # And the rejection refused it — surfaced as a tool ERROR (status="error") carrying the
    # person's refusal on error_text (the broker's reject is a person's no, so it reads as
    # their sentence, not as the gate's "permission denied"), not a "completed" card with
    # the denial in its output (opencode honors the MCP isError flag the loopback sets).
    tool_updates = [e for e in events if isinstance(e, ToolCallUpdate)]
    refusal_text = [(e.error_text or "") + json.dumps(e.output) for e in tool_updates]
    assert any(NOT_GRANTED in t for t in refusal_text), (
        f"rejected DROP was not refused (updates={refusal_text})"
    )
    assert any(e.status == "error" for e in tool_updates), (
        "a refused destructive tool must surface as a tool error, not a completed result"
    )


async def test_opencode_spawns_agent_via_the_hot_spawn_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL opencode subprocess calls the HOT ``spawn_agent`` tool DIRECTLY (no
    call_tool — it's in the hot prefix now) over the parent-hosted MCP server;
    ctx.spawn fires and the child's report + stats flow back. (The child-turn-
    driving itself is unit-proven in test_subagent_spawn.)"""
    spawn_calls: list[tuple[str, str]] = []

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: object = None, background: bool = False
    ) -> SubagentRunResult:
        spawn_calls.append((prompt, agent))
        return SubagentRunResult(
            summary="SUBAGENT FOUND: 3 stale tables",
            stats=AgentUsageStats(tool_calls=7, model="haiku"),
        )

    prompt = "delegate the investigation to a subagent"
    script = {
        prompt: tool_call_chunks(
            "alkera_spawn_agent",
            {"prompt": "find stale tables", "agent": "explore"},
        ),
        "*": text_chunks("The subagent reported back."),
    }

    async with alkera_tool_server(tmp_path, spawn=_spawn) as tools:
        block = _alkera_mcp_block(tools.url, tools.auth_headers(), claude=False)
        async with opencode_e2e_adapter(
            tmp_path,
            monkeypatch,
            mock_script=script,
            extra_config={"mcp": block},
        ) as (adapter, _server):
            sub = adapter.subscribe()
            await adapter.send_prompt(PromptInput(text=prompt))
            events = await _drain(sub)

    # The model invoked spawn_agent directly → ctx.spawn fired with the right args...
    assert spawn_calls == [("find stale tables", "explore")]
    # ...and the child's report flowed back through the tool result (summary + stats).
    outputs = [
        json.dumps(e.output)
        for e in events
        if isinstance(e, ToolCallUpdate) and e.output is not None
    ]
    assert any("SUBAGENT FOUND: 3 stale tables" in o for o in outputs), (
        f"subagent summary missing from {outputs}"
    )


async def test_opencode_parallel_spawn_agent_calls_overlap_in_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The linchpin: TWO ``spawn_agent`` calls emitted in ONE
    assistant turn run CONCURRENTLY. Each spawn sleeps ~1s; if the opencode binary
    fired them serially the two intervals would be disjoint. We assert they OVERLAP
    (max(starts) < min(ends)) — robust to subprocess/MCP overhead — proving the
    vendor binary fires parallel tool_use as concurrent MCP requests with NO
    serialization on our side (the loopback server is stateless + lock-free)."""
    spans: list[tuple[float, float]] = []

    async def _slow_spawn(
        prompt: str, *, agent: str = "explore", description: object = None, background: bool = False
    ) -> SubagentRunResult:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(1.0)
        spans.append((start, loop.time()))
        return SubagentRunResult(summary=f"explored {prompt}", stats=AgentUsageStats())

    prompt = "explore the auth flow and the gateway at the same time"
    script = {
        prompt: parallel_tool_call_chunks(
            [
                ("alkera_spawn_agent", {"prompt": "map the auth flow", "agent": "explore"}),
                ("alkera_spawn_agent", {"prompt": "map the gateway", "agent": "explore"}),
            ]
        ),
        "*": text_chunks("Both explorations are done."),
    }

    async with alkera_tool_server(tmp_path, spawn=_slow_spawn) as tools:
        block = _alkera_mcp_block(tools.url, tools.auth_headers(), claude=False)
        async with opencode_e2e_adapter(
            tmp_path,
            monkeypatch,
            mock_script=script,
            extra_config={"mcp": block},
        ) as (adapter, _server):
            sub = adapter.subscribe()
            await adapter.send_prompt(PromptInput(text=prompt))
            await _drain(sub)

    assert len(spans) == 2, f"expected 2 concurrent spawns, got {len(spans)}"
    (s1, e1), (s2, e2) = spans
    assert max(s1, s2) < min(e1, e2), (
        f"the two spawn_agent calls did NOT overlap → opencode serialized them (spans={spans})"
    )
