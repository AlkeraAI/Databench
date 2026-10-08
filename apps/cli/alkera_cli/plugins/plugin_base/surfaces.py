"""Plugin protocols and their shared capability contracts.

Plugins implement only the surfaces they provide. Structural protocols let
the registry discover those surfaces without coupling plugins to consumers.
Writer ABCs enforce the capability boundary for every emitted assertion.

Lineage providers can skip unchanged sources, append from persisted cursors,
or reconcile complete snapshots. Refresh metadata controls cadence and
coalesces related work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Protocol,
    runtime_checkable,
)

from alkera_core.connectors.identity import rebind
from alkera_core.versioning import VersionedModel
from pydantic import BaseModel, ConfigDict, Field, field_validator

from alkera_cli.contracts.tool_types import (
    AccessEvent,  # noqa: F401  (re-exported context for providers' shared source)
    ActionDescriptor,
    Effect,
)
from alkera_cli.plugins.plugin_base.artifacts import LIVE_WAREHOUSE_REFRESH_CADENCE_SECONDS
from alkera_cli.plugins.plugin_base.capabilities import CapabilitySet
from alkera_cli.plugins.plugin_base.connection import Connection

if TYPE_CHECKING:
    from alkera_core.project.directory import ProjectDirectory

    from alkera_cli.plugins.plugin_base.tool import Tool


# ---------------------------------------------------------------------------
# Runtime context handed to a plugin
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """What a plugin gets at discovery / enable time — the project store +
    the workspace root it can scan for activation signals."""

    project: ProjectDirectory
    workspace_root: Path


PROBE_RESULT_ATTRIBUTE = "connection_probe"
"""Connection attribute containing the last successful authenticated probe."""


class ConnectionProbeResult(VersionedModel):
    """Durable, non-secret evidence collected by an authenticated connection probe."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    model_config = ConfigDict(frozen=True)

    service_identity: str = ""
    observed_cluster_ids: dict[str, str] = Field(default_factory=dict)
    measured_grants: dict[str, list[str]] = Field(default_factory=dict)
    endpoint_reachability: dict[str, bool] = Field(default_factory=dict)

    @field_validator("service_identity")
    @classmethod
    def _bind_service_identity(cls, value: str) -> str:
        # The persistence sink re-validates shape, so a value that skipped the
        # secret-aware capture bound still cannot ride into a URN authority.
        return rebind(value).value

    @field_validator("observed_cluster_ids")
    @classmethod
    def _bind_observed(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: bound for key, raw in value.items() if (bound := rebind(raw).value)}


def probe_result_payload(result: ConnectionProbeResult) -> dict[str, Any]:
    """Serialize only the declared non-secret probe fields from a live provider."""
    return result.model_dump(
        mode="json",
        include={
            "schema_version",
            "service_identity",
            "observed_cluster_ids",
            "measured_grants",
            "endpoint_reachability",
        },
    )


