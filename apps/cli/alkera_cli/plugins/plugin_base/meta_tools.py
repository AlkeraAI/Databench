"""The two daemon-side meta-tools.

Tool search runs ENTIRELY in our daemon — no provider-native Tool Search, no
``defer_loading``, no enable/defer path. Exactly two hot-core meta-tools reach
the long tail, and they (plus any first-class tools) form the FIXED hot prefix
that is composed once per session and never mutated:

- ``search_tools(query, app?, k=8)`` → top-k tool schemas (name, description,
  input_schema, app) as the tool result (lands in the messages region).
- ``call_tool(name, args)`` → the generic dispatcher; the ONLY way a non-hot
  tool is invoked. Validates args against the target tool's ``Input`` and
  surfaces a typed error to the model on mismatch.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.plugin import PluginInfo
from alkera_cli.plugins.plugin_base.tool import (
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
)
from alkera_cli.plugins.plugin_base.wire import is_tool_error_result


class ToolCard(BaseModel):
    """A discovered tool's advertised shape (what ``search_tools`` returns)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    app: str | None = None


class SearchToolsInput(BaseModel):
    query: str
    app: str | None = None
    k: int = 8


class SearchToolsOutput(BaseModel):
    tools: list[ToolCard] = Field(default_factory=list)


class SearchToolsTool(Tool[SearchToolsInput, SearchToolsOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="search_tools",
        title="Search tools",
        description=(
            "Find tools available for this project by keyword. Returns the "
            "matching tools' names, descriptions, and input schemas. Call "
            "call_tool with a returned name to actually run it."
        ),
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = SearchToolsInput
    Output: ClassVar[type[BaseModel]] = SearchToolsOutput

    async def run(self, args: SearchToolsInput, ctx: ToolContext) -> SearchToolsOutput:
        specs = await ctx.registry.search(args.query, app=args.app, k=args.k)
        cards = [
            ToolCard(
                name=spec.name,
                description=spec.description,
                input_schema=ctx.registry.input_schema_for(spec.name),
                app=spec.app,
            )
            for spec in specs
        ]
        return SearchToolsOutput(tools=cards)


class CallToolInput(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class CallToolOutput(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)


class CallToolTool(Tool[CallToolInput, CallToolOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="call_tool",
        title="Call tool",
        description=(
            "Invoke a tool by name (e.g. one returned by search_tools) with its "
            "arguments. The arguments are validated against the tool's schema."
        ),
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = CallToolInput
    Output: ClassVar[type[BaseModel]] = CallToolOutput

    async def run(self, args: CallToolInput, ctx: ToolContext) -> CallToolOutput:
        # The inner tool's own effect is gated inside this dispatch. Thread the
        # WHOLE session context — the live mode, the tool scope, the abort signal,
        # the fence, every gating and scratch handle — through the one spelling
        # ``ToolContext`` owns, so a tool reached through here is bound exactly
        # as one dispatched directly: a scoped agent cannot name its way to an
        # out-of-scope tool, and Stop still reaches a long tool.
        result = await ctx.registry.dispatch(args.name, args.args, **ctx.dispatch_kwargs())
        # A long-tail tool is ONLY reachable through here, so its failure must not be
        # buried inside a "successful" call_tool result — the model would read a failure
        # as data. Re-raise it as this call's own ToolError so the outer dispatch marks
        # it and every transport sets the provider error flag (MCP isError / is_error).
        # The inner message already names the operation where it matters.
        if is_tool_error_result(result):
            message = str(result.get("error") or f"{args.name} failed")
            raise ToolError(message, classification=result.get("classification"))
        return CallToolOutput(result=result)


class ListPluginsInput(BaseModel):
    pass  # no args — returns the full plugin/connection picture for the project


class ListPluginsOutput(BaseModel):
    plugins: list[PluginInfo] = Field(default_factory=list)


class ListPluginsTool(Tool[ListPluginsInput, ListPluginsOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="list_plugins",
        title="List plugins & connections",
        description=(
            "List the data integrations (plugins) supported for THIS project and "
            "their status: which are ACTIVE, which are available to enable, each one's "
            "connections (live vs merely detected), and its URN format. Call this when "
            "the user wants to work with a warehouse / dbt / BI / data source — if the "
            "right plugin or connection isn't active, tell the user how to configure it "
            "(a connection profile, env vars, a project file) so the agent gets real "
            "support (lineage, schema, cost, safe SQL) instead of improvising by hand."
        ),
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = ListPluginsInput
    Output: ClassVar[type[BaseModel]] = ListPluginsOutput

    async def run(self, args: ListPluginsInput, ctx: ToolContext) -> ListPluginsOutput:
        return ListPluginsOutput(plugins=list(ctx.registry.plugin_snapshot))


def register_meta_tools(registry: ToolRegistry) -> None:
    """Register the hot meta-tools on a registry. Both backends call this so they
    derive the same hot set."""
    registry.register(SearchToolsTool)
    registry.register(CallToolTool)
    registry.register(ListPluginsTool)


__all__ = [
    "CallToolInput",
    "CallToolOutput",
    "CallToolTool",
    "ListPluginsInput",
    "ListPluginsOutput",
    "ListPluginsTool",
    "SearchToolsInput",
    "SearchToolsOutput",
    "SearchToolsTool",
    "ToolCard",
    "register_meta_tools",
]
