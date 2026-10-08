"""The registrar a plugin's ``register`` call fills: a record of every class
and definition the plugin declares.

``PluginRegistry.discover`` keeps one per plugin; anything that needs a
plugin's declarations without a project (the tool manifest exporter, a test)
registers the plugin into one directly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from alkera_core.extensions import ExtensionPoint

from alkera_cli.plugins.plugin_base.plugin import Registrar

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.surfaces import (
        AgentDefinition,
        CapabilityProvider,
        ClassifierProvider,
        ConnectionProvider,
        ContextProvider,
        LineageProvider,
        ProbeProvider,
        RefreshProvider,
        SkillDef,
        ToolProvider,
    )
    from alkera_cli.plugins.plugin_base.tool import Tool


class RecordingRegistrar(Registrar):
    """Concrete ``Registrar`` that records a plugin's declared classes."""

    def __init__(self) -> None:
        self.connection_providers: list[type[ConnectionProvider]] = []
        self.probe_providers: list[type[ProbeProvider]] = []
        self.capability_providers: list[type[CapabilityProvider]] = []
        self.tools: list[type[Tool[Any, Any]]] = []
        self.tool_providers: list[type[ToolProvider]] = []
        self.context_providers: list[type[ContextProvider]] = []
        self.lineage_providers: list[type[LineageProvider]] = []
        self.refresh_providers: list[type[RefreshProvider]] = []
        self.classifiers: list[type[ClassifierProvider]] = []
        self.agent_defs: list[AgentDefinition] = []
        self.skill_defs: list[SkillDef] = []

    def connection_provider(self, p: type[ConnectionProvider]) -> None:
        self.connection_providers.append(p)

    def probe_provider(self, p: type[ProbeProvider]) -> None:
        self.probe_providers.append(p)

    def capabilities(self, p: type[CapabilityProvider]) -> None:
        self.capability_providers.append(p)

    def tool(self, t: type[Tool[Any, Any]]) -> None:
        self.tools.append(t)

    def tool_provider(self, p: type[ToolProvider]) -> None:
        self.tool_providers.append(p)

    def context_provider(self, p: type[ContextProvider]) -> None:
        self.context_providers.append(p)

    def lineage_provider(self, p: type[LineageProvider]) -> None:
        self.lineage_providers.append(p)

    def refresh_provider(self, p: type[RefreshProvider]) -> None:
        self.refresh_providers.append(p)

    def classifier(self, c: type[ClassifierProvider]) -> None:
        self.classifiers.append(c)

    def agents(self, defs: Sequence[AgentDefinition]) -> None:
        self.agent_defs.extend(defs)

    def skills(self, defs: Sequence[SkillDef]) -> None:
        self.skill_defs.extend(defs)

    def provider_classes(self) -> list[type[object]]:
        """All provider classes (not tools/defs) for instantiation."""
        out: list[type[object]] = []
        out.extend(self.connection_providers)
        out.extend(self.probe_providers)
        out.extend(self.capability_providers)
        out.extend(self.tool_providers)
        out.extend(self.context_providers)
        out.extend(self.lineage_providers)
        out.extend(self.refresh_providers)
        out.extend(self.classifiers)
        return out


#: A check run on each plugin's declarations as discovery records them, before
#: the plugin joins the registry. A check refuses a plugin by raising. A
#: distribution registers the guarantees its own surfaces need (the knowledge
#: extension refuses a knowledge writer with no declared provenance).
RegistrationCheck = Callable[[str, RecordingRegistrar], None]

PLUGIN_REGISTRATION_CHECKS: ExtensionPoint[RegistrationCheck] = ExtensionPoint(
    "plugin_registration_checks"
)


__all__ = ["PLUGIN_REGISTRATION_CHECKS", "RecordingRegistrar", "RegistrationCheck"]
