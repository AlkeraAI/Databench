"""The SQL classifier's vocabulary and its untyped paths (PERMISSIONS section 6.2).

What decides an effect without a typed parse tree: the ``exp.Command`` verb
tables, the raw-text sniffs for the fail-closed path, the quoted-literal walk
that re-classifies dynamic SQL, the ``EXPLAIN ANALYZE`` body split, and the
fallback for SQL strict parsing rejected. The severity order and the per-dialect
dangerous-function deny lists live here too, because the typed walk in
``classifier`` reads the same vocabulary and the two paths must never drift.

``classifier`` is the entry module and imports this one, so the re-classification
of an extracted literal reaches ``descriptor_from_sql`` through a call-time
import, never at load.
"""

from __future__ import annotations

import re

from sqlglot import expressions as exp

from alkera_cli.contracts.tool_types import Confidence, Effect, StatementInfo

MAX_DYNAMIC_DEPTH = 3

SEVERITY: dict[Effect, int] = {
    Effect.READ: 0,
    Effect.WRITE: 1,
    Effect.EGRESS: 2,
    Effect.DESTROY: 3,
    # EXEC tops the ladder: running a program or reaching the filesystem on the
    # DATABASE HOST is worse than losing rows (destroy) or leaking them (egress),
    # so in a multi-statement string it dominates every other tier.
    Effect.EXEC: 4,
}


def effect_max(a: Effect, b: Effect) -> Effect:
    return a if SEVERITY[a] >= SEVERITY[b] else b


# Per-dialect dangerous functions -- functions inside SQL that dial an external
# HOST or read arbitrary object STORAGE (SSRF / credentials in SQL / data
# egress). A name here classifies the statement EGRESS. Server-filesystem and
# program-execution reach (``pg_read_file``, ``lo_export``,
# ``system$execute_program``, ClickHouse ``file``/``executable``) lives in the
# stricter :data:`EXEC_FUNCS` tier below.
DANGEROUS_FUNCS: dict[str, set[str]] = {
    "postgres": {
        "dblink",
        "dblink_open",
        "dblink_fetch",
    },
    # Every DuckDB function that reads a file or an object store into a result is
    # one tier, the one ``read_csv`` has always had: a person or the judge sees it.
    # What the connection may reach on disk is bounded by the engine's own lock.
    "duckdb": {
        "read_csv",
        "read_csv_auto",
        "sniff_csv",
        "read_parquet",
        "parquet_scan",
        "parquet_metadata",
        "parquet_schema",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "read_json",
        "read_json_auto",
        "read_json_objects",
        "read_json_objects_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "read_text",
        "read_blob",
        "read_xlsx",
        "glob",
        "sqlite_scan",
        "delta_scan",
        "iceberg_scan",
        "iceberg_metadata",
        "iceberg_snapshots",
        "parquet_bloom_probe",
        "st_read",
        "postgres_scan",
        "mysql_scan",
        # ``query('<sql>')`` / ``query_table(...)`` run SQL text (or open a name the
        # replacement scan may read as a file) that this walk cannot see into.
        "query",
        "query_table",
    },
    "tsql": {"openrowset", "opendatasource"},
    # ClickHouse table functions that dial an external host or read arbitrary
    # object storage from the customer's SERVER (SSRF / credentials in SQL). A
    # `readonly=1` session still executes these — they are reads to ClickHouse —
    # so the classifier is the only thing that can stop them. The list mirrors the
    # engine's documented remote/storage table functions and their `*Cluster`
    # siblings; a name here classifies the SELECT as EGRESS regardless.
    # ``file`` and ``executable`` are NOT here — they reach the server filesystem
    # / run a program, so they are in :data:`EXEC_FUNCS`.
    "clickhouse": {
        "url",
        "urlcluster",
        "s3",
        "s3cluster",
        "remote",
        "remotesecure",
        "hdfs",
        "hdfscluster",
        "azureblobstorage",
        "azureblobstoragecluster",
        "gcs",
        "gcscluster",
        "postgresql",
        "mysql",
        "mongodb",
        "redis",
        "jdbc",
        "odbc",
        "sqlite",
        "iceberg",
        "icebergs3",
        "icebergazure",
        "iceberghdfs",
        "iceberglocal",
        "icebergs3cluster",
        "deltalake",
        "deltalakes3",
        "deltalakeazure",
        "deltalakelocal",
        "deltalakecluster",
        "hudi",
        "hudicluster",
    },
    "_universal": set(),
}

