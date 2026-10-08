"""The agent tools (``spawn_agent`` / ``list_agent_types``) → ctx.spawn wiring.

Proves ``spawn_agent`` dispatches to the bound spawn callable (carrying summary +
stats), threads through ``call_tool``, surfaces a child error as ``{error, tool}``,
defaults to ``explore``, and that the agent tools are refused when no
spawn is wired (a subagent session, where recursion is off). The runtime↔session
wiring is covered in test_subagent_spawn; the live cross-process path in the
opencode e2e.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from alkera_cli.plugins.plugin_base import ToolRegistry
from alkera_cli.plugins.plugin_base.agent_result import AgentUsageStats, SubagentRunResult
from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP, deliver_generic
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.subagent_tool import register_subagent_tools
from alkera_cli.plugins.plugin_base.surfaces import AgentDefinition
from alkera_core.project.directory import ProjectDirectory


def _registry(tmp_path: Path, *, agents: list[AgentDefinition] | None = None) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs(), agents=agents)
    register_meta_tools(registry)
    register_subagent_tools(registry)
    return registry


def _ok(summary: str = "child summary") -> SubagentRunResult:
    return SubagentRunResult(summary=summary, stats=AgentUsageStats(tool_calls=3, model="haiku"))


async def _spawn_present(
    prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
) -> SubagentRunResult:
    """A non-None spawn binding (root session) — list_agent_types never calls it,
    it just needs spawn wired so the recursion backstop doesn't refuse it."""
    return _ok()


