"""Generic ``information_schema``-backed schema introspection for any SQL engine.

Lists + describes relations through the capability-gated :class:`DbapiConnector`, minting
per-system URNs via :func:`warehouse_relation_urn` (with the registered
:func:`relation_authority`) so a warehouse table and the dbt model that materializes it
fold onto ONE node — the same identity contract the dbt providers use.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import structlog
from alkera_core.extensions import ExtensionError, ExtensionPoint

from alkera_cli.contracts.tool_types import (
    URN,
    ColumnMeta,
    Effect,
    RelationMeta,
    TableMeta,
)
from alkera_cli.plugins.plugin_base.capabilities import IntrospectSchemaCapability
from alkera_cli.plugins.plugin_base.sql.foreign_keys import ForeignKeySnapshot
from alkera_cli.plugins.plugin_base.urns import dataset_relation_fqn, warehouse_relation_urn

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.sql.connector import DbapiConnector
    from alkera_cli.plugins.plugin_base.sql.spec import SqlEngineSpec

logger = structlog.get_logger(__name__)

#: ``(system, connection attributes) -> authority``: the dev-versus-prod part of a
#: relation's URN (an account, a ``host:port``), or ``""`` for none.
RelationAuthority = Callable[[str, Mapping[str, Any]], str]

#: The authority a distribution names relations with, so its warehouse tables fold
#: with the dbt models that build them. At most one registers; with none, relation
#: URNs carry no authority.
RELATION_AUTHORITIES: ExtensionPoint[RelationAuthority] = ExtensionPoint("sql.relation_authority")


def relation_authority(
    system: str,
    attributes: Mapping[str, Any],
    point: ExtensionPoint[RelationAuthority] = RELATION_AUTHORITIES,
) -> str:
    """The registered authority for a connection's relations, or ``""``."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} relation authorities registered on {point.name!r}; "
            "relation names take exactly one"
        )
    return registered[0](system, attributes) if registered else ""


#: Cap on rows pulled from information_schema in one introspection query.
_INTROSPECT_LIMIT = 50_000

#: Cap on rows pulled by ONE bulk namespace read — it carries every column of every
#: relation the statement names, so it needs far more headroom than a single relation's
#: describe.
_BULK_INTROSPECT_LIMIT = 500_000

#: Relation names bound into ONE bulk statement. The bulk read is scoped to the relations
#: the caller actually asked about, so a namespace holding more than this many of them is
#: split across several statements — an unbounded ``IN`` list outgrows an engine's parser,
#: and an unscoped read would pull the whole namespace's columns (every relation past the
#: caller's own cap included) only to throw them away.
_BULK_NAME_CHUNK = 200

#: Cap on foreign-key rows — one row per constrained COLUMN, so a wide schema with
#: composite keys still fits comfortably under it.
_FK_LIMIT = 200_000


async def introspect_foreign_keys(
    spec: SqlEngineSpec, connector: DbapiConnector, *, limit: int = _FK_LIMIT
) -> ForeignKeySnapshot:
    """The engine's declared foreign keys, or an UNCAPTURED snapshot.

    Degrades silently by design: an engine with no FK concept, no catalog view, or a role
    without the privilege to read one must never fail a seed. Every non-authoritative
    outcome (no query, a driver error, a truncated read) returns ``captured=False``, which
    keeps the caller from exact-GC'ing real edges against a snapshot that isn't complete.
    """
    if not spec.foreign_keys_sql:
        return ForeignKeySnapshot()
    try:
        res = await connector.execute(
            spec.foreign_keys_sql, effect=Effect.READ, cap_token=None, limit=limit
        )
    except Exception as exc:
        logger.debug(
            "sql_lineage.foreign_keys_unavailable",
            system=spec.system,
            connection=connector.connection.handle,
            error=str(exc),
        )
        return ForeignKeySnapshot()
    if res.truncated:
        logger.warning(
            "sql_lineage.foreign_keys_truncated",
            system=spec.system,
            connection=connector.connection.handle,
        )
        return ForeignKeySnapshot()
    return ForeignKeySnapshot(constraints=tuple(spec.foreign_keys_parser(res.rows)), captured=True)