# Per-dialect functions that RUN A PROGRAM or READ/WRITE the server's FILESYSTEM
# -- the EXEC tier. These are not data egress: they reach the host
# the database runs on. A `readonly=1` / read-only transaction still executes
# them, so the classifier is the only control. A name here classifies the
# statement EXEC, which is never auto-allowed and refused under `bypass` without
# an explicit rule.
EXEC_FUNCS: dict[str, set[str]] = {
    "postgres": {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_stat_file",
        "pg_ls_dir",
        "pg_ls_waldir",
        "pg_ls_logdir",
        "pg_ls_tmpdir",
        "pg_ls_archive_statusdir",
        "lo_import",
        "lo_export",
        # adminpack: writes, renames and deletes files on the server.
        "pg_file_write",
        "pg_file_rename",
        "pg_file_unlink",
        "pg_file_sync",
        "pg_logdir_ls",
        # A dblink connection is opened FROM the database host, with the server's
        # network position and (``_u``) whatever credentials that host holds: a
        # reach of the host, not of the data. Running SQL over it is
        # ``dblink_exec`` below; reading through it is ``dblink``.
        "dblink_connect",
        "dblink_connect_u",
    },
    "redshift": {"pg_read_file", "pg_read_binary_file", "pg_ls_dir"},
    "mysql": {"load_file", "sys_exec", "sys_eval"},
    "snowflake": {"system$execute_program"},
    "tsql": {"xp_cmdshell"},
    # ClickHouse `file()` reads a path on the server; `executable()` runs a
    # program and streams its output as a table.
    "clickhouse": {"file", "filecluster", "executable"},
    # Loading an extension loads native code into the engine on the server — RCE
    # regardless of dialect (SQLite `load_extension()`). The DuckDB `INSTALL`/
    # `LOAD` *statement* forms are caught by verb + raw-text below, not here,
    # because a bare ``load``/``install`` is too generic a function name to deny.
    "_universal": {"load_extension", "install_extension"},
}


def exec_funcs_for(dialect: str) -> set[str]:
    """The EXEC function names that apply on ``dialect``. A connection with no
    recognized dialect (``""``: empty, or a name sqlglot does not know) could be
    any engine, so it gets every dialect's names: a Postgres ``pg_read_file``
    stays EXEC on a generic connection instead of falling to a read."""
    if not dialect:
        return set().union(*EXEC_FUNCS.values())
    return EXEC_FUNCS.get(dialect, set()) | EXEC_FUNCS["_universal"]


#: A table named by a quoted path or URL, which an engine reads as a file or an
#: object store: ``FROM 's3://b/k.parquet'`` anywhere, and on DuckDB (whose
#: replacement scan opens any unresolved name that looks like a file)
#: ``FROM '/etc/passwd'`` / ``FROM 'orders.csv'`` / ``FROM 'data/*.parquet'``.
URL_TABLE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
DUCKDB_FILE_TABLE = re.compile(
    r"[/\\]|^~|[*?]|\.(?:csv|tsv|txt|parquet|json|jsonl|ndjson|xlsx|arrow|avro|db|duckdb"
    r"|sqlite|gz|zst|bz2)$",
    re.IGNORECASE,
)

#: A routine whose body runs as native code or in an UNTRUSTED procedural language
#: (Postgres ``LANGUAGE C`` / ``internal`` and the ``…u`` languages: plpython3u,
#: plperlu, pltclu), which reaches the host as the server user. A later plain
#: ``SELECT f()`` classifies READ, so the definition is where it is caught.
UNTRUSTED_ROUTINE = re.compile(
    r"\bcreate\s+(?:or\s+replace\s+)?(?:function|procedure)\b[\s\S]*?"
    r"\blanguage\s+['\"]?(?:c|internal|plsh|plr|[a-z_][a-z0-9_]*u)['\"]?(?![\w$])",
    re.IGNORECASE,
)

