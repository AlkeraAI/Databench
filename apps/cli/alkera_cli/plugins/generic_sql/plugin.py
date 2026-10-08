"""The generic SQL connector: any SQL database through a SQLAlchemy URL.

The shared SQL base is engine-agnostic, so this plugin is the catch-all for engines
without a dedicated connector. It opens a SQLAlchemy engine from a URL and reuses the
base's gated ``RunSQL`` and ``information_schema`` introspection. A distribution may
add schema cards and lineage by registering a subclass. The sqlglot dialect follows
the URL's backend when sqlglot knows it, and there is no native cost or query history.

The URL's backend names the URN ``system`` (``postgresql`` → ``postgres`` …) so a generic
connection still FOLDS with dbt + a dedicated connector for a known engine. The inline
password is parsed OUT of the URL into a ``CredentialRef`` — never stored on the
``Connection`` or echoed back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

import duckdb
from alkera_core.connectors import generic_sql as generic_sql_descriptor
from alkera_core.connectors.generic_sql import URL_ATTR
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlglot.dialects.dialect import Dialect

from alkera_cli.plugins.plugin_base import (
    ActivationSpec,
    CapabilitySet,
    Connection,
    ConnectionFormSchema,
    Credential,
    IntegrationSdkCapability,
    Plugin,
    PluginManifest,
    Registrar,
    SurfaceKind,
    WorkspaceContext,
)
from alkera_cli.plugins.plugin_base.connection import TokenCredential
from alkera_cli.plugins.plugin_base.credential_manager import LocalCredentialManager
from alkera_cli.plugins.plugin_base.duckdb_confine import MEMORY, confine_duckdb, database_paths
from alkera_cli.plugins.plugin_base.sql import (
    LINEAGE_COLUMNS_SQL_NO_PRECISION,
    MYSQL_FOREIGN_KEYS_SQL,
    TWO_LEVEL_LINEAGE_COLUMNS_SQL,
    TWO_LEVEL_RELATIONS_SQL,
    TWO_LEVEL_VIEWS_SQL,
    DbapiConnection,
    DbapiConnector,
    DbapiRunSQL,
    GenericSqlIntrospect,
    SqlEngineSpec,
)
from alkera_cli.plugins.plugin_base.surfaces import PollRefreshProvider

PLUGIN = "generic_sql"


def _classifier_dialect_for(system: str) -> str:
    """The sqlglot dialect the per-connection spec classifies with: the backend's ``system``
    when sqlglot recognizes it (``mysql``/``postgres``/``redshift``/…), else ``""`` (neutral).
    It MUST agree with what the GATE uses — ``conn.dialect=system``, itself degraded to neutral
    when sqlglot doesn't know it — so a human-approved write's cap-token fingerprint matches the
    connector's own re-classification. A hard-coded neutral here desynced them: dialect-specific
    syntax (MySQL backtick-quoted identifiers) parsed on the gate side but not the connector side
    (op=update vs op=unknown), so EVERY approved write was refused at the chokepoint."""
    try:
        Dialect.get_or_raise(system)
        return system
    except Exception:
        return ""


def _connect_duckdb(conn: Connection, *, read_only: bool) -> DbapiConnection:
    """Open a ``duckdb:///<path>`` URL with DuckDB's own driver, read-only for a read,
    and confined to that database file: DuckDB can read any file it is pointed at, and
    a query must not."""
    path = make_url(str(conn.attributes.get(URL_ATTR, ""))).database or MEMORY
    con = duckdb.connect(path, read_only=read_only and path != MEMORY)
    try:
        confine_duckdb(con, paths=database_paths(path))
    except BaseException:
        con.close()
        raise
    return con


def _connect(conn: Connection, *, read_only: bool) -> DbapiConnection:
    """Open a SQLAlchemy engine from the connection's URL (re-inserting the resolved
    secret at the I/O boundary) and return a raw PEP-249 connection. ``NullPool`` so each
    connection's ``close()`` really closes — no pooled connection holds the secret open.
    A team record's connection is vetted and pinned before this runs (the spec guards
    every ``connect``)."""
    if conn.attributes.get("system") == "duckdb":
        return _connect_duckdb(conn, read_only=read_only)
    url = make_url(str(conn.attributes.get(URL_ATTR, "")))
    ref = conn.credential_ref
    if ref is not None and ref.locator:
        cred = LocalCredentialManager().resolve_sync(ref)
        if isinstance(cred, TokenCredential):
            url = url.set(password=cred.token.get_secret_value())
    connect_args = dict(dialect_for(conn).read_only_connect_args) if read_only else {}
    engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
    raw: DbapiConnection = engine.raw_connection()
    return raw


#: Backends that are 2-level (database.table, no sub-schema) — the generic spec must use
#: name_levels=2 + the 2-level introspection SQL for these, or it mints a phantom 'def'
#: catalog segment that folds with neither dbt nor the dedicated connector.
_TWO_LEVEL_BACKENDS = frozenset({"mysql", "clickhouse"})


@dataclass(frozen=True)
class SqlDialect:
    """What the generic connector knows about one backend, keyed by its ``system``.

    ``read_only_preamble`` runs before every read inside the rolled-back read
    transaction, so the engine itself refuses a write the classifier missed;
    ``read_only_connect_args`` opens the read's connection read-only instead, for an
    engine whose containment is the connection. ``timeout_stmt`` caps a
    statement server-side (a ``str.format`` template taking ``{seconds}`` and
    ``{ms}``). A backend this table does not know gets none of them and relies on
    the classifier and the always-rollback."""

    read_only_preamble: tuple[str, ...] = ()
    read_only_snapshot_stmt: str = ""
    read_only_connect_args: Mapping[str, Any] = field(default_factory=dict)
    timeout_stmt: str = ""
    #: Catalog reads for a backend with no ``information_schema``, in the shape the
    #: SQL base's introspection reads; ``None`` keeps the standard queries.
    catalog: SqlCatalog | None = None


@dataclass(frozen=True)
class SqlCatalog:
    """A backend's relation and column reads, answering as ``information_schema``."""

    relations_sql: str
    columns_select: str
    columns_bulk_select: str


