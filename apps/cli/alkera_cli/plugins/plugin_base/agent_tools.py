"""Where the agent's tools come from beyond the platform's own: the
``AGENT_TOOLS`` extension point.

The platform's tool registry carries its own tools (the meta tools, blobs,
shell, tasks, subagents, skills, web) and every tool a registered plugin
contributes. A tool source adds the rest: a distribution that brings its own
tool family, with the stores those tools read, registers an
:class:`AgentToolSource` here, and :meth:`PluginRegistry.tool_registry` asks
every source, in registration order, for its services and then its tools.

With nothing registered the registry is the platform's alone: no knowledge
store, no lineage store, no cost ledger, and tool search ranks by keyword.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Protocol

from alkera_core.extensions import ExtensionError, ExtensionPoint

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base.capabilities import CapabilitySet
    from alkera_cli.plugins.plugin_base.registry import PluginRegistry
    from alkera_cli.plugins.plugin_base.tool import ToolRegistry


@dataclass(frozen=True)
class ToolBuild:
    """What a source sees of the registry being built for one project."""

    project: ProjectDirectory
    plugins: PluginRegistry
    #: The capability sets of the connections the build registered, one per
    #: connection (``None`` for a connection whose plugin declares none). A
    #: source lights a tool family up from them, the way the SQL tools appear
    #: once a connection can run SQL.
    capabilities: Sequence[CapabilitySet | None] = ()
    #: A listing build (the tool manifest), not a session's: a source registers
    #: every family it could light up, whatever the connections.
    every_family: bool = False


@dataclass(frozen=True)
class ToolServices:
    """The stores a source hands the registry. Each is provided by at most one
    source; ``None`` leaves it unset."""

    context_store: Any = None
    lineage_store: Any = None
    cost_ledger: Any = None
    #: The embedding function tool search ranks with beside keywords.
    embedder: Any = None
    #: Answers a connection with the provider that reads its documents, or None.
    knowledge_reader: Callable[[Any], Any] | None = None


class AgentToolSource(Protocol):
    """One family of agent tools and the stores they read."""

    @property
    def name(self) -> str: ...

    def services(self, build: ToolBuild) -> ToolServices:
        """The stores this source provides, built for ``build``'s project."""
        ...

    def register(self, registry: ToolRegistry, build: ToolBuild) -> None:
        """Register this source's tools into ``registry``."""
        ...


AGENT_TOOLS: ExtensionPoint[AgentToolSource] = ExtensionPoint("agent_tools")


def combined_services(sources: Iterable[AgentToolSource], build: ToolBuild) -> ToolServices:
    """Every source's services as one set. Two sources providing the same
    store is a composition error, never a silent pick of one."""
    chosen: dict[str, Any] = {}
    owner: dict[str, str] = {}
    for source in sources:
        provided = source.services(build)
        for field in fields(ToolServices):
            value = getattr(provided, field.name)
            if value is None:
                continue
            if field.name in chosen:
                raise ExtensionError(
                    f"tool sources {owner[field.name]!r} and {source.name!r} "
                    f"both provide {field.name}"
                )
            chosen[field.name] = value
            owner[field.name] = source.name
    return ToolServices(**chosen)


__all__ = [
    "AGENT_TOOLS",
    "AgentToolSource",
    "ToolBuild",
    "ToolServices",
    "combined_services",
]
