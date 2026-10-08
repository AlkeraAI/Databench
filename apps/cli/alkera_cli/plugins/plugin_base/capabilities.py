"""The capability hierarchy.

Leaf capability ABCs grouped under abstract groups. A ``Connection``'s
live abilities are exposed as a typed ``CapabilitySet``; tools / context /
lineage **consume** capabilities rather than re-implementing I/O — so
"add a warehouse" = implement the capability set, and the built-in tools +
context + lineage light up for free.

These are plain ABCs (behavioral, transient) — NOT ``VersionedModel`` — per
the boundary rule: only things written to ``.alkera/`` are versioned.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from alkera_cli.contracts.tool_types import (
    URN,
    AccessEvent,
    ActionDescriptor,
    CapToken,
    CostActual,
    CostEstimate,
    Effect,
    QueryResult,
    RelationMeta,
    TableMeta,
    Watermark,
)

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.connection import Connection


class Capability(ABC):
    """A leaf capability — one coherent ability a connection can perform.

    Subclasses set a stable ``key`` ClassVar used as the registry key and
    in audit logs.
    """

    key: ClassVar[str]


C = TypeVar("C", bound=Capability)


# ---------------------------------------------------------------------------
# Leaf capabilities
# ---------------------------------------------------------------------------


class RunSQLCapability(Capability):
    """Execute SQL against the connection's data system."""

    key: ClassVar[str] = "run_sql"

    @property
    def read_only_reason(self) -> str:
        """Why this connection cannot be WRITTEN at all, or ``""`` when it can.

        A non-empty reason is a fact about the connection — a wire with no write
        path, a role that only reads — and holds in every permission mode: the
        ``sql.query`` tool refuses a mutating statement against it with this reason
        before any gate or reader is consulted. The default says nothing, so a
        capability that can write needs no override."""
        return ""

    @abstractmethod
    async def run(
        self,
        sql: str,
        *,
        effect: Effect,
        cap_token: CapToken | None,
        limit: int | None,
        params: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        """Run ``sql``. Reads run in a read-only transaction; write/destroy/
        egress require a valid ``cap_token`` (enforced at the connector).
        ``params`` are bound values for the placeholders ``sql`` carries
        (``%(name)s``, ``{name:Type}``, …): handed to the driver beside the
        statement, never rendered into it. A connector without a binding path
        refuses a non-empty ``params`` rather than interpolating."""


class IntrospectSchemaCapability(Capability):
    """List + describe the relations a connection exposes."""

    key: ClassVar[str] = "introspect_schema"

    @abstractmethod
    async def list_relations(self) -> list[RelationMeta]: ...

    @abstractmethod
    async def describe(self, urn: URN) -> TableMeta: ...

    async def describe_many(self, urns: Sequence[URN]) -> dict[URN, TableMeta]:
        """Describe a BATCH of relations — what a caller that is about to describe a whole
        warehouse should call.

        The default is one :meth:`describe` per urn, which is what an introspection with no
        cheaper bulk read (a REST metadata API, a manifest, a local file) can honestly do.
        An engine whose catalog answers a whole namespace in one query overrides this so a
        260-relation warehouse costs a handful of round trips instead of 260 — the schema
        cards a box builds on its first chat are exactly that caller.

        A urn missing from the result never happened: every requested urn is a key, with an
        empty-column :class:`TableMeta` when the catalog knows nothing about it."""
        return {urn: await self.describe(urn) for urn in urns}


class QueryHistoryCapability(Capability):
    """Mine recent access events — the SHARED lineage + context source."""

    key: ClassVar[str] = "query_history"

    @abstractmethod
    async def recent_access(self, since: Watermark) -> list[AccessEvent]: ...


class EstimateSQLCostCapability(Capability):
    """Estimate (and, post-execution, settle) the cost of an action — BQ dry-run
    bytes / Snowflake credits."""

    key: ClassVar[str] = "estimate_sql_cost"

    @abstractmethod
    async def estimate(self, descriptor: ActionDescriptor) -> CostEstimate: ...

    async def settle(
        self, descriptor: ActionDescriptor, result: QueryResult, estimate: CostEstimate
    ) -> CostActual:
        """Refine the estimate into the actual cost after execution. Default: the
        estimate stands (no post-hoc source). A connector with a QUERY_HISTORY
        settle (Snowflake credits) overrides to read the real consumption."""
        return CostActual(usd_amount=estimate.usd_amount, wallet_currency=estimate.wallet_currency)


class CloneCapability(Capability):
    """Sandbox DDL for the gate's ephemeral per-PR schemas (the sandbox tier).

    Gate-owned, never agent-facing: no tool consumes it, so it is reachable
    only from the gate's execution tier. It runs DDL directly under the
    connection's credential; in CI that identity is scoped to CREATE on a
    dedicated CI database, so the blast radius stays the sandbox database
    regardless of what this code does. Implementations validate every
    identifier they interpolate; the sandbox planner mints them, never free
    text.
    """

    key: ClassVar[str] = "clone"

    @abstractmethod
    async def create_schema(self, database: str, schema: str) -> None:
        """Create the sandbox schema (idempotent)."""

    @abstractmethod
    async def create_table_as(
        self, database: str, schema: str, table: str, select_sql: str
    ) -> None:
        """Create-or-replace a sandbox table from ``select_sql`` (the zero-row
        default build proves the proposed SQL compiles and its column shape,
        at no scan cost)."""

    @abstractmethod
    async def clone_table(self, database: str, schema: str, table: str, source_name: str) -> None:
        """Zero-copy clone the dotted physical ``source_name`` into the
        sandbox, where the engine supports it (Snowflake CLONE, BigQuery table
        clones, Databricks shallow clones)."""

    @abstractmethod
    async def drop_schema(self, database: str, schema: str) -> None:
        """Drop the sandbox schema and everything in it (idempotent)."""

    @abstractmethod
    async def list_schemas(self, database: str, prefix: str) -> list[str]:
        """Schema names in ``database`` starting with ``prefix`` — the
        janitor's sweep source."""


class IntegrationSdkCapability(Capability):
    """Expose a live, authenticated vendor SDK client for a connection so the agent
    can drive operations that have no native tool — the universal escape hatch behind
    the ``call_integration_sdk`` tool. That tool binds the object this capability's
    factory returns as the Python variable ``connection`` and runs model-provided
    Python against it (Databricks ``WorkspaceClient``, Snowflake ``SnowflakeConnection``
    / ``snowflake.core.Root``, …), covering "do X with Databricks/Snowflake" whenever no
    typed tool covers X.

    Instance-configured — a plugin adds one per connection's ``CapabilitySet``:
    ``caps.add(IntegrationSdkCapability(client_factory=…, client_label=…, sdk_modules=…))``.

    ``client_factory`` is a dotted ``"module:function"`` path to a
    ``(Connection) -> object`` factory that MUST be importable in a fresh interpreter:
    ``call_integration_sdk`` runs the model's code in a CHILD process (arbitrary code
    must never be able to hang or crash the daemon), so the child re-imports the factory
    and re-resolves the connection's credentials at its own I/O boundary — no secret ever
    crosses the process boundary (the serialized ``Connection`` carries only a
    ``credential_ref``).
    """

    key: ClassVar[str] = "integration_sdk"

    def __init__(
        self,
        *,
        client_factory: str,
        client_label: str,
        sdk_modules: tuple[str, ...] = (),
        docs: dict[str, str] | None = None,
        version_dist: str = "",
        distributions: tuple[str, ...] = (),
    ) -> None:
        if ":" not in client_factory:
            raise ValueError(
                f"client_factory must be a dotted 'module:function' path, got {client_factory!r}"
            )
        self.client_factory = client_factory
        """Dotted ``module:function`` path to the ``(Connection) -> object`` client factory."""
        self.client_label = client_label
        """The bound client's type name (e.g. ``databricks.sdk.WorkspaceClient``), shown to
        the model so it knows what ``connection`` is."""
        self.sdk_modules = tuple(sdk_modules)
        """Primary importable SDK modules the model may use (surfaced in the tool's env
        report and description)."""
        self.docs = dict(docs or {})
        """Reference doc URLs (label → url) surfaced by ``mode='env'`` so the agent can fetch
        the RIGHT page instead of guessing. A ``{version}`` token in a url is filled from
        ``version_dist``'s INSTALLED version (pin source/docs to the runtime — the only doc
        guaranteed to match). Plugin-DECLARED, so the shape stays generic (each connector picks
        its own labels/urls) rather than overfit to one vendor."""
        self.version_dist = version_dist
        """The distribution whose installed version fills ``{version}`` in ``docs`` URLs (e.g.
        ``"databricks-sdk"``). Empty → ``{version}`` is left as-is."""
        self.distributions = tuple(distributions)
        """This connector's OWN installed distributions to surface (with their versions) in
        the tool's ``mode='env'`` package report, on top of the shared base list — so a
        connection's report names the packages its SDK actually ships as."""

    def open_client(self, conn: Connection) -> object:
        """Instantiate the authenticated SDK client for ``conn`` IN-PROCESS.

        Used by tests and any in-process caller; the ``call_integration_sdk`` child
        resolves the SAME ``client_factory`` by dotted path so the two paths agree."""
        module_path, _, attr = self.client_factory.partition(":")
        module = importlib.import_module(module_path)
        factory = getattr(module, attr)
        return factory(conn)


# ---------------------------------------------------------------------------
# Capability groups
# ---------------------------------------------------------------------------


class DatabaseCapabilityGroup(ABC):
    """An abstract grouping a warehouse connector satisfies.

    A connector exposing the group lights up the built-in ``sql.query`` /
    ``sql.schema`` tools for free. ``history`` is optional — a warehouse
    without query logs simply returns ``None`` and degrades gracefully
    (no query-log lineage).
    """

    @property
    @abstractmethod
    def sql(self) -> RunSQLCapability: ...

    @property
    @abstractmethod
    def schema(self) -> IntrospectSchemaCapability: ...

    @property
    @abstractmethod
    def cost(self) -> EstimateSQLCostCapability: ...

    @property
    @abstractmethod
    def history(self) -> QueryHistoryCapability | None: ...


# ---------------------------------------------------------------------------
# CapabilitySet — typed registry keyed by capability class
# ---------------------------------------------------------------------------


class CapabilitySet:
    """A connection's live capabilities, looked up by capability class.

    ``conn_caps.get(RunSQLCapability)`` returns the typed capability or
    ``None``. Keyed by the concrete leaf ABC the impl subclasses, so a
    connector registers each capability it provides under its leaf type.
    """

    def __init__(self, capabilities: dict[type[Capability], Capability] | None = None) -> None:
        self._by_type: dict[type[Capability], Capability] = dict(capabilities or {})

    def add(self, cap: Capability) -> None:
        """Register ``cap`` under each leaf ``Capability`` ABC it implements."""
        for base in type(cap).__mro__:
            if base in (Capability, object):
                continue
            if isinstance(base, type) and issubclass(base, Capability):
                self._by_type.setdefault(base, cap)

    def get(self, cap: type[C]) -> C | None:
        """Return the registered capability of type ``cap``, or ``None``."""
        found = self._by_type.get(cap)
        if found is None:
            return None
        # Sound: we only ever store an instance under a type it subclasses.
        return cast(C, found)

    def has(self, cap: type[Capability]) -> bool:
        return cap in self._by_type

    def keys(self) -> list[type[Capability]]:
        return list(self._by_type)


__all__ = [
    "Capability",
    "CapabilitySet",
    "CloneCapability",
    "DatabaseCapabilityGroup",
    "EstimateSQLCostCapability",
    "IntegrationSdkCapability",
    "IntrospectSchemaCapability",
    "QueryHistoryCapability",
    "RunSQLCapability",
]