def cached_probe_result(attributes: Mapping[str, Any]) -> ConnectionProbeResult | None:
    """Decode cached probe evidence, returning ``None`` for absent or invalid payloads."""
    raw = attributes.get(PROBE_RESULT_ATTRIBUTE)
    if not isinstance(raw, Mapping):
        return None
    try:
        return ConnectionProbeResult.model_validate(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Companion contracts — minimal today; they expand IN PLACE
# ---------------------------------------------------------------------------


class ClassifyCtx(BaseModel):
    """Context a classifier gets — the connection's dialect."""

    dialect: str | None = None


class AgentDefinition(VersionedModel):
    """A subagent definition.

    The agent registry resolves these from the built-ins {worker, explore},
    ``.alkera/agents/*.md`` and programmatic providers, and spawns them.
    """

    SCHEMA_VERSION = "1.0.0"

    name: str
    description: str = ""
    prompt: str = ""
    model_slug: str | None = None
    """Pin an exact model by slug — wins over ``model_tier`` routing."""
    model_tier: str | None = None
    """A cost preference ("cheap"/"standard"/"frontier") used when no ``model_slug``
    is pinned: a sub-frontier preference routes the subagent to the cheapest
    eligible model. Plain str for forward-compat."""
    tool_scope: list[str] | Literal["read_only"] = Field(default_factory=list)
    mode: Literal["worker", "explore"] = "worker"
    harness_type: str | None = None
    can_spawn: bool = False


class SkillDef(VersionedModel):
    """Markdown knowledge + tool-usage guidance, conditionally loaded.
    Minimal by design."""

    SCHEMA_VERSION = "1.0.0"

    name: str
    description: str = ""
    body: str = ""


class RefreshSpec(BaseModel):
    """How a plugin's facts stay fresh — drives the daemon scheduler.

    ``mode="push"`` where a webhook/emitter exists, ``"poll"`` otherwise with
    a cron ``cadence``.
    """

    kind: str
    """The job kind — e.g. "kb_reenrich" | "lineage_poll"."""
    mode: Literal["push", "poll"] = "poll"
    cadence_seconds: int | None = None
    priority: int = 0
    """Higher runs first when several jobs are due in the SAME beat — so a cheap/primary
    refresh lands before a slow one. Default 0 (insertion order by job_id breaks ties)."""
    coalesce_key: str | None = None
    """Jobs sharing a non-None key never run AT ONCE: while one is in flight, the beat
    defers its due siblings (they stay scheduled, claimed once it finishes). This bounds
    how many heavy seeds run concurrently at the SCHEDULER level — a guarantee that holds
    across daemons, unlike the runtime's in-process seed lock — and keeps the Jobs UI
    honest (a deferred sibling shows "scheduled", not a fake "running" blocked on a lock).
    ``None`` (default) = no coalescing, each job runs on its own schedule."""


# ---------------------------------------------------------------------------
# The surface Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ConnectionProvider(Protocol):
    """A plugin manages MANY connections → discover returns a LIST."""

    async def discover_connections(self, ctx: WorkspaceContext) -> list[Connection]: ...


@runtime_checkable
class ProbeProvider(Protocol):
    """Authenticated add-time validation that returns reusable connection evidence."""

    async def probe(self, conn: Connection) -> ConnectionProbeResult: ...


@runtime_checkable
class CapabilityProvider(Protocol):
    """What a connection can DO."""

    def capabilities(self, conn: Connection) -> CapabilitySet: ...


@runtime_checkable
class ToolProvider(Protocol):
    """Agent tools NOT already covered by capability-backed built-ins."""

    def tools(self, conn: Connection) -> list[type[Tool[Any, Any]]]: ...


@runtime_checkable
class ContextProvider(Protocol):
    """Enrich the KB: schema, descriptions, mined history."""

    async def ingest_context(self, conn: Connection, kb: Any) -> None:
        """Write what ``conn`` knows through ``kb``, the knowledge writer the
        knowledge extension hands the provider."""
        ...

    def source_fingerprint(self, conn: Connection) -> str | None:
        """An opaque token that changes IFF this connection's source changed since the
        last KB seed — the context twin of ``LineageProvider.source_fingerprint``. The
        seed orchestrator persists it and skips re-ingesting (and re-embedding) an
        unchanged source: a no-op KB refresh costs a string compare, not a re-walk + a
        re-embed of every card. Return ``None`` to opt out (a live stream that should
        always re-poll — e.g. a warehouse query-history miner)."""
        ...


def provider_metered(provider: object) -> bool:
    """Whether a lineage/context provider queries a BILLED source — read tolerantly so a
    provider that predates the ``metered()`` method (or a stub) reads as FREE. The cost-tier
    split keys on this: only free providers run on the automatic refresh cadence."""
    fn = getattr(provider, "metered", None)
    return bool(fn()) if callable(fn) else False


@runtime_checkable
class LineageProvider(Protocol):
    """Contribute typed nodes + edges to the lineage graph."""

    async def emit_lineage(self, conn: Connection, writer: Any) -> None:
        """Emit ``conn``'s nodes and edges through ``writer``, the lineage writer
        the lineage extension hands the provider."""
        ...

    def supports_columns(self) -> bool: ...

    def seeds_on_activation(self) -> bool:
        """True if this provider is offline-safe to run ONCE at plugin activation —
        a local file or committed-manifest read, never a remote or billable query —
        so the graph is populated the moment the plugin activates. A provider that
        must query a warehouse to produce lineage returns False and stays on the
        refresh path over explicitly-added connections (so activation never fires a
        warehouse query)."""
        ...

    def source_fingerprint(self, conn: Connection) -> str | None:
        """An opaque token that changes IFF this connection's source changed since the
        last seed (e.g. a manifest's mtime+size via ``artifact_fingerprint``, a
        warehouse's max query-start-time). The engine PERSISTS it and skips re-emitting
        a provider whose token is unchanged — a restart-proof generalization of the
        in-memory mtime gate, so a daemon restart on an unchanged project doesn't re-emit
        (and re-accumulate) the whole graph. Return ``None`` to opt out (always re-emit) —
        the right default for a live source with no cheap change-token."""
        ...

    def emits_complete_snapshot(self) -> bool:
        """True if ``emit_lineage`` produces a COMPLETE authoritative snapshot (a dbt
        manifest, a warehouse introspection) — so the engine may exact-reconcile vanished
        facts. False for an append-only log STREAM (a query-log poll) whose absence of a
        fact is NOT proof of deletion; those keep watermark + TTL decay, never snapshot-
        reconcile. Mutually exclusive with :meth:`supports_delta` (a stream isn't a
        snapshot). Default True — the file/introspection producer is the common case."""
        ...

    def supports_delta(self) -> bool:
        """True if this provider can emit only what CHANGED since a cursor via
        :meth:`emit_delta`, instead of re-emitting the whole graph each pass. Default
        False — a provider re-emits in full (cheap when gated by ``source_fingerprint``,
        which already skips an unchanged source). Opt in only when the source exposes a
        cheap changelog the full re-emit would otherwise waste work re-reading."""
        ...

    def rescan_cost(self) -> int:
        """A relative ADVISORY hint (>=0) for how expensive a full re-emit is — a heavy
        manifest-parse + column pass returns a larger number, a tiny file returns 1
        (the default). Declared by providers and available to scheduling heuristics (e.g.
        to prefer a cheap refresh when several are due); not yet consumed by the scheduler,
        which orders on the explicit ``RefreshSpec.priority``."""
        ...

    async def emit_delta(self, conn: Connection, writer: Any, *, since: str | None) -> str | None:
        """Emit ONLY the facts changed since the opaque ``since`` cursor
        (``writer.checkpoint_cursor`` from the last pass; ``None`` on the first), and
        RETURN the new cursor the engine persists via ``writer.checkpoint``. Called only
        when :meth:`supports_delta` is True. The default raises ``NotImplementedError`` so
        the orchestrator falls back to a full :meth:`emit_lineage` — a provider that
        advertises delta MUST override this."""
        raise NotImplementedError


@runtime_checkable
class ClassifierProvider(Protocol):
    """Raw tool call → ``ActionDescriptor`` for the permission gate."""

    capability: str

    def classify(
        self, tool_name: str, tool_input: dict[str, Any], ctx: ClassifyCtx
    ) -> ActionDescriptor: ...


@runtime_checkable
class AgentProvider(Protocol):
    """Contribute ``AgentDefinition``s for subagents."""

    def agents(self) -> list[AgentDefinition]: ...


@runtime_checkable
class SkillProvider(Protocol):
    """Markdown skills / knowledge, activation-scoped (not always-on)."""

    def skills(self) -> list[SkillDef]: ...


@runtime_checkable
class RefreshProvider(Protocol):
    """How this plugin's facts stay fresh — drives the daemon scheduler."""

    def refresh_spec(self, conn: Connection) -> RefreshSpec: ...


class PollRefreshProvider:
    """Poll refresh at a fixed cadence; a connector declares only its job kind."""

    KIND: ClassVar[str]
    CADENCE_SECONDS: ClassVar[int] = LIVE_WAREHOUSE_REFRESH_CADENCE_SECONDS
    COALESCE_KEY: ClassVar[str | None] = None

    def refresh_spec(self, conn: Connection) -> RefreshSpec:
        del conn
        return RefreshSpec(
            kind=self.KIND,
            mode="poll",
            cadence_seconds=self.CADENCE_SECONDS,
            coalesce_key=self.COALESCE_KEY,
        )


class EnvConnectionProvider:
    """Discovery that reads only the environment; a connector declares its finder."""

    DISCOVER: ClassVar[Callable[[], list[Connection]]]

    async def discover_connections(self, ctx: WorkspaceContext) -> list[Connection]:
        del ctx
        return type(self).DISCOVER()


# Effect is re-exported for convenience (classifiers map operations → Effect).
__all__ = [
    "PROBE_RESULT_ATTRIBUTE",
    "AgentDefinition",
    "AgentProvider",
    "CapabilityProvider",
    "ClassifierProvider",
    "ClassifyCtx",
    "ConnectionProbeResult",
    "ConnectionProvider",
    "ContextProvider",
    "Effect",
    "LineageProvider",
    "ProbeProvider",
    "RefreshProvider",
    "RefreshSpec",
    "SkillDef",
    "SkillProvider",
    "ToolProvider",
    "WorkspaceContext",
    "cached_probe_result",
    "probe_result_payload",
    "provider_metered",
]