# Functions that CHANGE something on the LOCAL server without writing a row the
# read-only transaction or the rollback can undo: they terminate/cancel other
# sessions, reload config, reset stats, switch WAL, take advisory locks, or persist
# a GUC. To the engine they are ordinary SELECT-able functions, so the read-only
# transaction and the always-rollback never see them — only the classifier can.
# A name here floors the statement to WRITE, so a read-only session (which refuses
# every write) will not run it, and any other mode requires a cap token first.
SIDE_EFFECT_FUNCS: dict[str, set[str]] = {
    "postgres": {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_switch_wal",
        "pg_switch_xlog",
        "pg_create_restore_point",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_xact_lock",
        "pg_advisory_xact_lock_shared",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "pg_try_advisory_xact_lock",
        "pg_try_advisory_xact_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_shared",
        "pg_advisory_unlock_all",
        "pg_stat_reset",
        "pg_stat_reset_shared",
        "pg_stat_reset_single_table_counters",
        "pg_stat_reset_single_function_counters",
        "pg_stat_reset_slru",
        "pg_notify",
        "pg_logical_emit_message",
        "pg_drop_replication_slot",
        "pg_create_physical_replication_slot",
        "pg_create_logical_replication_slot",
        "pg_replication_origin_create",
        "pg_replication_origin_drop",
        "set_config",
    },
    "redshift": {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "set_config",
    },
}

# Functions that run SQL on ANOTHER server over a separate connection. Neither
# containment reaches it: the local ``SET TRANSACTION READ ONLY`` preamble constrains
# only this transaction, and the forced rollback can't undo what already committed on
# the far side. So they take the DESTROY floor (always a human) rather than the EGRESS
# tier, which auto mode routes to the safety judge instead of the human.
REMOTE_EXEC_FUNCS: dict[str, set[str]] = {
    "postgres": {"dblink_exec", "dblink_send_query"},
    "redshift": {"dblink_exec", "dblink_send_query"},
}

# A COPY that RUNS A PROGRAM or reaches the SERVER FILESYSTEM -- the EXEC tier.
# ``FROM/TO PROGRAM '<cmd>'`` runs an arbitrary shell command on the
# database host (RCE); ``FROM/TO '<local file>'`` reads or writes the server's
# own filesystem. A quoted destination that is an ``@stage`` or a ``scheme://``
# object store is NOT a local file -- it is network egress, matched by
# :data:`EGRESS_COPY` instead -- so the two negative lookaheads exclude them. An
# optional escape-string prefix (Postgres ``E'...'`` / ``n'...'`` / ``b'...'``)
# may precede the quote, so ``COPY t TO E'/x'`` is still seen. Checked BEFORE
# ``EGRESS_COPY`` (a local-file COPY matches both; EXEC wins).
EXEC_COPY = re.compile(
    r"copy\b[\s\S]*?\b(from|to)\s+"
    r"(program\b"
    r"|[a-z]?'\s*(?!@)(?![a-z0-9]+://)"
    r"|[a-z]?\"\s*(?!@)(?![a-z0-9]+://))",
    re.IGNORECASE,
)

# COPY/UNLOAD outbound to an OBJECT STORE / stage (network egress): snowflake
# ``INTO @stage|'scheme://'`` unload; postgres/redshift ``... TO @|s3:|scheme://``.
# ``TO PROGRAM`` and a quoted LOCAL path are NOT here -- they are :data:`EXEC_COPY`.
# The inbound load form (``COPY INTO t FROM @stage``) is a plain write.
EGRESS_COPY = re.compile(
    r"copy\b[\s\S]*?\b(into\s+(@|['\"]?[a-z0-9]+://)|to\s+(@|s3:|['\"]?[a-z0-9]+://))",
    re.IGNORECASE,
)

