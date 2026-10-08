"""Shared primitive types for the plugin framework.

The connection *primitives* (``Effect``, ``Environment``, ``CredentialMode``,
``ConnectionOrigin``, ``CredentialRef``, ``environment_for``) moved to
the connector descriptors package (the shared driver-free package the backend + worker also
import) and are re-exported here unchanged, so every existing
``contracts.tool_types`` import keeps working.

What REMAINS here is the agent/policy machinery — meaningless without the
harness around it, so it deliberately stays in the CLI:

- ``SurfaceKind`` — the surfaces a plugin can declare it provides.
- Small transient result models the capability ABCs and the connector return.
- ``ActionDescriptor`` / ``CapToken`` — the gated-action subject + the
  capability token the broker mints on approval.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, NewType

from alkera_core.connectors.primitives import (
    ConnectionOrigin,
    CredentialMode,
    CredentialRef,
    Effect,
    Environment,
    environment_for,
)
from alkera_core.versioning import VersionedModel
from pydantic import BaseModel, Field


class SurfaceKind(StrEnum):
    """The surfaces a plugin may declare it provides.

    Declared in the manifest so heavy dependencies import lazily on
    activation rather than at load.
    """

    CONNECTION = "connection"
    CAPABILITY = "capability"
    TOOL = "tool"
    CONTEXT = "context"
    LINEAGE = "lineage"
    CLASSIFIER = "classifier"
    AGENT = "agent"
    SKILL = "skill"
    REFRESH = "refresh"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

URN = NewType("URN", str)
"""Canonical cross-plugin asset identity (lineage/context join key).

A connection mints URNs in its own ``urn_namespace`` so a dbt model and the
warehouse table it materializes into resolve to one another by URN even
though they're distinct nodes under distinct plugins."""


# ---------------------------------------------------------------------------
# Transient result models (NOT persisted → plain BaseModel)
# ---------------------------------------------------------------------------


