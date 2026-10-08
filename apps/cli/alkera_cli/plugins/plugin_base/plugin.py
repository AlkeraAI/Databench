"""The ``Plugin`` model + the typed ``register()`` API.

Two distinct phases, deliberately separated:

- ``register(r)`` — the DECLARATIVE "here are my classes" call, run once at
  load to build the typed surface map (cheap, no heavy imports).
- ``activation()`` + ``on_enable()`` — the LIFECYCLE: when the plugin turns on
  for a project and does work (discover connections, prime providers).

A plugin can be *registered* (known) but not *active* (its tools off the
surface) — the anti-tool-hell / anti-poisoning scoping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from alkera_core.extensions import ExtensionPoint
from alkera_core.versioning import Migration, VersionedModel
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer

from alkera_cli.contracts.tool_types import SurfaceKind
from alkera_cli.plugins.plugin_base.connection import ConnectionBuild
from alkera_cli.plugins.plugin_base.surfaces import (
    AgentDefinition,
    SkillDef,
    WorkspaceContext,
)

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.connection_form import ConnectionFormSchema
    from alkera_cli.plugins.plugin_base.surfaces import (
        CapabilityProvider,
        ClassifierProvider,
        ConnectionProvider,
        ContextProvider,
        LineageProvider,
        ProbeProvider,
        RefreshProvider,
        ToolProvider,
    )
    from alkera_cli.plugins.plugin_base.tool import Tool


# ---------------------------------------------------------------------------
# Declared capabilities + activation
# ---------------------------------------------------------------------------


class NodeCapabilities(BaseModel):
    """What certainties a plugin may assert — the extension point for future
    per-plugin declarations (lineage-certainty / context-trust tiers).

    Currently empty: the per-plugin/-connection effect ceiling was removed
    (writes are governed by the permission gate + modes, not a declared ceiling).

    ``extra="allow"`` because this is embedded in ``PluginManifest`` (a
    ``VersionedModel``): a field a NEWER writer adds here must survive an OLDER
    reader's round-trip instead of being silently dropped — the same forward-compat
    invariant every persisted model carries.
    """

    model_config = ConfigDict(frozen=True, extra="allow")


class ConnectionSignal(BaseModel):
    """A hint for where to find connections — e.g. a
    Snowflake profile, an env var, a keychain entry."""

    model_config = ConfigDict(frozen=True)

    kind: str
    """"profile" | "env" | "keychain" | "dbt_profile" | …"""
    locator: str = ""


class ActivationSpec(BaseModel):
    """WHEN a plugin self-enables + how to find its connections."""

    workspace_contains: list[str] = Field(default_factory=list)
    """Glob signals — dbt → ["**/dbt_project.yml"] (multiple roots ⇒ many
    Connections)."""
    connection_signals: list[ConnectionSignal] = Field(default_factory=list)
    always: bool = False
    """Core plugins (fs / shell classifier) are always on."""


class WorkspaceEvent(BaseModel):
    """The trigger ``evaluate_activation`` reasons over."""

    kind: Literal["open", "file_change", "ide_hook"] = "open"
    workspace_root: Path
    changed_paths: list[Path] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The Registrar — typed registration
# ---------------------------------------------------------------------------


class Registrar(Protocol):
    """The typed surface a plugin's ``register()`` writes into. Each method
    only accepts the right class (a ``mypy`` error if you register a ``Tool``
    as a ``ContextProvider``)."""

    def connection_provider(self, p: type[ConnectionProvider]) -> None: ...
    def probe_provider(self, p: type[ProbeProvider]) -> None: ...
    def capabilities(self, p: type[CapabilityProvider]) -> None: ...
    def tool(self, t: type[Tool[Any, Any]]) -> None: ...
    def tool_provider(self, p: type[ToolProvider]) -> None: ...
    def context_provider(self, p: type[ContextProvider]) -> None: ...
    def lineage_provider(self, p: type[LineageProvider]) -> None: ...
    def refresh_provider(self, p: type[RefreshProvider]) -> None: ...
    def classifier(self, c: type[ClassifierProvider]) -> None: ...
    def agents(self, defs: Sequence[AgentDefinition]) -> None: ...
    def skills(self, defs: Sequence[SkillDef]) -> None: ...


# ---------------------------------------------------------------------------
# The Plugin model
# ---------------------------------------------------------------------------


def _migrate_manifest_v1_0_0_to_v2_0_0(data: dict[str, Any]) -> dict[str, Any]:
    """2.0.0 removed ``declared_capabilities.max_effect`` (the per-plugin effect
    ceiling — declared but never enforced). Drop the key so an old manifest
    round-trips to the current shape."""
    caps = data.get("declared_capabilities")
    if isinstance(caps, dict):
        caps.pop("max_effect", None)
    data["schema_version"] = "2.0.0"
    return data


class PluginManifest(VersionedModel):
    """Static identity + declared surfaces."""

    SCHEMA_VERSION = "2.0.0"
    MIGRATIONS: ClassVar[dict[str, Migration]] = {
        "1.0.0": _migrate_manifest_v1_0_0_to_v2_0_0,
    }

    name: str
    """Namespace for tools / URNs / agents — e.g. "snowflake"."""
    version: str = "0.1.0"
    surfaces: frozenset[SurfaceKind] = Field(default_factory=frozenset)
    """Declared so heavy deps import lazily on activation."""
    declared_capabilities: NodeCapabilities = Field(default_factory=NodeCapabilities)
    description: str = ""

    @field_serializer("surfaces")
    def _sort_surfaces(self, surfaces: frozenset[SurfaceKind]) -> list[str]:
        """Deterministic order — a frozenset's iteration order varies by hash
        seed, which would churn persisted manifests + fixtures."""
        return sorted(s.value for s in surfaces)


class Plugin(ABC):
    """An integration *type*. Subclasses declare a static ``manifest`` and
    implement ``register`` + ``activation``; ``on_enable`` / ``on_disable``
    default to no-ops."""

    manifest: ClassVar[PluginManifest]

    @abstractmethod
    def register(self, r: Registrar) -> None:
        """DECLARE the classes this plugin provides (run once at load)."""

    @abstractmethod
    def activation(self) -> ActivationSpec:
        """WHEN to self-enable + how to find connections."""

    def auto_activates(self) -> bool:
        """Whether this plugin's DISCOVERED connections are safe to ADD automatically
        (go live without an explicit human add). True only for local/offline
        resources with no prod or network risk — a local DuckDB file, a dbt project's
        committed manifest. Remote/warehouse plugins return False (the default) so a
        person still explicitly adds them — the prod-by-accident guard. The
        permission gate still applies to every action on an auto-added connection."""
        return False

    async def on_enable(self, ctx: WorkspaceContext) -> None:  # noqa: B027
        """Idempotent — discover connections, prime providers.

        Optional lifecycle hook with a no-op default (B027 is wrong here:
        subclasses override only if they do enable-time work)."""

    async def on_disable(self) -> None:  # noqa: B027
        """Optional lifecycle hook; no-op default."""

    def urn_help(self) -> str:
        """One short line describing how this plugin's assets are addressed as URNs,
        so the agent can construct one by hand (or recognize one). Empty = the plugin
        contributes no lineage/asset URNs. Surfaced for ACTIVE plugins in the agent's
        session brief, e.g. ``"warehouse://<db>.<schema>.<table>"``."""
        return ""

    def connection_form_schema(self) -> ConnectionFormSchema | None:
        """The declarative form the Plugins & Connections UI renders to add a
        connection (auth methods + fields). ``None`` (the default) = no form: the
        plugin is pure detect-then-add, so the UI offers only the discovered
        candidates with an Add button. Local/zero-auth plugins (DuckDB, dbt) keep
        ``None`` — the generic form is backward-compatible."""
        return None

    def build_connection(
        self, handle: str, auth_method: str, fields: dict[str, str]
    ) -> ConnectionBuild:
        """Validate the user-entered ``fields`` for ``auth_method`` and build a
        ``Connection`` plus its primary and optional named credentials. The framework
        stores every credential independently and stamps only references on the
        connection, so secret material never lands on the returned ``Connection`` or
        in agent context. Raise ``ValueError`` on invalid input. Only plugins that
        declare a ``connection_form_schema`` implement this; the default raises."""
        raise NotImplementedError(f"{type(self).__name__} declares no connection form")


#: The plugins a project can discover: what ``PluginRegistry.discover`` loads.
#: Empty in the open platform; the product's data plugins register here.
CLI_PLUGINS: ExtensionPoint[type[Plugin]] = ExtensionPoint("cli_plugins")


class PluginConnectionInfo(BaseModel):
    """A connection a plugin manages, for the agent's discovery tool — live
    (``added``) or merely detected (a candidate the user can enable)."""

    handle: str
    plugin: str
    environment: str = ""
    added: bool = False
    _derived_from: tuple[str, str] | None = PrivateAttr(default=None)
    """The ``(plugin, handle)`` of the live connection a detected row was learned
    through (``plugin_base.derivation``), kept off the wire: a scoped view shows a
    detected row only when that connection is in the chat's scope."""

    @property
    def derived_from(self) -> tuple[str, str] | None:
        """Where this detected row was learned, or ``None`` when nothing says."""
        return self._derived_from


class PluginInfo(BaseModel):
    """One plugin's status for the agent's ``list_plugins`` discovery tool."""

    name: str
    description: str = ""
    active: bool = False
    """The plugin self-activated — its signal (a file, env var, or profile) is present
    in this workspace. An INACTIVE plugin is one Alkera supports but the user hasn't
    configured: tell them how to enable it."""
    enabled: bool = True
    """Whether the user has this plugin turned ON (default true). A user can disable a
    plugin in the Plugins & Connections UI; a disabled plugin's tools are off the
    surface even though it's discovered."""
    surfaces: list[str] = Field(default_factory=list)
    urn_format: str = ""
    """How this plugin's assets are addressed as URNs (empty if it has none)."""
    connections: list[PluginConnectionInfo] = Field(default_factory=list)


__all__ = [
    "CLI_PLUGINS",
    "ActivationSpec",
    "ConnectionSignal",
    "NodeCapabilities",
    "Plugin",
    "PluginConnectionInfo",
    "PluginInfo",
    "PluginManifest",
    "Registrar",
    "WorkspaceEvent",
]
