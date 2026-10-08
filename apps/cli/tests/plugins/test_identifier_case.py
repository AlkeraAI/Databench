"""Per-system column identifier case rules (``identifier_case.fold_column_identifier``).

Every rule here was probed against sqlglot 30.10.0 (see the dialect map's module
docstring): the fold must match how each system actually resolves an identifier,
including the systems that fold lower EVEN when quoted, the two that fold UPPER,
the genuinely case-sensitive one, and the MySQL columns-are-case-insensitive
override that beats sqlglot's table rule.
"""

from __future__ import annotations

import pytest
from alkera_cli.plugins.plugin_base.identifier_case import fold_column_identifier


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        pytest.param("snowflake", "MIXED", id="snowflake-upper"),
        pytest.param("oracle", "MIXED", id="oracle-upper"),
        pytest.param("postgres", "mixed", id="postgres-lower"),
        pytest.param("postgresql", "mixed", id="postgresql-alias"),
        pytest.param("athena", "mixed", id="athena-lower"),
        pytest.param("glue", "mixed", id="glue-via-athena"),
        pytest.param("redshift", "mixed", id="redshift-lower"),
        pytest.param("trino", "mixed", id="trino-lower"),
        pytest.param("presto", "mixed", id="presto-alias"),
        pytest.param("spark", "mixed", id="spark-lower"),
        pytest.param("hive", "mixed", id="hive-lower"),
        pytest.param("databricks", "mixed", id="databricks-lower"),
        pytest.param("sqlite", "mixed", id="sqlite-lower"),
        pytest.param("duckdb", "mixed", id="duckdb-lower"),
        pytest.param("bigquery", "mixed", id="bigquery-lower"),
        pytest.param("sqlserver", "mixed", id="sqlserver-via-tsql"),
        pytest.param("mssql", "mixed", id="mssql-alias"),
        pytest.param("tsql", "mixed", id="tsql-alias"),
        pytest.param("clickhouse", "MiXeD", id="clickhouse-preserves"),
        pytest.param("mysql", "mixed", id="mysql-override-lower"),
        pytest.param("mariadb", "mixed", id="mariadb-override-lower"),
        pytest.param("fivetran", "mixed", id="fivetran-blanket-lower"),
        pytest.param("hex", "mixed", id="hex-blanket-lower"),
        pytest.param("sigma", "mixed", id="sigma-blanket-lower"),
        pytest.param("looker", "mixed", id="looker-blanket-lower"),
        pytest.param("warehouse", "mixed", id="legacy-warehouse-blanket-lower"),
        pytest.param("", "mixed", id="empty-system-blanket-lower"),
    ],
)
def test_unquoted_reference_folds_by_system_rule(system: str, expected: str) -> None:
    assert fold_column_identifier(system, "MiXeD", quoted=False) == expected


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        pytest.param("snowflake", "MiXeD", id="snowflake-exact"),
        pytest.param("oracle", "MiXeD", id="oracle-exact"),
        pytest.param("postgres", "MiXeD", id="postgres-exact"),
        pytest.param("athena", "MiXeD", id="athena-exact"),
        pytest.param("clickhouse", "MiXeD", id="clickhouse-exact"),
        pytest.param("duckdb", "mixed", id="duckdb-folds-even-quoted"),
        pytest.param("bigquery", "mixed", id="bigquery-folds-even-quoted"),
        pytest.param("sqlserver", "mixed", id="tsql-folds-even-quoted"),
        pytest.param("redshift", "mixed", id="redshift-folds-even-quoted"),
        pytest.param("databricks", "mixed", id="databricks-folds-even-quoted"),
        pytest.param("trino", "mixed", id="trino-folds-even-quoted"),
        pytest.param("spark", "mixed", id="spark-folds-even-quoted"),
        pytest.param("hive", "mixed", id="hive-folds-even-quoted"),
        pytest.param("sqlite", "mixed", id="sqlite-folds-even-quoted"),
        # The override is the asymmetric pin: sqlglot alone would PRESERVE MySQL case
        # (its table rule); losing the override silently flips these two.
        pytest.param("mysql", "mixed", id="mysql-override-beats-sqlglot"),
        pytest.param("mariadb", "mixed", id="mariadb-override-beats-sqlglot"),
        # No SQL dialect → blanket lower, quoting notwithstanding: a BI cell or a
        # Fivetran field has no quoted-identifier semantics to honor.
        pytest.param("fivetran", "mixed", id="fivetran-blanket-ignores-quoted"),
        pytest.param("hex", "mixed", id="hex-blanket-ignores-quoted"),
        pytest.param("warehouse", "mixed", id="legacy-warehouse-blanket-ignores-quoted"),
    ],
)
def test_quoted_identifier_folds_by_stored_name_rule(system: str, expected: str) -> None:
    assert fold_column_identifier(system, "MiXeD", quoted=True) == expected


@pytest.mark.parametrize(
    ("system", "name", "expected"),
    [
        pytest.param("snowflake", '"MiXeD"', "MiXeD", id="double-quoted-infers-quoted"),
        pytest.param("snowflake", "MiXeD", "MIXED", id="bare-infers-unquoted"),
        pytest.param("sqlserver", "[MiXeD]", "mixed", id="bracketed-still-folds-tsql"),
        pytest.param("bigquery", "`MiXeD`", "mixed", id="backticked-still-folds-bigquery"),
        pytest.param("clickhouse", '"MiXeD"', "MiXeD", id="quoted-clickhouse-exact"),
        pytest.param("hex", '"MiXeD"', "mixed", id="quoted-no-dialect-still-lowers"),
        pytest.param("postgres", '"MiXeD"', "MiXeD", id="quoted-postgres-exact"),
    ],
)
def test_inferred_quoting_from_embedded_quote_characters(
    system: str, name: str, expected: str
) -> None:
    assert fold_column_identifier(system, name) == expected


@pytest.mark.parametrize(
    ("system", "name", "quoted", "expected"),
    [
        # `#` is the column-URN separator; a quote character or backslash can never
        # survive into a URN part. Stripped in every mode, before the case rule.
        pytest.param("snowflake", "pts#home", False, "PTSHOME", id="hash-stripped"),
        pytest.param("postgres", "a\\b", True, "ab", id="backslash-stripped"),
        pytest.param("snowflake", " id ", False, "ID", id="whitespace-trimmed"),
        pytest.param("snowflake", '""', None, "", id="only-quotes-empty"),
        pytest.param("hex", "   ", False, "", id="only-whitespace-empty"),
        pytest.param("snowflake", "", None, "", id="empty-name-empty"),
    ],
)
def test_urn_safety_stripping(system: str, name: str, quoted: bool | None, expected: str) -> None:
    assert fold_column_identifier(system, name, quoted=quoted) == expected


@pytest.mark.parametrize(
    "system",
    [
        "snowflake",
        "oracle",
        "postgres",
        "athena",
        "clickhouse",
        "duckdb",
        "bigquery",
        "sqlserver",
        "redshift",
        "databricks",
        "trino",
        "spark",
        "hive",
        "sqlite",
        "mysql",
        "fivetran",
        "warehouse",
    ],
)
def test_stored_identity_is_a_fixed_point(system: str) -> None:
    # Re-minting an already-recorded name (schema-diff, drift, connection relink)
    # folds it with quoted=True; the recorded identity must map to itself — whether
    # it was first minted from a quoted identifier or an unquoted reference.
    for first_quoted in (True, False):
        stored = fold_column_identifier(system, "MiXeD", quoted=first_quoted)
        assert fold_column_identifier(system, stored, quoted=True) == stored