class QueryResult(BaseModel):
    """A connector/capability query result — raw columns + rows.

    The agent-facing tool wraps this into a preview + blob handle;
    this is the low-level shape capabilities return.
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    query_id: str | None = None
    """The warehouse's id for the executed query (Snowflake ``sfqid``) — lets the
    cost model settle the REAL consumption from QUERY_HISTORY."""
    engine: str = ""
    """The engine that ran the statement (the SQL spec's ``system``) — filled by
    the connector at execution so the receipt names what actually answered.
    Empty from a capability that does not know."""
    executed_at: datetime | None = None
    """When the statement ran (UTC), stamped by the connector at execution."""
    duration_ms: int | None = None
    """Wall-clock milliseconds from opening the driver session to the last row
    fetched, measured by the connector. ``None`` from a capability that does
    not measure."""


class ColumnMeta(BaseModel):
    name: str
    data_type: str = ""
    nullable: bool = True


class RelationMeta(BaseModel):
    """A table/view as seen by schema introspection."""

    urn: URN
    name: str
    kind: str = "table"
    """"table" | "view" | "materialized_view" | "dynamic_table" | …"""


class TableMeta(BaseModel):
    """Full table description (columns + attributes)."""

    urn: URN
    name: str
    columns: list[ColumnMeta] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


Watermark = NewType("Watermark", int)
"""A monotonically-advancing cursor (e.g. last query_start_time scanned)."""


class AccessEvent(BaseModel):
    """One warehouse access record (from ACCESS_HISTORY / QUERY_HISTORY).

    Shared source for BOTH lineage ``LOG_OBSERVED`` edges AND context
    query-history exemplars, so both read one record."""

    query_id: str
    sql: str = ""
    objects_accessed: list[URN] = Field(default_factory=list)
    objects_modified: list[URN] = Field(default_factory=list)
    started_at: int = 0
    actor: str | None = None


class CostEstimate(BaseModel):
    """Pre-execution cost estimate."""

    usd_amount: float = 0.0
    bytes_scanned: int | None = None
    rows_scanned: int | None = None
    wallet_currency: str = "usd"
    """"usd" | "credits" | "dbus" — the connector's native unit."""


class CostActual(BaseModel):
    """Post-execution actual cost — the ``settle`` half of the cost model.
    Mirrors :class:`CostEstimate`; a transient result, never
    persisted on its own (it's folded into a ``CostLedgerEntry``)."""

    usd_amount: float = 0.0
    bytes_scanned: int | None = None
    rows_scanned: int | None = None
    wallet_currency: str = "usd"


# ---------------------------------------------------------------------------
# The gated-action subject + the capability token
# ---------------------------------------------------------------------------

Confidence = Literal["exact", "heuristic", "unknown"]


class ResourceRef(BaseModel):
    """A target an action touches — a table/schema/file, optionally scoped to a
    connection + environment (so the policy can reason about prod targets)."""

    kind: str = "object"
    """"table" | "schema" | "database" | "file" | …"""
    name: str = ""
    connection: str | None = None
    environment: str | None = None


class StatementInfo(BaseModel):
    """Per-statement classification (multi-statement SQL → one each)."""

    effect: Effect = Effect.READ
    operation: str = ""
    """e.g. "select" | "drop_table" | "update" | "copy_into"."""
    targets: list[ResourceRef] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    """The columns the statement names as changed (an ALTER ... DROP COLUMN). The
    gate resolves impact at this grain, since a dropped column breaks its own
    consumers rather than the whole table's."""
    has_where: bool = True
    """For UPDATE/DELETE: a missing (or trivially-true) WHERE escalates to DESTROY."""
    dangerous_functions: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    heuristic: bool = False
    """True when the effect was inferred from a Command verb / dynamic SQL rather
    than a fully-parsed statement — the descriptor's confidence drops to
    ``heuristic`` so the policy errs toward prompting."""


class ActionDescriptor(VersionedModel):
    """The ``(capability, effect)`` subject of a gated action.

    The single structure the policy gate + the connector chokepoint reason over.
    ``confidence="unknown"`` (unparseable SQL, no effect metadata) is treated as
    at least ``write`` and prompted (fail-safe). Persisted only as
    ``PermissionRequest.subject``, so its lineage fixture rides there.
    """

    # 1.1.0: ``effect`` admits ``exec`` (a statement that runs a program or
    # reaches the database host's filesystem). Additive: a 1.0.0 reader that
    # meets ``exec`` fails validation and falls back, which is the fail-closed
    # side; a 1.1.0 reader loads every 1.0.0 record unchanged.
    # 1.2.0: ``scope`` (additive, defaulted to ``operation``).
    SCHEMA_VERSION = "1.2.0"

    capability: str = ""
    """"sql" | "shell" | "dbt" | …"""
    effect: Effect = Effect.READ
    """The MAX effect across all statements — the load-bearing policy axis."""
    operation: str = ""
    targets: list[ResourceRef] = Field(default_factory=list)
    raw: str | None = None
    """The command/SQL text — for pattern matching + the loud approval prompt."""
    statements: list[StatementInfo] = Field(default_factory=list)
    cost_estimate: CostEstimate | None = None
    reasons: list[str] = Field(default_factory=list)
    classifier: str = ""
    """Which classifier produced this (audit)."""
    confidence: Confidence = "exact"
    scope: Literal["operation", "command"] = "operation"
    """What a standing answer to this action records. ``operation``: the classified
    ``(capability, operation)`` family, wider than the one invocation (approving
    ``git status`` stops every ``git status`` being asked). ``command``: only this
    exact text — a compound shell command (a pipe, a list, a loop, a subshell, a
    write redirect) is several actions and has no one family to name."""

    def fingerprint(self) -> str:
        """A stable hash binding a ``CapToken`` to THIS action (capability +
        effect + operation + targets + raw). The connector recomputes it to
        reject a token minted for a different action."""
        payload = json.dumps(
            {
                "capability": self.capability,
                "effect": str(self.effect),
                "operation": self.operation,
                "raw": self.raw or "",
                "targets": sorted(f"{t.kind}:{t.connection}:{t.name}" for t in self.targets),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class CapToken(VersionedModel):
    """A capability token the broker mints on approval; the connector refuses a
    write/destroy/egress without a valid, unexpired one bound to THIS action
    (the trust boundary: even a dynamically-built tool that hand-rolls a
    DELETE is refused). In-process + per-session, so not persisted (no fixture)."""

    SCHEMA_VERSION = "1.0.0"

    descriptor_fingerprint: str = ""
    expires_at: float = 0.0
    """Unix epoch seconds; 0 = already expired."""

    @classmethod
    def mint(cls, descriptor: ActionDescriptor, *, ttl_seconds: float = 300.0) -> CapToken:
        return cls(
            descriptor_fingerprint=descriptor.fingerprint(),
            expires_at=time.time() + ttl_seconds,
        )

    def refreshed(self, *, ttl_seconds: float = 300.0) -> CapToken:
        """A copy whose TTL restarts now. The TTL bounds the gap between the LAST
        human approval and execution — but the permission gate (which mints) can
        be followed by a cost prompt the human deliberates on for longer than the
        TTL, expiring a token the human just approved twice. The gating
        chokepoint calls this after its final blocking step, immediately before
        execution; nothing else should."""
        return self.model_copy(update={"expires_at": time.time() + ttl_seconds})

    def authorizes(self, descriptor: ActionDescriptor, *, now: float | None = None) -> bool:
        """True iff this token was minted for ``descriptor`` and hasn't expired."""
        current = time.time() if now is None else now
        return self.descriptor_fingerprint == descriptor.fingerprint() and current < self.expires_at


__all__ = [
    "URN",
    "AccessEvent",
    "ActionDescriptor",
    "CapToken",
    "ColumnMeta",
    "Confidence",
    "ConnectionOrigin",
    "CostActual",
    "CostEstimate",
    "CredentialMode",
    "CredentialRef",
    "Effect",
    "Environment",
    "QueryResult",
    "RelationMeta",
    "ResourceRef",
    "StatementInfo",
    "SurfaceKind",
    "TableMeta",
    "Watermark",
    "environment_for",
]
