"""The Alkera-MCP tool descriptors: one contract, two backends.

Both harness backends reach the daemon-side ``ToolRegistry`` through the same
descriptors defined here, served by one parent-hosted loopback MCP server
(``alkera_cli.harness.mcp_server``). Claude wires it as an ``http`` MCP server,
OpenCode as a ``remote`` one.

Dispatch runs in the parent process that owns the ``ChatSession``, so the
session's ``PermissionBroker``, live permission mode and ``permissions.yml``
gate every write identically on both backends.

The model sees the fixed hot set (``search_tools`` + ``call_tool`` + any
first-class hot tool); the long tail is reached via ``call_tool``. Both backends
derive their tool set from ``registry.hot_prefix()`` via
``alkera_tool_descriptors`` below, which keeps them in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from alkera_cli.plugins.plugin_base.tool import (
    FENCED_CODE_TOOLS,
    sanitize_input_schema,
    tool_in_scope,
)

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.tool import ToolRegistry, ToolScope


@dataclass(frozen=True)
class ToolDescriptor:
    """A tool's MCP-facing shape — derived once from a ``ToolSpec``."""

    name: str
    description: str
    input_schema: dict[str, Any]


def alkera_tool_descriptors(
    registry: ToolRegistry,
    tool_scope: ToolScope = None,
    *,
    allow_agent_tools: bool = True,
    allow_task_tools: bool = True,
    allow_background_tools: bool = True,
    allow_code_tools: bool = True,
) -> list[ToolDescriptor]:
    """The fixed hot set as MCP tool descriptors (the cache-stable prefix). The
    SINGLE source both transports' tool lists derive from (the lockstep rule).

    ``tool_scope`` (a subagent's restriction) filters the set so an ``explore``
    agent only ever SEES read tools — paired with the binding ``dispatch`` refusal
    so the hidden tools can't be reached out-of-band either.

    ``allow_agent_tools`` is ``False`` for a CHILD session (a subagent cannot spawn):
    the agent-spawning tools (``spawn_agent`` / ``list_agent_types``, ``app=="agent"``)
    are READ-hinted so ``tool_scope`` alone wouldn't hide them — they're dropped here
    so a child never even SEES an agent tool (paired with the dispatch backstop).

    ``allow_task_tools`` is likewise ``False`` for a child: the TODO tool
    (``manage_tasks``, ``app=="tasks"``) is root-only — it's READ-hinted (so it never
    prompts), so ``tool_scope`` alone wouldn't hide it; dropped here so a subagent
    never sees it (paired with the dispatch backstop).

    ``allow_background_tools`` is likewise ``False`` for a child: the background
    job-management tools (``background_status`` / ``background_cancel``,
    ``app=="background"``) are root-only + READ-hinted, dropped here so a subagent
    never sees them (paired with the dispatch backstop).

    The web tools (``app=="web"``) are deliberately ABSENT here: they're served on
    the dedicated ``web`` loopback mount (``web_tool_descriptors``) so the model
    sees clean ``web_search``/``web_fetch`` names instead of an ``alkera_``-prefixed
    MCP composite."""
    return [
        ToolDescriptor(
            name=spec.name,
            description=spec.description,
            # Normalized for the WIRE: MCP requires an object ``inputSchema``, and a consumer
            # that rejects one tool's schema drops the whole server's tool list. The registry
            # keeps the raw union schema for ``search_tools``.
            input_schema=sanitize_input_schema(registry.input_schema_for(spec.name)),
        )
        for spec in registry.hot_prefix()
        if tool_in_scope(spec, tool_scope)
        and spec.app != "web"
        and (allow_agent_tools or spec.app != "agent")
        and (allow_task_tools or spec.app != "tasks")
        and (allow_background_tools or spec.app != "background")
        and (allow_code_tools or spec.name not in FENCED_CODE_TOOLS)
    ]


#: Registry names of the web tools are ``web.<name>``; the dedicated ``web`` MCP
#: mount advertises the bare ``<name>`` so the backend's ``<server>_<tool>``
#: composition yields the model-facing ``web_search`` / ``web_fetch``.
WEB_TOOL_NAME_PREFIX = "web."


def web_tool_descriptors(
    registry: ToolRegistry,
    tool_scope: ToolScope = None,
) -> list[ToolDescriptor]:
    """The web tools (``app=="web"``), advertised under their BARE names
    (``web.search`` → ``search``) for the dedicated ``web`` loopback mount. The
    backend composes ``<server>_<tool>``, so the model sees ``web_search`` /
    ``web_fetch`` — no ``alkera_`` prefix. ``web_registry_name`` maps an advertised
    name back to the registry name at call time. Empty when the org's web toggle
    left the tools unregistered."""
    return [
        ToolDescriptor(
            name=spec.name.removeprefix(WEB_TOOL_NAME_PREFIX),
            description=spec.description,
            input_schema=sanitize_input_schema(registry.input_schema_for(spec.name)),
        )
        for spec in registry.hot_prefix()
        if spec.app == "web" and tool_in_scope(spec, tool_scope)
    ]


def web_registry_name(advertised: str) -> str:
    """The registry name for a tool advertised on the ``web`` mount
    (``search`` → ``web.search``)."""
    return f"{WEB_TOOL_NAME_PREFIX}{advertised}"


__all__ = [
    "WEB_TOOL_NAME_PREFIX",
    "ToolDescriptor",
    "alkera_tool_descriptors",
    "web_registry_name",
    "web_tool_descriptors",
]