# Raw-text EXEC patterns, for the paths where sqlglot gives no typed node to read
# (a parse failure -> ``fallback_classify``; a dialect verb -> ``exp.Command`` or
# the "unrecognized statement" branch). Run on ONE statement's own text (never a
# wrapping ``SELECT '...'`` literal, which parses to a Select and never reaches
# here), so a column named ``program`` or a string that merely spells the attack
# keeps its class. EXEC takes precedence over the egress/destroy raw sniffs.
_RAW_EXEC = re.compile(
    # COPY ... FROM/TO PROGRAM (RCE), or FROM/TO a quoted LOCAL path (server fs).
    r"\bcopy\b[\s\S]*?\b(from|to)\s+program\b"
    r"|\bcopy\b[\s\S]*?\b(from|to)\s+[a-z]?['\"]\s*(?!@)(?![a-z0-9]+://)"
    # MySQL LOAD DATA [LOCAL] INFILE (server/client filesystem read).
    r"|\bload\s+data\b[\s\S]*?\binfile\b"
    # SELECT ... INTO OUTFILE/DUMPFILE (server filesystem write).
    r"|\binto\s+(out|dump)file\b"
    # CREATE EXTENSION (loads native server code; dblink / postgres_fdw setup).
    r"|\bcreate\s+extension\b"
    # ATTACH '<file>' -- opens/creates a database file on the server.
    r"|\battach\b(?:\s+database)?\s+[a-z]?['\"]"
    # DuckDB INSTALL/LOAD <extension> (loads native code).
    r"|^\s*(install|load)\s+[a-z_][\w-]*\s*;?\s*$"
    # ClickHouse SYSTEM <subcommand> (admin: RELOAD, SHUTDOWN, KILL, DROP CACHE …),
    # anchored at the statement start so ``SHOW system tables`` is not swept in.
    r"|^\s*system\s+[a-z]"
    # T-SQL xp_cmdshell (shell) reached via EXEC/EXECUTE rather than a function call.
    r"|\bxp_cmdshell\b"
    # Postgres/DuckDB LOAD '<library or file>' (loads native code by path).
    r"|^\s*load\s+[a-z]?['\"]"
    # CREATE [TRUSTED] [PROCEDURAL] LANGUAGE (installs a language handler).
    r"|\bcreate\s+(?:or\s+replace\s+)?(?:trusted\s+)?(?:procedural\s+)?language\b"
    # A routine in C or an untrusted language (see UNTRUSTED_ROUTINE).
    rf"|{UNTRUSTED_ROUTINE.pattern}"
    # ALTER SYSTEM (rewrites server config: archive_command and kin run programs).
    r"|\balter\s+system\s+(?:set|reset)\b",
    re.IGNORECASE,
)

# A ``SET`` that changes the TRANSACTION's read/write mode, the session's
# AUTHORIZATION, or the current ROLE is not a harmless preamble tweak: it can undo
# the ``SET TRANSACTION READ ONLY`` the connector runs, or escalate privileges — so
# such a SET is a WRITE (needs a cap token, refused in read_only), while a plain
# ``SET statement_timeout`` / ``SET search_path`` stays a read. ``transaction`` has
# no word boundary on the left so it also catches ``transaction_read_only`` and
# ``default_transaction_read_only``.
_SET_MODE_WRITE = re.compile(r"transaction|authorization|\brole\b", re.IGNORECASE)


def set_statement_is_write(text: str) -> bool:
    """Whether a ``SET`` statement changes transaction mode / authorization / role
    (a WRITE), rather than a benign session GUC (a read). Shared by the typed
    ``exp.Set`` path and the ``exp.Command(SET)`` fallback so the two never drift."""
    return bool(_SET_MODE_WRITE.search(text))


def looks_like_exec(text: str) -> bool:
    """Whether ONE statement's text names server-side program execution or
    server-filesystem access (the EXEC tier). Applied only to a single statement
    that produced no typed node the classifier could read (a ``exp.Command`` verb
    or the unrecognized-statement branch), never to a wrapping ``SELECT '...'``
    literal — so a column named ``program`` or a string spelling the attack keeps
    its class."""
    return bool(_RAW_EXEC.search(text))


