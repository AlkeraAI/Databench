"""The per-engine SQL spec — the small set of knobs that differ between PEP-249 SQL
warehouses (Postgres / Redshift / MySQL / ClickHouse / DuckDB / Snowflake / …).

Everything generic — the capability gate, the read-only transaction, ``LIMIT``
truncation, ``information_schema`` introspection, view-definition lineage — lives in
the base. A connector supplies only this spec: its system name (the URN scheme + the
``warehouse_authority`` key), the sqlglot dialect for classification/parsing, a
``connect`` callable that resolves the secret and opens a driver connection, and the
read-only mechanism. The default ``information_schema`` SQL is standard across these
engines; override a field only where an engine genuinely differs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from alkera_core.connectors.read_only import ReadOnlyEnforcement

from alkera_cli.plugins.plugin_base.connection import Connection
from alkera_cli.plugins.plugin_base.sql.foreign_keys import (
    ANSI_FOREIGN_KEYS_SQL,
    FkRowParser,
    parse_canonical_fk_rows,
)
from alkera_cli.plugins.plugin_base.sql.shared_reach import guard_connect


class DbapiCursor(Protocol):
    """The minimal PEP-249 cursor surface the base uses (psycopg / pymysql / duckdb /
    clickhouse-connect / trino / the Tinybird façade all satisfy it).

    Every parameter is POSITIONAL-ONLY: the drivers spell them differently
    (``sql``/``query``/``operation``/``command``, ``params``/``parameters``) and PEP-249
    only ever promises the positions. ``execute`` and ``close`` are typed ``-> object``
    because their return value is always ignored AND the drivers disagree about it (DuckDB
    returns the connection, psycopg the cursor, Snowflake's ``close`` a bool). ``rowcount``
    is ``int | None`` because Snowflake reports ``None`` for a statement that produced no
    row count, and ``description`` is ``Sequence[Any] | None`` — ``None`` is how a driver
    says "this statement produced no result set", which the base branches on.

    ``fetchmany``'s size is REQUIRED, not defaulted: the databricks driver declares no
    default for it, and the base always passes one anyway."""

    @property
    def description(self) -> Sequence[Any] | None: ...
    @property
    def rowcount(self) -> int | None: ...
    def execute(self, operation: str, parameters: Any = ..., /) -> object: ...
    def fetchmany(self, size: int, /) -> Sequence[Any]: ...
    def fetchall(self) -> Sequence[Any]: ...
    def close(self) -> object: ...


class DbapiConnection(Protocol):
    """The minimal PEP-249 connection surface the base uses (psycopg / pymysql /
    duckdb / clickhouse-connect / … all satisfy it).

    ``commit``/``rollback`` are typed ``-> object`` (not ``None``) because their return
    value is always ignored AND some drivers are fluent — DuckDB's ``commit``/``rollback``
    return the connection. ``object`` accepts both a PEP-249 ``None`` and a fluent self."""

    def cursor(self) -> DbapiCursor: ...
    def commit(self) -> object: ...
    def rollback(self) -> object: ...
    def close(self) -> None: ...


class ConnectFn(Protocol):
    """Opens a driver connection for ``conn``, resolving the secret at the I/O boundary
    (never persisting/logging it). ``read_only`` lets a driver that supports a read-only
    *connection* (DuckDB/SQLite) honor it; engines that enforce read-only with a
    transaction preamble instead can ignore it."""

    def __call__(self, conn: Connection, *, read_only: bool) -> DbapiConnection: ...


class AdminConnectFn(Protocol):
    """Opens a DEADLINE-BOUNDED driver connection for a fixed admin statement
    (cancel/kill). Unlike :class:`ConnectFn` — whose sessions must allow long
    legitimate reads — this applies driver-level connect/read deadlines so a wedged
    engine releases the worker thread running the statement (the async caller can only
    abandon that thread; a driver deadline is what actually frees it)."""

    def __call__(self, conn: Connection) -> DbapiConnection: ...


#: Schemas that are engine internals, never user data — excluded from introspection +
#: lineage (their views would otherwise parse into a cloud of phantom edges).
_DEFAULT_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog"})

#: Standard ``information_schema`` queries — identical column names across Postgres,
#: Redshift, MySQL, Snowflake, DuckDB, ClickHouse. An engine that diverges overrides
#: the field on its spec.
_RELATIONS_SQL = (
    "SELECT table_catalog, table_schema, table_name, table_type FROM information_schema.tables"
    # Deterministic order so the schema-card pass cards a STABLE subset when a warehouse exceeds
    # the per-pass relation cap — without it the engine may return a different scan order each
    # poll, churning (delete + re-embed) a rotating slice of cards with no schema change.
    " ORDER BY table_catalog, table_schema, table_name"
)
_VIEWS_SQL = (
    "SELECT table_catalog, table_schema, table_name, view_definition FROM information_schema.views"
)
_COLUMNS_SELECT = "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
#: The BULK columns read — the same projection with the relation name in front, so ONE query
#: describes every relation in a namespace (a catalog+schema, or a database on a 2-level
#: engine) instead of one describe per relation. The caller appends the namespace's own
#: ``WHERE`` (the same clauses a single describe builds, minus the table) and
#: ``columns_bulk_order_by``. Identical source + projection to ``columns_select``, so a card
#: built in bulk says exactly what a card built one relation at a time said.
_COLUMNS_BULK_SELECT = (
    "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns"
)
#: Appended AFTER the caller's ``WHERE`` so each relation's columns arrive in the relation's
#: own order. An engine whose bulk select is a derived table (Postgres orders inside its
#: matview union) sets this empty — there is no ``ordinal_position`` left to order on.
_COLUMNS_BULK_ORDER_BY = " ORDER BY table_name, ordinal_position"
_LINEAGE_COLUMNS_SQL = (
    "SELECT table_catalog, table_schema, table_name, column_name, data_type, is_nullable, "
    "numeric_precision, numeric_scale, character_maximum_length "
    "FROM information_schema.columns ORDER BY ordinal_position"
)

#: Bases whose ``numeric_precision``/``numeric_scale`` are decimal digits worth
#: recording. Floats also report a precision, but in BINARY bits -- composing it
#: would invent types like ``double(53,0)`` and make identical floats diff.
_DECIMAL_BASES = frozenset({"number", "numeric", "decimal", "dec"})

#: Bases whose ``character_maximum_length`` is a real capacity bound.
_CHAR_BASES = frozenset(
    {
        "varchar",
        "char",
        "character varying",
        "character",
        "char varying",
        "text",
        "string",
        "nvarchar",
        "nchar",
    }
)


def composed_data_type(data_type: Any, precision: Any, scale: Any, char_length: Any) -> str:
    """The full parametrized type when ``information_schema`` reports a bare
    base. Snowflake (and Postgres for scale-only edits) report ``NUMBER`` /
    ``numeric`` with the digits in separate columns, which made a
    precision/scale narrowing invisible to the type lattice. Engines that
    already parametrize (ClickHouse ``Decimal(18, 2)``) pass through."""
    base = str(data_type or "")
    if "(" in base:
        return base
    if base.strip().lower() in _DECIMAL_BASES and precision not in (None, ""):
        effective_scale = scale if scale not in (None, "") else 0
        return f"{base}({int(precision)},{int(effective_scale)})"
    if base.strip().lower() in _CHAR_BASES and char_length not in (None, ""):
        return f"{base}({int(char_length)})"
    return base


#: The 2-level (``database.table``, no sub-schema) variants for engines whose
#: ``information_schema.table_catalog`` is meaningless (MySQL/MariaDB → ``"def"``;
#: ClickHouse). They select the DATABASE into the catalog column + an empty sub-schema, so
#: the base mints ``system://authority/database.table`` (used by mysql, clickhouse, and the
#: generic connector pointed at one of those). Pair these with ``name_levels=2``.
TWO_LEVEL_RELATIONS_SQL = (
    "SELECT table_schema, '', table_name, table_type FROM information_schema.tables"
    " ORDER BY table_schema, table_name"  # stable subset under the card cap (see _RELATIONS_SQL)
)
TWO_LEVEL_VIEWS_SQL = (
    "SELECT table_schema, '', table_name, view_definition FROM information_schema.views"
)
TWO_LEVEL_LINEAGE_COLUMNS_SQL = (
    "SELECT table_schema, '', table_name, column_name, data_type, is_nullable, "
    "numeric_precision, numeric_scale, character_maximum_length "
    "FROM information_schema.columns ORDER BY ordinal_position"
)
LINEAGE_COLUMNS_SQL_NO_PRECISION = (
    "SELECT table_catalog, table_schema, table_name, column_name, data_type, is_nullable, "
    "NULL AS numeric_precision, NULL AS numeric_scale, NULL AS character_maximum_length "
    "FROM information_schema.columns ORDER BY ordinal_position"
)
"""For engines whose ``information_schema.columns`` lacks the precision columns
(Trino, and unknown engines behind the generic connector). Their ``data_type``
carries the parameters inline (``decimal(10,2)``), which ``composed_data_type``
passes through untouched, so nothing is lost -- selecting the missing columns
would error the whole introspection instead."""


@dataclass(frozen=True)
class SqlEngineSpec:
    """The per-engine knobs the SQL base reads. ``system`` drives the URN scheme +
    :func:`warehouse_authority`; ``classifier_dialect`` is the sqlglot dialect the
    permission classifier + view parser use; ``connect`` opens a driver connection."""

    system: str
    classifier_dialect: str
    connect: ConnectFn
    #: Optional deadline-bounded connection factory for the fixed ADMIN statements
    #: (cancel/kill a query). Engines whose driver supports connect/read deadlines
    #: (PyMySQL ``read_timeout``, clickhouse-connect ``send_receive_timeout``, trino
    #: ``request_timeout``) provide one so a wedged engine releases the worker thread;
    #: ``None`` → the admin path falls back to ``connect(read_only=False)`` (engines
    #: whose normal connect already bounds itself — psycopg ``connect_timeout`` plus the
    #: server-side statement timeout — or local file engines with no network to stall on).
    admin_connect: AdminConnectFn | None = None
    sqlglot_dialect: str = ""
    #: How many identifier levels the engine's relations have. 3 = ``catalog.schema.table``
    #: (Postgres/Snowflake/Redshift/DuckDB). 2 = ``database.table`` (MySQL/MariaDB, where
    #: ``information_schema.table_catalog`` is the meaningless literal ``"def"`` and the
    #: database lives in ``table_schema``): a 2-level engine puts the database in the URN's
    #: CONTAINER slot with no sub-schema, and a ``db.table`` view-reference (which sqlglot
    #: parses into the schema slot) maps to that same container — so nodes + view edges
    #: line up. A 2-level engine MUST override the introspection SQL to select the database
    #: into the catalog column (see the MySQL spec).
    name_levels: int = 3
    #: What actually keeps this engine from writing on a READ, and whether the credential's
    #: own permissions were checked. ``None`` when the only guarantee is the shared gate
    #: plus the always-rollback — an empty badge is the honest answer there, and an engine
    #: that adds a real second layer fills this in rather than the UI inventing a claim.
    read_only_enforcement: ReadOnlyEnforcement | None = None
    #: Why this engine cannot be WRITTEN at all — one sentence, or empty for an engine
    #: with a write path. Distinct from ``read_only_enforcement`` (which describes what
    #: keeps a READ honest on an engine that can also write): a non-empty reason means
    #: the wire itself has no write (Tinybird's Query API executes SELECT and nothing
    #: else), so a mutating statement is refused with this reason in EVERY permission
    #: mode, before any reader is asked — a fact about the connection, not a mode. The
    #: brief states it per connection; ``read_only_engine_reason`` looks it up by
    #: ``system``, populated by construction so a new engine registers itself.
    read_only_reason: str = ""
    #: Statements run (inside the read transaction) before a READ to make the engine
    #: itself refuse a misclassified write. Empty when ``connect`` opens a read-only
    #: *connection* instead (DuckDB/SQLite). The read is ALWAYS rolled back regardless.
    read_only_preamble: tuple[str, ...] = ("SET TRANSACTION READ ONLY",)
    #: A trivial query run right AFTER the read-only preamble (and before the user's
    #: statement) to take the transaction's snapshot, so the engine locks the
    #: read/write mode: on Postgres/Redshift a `SET TRANSACTION READ WRITE` is only
    #: legal before the first query, so this `SELECT 1` makes the engine itself refuse
    #: any attempt to leave the read-only transaction from inside the user statement.
    #: Empty (the default) for engines whose read containment is a read-only
    #: CONNECTION rather than a transaction (DuckDB/SQLite), or a stateless HTTP driver
    #: (ClickHouse/Tinybird) where a per-request `SELECT 1` buys nothing.
    read_only_snapshot_stmt: str = ""
    #: A statement run before EVERY query (read AND write) capping how long the engine
    #: lets it run SERVER-SIDE — so a runaway query is cancelled by the engine instead
    #: of billing unbounded warehouse compute. A ``str.format`` template with two
    #: placeholders, ``{seconds}`` and ``{ms}`` (the same timeout in each unit); the
    #: engine picks the unit its setting wants. Empty (the default) = no timeout
    #: statement: the local/free engines (DuckDB, SQLite) and the stateless
    #: ClickHouse-over-HTTP driver (a session ``SET`` doesn't persist across its
    #: requests) opt out. Only set it for engines whose driver holds ONE session a
    #: ``SET`` persists across (Postgres/Redshift/MySQL/Trino/Databricks/Snowflake).
    #: The seconds come from the user preference / env at run time (see
    #: :func:`resolve_sql_statement_timeout_seconds`); ``0`` skips the statement.
    statement_timeout_stmt: str = ""
    #: A cursor attribute carrying the engine's id for the executed query (Snowflake
    #: ``sfqid``) — for cost settle. Empty for engines without one.
    query_id_attr: str = ""
    system_schemas: frozenset[str] = _DEFAULT_SYSTEM_SCHEMAS
    system_catalogs: frozenset[str] = frozenset()
    #: Relation-NAME prefixes that identify a system relation regardless of qualification.
    #: Catalog SQL is essentially always written unqualified (``FROM pg_class c``, not
    #: ``FROM pg_catalog.pg_class c``), and an unqualified ref carries no namespace at all —
    #: so a schema/catalog test can never see it, and the ref resolves into whatever schema
    #: the reading relation happens to live in, minting a relation that does not exist.
    #: These prefixes are the only thing that makes an unqualified system relation
    #: recognizable. Matched case-insensitively against the bare relation name.
    system_relation_prefixes: frozenset[str] = frozenset()
    #: Exact relation names that are system objects but carry no distinguishing prefix
    #: (BigQuery's ``__TABLES__`` meta-relations, SQLite's ``sqlite_master`` siblings).
    system_relation_names: frozenset[str] = frozenset()
    relations_sql: str = _RELATIONS_SQL
    views_sql: str = _VIEWS_SQL
    columns_select: str = _COLUMNS_SELECT
    #: The bulk namespace read behind :meth:`GenericSqlIntrospect.describe_many` — see
    #: ``_COLUMNS_BULK_SELECT``. An engine overrides it exactly when it overrides
    #: ``columns_select``, so the two stay in step.
    columns_bulk_select: str = _COLUMNS_BULK_SELECT
    columns_bulk_order_by: str = _COLUMNS_BULK_ORDER_BY
    #: How many bulk metadata reads the schema-card pass may have in flight on this engine.
    #: Each is its own driver session (the connector opens one per statement), so this is a
    #: session budget, not a CPU one: an engine with a tight concurrent-session cap — or a
    #: warehouse that bills per session — lowers it; ``1`` is strictly serial.
    metadata_concurrency: int = 4
    lineage_columns_sql: str = _LINEAGE_COLUMNS_SQL
    #: The query returning this engine's declared foreign keys in the canonical row shape
    #: (see ``plugin_base.sql.foreign_keys``). Defaults to the portable ANSI
    #: ``information_schema`` form; a dedicated engine overrides it with its own catalog
    #: query. EMPTY = this engine has no foreign-key concept (ClickHouse) or exposes none
    #: through SQL (Trino, Snowflake's SHOW) — no query is issued at all.
    foreign_keys_sql: str = ANSI_FOREIGN_KEYS_SQL
    #: How to turn ``foreign_keys_sql``'s rows into constraints. The default reads the
    #: canonical shape; an engine whose metadata command has its own grid supplies its own.
    foreign_keys_parser: FkRowParser = field(default=parse_canonical_fk_rows)

    @property
    def parse_dialect(self) -> str:
        """The sqlglot dialect for view parsing — falls back to the classifier dialect."""
        return self.sqlglot_dialect or self.classifier_dialect

    def keep_relation(self, catalog: str, schema: str) -> bool:
        """Whether a relation is user data worth graphing — excludes the engine's own
        system schemas/catalogs and dbt's ``*_dbt_test__audit`` artifacts."""
        cat, sch = catalog.strip().lower(), schema.strip().lower()
        if cat in self.system_catalogs or sch in self.system_schemas:
            return False
        # dbt's test-audit container ends in ``_dbt_test__audit``. On a 3-level engine it's a
        # SCHEMA; on a 2-level engine (MySQL/ClickHouse) the database lands in the CATALOG slot
        # with an empty schema — so check BOTH slots or the audit tables leak into the graph.
        return not (sch.endswith("dbt_test__audit") or cat.endswith("dbt_test__audit"))

    def is_system_relation(self, name: str) -> bool:
        """Whether a BARE relation name is one of this engine's system objects.

        Needed because the namespace tests above are unreachable for the way catalog SQL is
        actually written: ``FROM pg_class c`` carries no schema, so defaulting it to the
        reading relation's schema invents ``<that schema>.pg_class`` — a relation that does
        not exist and was never introspected."""
        n = name.strip().strip('"').lower()
        if not n:
            return False
        # A system namespace can ride INSIDE the name: BigQuery's INFORMATION_SCHEMA is a
        # pseudo-dataset, so `ds.INFORMATION_SCHEMA.TABLES` arrives with the namespace as
        # part of the relation name rather than in the schema slot.
        if "." in n and n.rsplit(".", 1)[0] in self.system_schemas:
            return True
        return n in self.system_relation_names or any(
            n.startswith(p) for p in self.system_relation_prefixes
        )

    def __post_init__(self) -> None:
        if self.read_only_reason:
            _READ_ONLY_ENGINES[self.system] = self.read_only_reason
        # Every dial an engine makes goes through these two callables, so this is where
        # a team record's connection is vetted and pinned to the server it names
        # (:mod:`.shared_reach`), whichever engine and whichever caller.
        object.__setattr__(self, "connect", guard_connect(self.connect))
        if self.admin_connect is not None:
            object.__setattr__(self, "admin_connect", guard_connect(self.admin_connect))


#: ``system`` → why that engine cannot be written, filled by every spec that declares
#: one when it is constructed. A lookup here needs no registry and no credential, which
#: is what lets the workspace's source cards state the fact per connection.
_READ_ONLY_ENGINES: dict[str, str] = {}


def read_only_engine_reason(system: str) -> str:
    """Why the engine named ``system`` refuses every write, or ``""`` when it has a
    write path (or its spec has not been constructed in this process)."""
    return _READ_ONLY_ENGINES.get(system, "")


__all__ = [
    "LINEAGE_COLUMNS_SQL_NO_PRECISION",
    "TWO_LEVEL_LINEAGE_COLUMNS_SQL",
    "TWO_LEVEL_RELATIONS_SQL",
    "TWO_LEVEL_VIEWS_SQL",
    "AdminConnectFn",
    "ConnectFn",
    "DbapiConnection",
    "DbapiCursor",
    "SqlEngineSpec",
    "composed_data_type",
    "read_only_engine_reason",
]