async def test_spawn_tool_calls_ctx_spawn_and_returns_summary_and_stats(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls: list[tuple[str, str, str | None]] = []

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        calls.append((prompt, agent, description))
        return _ok()

    out = await registry.dispatch(
        "spawn_agent",
        {"prompt": "analyze the orders table", "agent": "explore", "description": "d"},
        spawn=_spawn,
    )
    assert out["summary"] == "child summary"
    assert out["stats"]["tool_calls"] == 3
    assert out["stats"]["model"] == "haiku"
    assert calls == [("analyze the orders table", "explore", "d")]


async def test_spawn_tool_child_error_surfaces_as_tool_error(tmp_path: Path) -> None:
    # A child failure (error-bearing result) becomes a clean {error, tool}.
    registry = _registry(tmp_path)

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        return SubagentRunResult(summary="", stats=AgentUsageStats(), error="gateway exploded")

    out = await registry.dispatch("spawn_agent", {"prompt": "x"}, spawn=_spawn)
    assert out["error"] == "gateway exploded"
    assert out["tool"] == "spawn_agent"


def test_agent_tools_are_hot_and_excluded_from_children(tmp_path: Path) -> None:
    # Both agent tools are HOT (always advertised to a root), and a CHILD
    # (allow_agent_tools=False, recursion off) sees NEITHER in its descriptors.
    from alkera_cli.plugins.plugin_base.mcp_entry import alkera_tool_descriptors

    registry = _registry(tmp_path)
    hot = {spec.name for spec in registry.hot_prefix()}
    assert {"spawn_agent", "list_agent_types"} <= hot

    root_names = {d.name for d in alkera_tool_descriptors(registry)}
    child_names = {d.name for d in alkera_tool_descriptors(registry, allow_agent_tools=False)}
    assert {"spawn_agent", "list_agent_types"} <= root_names
    assert "spawn_agent" not in child_names
    assert "list_agent_types" not in child_names


def test_spawn_tool_description_surfaces_read_only_sql_and_data() -> None:
    # The model reads THIS description to decide whether explore fits a DATA task —
    # so it must say explore does read-only SQL / data work, not just "codebase".
    from alkera_cli.plugins.plugin_base.subagent_tool import SpawnAgentTool

    desc = SpawnAgentTool.spec.description
    assert "SQL" in desc
    assert "data" in desc


def test_spawn_tool_description_points_at_review() -> None:
    # The always-hot blurb must also point the model at agent='review' for a
    # read-only review of a change before finalizing.
    from alkera_cli.plugins.plugin_base.subagent_tool import SpawnAgentTool

    desc = SpawnAgentTool.spec.description
    assert "review" in desc


async def test_agent_tools_refused_without_spawn_wiring(tmp_path: Path) -> None:
    # Recursion off: a child has no spawn wiring → the dispatch backstop
    # refuses BOTH agent tools (app=="agent" + spawn is None), independent of scope.
    registry = _registry(tmp_path)
    spawn_out = await registry.dispatch("spawn_agent", {"prompt": "recurse"}, spawn=None)
    assert "not available to subagents" in spawn_out["error"]
    list_out = await registry.dispatch("list_agent_types", {}, spawn=None)
    assert "not available to subagents" in list_out["error"]


async def test_spawn_tool_via_call_tool_threads_spawn(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        return _ok(f"did: {prompt}")

    out = await registry.dispatch(
        "call_tool",
        {"name": "spawn_agent", "args": {"prompt": "summarize"}},
        spawn=_spawn,
    )
    assert out["result"]["summary"] == "did: summarize"


async def test_spawn_tool_defaults_to_explore(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    seen: list[str] = []

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        seen.append(agent)
        return _ok("")

    await registry.dispatch("spawn_agent", {"prompt": "x"}, spawn=_spawn)
    assert seen == ["explore"]


async def test_concurrent_spawn_dispatch_overlaps_not_serialized(tmp_path: Path) -> None:
    # Backend-agnostic concurrency proof: two spawn_agent dispatches
    # through the SAME ToolRegistry.dispatch path BOTH backends use must OVERLAP in
    # wall-clock — no lock/semaphore serializes them. (The vendor binaries' parallel
    # tool_use firing is proven by the e2e overlap tests; this pins OUR path.)
    registry = _registry(tmp_path)
    spans: list[tuple[float, float]] = []

    async def _slow(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(0.3)
        spans.append((start, loop.time()))
        return _ok()

    await asyncio.gather(
        registry.dispatch("spawn_agent", {"prompt": "a"}, spawn=_slow),
        registry.dispatch("spawn_agent", {"prompt": "b"}, spawn=_slow),
    )
    assert len(spans) == 2
    (s1, e1), (s2, e2) = spans
    assert max(s1, s2) < min(e1, e2), f"dispatch serialized concurrent spawns (spans={spans})"


async def test_list_agent_types_returns_registered_agents(tmp_path: Path) -> None:
    agents = [
        AgentDefinition(name="explore", description="read-only explorer", tool_scope="read_only"),
        AgentDefinition(name="scout", description="a custom one"),
    ]
    registry = _registry(tmp_path, agents=agents)
    out = await registry.dispatch("list_agent_types", {}, spawn=_spawn_present)
    names = {a["name"] for a in out["agents"]}
    assert names == {"explore", "scout"}
    explore = next(a for a in out["agents"] if a["name"] == "explore")
    # The advertised input shape is the spawn_agent input minus `agent`.
    assert "prompt" in explore["input_schema"]["properties"]
    assert "agent" not in explore["input_schema"]["properties"]
    assert explore["input_schema"]["required"] == ["prompt"]


async def test_a_long_report_reaches_the_parent_whole(tmp_path: Path) -> None:
    """A child's report is the only thing the parent ever sees of an entire child
    session, and it is already the compression — so length is not a reason to
    replace it with a preview and a handle the parent has to go fetch. The same
    payload through the generic door proves the inline cap is still live for
    every other tool: this result is exempt, the door is not gone."""
    registry = _registry(tmp_path)
    report = "finding: orders.total is null for 3% of rows at loader.py:88\n" * 4000
    assert len(report.encode()) > RESULT_INLINE_BYTE_CAP

    async def _spawn(
        prompt: str, *, agent: str = "explore", description: Any = None, background: bool = False
    ) -> SubagentRunResult:
        return _ok(report)

    out = await registry.dispatch("spawn_agent", {"prompt": "dig"}, spawn=_spawn)
    assert out["summary"] == report
    assert "blob" not in out and not out.get("truncated")

    spilled = deliver_generic(
        ProjectDirectory(tmp_path / ".alkera").blobs(), "spawn_agent", dict(out)
    )
    assert spilled["truncated"] is True, "the inline cap must still bind every other tool"
