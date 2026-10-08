"""The agent tools — harness-agnostic, both transports.

Two HOT core tools the model always sees:

- ``spawn_agent`` — delegate a self-contained subtask to a subagent (``explore``
  by default) and get back its full report + usage stats. It calls into the
  parent session via ``ctx.spawn`` — wired to ``ChatSession.spawn_subagent`` for a
  ROOT session only. A subagent session gets ``ctx.spawn = None`` (a subagent
  cannot spawn), and the agent tools are withheld from a child's advertised
  set entirely, as defense in depth.
- ``list_agent_types`` — discover the spawnable agent types + their input shape.

``spawn_agent`` is BLOCKING: ``N`` of them in one assistant turn run concurrently
(the only parallelization mechanism). A child failure routes up as a tool-call
error, never crashing the parent or its siblings.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.agent_result import AgentUsageStats
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec


class SpawnAgentInput(BaseModel):
    agent: str = "explore"
    """Which agent type (from ``list_agent_types``). Built-ins: ``explore`` (read-only,
    fast, parallel investigation of code AND data) and ``review`` (read-only, thorough
    middle-tier review of a change you've made — code AND data — before you finalize);
    custom ``.alkera/agents/*.md`` resolve too."""
    prompt: str
    """The full, self-contained task brief for the worker — its ONLY input. The
    worker sees none of this conversation, so include every detail it needs."""
    description: str | None = None
    """Optional 3-5 word label for the UI chip."""
    background: bool = False
    """Run this agent in the BACKGROUND: return a job id immediately instead of
    blocking, and notify you with the agent's full report when it finishes. Use ONLY
    for independent investigation you can run while you do other work — a backgrounded
    agent is clamped to read-only. Don't poll; you'll be notified. Leave false
    (blocking) when you need the report to take your next step."""


class SpawnAgentResult(BaseModel):
    summary: str
    """The worker's FULL self-contained report — the only thing you see."""
    stats: AgentUsageStats
    child_session_id: str = ""
    """The spawned child chat's session id — the parent↔child link, so the UI can
    drill into the child's transcript from this one tool card."""


class SpawnAgentTool(Tool[SpawnAgentInput, SpawnAgentResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="spawn_agent",
        title="Spawn agent",
        description=(
            "Spawn a BLOCKING subagent to do a self-contained task and return its "
            "full report + usage stats. Use agent='explore' (the default) for fast, "
            "read-only, highly-parallel investigation of the code AND the data: it "
            "reads many files in parallel AND runs read-only SQL (SELECT / SHOW / "
            "DESCRIBE — list connections, inspect schemas, profile real data), and "
            "hands back ONE dense report with file:line citations (positive AND "
            "negative findings) — so reach for it on data/warehouse questions, not "
            "just code. Use agent='review' for a thorough, read-only review of a change "
            "you've made (code AND data) before you finalize — it checks correctness + "
            "upstream/downstream lineage impact and reports findings by severity with "
            "file:line. To investigate or review several areas at once, emit MULTIPLE "
            "spawn_agent calls in a SINGLE turn — they run concurrently; a single "
            "call blocks until that agent finishes. See list_agent_types for the "
            "available agents."
        ),
        app="agent",
        hot=True,
        effect_hint=Effect.READ,
        # The report is the whole point of the spawn and the only thing the
        # caller sees of the child, so it is never traded for a preview and a
        # fetch — however long it runs to.
        deliver_whole=True,
    )
    Input: ClassVar[type[BaseModel]] = SpawnAgentInput
    Output: ClassVar[type[BaseModel]] = SpawnAgentResult

    async def run(self, args: SpawnAgentInput, ctx: ToolContext) -> SpawnAgentResult:
        if ctx.spawn is None:
            # Recursion-off: a subagent (or a session with no spawn wiring) can't spawn.
            raise ToolError("subagents cannot spawn further subagents")
        res = await ctx.spawn(
            args.prompt,
            agent=args.agent,
            description=args.description,
            background=args.background,
        )
        if res.error is not None:
            # A child failure becomes a clean {error, tool} result via the
            # dispatcher — it never crashes the parent turn or its concurrent siblings.
            raise ToolError(res.error)
        return SpawnAgentResult(
            summary=res.summary, stats=res.stats, child_session_id=res.child_session_id
        )


class AgentTypeInfo(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    """The ``spawn_agent`` input shape: ``{prompt: str (required), description?: str}``."""


class ListAgentTypesInput(BaseModel):
    """No arguments — listing is unconditional."""


class ListAgentTypesResult(BaseModel):
    agents: list[AgentTypeInfo]


class ListAgentTypesTool(Tool[ListAgentTypesInput, ListAgentTypesResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="list_agent_types",
        title="List agent types",
        description=(
            "List the agent types you can spawn with spawn_agent, each with a "
            "description of what it's for and its input shape. Cheap, read-only."
        ),
        app="agent",
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = ListAgentTypesInput
    Output: ClassVar[type[BaseModel]] = ListAgentTypesResult

    async def run(self, args: ListAgentTypesInput, ctx: ToolContext) -> ListAgentTypesResult:
        # The input shape is uniform across agents — the spawn_agent input minus
        # `agent` (which list_agent_types is itself answering).
        spawn_schema = SpawnAgentInput.model_json_schema()
        props = {k: v for k, v in spawn_schema.get("properties", {}).items() if k != "agent"}
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "required": [r for r in spawn_schema.get("required", []) if r != "agent"],
        }
        agents = [
            AgentTypeInfo(name=a.name, description=a.description, input_schema=input_schema)
            for a in ctx.registry.agents
        ]
        return ListAgentTypesResult(agents=agents)


def register_subagent_tools(registry: ToolRegistry) -> None:
    """Register the two hot agent tools. Always present in the registry; a child
    never SEES them (withheld from its advertised descriptors) and a child
    dispatch is refused (the ``app=="agent"`` + ``spawn is None`` backstop)."""
    registry.register(SpawnAgentTool)
    registry.register(ListAgentTypesTool)


__all__ = [
    "AgentTypeInfo",
    "ListAgentTypesInput",
    "ListAgentTypesResult",
    "ListAgentTypesTool",
    "SpawnAgentInput",
    "SpawnAgentResult",
    "SpawnAgentTool",
    "register_subagent_tools",
]