def normalize_kind(raw: object) -> str:
    """An ``information_schema.tables.table_type`` → a lineage relation kind. ``BASE
    TABLE`` → ``table``; anything containing ``view`` → ``view`` (``materialized_view``
    when it says so); ``DYNAMIC TABLE`` (the Snowflake spec's IS_DYNAMIC rewrite) →
    ``dynamic_table``."""
    k = str(raw or "table").strip().lower()
    if "dynamic" in k:
        return "dynamic_table"
    if "view" in k:
        return "materialized_view" if "materialized" in k else "view"
    return "table"


class TruncatedIntrospectError(Exception):
    """A bulk namespace read hit the row cap — the columns it returned are incomplete, so
    the relations in it must not be carded from them."""


class GenericSqlIntrospect(IntrospectSchemaCapability):
    """List + describe a SQL engine's relations from ``information_schema``."""

    def __init__(self, connector: DbapiConnector) -> None:
        self._connector = connector
        self._spec = connector.spec
        # The dev≠prod authority (account / host:port) from the connection config — the
        # same value the dbt provider derives, so the URNs fold.
        self._authority = relation_authority(self._spec.system, connector.connection.attributes)

    async def list_relations(self) -> list[RelationMeta]:
        res = await self._connector.execute(
            self._spec.relations_sql, effect=Effect.READ, cap_token=None, limit=_INTROSPECT_LIMIT
        )
        out: list[RelationMeta] = []
        for cat, schema, name, kind in res.rows:
            if not self._spec.keep_relation(str(cat), str(schema)):
                continue
            urn = warehouse_relation_urn(
                self._spec.system,
                container=str(cat),
                schema=str(schema),
                table=str(name),
                authority=self._authority,
            )
            # Join only the non-empty parts so a 2-level engine (empty schema slot) reads
            # "database.table", not "database..table".
            display = ".".join(p for p in (str(cat), str(schema), str(name)) if p)
            out.append(RelationMeta(urn=URN(urn), name=display, kind=normalize_kind(kind)))
        return out

    @staticmethod
    def _namespace_clauses(parts: Sequence[str]) -> list[str]:
        """The catalog/schema predicates for the namespace ``parts`` names — everything
        LEFT of the relation. A 2-level engine (the database in the catalog slot, an empty
        sub-schema) mints 2 parts, so its database is matched on ``table_schema``, which is
        where its ``information_schema`` actually keeps it."""
        clauses: list[str] = []
        if len(parts) >= 2:
            clauses.append(f"table_schema = '{parts[-2]}'")
        if len(parts) >= 3:
            clauses.append(f"table_catalog = '{parts[-3]}'")
        return clauses

    async def describe(self, urn: URN) -> TableMeta:
        # The SQL-visible relation parts (already case-folded per system at mint, so they
        # match the engine's stored case). Scope the column lookup by ALL of
        # catalog+schema+table so a name present in several schemas doesn't merge columns.
        parts = dataset_relation_fqn(str(urn))
        rel = ".".join(parts)
        if not parts:
            return TableMeta(urn=urn, name=str(urn).rsplit("/", 1)[-1])
        clauses = [f"table_name = '{parts[-1]}'", *self._namespace_clauses(parts)]
        # The parts are de-quoted identifiers (urns._STRIP removes quotes/specials), so this
        # interpolation can't break out of the literal — the established pattern (Snowflake).
        query = f"{self._spec.columns_select} WHERE {' AND '.join(clauses)}"
        res = await self._connector.execute(
            query, effect=Effect.READ, cap_token=None, limit=_INTROSPECT_LIMIT
        )
        cols = [
            ColumnMeta(name=str(n), data_type=str(t), nullable=str(nul).strip().upper() == "YES")
            for n, t, nul in res.rows
        ]
        return TableMeta(urn=urn, name=rel, columns=cols)

    async def describe_many(self, urns: Sequence[URN]) -> dict[URN, TableMeta]:
        """Every relation's columns in ONE query per NAMESPACE (catalog+schema, or the
        database on a 2-level engine) instead of one per relation.

        This is what makes the first schema-card pass on a real warehouse affordable: 260
        relations spread over 5 schemas cost 5 round trips, not 260. Each namespace read is
        the same projection ``describe`` uses, so the cards are byte-identical to the ones
        the per-relation path produced — only the number of trips changes. The reads run
        concurrently under ``spec.metadata_concurrency``, which is a SESSION budget (the
        connector opens one driver session per statement), and a namespace whose read fails
        propagates: a caller reconciling cards must never read a partial catalog as
        "these relations are gone"."""
        by_namespace: dict[tuple[str, ...], list[tuple[URN, tuple[str, ...]]]] = {}
        out: dict[URN, TableMeta] = {}
        for urn in urns:
            parts = tuple(dataset_relation_fqn(str(urn)))
            if len(parts) < 2:
                # Nothing to group on (a bare relation name) — the single describe is
                # already the cheapest honest read for it.
                out[urn] = await self.describe(urn)
                continue
            by_namespace.setdefault(parts[:-1], []).append((urn, parts))

        limit = max(1, self._spec.metadata_concurrency)
        gate = asyncio.Semaphore(limit)

        async def _one(namespace: tuple[str, ...]) -> dict[str, list[ColumnMeta]]:
            async with gate:
                return await self._namespace_columns(
                    namespace, [parts[-1] for _, parts in by_namespace[namespace]]
                )

        namespaces = list(by_namespace)
        results = await asyncio.gather(*(_one(ns) for ns in namespaces))
        for namespace, columns in zip(namespaces, results, strict=True):
            for urn, parts in by_namespace[namespace]:
                out[urn] = TableMeta(
                    urn=urn, name=".".join(parts), columns=columns.get(parts[-1], [])
                )
        return out

    async def _namespace_columns(
        self, namespace: tuple[str, ...], names: Sequence[str]
    ) -> dict[str, list[ColumnMeta]]:
        """The NAMED relations' columns in one namespace, keyed by relation name.

        Bound to the names the caller asked about, in chunks: an unscoped namespace read
        carries every relation in the schema, so a catalog with far more relations than the
        caller cards (the per-pass cap exists for exactly that shape) pays for — and can
        blow the row cap on — columns nobody asked for."""
        namespace_clauses = self._namespace_clauses((*namespace, ""))
        by_relation: dict[str, list[ColumnMeta]] = {}
        wanted = sorted(set(names))
        for start in range(0, len(wanted), _BULK_NAME_CHUNK):
            chunk = wanted[start : start + _BULK_NAME_CHUNK]
            # De-quoted identifiers (urns._STRIP removes quotes/specials), so the literal
            # list can't break out — the same property `describe`'s interpolation relies on.
            in_list = ", ".join(f"'{name}'" for name in chunk)
            clauses = [f"table_name IN ({in_list})", *namespace_clauses]
            query = (
                f"{self._spec.columns_bulk_select} WHERE {' AND '.join(clauses)}"
                f"{self._spec.columns_bulk_order_by}"
            )
            res = await self._connector.execute(
                query, effect=Effect.READ, cap_token=None, limit=_BULK_INTROSPECT_LIMIT
            )
            if res.truncated:
                # A truncated namespace read would hand back relations with HALF their
                # columns, and a half-described relation is a card that lies. Refuse it: the
                # caller's failure path keeps the cards it already has.
                raise TruncatedIntrospectError(
                    f"{self._spec.system}: the columns read for "
                    f"{'.'.join(namespace)} exceeded {_BULK_INTROSPECT_LIMIT} rows"
                )
            for name, col, dtype, nullable in res.rows:
                by_relation.setdefault(str(name), []).append(
                    ColumnMeta(
                        name=str(col),
                        data_type=str(dtype),
                        nullable=str(nullable).strip().upper() == "YES",
                    )
                )
        return by_relation


__all__ = [
    "GenericSqlIntrospect",
    "TruncatedIntrospectError",
    "introspect_foreign_keys",
    "normalize_kind",
]