# exp.Command.name (leading verb) -> effect, across the common dialects.
# ``EXPLAIN`` is deliberately NOT here -- ``EXPLAIN ANALYZE`` EXECUTES its body, so it
# goes through ``_classify_explain`` (an unrecognized verb falls closed to write).
# ``SET`` is handled specially (``set_statement_is_write``) BEFORE this table so a
# transaction/role/authorization SET escalates to write.
_CMD_READ = frozenset({"SHOW", "DESCRIBE", "DESC", "LIST", "LS", "USE", "SET", "HELP"})
_CMD_PRIV = frozenset({"GRANT", "REVOKE", "DENY"})  # T-SQL DENY too -> destroy
_CMD_DESTROY = frozenset({"TRUNCATE", "DROP", "REMOVE", "RM", "PURGE", "EXPIRE"})
# ``PUT`` uploads a local file to a remote stage (Snowflake) — network egress,
# grouped with GET / UNLOAD / EXPORT / COPY INTO stage.
_CMD_EGRESS = frozenset({"UNLOAD", "GET", "EXPORT", "DUMP", "OUTFILE", "PUT"})
_CMD_DYNAMIC = frozenset({"EXECUTE", "EXEC", "SP_EXECUTESQL"})
#: Verbs that RUN A PROGRAM or load native code on the server (the EXEC tier):
#: ClickHouse ``SYSTEM`` admin statements, DuckDB ``INSTALL`` <extension>. Other
#: EXEC shapes (``CREATE EXTENSION``, ``ATTACH '<file>'``, ``LOAD`` <extension>)
#: keep their write verb but escalate via the raw-text sniff below.
_CMD_EXEC = frozenset({"SYSTEM", "INSTALL"})
#: Anonymous procedural blocks -- the body (a dollar-quoted literal) can hide a DROP/TRUNCATE/GRANT/
#: COPY...TO, so it is inspected like dynamic SQL rather than trusted as a plain write. Otherwise a
#: ``DO $$ BEGIN DROP TABLE t; END $$`` reads as WRITE and slips past the human destroy-floor.
_CMD_BLOCK = frozenset({"DO"})
_CMD_WRITE = frozenset(
    {
        "CALL",
        "LOAD",
        "IMPORT",
        "BULK",
        "REPLACE",
        "UPSERT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "VACUUM",
        "OPTIMIZE",
        "REINDEX",
        "REFRESH",
        "BACKUP",
        "RESTORE",
        "COMMENT",
        "RENAME",
        "UNDROP",
        "CLUSTER",
        "MSCK",
        "ANALYZE",
        "ALTER",
        "CREATE",
        "ATTACH",
        "DETACH",
        "CHECKPOINT",
    }
)

# A quoted SQL literal carried by a dynamic-exec / anonymous-block command: a single-quoted string
# (``'...'`` with ``''`` escapes, optional ``N`` national prefix), an ANONYMOUS dollar-quote
# (``$$...$$``), or a NAMED dollar-quote (``$tag$...$tag$`` -- Postgres lets a DO/function body pick
# any tag, so ``DO $body$ BEGIN DROP ... END $body$`` must be matched too, not just ``$$``).
_QUOTED = re.compile(
    r"N?'(?P<q>(?:[^']|'')*)'"
    r"|\$\$(?P<anon>[\s\S]*?)\$\$"
    r"|\$(?P<tag>[A-Za-z_]\w*)\$(?P<named>[\s\S]*?)\$(?P=tag)\$",
    re.IGNORECASE,
)

# Raw-text sniffs used ONLY on the fail-closed path (when strict parsing raised,
# e.g. dialect-specific OUTFILE / EXEC sqlglot can't fully parse) -- they refine
# the safe ``write`` default into the more precise tier.
_RAW_DYNAMIC = re.compile(r"\b(execute\s+immediate|execute|exec|sp_executesql)\b", re.IGNORECASE)
_RAW_EGRESS = re.compile(
    # Kept in lockstep with EGRESS_COPY (the parse-success path): an UNLOAD /
    # EXPORT DATA, or a COPY to an object store / stage that sqlglot couldn't
    # parse. The server-filesystem / program COPY and INTO OUTFILE forms are the
    # stricter EXEC tier now (``_RAW_EXEC``, checked first), so they are not here.
    r"\bunload\b|\bexport\s+data\b"
    r"|\bcopy\b[\s\S]*?\b(into\s+(@|['\"]?[a-z0-9]+://)|to\s+(@|s3:|['\"]?[a-z0-9]+://))",
    re.IGNORECASE,
)
_RAW_DESTROY = re.compile(r"\b(drop|truncate|grant|revoke|deny|remove|purge)\b", re.IGNORECASE)

# ``EXPLAIN`` is a plan dump ONLY without ``ANALYZE``. ``EXPLAIN ANALYZE`` (Postgres
# also spells it ``ANALYSE``) RUNS the wrapped statement on Postgres/Redshift/MySQL/
# Trino/Databricks, so the body has to be classified rather than waved through as a
# read. Split the leading verb + its option block (``ANALYZE VERBOSE`` or
# ``(ANALYZE, BUFFERS)``) off the body, which starts at the first statement keyword.
_EXPLAIN_BODY = re.compile(
    r"^\s*EXPLAIN\b(?P<opts>[\s\S]*?)"
    r"(?P<body>\b(?:WITH|SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE"
    r"|GRANT|REVOKE|COPY|CALL|EXECUTE|EXEC|REPLACE|UPSERT|LOAD|UNLOAD|EXPORT|VALUES"
    r"|TABLE)\b[\s\S]*)$",
    re.IGNORECASE,
)
_EXPLAIN_ANALYZE = re.compile(r"\banaly[sz]e\b", re.IGNORECASE)
# What may legitimately sit between ``EXPLAIN`` and its body: bare option keywords
# (``ANALYZE VERBOSE``), a parenthesized list (``(ANALYZE, BUFFERS)``), a ``FORMAT=TREE``
# assignment. Anything else there (a quote, an operator, a leftover comment delimiter)
# means the split did NOT isolate the real statement, so the ANALYZE form must not be
# allowed to rest on the inner classification.
_EXPLAIN_OPTS = re.compile(r"[\sA-Za-z0-9_(),=]*")