#: SQLite's catalog as ``information_schema`` would answer it: every user table and
#: view in ``main``, and their columns from ``pragma_table_info``. The base appends its
#: own ``WHERE`` to the column reads, so they select from a derived table.
_SQLITE_CATALOG = SqlCatalog(
    relations_sql=(
        "SELECT '' AS table_catalog, 'main' AS table_schema, name AS table_name,"
        " CASE type WHEN 'view' THEN 'VIEW' ELSE 'BASE TABLE' END AS table_type"
        " FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name"
    ),
    columns_select=(
        "SELECT column_name, data_type, is_nullable FROM "
        "(SELECT '' AS table_catalog, 'main' AS table_schema, m.name AS table_name,"
        " p.cid AS ordinal_position, p.name AS column_name, p.type AS data_type,"
        " CASE WHEN p.\"notnull\" = 0 THEN 'YES' ELSE 'NO' END AS is_nullable"
        " FROM sqlite_master m JOIN pragma_table_info(m.name) p"
        " WHERE m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite_%') AS columns"
    ),
    columns_bulk_select=(
        "SELECT table_name, column_name, data_type, is_nullable FROM "
        "(SELECT '' AS table_catalog, 'main' AS table_schema, m.name AS table_name,"
        " p.cid AS ordinal_position, p.name AS column_name, p.type AS data_type,"
        " CASE WHEN p.\"notnull\" = 0 THEN 'YES' ELSE 'NO' END AS is_nullable"
        " FROM sqlite_master m JOIN pragma_table_info(m.name) p"
        " WHERE m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite_%') AS columns"
    ),
)


#: Every backend the generic connector has an answer for.
DIALECTS: dict[str, SqlDialect] = {
    "postgres": SqlDialect(
        read_only_preamble=("SET TRANSACTION READ ONLY",),
        read_only_snapshot_stmt="SELECT 1",
        timeout_stmt="SET statement_timeout = {ms}",
    ),
    "redshift": SqlDialect(timeout_stmt="SET statement_timeout = {ms}"),
    "mysql": SqlDialect(
        read_only_preamble=("SET SESSION TRANSACTION READ ONLY",),
        timeout_stmt="SET SESSION MAX_EXECUTION_TIME = {ms}",
    ),
    "sqlite": SqlDialect(read_only_preamble=("PRAGMA query_only = ON",), catalog=_SQLITE_CATALOG),
    # DuckDB opens through its own driver, read-only for a read (``_connect_duckdb``).
    "duckdb": SqlDialect(),
    "trino": SqlDialect(timeout_stmt="SET SESSION query_max_run_time = '{seconds}s'"),
    "snowflake": SqlDialect(
        timeout_stmt="ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds}"
    ),
    "databricks": SqlDialect(timeout_stmt="SET STATEMENT_TIMEOUT = {seconds}"),
}

#: An unknown backend: no read-only statement, no timeout statement.
UNKNOWN_DIALECT = SqlDialect()


def dialect_for(conn: Connection) -> SqlDialect:
    return DIALECTS.get(str(conn.attributes.get("system") or ""), UNKNOWN_DIALECT)


