"""The shared SQL-engine base — a generic DBAPI connector + ``information_schema``
introspection + view-definition lineage, so a new warehouse is a thin spec plus a
driver-``connect`` function rather than a copy of a full connector.

A connector plugin for any PEP-249 SQL engine declares a :class:`SqlEngineSpec`
(system name, sqlglot dialect, a ``connect`` callable, the read-only mechanism, and
— rarely — overridden ``information_schema`` SQL) and reuses:

- :class:`DbapiConnector` — the capability-gated chokepoint (re-classify the SQL,
  refuse a write/destroy/egress without a valid cap-token, run reads inside a
  rolled-back read-only transaction).
- :class:`DbapiRunSQL` — the ``RunSQLCapability`` the ``sql.query`` tool consumes.
- :class:`GenericSqlIntrospect` — ``information_schema``-backed list/describe, minting
  per-system URNs (via :func:`warehouse_relation_urn`) that fold with dbt.
"""

from __future__ import annotations

from alkera_cli.plugins.plugin_base.sql.connector import (
    DbapiConnector,
    DbapiRunSQL,
    SqlCapabilityDeniedError,
)
from alkera_cli.plugins.plugin_base.sql.foreign_keys import (
    ANSI_FOREIGN_KEYS_SQL,
    DATABRICKS_FOREIGN_KEYS_SQL,
    MYSQL_FOREIGN_KEYS_SQL,
    PG_FOREIGN_KEYS_SQL,
    REDSHIFT_FOREIGN_KEYS_SQL,
    ForeignKeyConstraint,
    ForeignKeySnapshot,
    parse_canonical_fk_rows,
    parse_snowflake_imported_keys,
    parse_sqlite_foreign_key_list,
)
from alkera_cli.plugins.plugin_base.sql.introspect import (
    GenericSqlIntrospect,
    introspect_foreign_keys,
)
from alkera_cli.plugins.plugin_base.sql.run_authorization import RunApproval, RunAuthorization
from alkera_cli.plugins.plugin_base.sql.spec import (
    LINEAGE_COLUMNS_SQL_NO_PRECISION,
    TWO_LEVEL_LINEAGE_COLUMNS_SQL,
    TWO_LEVEL_RELATIONS_SQL,
    TWO_LEVEL_VIEWS_SQL,
    DbapiConnection,
    DbapiCursor,
    SqlEngineSpec,
)
from alkera_cli.plugins.plugin_base.sql.timeout import (
    resolve_sql_statement_timeout_seconds,
)

__all__ = [
    "ANSI_FOREIGN_KEYS_SQL",
    "DATABRICKS_FOREIGN_KEYS_SQL",
    "LINEAGE_COLUMNS_SQL_NO_PRECISION",
    "MYSQL_FOREIGN_KEYS_SQL",
    "PG_FOREIGN_KEYS_SQL",
    "REDSHIFT_FOREIGN_KEYS_SQL",
    "TWO_LEVEL_LINEAGE_COLUMNS_SQL",
    "TWO_LEVEL_RELATIONS_SQL",
    "TWO_LEVEL_VIEWS_SQL",
    "DbapiConnection",
    "DbapiConnector",
    "DbapiCursor",
    "DbapiRunSQL",
    "ForeignKeyConstraint",
    "ForeignKeySnapshot",
    "GenericSqlIntrospect",
    "RunApproval",
    "RunAuthorization",
    "SqlCapabilityDeniedError",
    "SqlEngineSpec",
    "introspect_foreign_keys",
    "parse_canonical_fk_rows",
    "parse_snowflake_imported_keys",
    "parse_sqlite_foreign_key_list",
    "resolve_sql_statement_timeout_seconds",
]