def _blank_comments(sql: str) -> str:
    """``--``/``#`` line comments and ``/* ... */`` block comments replaced by spaces,
    string literals left intact and the LENGTH preserved -- so an offset into the result
    still indexes the original text.

    A comment is otherwise free to hijack a keyword split: ``EXPLAIN ANALYZE /* SELECT */
    DELETE FROM t`` splits at the ``SELECT`` *inside the comment*, and the remainder
    re-classifies as a plain read while the DELETE executes."""
    out = list(sql)
    i = 0
    n = len(sql)
    quote = ""
    while i < n:
        ch = sql[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            i += 1
            continue
        if sql.startswith(("--", "#"), i):
            while i < n and sql[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            for j in range(i, stop):
                if out[j] != "\n":
                    out[j] = " "
            i = stop
            continue
        i += 1
    return "".join(out)


def _classify_explain(
    stmt: exp.Command, dialect: str, depth: int
) -> tuple[Effect, str, list[str], bool]:
    """``EXPLAIN`` is a read ONLY without ``ANALYZE``.

    ``EXPLAIN ANALYZE <stmt>`` EXECUTES the wrapped statement, so its effect is
    inherited from the body -- otherwise an ``EXPLAIN ANALYZE DELETE`` runs (and, on an
    auto-commit engine with an empty read-only preamble, commits) as an auto-allowed
    read, and an ``EXPLAIN ANALYZE SELECT pg_read_file(...)`` hides the dangerous-function
    walk behind the wrapper.

    The split is located on a COMMENT-BLANKED copy (a comment between the verb and the
    body would otherwise hijack it) and is only trusted when everything preceding the
    body parses as an EXPLAIN option list -- an ANALYZE form whose body we can't prove we
    isolated never rests at READ."""
    raw = stmt.sql()
    scan = _blank_comments(raw)
    match = _EXPLAIN_BODY.match(scan)
    opts = match.group("opts") if match is not None else scan
    if not _EXPLAIN_ANALYZE.search(opts):
        return Effect.READ, "explain", [], False
    # ``scan`` only LOCATES the body; the slice comes from the original text so a comment
    # inside the body still reaches the inner classifier verbatim.
    body = raw[match.start("body") :] if match is not None else ""
    if body and _EXPLAIN_OPTS.fullmatch(opts) and depth < MAX_DYNAMIC_DEPTH:
        from alkera_cli.plugins.plugin_base.permissions.classifier import descriptor_from_sql

        inner = descriptor_from_sql(body, dialect=dialect, _depth=depth + 1)
        return (
            effect_max(Effect.READ, inner.effect),
            inner.operation or "explain_analyze",
            [f"EXPLAIN ANALYZE executes the wrapped {inner.operation}", *inner.reasons],
            True,
        )
    # An ANALYZE form whose body we can't isolate must never rest at READ.
    effect = Effect.WRITE
    if _RAW_EXEC.search(raw):
        effect = Effect.EXEC
    elif _RAW_DESTROY.search(raw):
        effect = Effect.DESTROY
    elif _RAW_EGRESS.search(raw):
        effect = Effect.EGRESS
    return (
        effect,
        "explain_analyze",
        ["EXPLAIN ANALYZE executes its statement (body could not be isolated)"],
        True,
    )


#: ``PREPARE name [(types)] AS <statement>`` (Postgres); the MySQL ``PREPARE name
#: FROM '<sql>'`` form carries its body as a literal instead.
_PREPARE_BODY = re.compile(
    r"^\s*prepare\s+[\w\"$]+(?:\s*\([^)]*\))?\s+as\s+(?P<body>[\s\S]+)$", re.IGNORECASE
)


def _classify_prepare(
    stmt: exp.Command, dialect: str, depth: int
) -> tuple[Effect, str, list[str], bool]:
    """A ``PREPARE`` is at least a write (it defines session state), and its BODY is
    what a later ``EXECUTE`` runs with no literal left to read, so the body is
    classified here and the higher tier wins: a prepared ``pg_read_file`` is EXEC,
    a prepared ``DELETE`` DESTROY."""
    raw = stmt.sql()
    match = _PREPARE_BODY.match(raw)
    if match is not None:
        from alkera_cli.plugins.plugin_base.permissions.classifier import descriptor_from_sql

        inner = descriptor_from_sql(match.group("body"), dialect=dialect, _depth=depth + 1)
        inner_effect, inner_reasons = inner.effect, inner.reasons
    else:
        inner_effect, inner_reasons = reclassify_dynamic(raw, dialect, depth + 1)
    return (
        effect_max(Effect.WRITE, inner_effect),
        "prepare",
        ["PREPARE defines a statement a later EXECUTE runs", *inner_reasons],
        True,
    )


def classify_command(
    stmt: exp.Command, dialect: str, depth: int
) -> tuple[Effect, str, list[str], bool]:
    """Classify a fallback ``exp.Command`` by its leading verb. Returns
    ``(effect, operation, reasons, heuristic)``."""
    verb = (stmt.name or "").upper()
    op = verb.lower() or "command"
    if verb == "EXPLAIN":
        return _classify_explain(stmt, dialect, depth)
    if verb == "SET" and set_statement_is_write(stmt.sql()):
        # A transaction-mode / authorization / role SET — not a benign preamble GUC.
        return (
            Effect.WRITE,
            "set",
            ["SET changes transaction mode / authorization / role (not a read)"],
            False,
        )
    if verb in _CMD_EXEC or _RAW_EXEC.search(stmt.sql()):
        # Runs a program or reaches the server filesystem: ``CREATE EXTENSION``,
        # ``ATTACH '<file>'``, DuckDB ``INSTALL``/``LOAD`` <extension>, ClickHouse
        # ``SYSTEM``, a COPY FROM/TO PROGRAM or a local file, LOAD DATA INFILE.
        # The EXEC tier — the read-only transaction and the rollback never reach it.
        return (
            Effect.EXEC,
            "server_exec",
            [f"{verb} runs a program or reaches the server filesystem"],
            True,
        )
    if verb == "PREPARE" and depth < MAX_DYNAMIC_DEPTH:
        return _classify_prepare(stmt, dialect, depth)
    if verb in _CMD_READ:
        return Effect.READ, op, [], False
    if verb in _CMD_PRIV:
        return Effect.DESTROY, op, ["privilege change"], False
    if verb in _CMD_DESTROY:
        return Effect.DESTROY, op, [f"{verb} (destructive)"], False
    if verb in _CMD_EGRESS:
        return Effect.EGRESS, op, [f"{verb} (data egress)"], False
    if verb in (_CMD_DYNAMIC | _CMD_BLOCK) and depth < MAX_DYNAMIC_DEPTH:
        # Inspect the body literal so a DROP/TRUNCATE/GRANT/COPY...TO hidden inside an
        # EXECUTE/EXEC or an anonymous DO block escalates to DESTROY/EGRESS instead of resting
        # at WRITE (which would slip past the deterministic human destroy-floor in auto mode).
        effect, reasons = reclassify_dynamic(stmt.sql(), dialect, depth + 1)
        kind = "anonymous block" if verb in _CMD_BLOCK else "dynamic SQL"
        return effect, "dynamic_sql", [f"{kind}: " + "; ".join(reasons or ["opaque"])], True
    if verb in _CMD_WRITE:
        # A multi-action / unusually-shaped statement sqlglot couldn't TYPED-parse falls back to a
        # Command whose verb alone reads as a plain write -- but the OPAQUE body may carry a
        # destructive or egress sub-action the typed walk would have caught, e.g. a mixed
        # ``ALTER TABLE t ADD COLUMN x, DROP COLUMN y`` (irreversible column loss) or a COPY...TO.
        # Sniff the raw body so it escalates to DESTROY/EGRESS instead of auto-allowing past the
        # human floor (fail-closed: over-prompting is acceptable, under-prompting is not).
        raw = stmt.sql()
        if _RAW_DESTROY.search(raw):
            return Effect.DESTROY, op, [f"{verb} with a destructive sub-action"], True
        if _RAW_EGRESS.search(raw):
            return Effect.EGRESS, op, [f"{verb} with a data-egress sub-action"], True
        return Effect.WRITE, op, [f"{verb} (mutating)"], False
    # New command verb (not in the known set) -> fail closed to write (NEVER read).
    # Framed as new-to-permissions, not invalid.
    return (
        Effect.WRITE,
        op or "command",
        [f'New command "{verb}" (treated as mutating)'],
        True,
    )


def reclassify_dynamic(raw: str, dialect: str, depth: int) -> tuple[Effect, list[str]]:
    """Extract quoted SQL literals from a dynamic-exec command + classify them.
    No literal (bind variable) -> write (assume mutating)."""
    from alkera_cli.plugins.plugin_base.permissions.classifier import descriptor_from_sql

    effect = Effect.READ
    reasons: list[str] = []
    found = False
    for m in _QUOTED.finditer(raw):
        # Pick the BODY group explicitly (a named dollar-quote also captures its tag, which must
        # NOT be mistaken for the body).
        inner = m.group("q") or m.group("anon") or m.group("named") or ""
        inner = inner.replace("''", "'").strip()
        # Skip the leading IMMEDIATE keyword / non-SQL fragments cheaply.
        if not inner or " " not in inner:
            continue
        found = True
        sub = descriptor_from_sql(inner, dialect=dialect, _depth=depth)
        effect = effect_max(effect, sub.effect)
        reasons.extend(sub.reasons)
    if not found:
        return Effect.WRITE, ["opaque dynamic SQL (no literal to inspect)"]
    return effect, reasons


def fallback_classify(sql: str, dialect: str, depth: int) -> tuple[list[StatementInfo], Confidence]:
    """Refine the fail-closed path for SQL strict-parsing rejected (dialect
    OUTFILE / EXEC / dynamic). Never returns ``read`` -- defaults to ``write`` and
    escalates on raw outbound / destructive / dynamic patterns."""
    # Dynamic exec -> reclassify the inner literal (a dynamic DROP is a destroy).
    if _RAW_DYNAMIC.search(sql) and depth < MAX_DYNAMIC_DEPTH:
        inner_effect, inner_reasons = reclassify_dynamic(sql, dialect, depth + 1)
        return (
            [
                StatementInfo(
                    effect=inner_effect,
                    operation="dynamic_sql",
                    has_where=False,
                    reasons=["dynamic SQL: " + "; ".join(inner_reasons or ["opaque"])],
                    heuristic=True,
                )
            ],
            "heuristic",
        )
    effect = Effect.WRITE
    reasons = ["unparseable SQL (fail-closed to write)"]
    if _RAW_EXEC.search(sql):
        effect = effect_max(effect, Effect.EXEC)
        reasons.append(
            "server program / filesystem pattern "
            "(COPY FROM/TO PROGRAM or a local file, LOAD DATA INFILE, INTO OUTFILE, "
            "CREATE EXTENSION, ATTACH, INSTALL/LOAD, SYSTEM)"
        )
    if _RAW_EGRESS.search(sql):
        effect = effect_max(effect, Effect.EGRESS)
        reasons.append("outbound data pattern (UNLOAD/EXPORT/COPY TO a stage)")
    if _RAW_DESTROY.search(sql):
        effect = effect_max(effect, Effect.DESTROY)
        reasons.append("destructive keyword")
    return (
        [
            StatementInfo(
                effect=effect, operation="unknown", has_where=False, reasons=reasons, heuristic=True
            )
        ],
        "unknown",
    )


__all__ = [
    "DANGEROUS_FUNCS",
    "DUCKDB_FILE_TABLE",
    "EGRESS_COPY",
    "EXEC_COPY",
    "EXEC_FUNCS",
    "MAX_DYNAMIC_DEPTH",
    "REMOTE_EXEC_FUNCS",
    "SEVERITY",
    "SIDE_EFFECT_FUNCS",
    "UNTRUSTED_ROUTINE",
    "URL_TABLE",
    "classify_command",
    "effect_max",
    "exec_funcs_for",
    "fallback_classify",
    "looks_like_exec",
    "reclassify_dynamic",
    "set_statement_is_write",
]