def spec_for(conn: Connection) -> SqlEngineSpec:
    """The per-connection spec — the ``system`` comes from the URL backend; the classifier
    dialect is the backend's sqlglot dialect when recognized (else neutral), so the connector
    classifies in lockstep with the gate (which uses ``conn.dialect=system``) and dialect-specific
    syntax like MySQL backticks parses on both sides. A read runs read-only where the backend
    has a way (:data:`DIALECTS`); the gate's reclassify-refuse and the always-rollback hold
    for every backend. A 2-level backend (MySQL/ClickHouse) gets the 2-level
    shape so its URNs fold with dbt + the dedicated connector."""
    system = str(conn.attributes.get("system") or "sql")
    dialect = dialect_for(conn)
    timeout_stmt = dialect.timeout_stmt
    if system in _TWO_LEVEL_BACKENDS:
        return SqlEngineSpec(
            system=system,
            classifier_dialect=_classifier_dialect_for(system),
            connect=_connect,
            read_only_preamble=dialect.read_only_preamble,
            read_only_snapshot_stmt=dialect.read_only_snapshot_stmt,
            statement_timeout_stmt=timeout_stmt,
            name_levels=2,
            system_catalogs=frozenset(
                {"information_schema", "mysql", "performance_schema", "sys", "system"}
            ),
            system_schemas=frozenset(),
            relations_sql=TWO_LEVEL_RELATIONS_SQL,
            views_sql=TWO_LEVEL_VIEWS_SQL,
            lineage_columns_sql=TWO_LEVEL_LINEAGE_COLUMNS_SQL,
            # MySQL/MariaDB expose foreign keys through KEY_COLUMN_USAGE; ClickHouse has
            # no foreign-key concept at all, so it issues no FK query.
            foreign_keys_sql=(MYSQL_FOREIGN_KEYS_SQL if system == "mysql" else ""),
        )
    spec = SqlEngineSpec(
        system=system,
        classifier_dialect=_classifier_dialect_for(system),
        connect=_connect,
        read_only_preamble=dialect.read_only_preamble,
        read_only_snapshot_stmt=dialect.read_only_snapshot_stmt,
        statement_timeout_stmt=timeout_stmt,
        # An unknown engine may lack the precision columns entirely (Trino
        # does); a parametrized data_type still composes, and a missing
        # column would error the whole introspection.
        lineage_columns_sql=LINEAGE_COLUMNS_SQL_NO_PRECISION,
    )
    if dialect.catalog is None:
        return spec
    return replace(
        spec,
        relations_sql=dialect.catalog.relations_sql,
        columns_select=dialect.catalog.columns_select,
        columns_bulk_select=dialect.catalog.columns_bulk_select,
    )


class GenericSqlConnections:
    """No auto-discovery — a generic connection is always added explicitly via the form
    (the prod-by-accident guard: we never auto-promote an arbitrary database URL)."""

    async def discover_connections(self, ctx: WorkspaceContext) -> list[Connection]:
        return []


class GenericSqlCapabilities:
    def capabilities(self, conn: Connection) -> CapabilitySet:
        caps = CapabilitySet()
        connector = DbapiConnector(conn, spec_for(conn))
        caps.add(DbapiRunSQL(connector))
        caps.add(GenericSqlIntrospect(connector))
        # The SDK escape hatch: a SQLAlchemy Engine on the connection's URL, the split-out
        # secret re-injected at the I/O boundary like _connect (NullPool). The model can
        # engine.connect() + text(), sqlalchemy.inspect(engine), pd.read_sql(..., engine),
        # and still reach the native DBAPI driver via engine.raw_connection() — the
        # engine-agnostic escape hatch for whatever backend the URL names.
        caps.add(
            IntegrationSdkCapability(
                client_factory="alkera_cli.plugins.generic_sql.sdk_client:open_engine",
                client_label="sqlalchemy.engine.Engine",
                sdk_modules=("sqlalchemy",),
                version_dist="sqlalchemy",
                distributions=("sqlalchemy",),
                docs={
                    "sqlalchemy": "https://docs.sqlalchemy.org/en/20/",
                    "dialects": "https://docs.sqlalchemy.org/en/20/dialects/",
                },
            )
        )
        return caps


class GenericSqlRefreshProvider(PollRefreshProvider):
    KIND = "generic_sql_refresh"


class GenericSqlPlugin(Plugin):
    manifest: ClassVar[PluginManifest] = PluginManifest(
        name=PLUGIN,
        surfaces=frozenset({SurfaceKind.CONNECTION, SurfaceKind.CAPABILITY, SurfaceKind.REFRESH}),
        description=(
            "Any SQL database via a SQLAlchemy URL, with no engine-specific "
            "features, plus an SDK escape hatch (SQLAlchemy Engine)"
        ),
    )

    def register(self, r: Registrar) -> None:
        r.connection_provider(GenericSqlConnections)
        r.capabilities(GenericSqlCapabilities)
        r.refresh_provider(GenericSqlRefreshProvider)

    def activation(self) -> ActivationSpec:
        # No signal — enabled + added by hand from the Plugins & Connections UI.
        return ActivationSpec()

    def auto_activates(self) -> bool:
        return False

    def urn_help(self) -> str:
        return (
            "<system>://<host>/<db>.<schema>.<table> — your generic-SQL relations, keyed by "
            "their warehouse relation (the system comes from the URL backend)"
        )

    def connection_form_schema(self) -> ConnectionFormSchema:
        return generic_sql_descriptor.form_schema()

    def build_connection(
        self, handle: str, auth_method: str, fields: dict[str, str]
    ) -> tuple[Connection, Credential | None]:
        return generic_sql_descriptor.build_connection(handle, auth_method, fields)


__all__ = ["GenericSqlCapabilities", "GenericSqlPlugin", "spec_for"]
