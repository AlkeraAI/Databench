"""The sqlglot-AST SQL classifier — the killer-feature firewall.

Reduces SQL (any of sqlglot's ~20 dialects) to an ``ActionDescriptor`` whose
``effect`` ∈ {read, write, destroy, egress, exec} is the load-bearing axis
(``exec`` = a program or filesystem reach on the DATA host itself — the strictest
tier, never auto-allowed, refused even in bypass without a rule). Rigor
properties:

- **Walk the WHOLE tree** — a data-modifying CTE (``WITH d AS (DELETE …) …``)
  parses to a ``Select`` root but is a DELETE; the effect is the MAX over every
  DML/DDL node anywhere in the tree.
- **Asymmetric escalation:** a missing OR trivially-true (``1=1`` / ``TRUE``)
  WHERE turns UPDATE/DELETE from ``write`` into ``destroy``; ``CREATE OR REPLACE``
  is ``destroy`` (it drops the existing object); ``ALTER … DROP COLUMN`` is
  ``destroy`` (data loss); ``MERGE`` is a single recoverable ``write`` (its inner
  UPDATE/DELETE clauses don't escalate).
- **Cross-dialect Command coverage:** sqlglot falls back to ``exp.Command`` for
  many dialect verbs (TRUNCATE/CALL/GET/PUT/UNLOAD/EXPORT/EXECUTE/REMOVE/…); we
  map every known mutating/destructive/egress verb, and ANY unrecognized command
  fails closed to ``write`` + ``heuristic`` (NEVER ``read``).
- **Dynamic SQL** (``EXECUTE IMMEDIATE '…'`` / ``EXEC`` / ``sp_executesql``):
  the inner string literal is extracted and RE-CLASSIFIED (heuristic) — a
  dynamic ``DROP`` is still a destroy.
- **Dangerous functions** (per dialect) → ``egress``. Null-byte + length guards.
- **Fail-closed:** unparseable / unrecognized → ``write`` + ``confidence="unknown"``.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import expressions as exp
from sqlglot.dialects.dialect import Dialect

from alkera_cli.contracts.tool_types import (
    ActionDescriptor,
    Confidence,
    Effect,
    ResourceRef,
    StatementInfo,
)
from alkera_cli.plugins.plugin_base.permissions.commands import (
    DANGEROUS_FUNCS,
    DUCKDB_FILE_TABLE,
    EGRESS_COPY,
    EXEC_COPY,
    MAX_DYNAMIC_DEPTH,
    REMOTE_EXEC_FUNCS,
    SEVERITY,
    SIDE_EFFECT_FUNCS,
    UNTRUSTED_ROUTINE,
    URL_TABLE,
    exec_funcs_for,
    fallback_classify,
    looks_like_exec,
    reclassify_dynamic,
    set_statement_is_write,
)
from alkera_cli.plugins.plugin_base.permissions.commands import (
    classify_command as _classify_command,
)
from alkera_cli.plugins.plugin_base.permissions.commands import (
    effect_max as _max,
)

_MAX_SQL_BYTES = 100_000

#: sqlglot node types (by class name) that are the EXEC tier but sit OUTSIDE
#: ``_KNOWN_STATEMENTS`` and render to an empty ``.sql()`` — so the unrecognized
#: branch cannot read them off text. ``Install`` is DuckDB ``INSTALL <ext>``
#: (loads native code); ``LoadData`` is MySQL ``LOAD DATA INFILE`` (server fs).
_EXEC_STATEMENT_TYPES: frozenset[str] = frozenset({"Install", "LoadData"})


def _targets(stmt: exp.Expression) -> list[ResourceRef]:
    seen: set[str] = set()
    out: list[ResourceRef] = []
    for table in stmt.find_all(exp.Table):
        name = table.sql()
        if name and name not in seen:
            seen.add(name)
            out.append(ResourceRef(kind="table", name=name))
    return out


def _with_columns(info: StatementInfo, stmt: exp.Expression) -> StatementInfo:
    """Stamp the columns an ALTER drops onto its statement. The gate resolves
    impact at column grain, which is the difference between "this table has 40
    consumers" and "these 3 read the column you are removing"."""
    dropped = [
        drop.this.name
        for drop in stmt.find_all(exp.Drop)
        if str(drop.args.get("kind") or "").lower() == "column"
        and drop.this is not None
        and drop.this.name
    ]
    return info if not dropped else info.model_copy(update={"columns": dropped})


# sqlglot parses some dialect builtins into TYPED nodes whose ``.name`` is NOT the function name
# (``exp.ReadCSV.name`` is the FILE PATH; ``exp.ReadParquet.name`` is ``""``), so the generic
# name/typename check below silently misses them — an arbitrary-file read slipping the egress gate.
# Map the typed reader nodes back to their deny-set key.
_TYPED_FUNC_KEYS: dict[type[exp.Expression], str] = {
    exp.ReadCSV: "read_csv",
    exp.ReadParquet: "read_parquet",
}


def _func_names(stmt: exp.Expression) -> set[str]:
    """Every function name invoked anywhere in the tree, lower-cased. A typed
    reader node maps back to its deny-set key (``exp.ReadCSV.name`` is the file
    path, not ``read_csv``)."""
    names: set[str] = set()
    for anon in stmt.find_all(exp.Anonymous):
        if anon.name:
            names.add(anon.name.lower())
    for func in stmt.find_all(exp.Func):
        key = next((name for typ, name in _TYPED_FUNC_KEYS.items() if isinstance(func, typ)), "")
        if not key:
            key = (getattr(func, "name", "") or type(func).__name__).lower()
        names.add(key)
    return names


def _dangerous(stmt: exp.Expression, dialect: str) -> list[str]:
    deny = (
        DANGEROUS_FUNCS.get(dialect, set())
        | DANGEROUS_FUNCS["_universal"]
        | REMOTE_EXEC_FUNCS.get(dialect, set())
    )
    return sorted(_func_names(stmt) & deny)


def _side_effect(stmt: exp.Expression, dialect: str) -> list[str]:
    """Local side-effecting functions (terminate a session, reload config, reset
    stats, take an advisory lock, persist a GUC) — invisible to the read-only
    transaction and the rollback, so the classifier floors them to WRITE."""
    return sorted(_func_names(stmt) & SIDE_EFFECT_FUNCS.get(dialect, set()))


def _exec_funcs(stmt: exp.Expression, dialect: str) -> list[str]:
    """Functions that RUN A PROGRAM or read/write the server's FILESYSTEM (the
    EXEC tier): ``pg_read_file`` / ``lo_export``, ``sys_exec``,
    ``system$execute_program``, ``xp_cmdshell``, ClickHouse ``file`` /
    ``executable``, ``load_extension``. Floored to EXEC — the read-only
    transaction and the rollback never reach the host they touch."""
    return sorted(_func_names(stmt) & exec_funcs_for(dialect))


def _file_tables(stmt: exp.Expression, dialect: str) -> list[str]:
    """Tables named by a quoted path or URL, which the engine reads as a file or
    an object store (:data:`URL_TABLE`, and on DuckDB :data:`DUCKDB_FILE_TABLE`).
    An unquoted name is a table, never a file."""
    out: list[str] = []
    for table in stmt.find_all(exp.Table):
        ident = table.this
        quoted = isinstance(ident, exp.Identifier) and bool(ident.args.get("quoted"))
        literal = isinstance(ident, exp.Literal) and ident.is_string  # Snowflake keeps the string
        if not (quoted or literal):
            continue
        name = ident.name
        if URL_TABLE.search(name) or (dialect == "duckdb" and DUCKDB_FILE_TABLE.search(name)):
            out.append(name)
    return out


def _pragma_assigns(name_node: exp.Expression, sql: str) -> bool:
    """Whether ``sql`` spells an assignment to the pragma ``name_node`` names."""
    name = getattr(name_node, "name", "") or ""
    if not name:
        return False
    return bool(
        re.search(rf"\bpragma\s+(?:[\w\"]+\.)?\"?{re.escape(name)}\"?\s*=", sql, re.IGNORECASE)
    )


def _trivial_true(node: exp.Expression | None) -> bool:
    """A WHERE predicate that's a constant true (``TRUE`` / ``1`` / ``1=1`` /
    ``'a'='a'``) — a tautology that makes UPDATE/DELETE an effective mass mutation."""
    if node is None:
        return False
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal):
        return node.name not in ("0", "", "false", "False")
    if (
        isinstance(node, exp.EQ)
        and isinstance(node.left, exp.Literal)
        and isinstance(node.right, exp.Literal)
    ):
        return node.left.name == node.right.name
    if isinstance(node, exp.Paren):
        return _trivial_true(node.this)
    return False


def _guarded(node: exp.Update | exp.Delete) -> bool:
    where = node.args.get("where")
    if where is None:
        return False
    return not _trivial_true(where.this)


def _classify_one(
    stmt: exp.Expression, dialect: str, depth: int, original_sql: str
) -> StatementInfo:
    if isinstance(stmt, exp.Command):
        effect, operation, reasons, heuristic = _classify_command(stmt, dialect, depth)
        return StatementInfo(
            effect=effect,
            operation=operation,
            targets=_targets(stmt),
            dangerous_functions=_dangerous(stmt, dialect),
            reasons=reasons,
            heuristic=heuristic,
        )

    # Transaction control (BEGIN / COMMIT / END / ROLLBACK / SAVEPOINT variants)
    # is a WRITE, not a read: a COMMIT inside a chain ends the connector's
    # read-only transaction (so the always-rollback undoes nothing), and a
    # SET TRANSACTION READ WRITE re-opens writes. It needs a cap token, so a
    # read-only session refuses it and a multi-statement chain can't smuggle one.
    _txn_types = tuple(
        t
        for t in (getattr(exp, n, None) for n in ("Transaction", "Commit", "Rollback"))
        if isinstance(t, type)
    )
    if _txn_types and isinstance(stmt, _txn_types):
        return StatementInfo(
            effect=Effect.WRITE,
            operation="transaction_control",
            targets=_targets(stmt),
            reasons=["transaction control (BEGIN/COMMIT/ROLLBACK) is not a read"],
        )
    # A typed ``exp.Set`` whose target is the transaction mode / authorization /
    # role is a WRITE (see ``set_statement_is_write``); a plain session GUC
    # (``SET statement_timeout``) stays a read.
    _set_type = getattr(exp, "Set", None)
    if (
        isinstance(_set_type, type)
        and isinstance(stmt, _set_type)
        and set_statement_is_write(stmt.sql())
    ):
        return StatementInfo(
            effect=Effect.WRITE,
            operation="set",
            targets=_targets(stmt),
            reasons=["SET changes transaction mode / authorization / role (not a read)"],
        )

    effect = Effect.READ
    operation = type(stmt).__name__.lower()
    reasons = []
    has_where = True
    heuristic = False

    # Dynamic SQL as a typed node (T-SQL EXEC / sp_executesql) → reclassify the
    # inner literal (a dynamic DROP is still a destroy).
    _execute_types = tuple(
        t for t in (getattr(exp, n, None) for n in ("Execute", "ExecuteSql")) if isinstance(t, type)
    )
    if _execute_types and isinstance(stmt, _execute_types):
        # ``EXEC xp_cmdshell 'whoami'`` parses to a typed Execute node whose
        # ``.name`` is the procedure and whose ``.sql()`` is empty — so the
        # function-name walk can't see it. A named exec-tier procedure (T-SQL
        # ``xp_cmdshell``) is EXEC regardless of its arguments.
        if (stmt.name or "").lower() in exec_funcs_for(dialect):
            return StatementInfo(
                effect=Effect.EXEC,
                operation="server_exec",
                targets=_targets(stmt),
                reasons=[f"{stmt.name} runs a program on the server"],
                heuristic=True,
            )
    if _execute_types and isinstance(stmt, _execute_types) and depth < MAX_DYNAMIC_DEPTH:
        # Reclassify the ORIGINAL sql — sqlglot rewrites the inner string literal
        # (e.g. T-SQL ``'DROP TABLE t'`` → ``[DROP TABLE t]``), stripping the
        # quotes the literal extractor needs.
        inner_effect, inner_reasons = reclassify_dynamic(original_sql, dialect, depth + 1)
        return StatementInfo(
            effect=inner_effect,
            operation="dynamic_sql",
            targets=_targets(stmt),
            reasons=["dynamic SQL: " + "; ".join(inner_reasons or ["opaque"])],
            heuristic=True,
        )
    # BigQuery EXPORT DATA … → egress.
    _export = getattr(exp, "Export", None)
    if isinstance(_export, type) and list(stmt.find_all(_export)):
        operation = "export"
        reasons.append("EXPORT DATA (egress)")
        effect = _max(effect, Effect.EGRESS)
    # Snowflake GET @stage <local> downloads staged data to the client filesystem = egress. The
    # QUOTED-destination form parses to a typed exp.Get (not a Command(GET)), so the verb-based
    # egress set misses it and it defaults to a plain write. PUT (local→stage upload) is a write.
    _get = getattr(exp, "Get", None)
    if isinstance(_get, type) and list(stmt.find_all(_get)):
        operation = "get"
        reasons.append("GET stage download (egress)")
        effect = _max(effect, Effect.EGRESS)
    _put = getattr(exp, "Put", None)
    if isinstance(_put, type) and list(stmt.find_all(_put)):
        # PUT uploads a local file to a Snowflake stage — network egress (the
        # stage is remote), grouped with GET / COPY INTO stage.
        operation = "put"
        reasons.append("PUT uploads to a stage (network egress)")
        effect = _max(effect, Effect.EGRESS)
    # SELECT … INTO OUTFILE/DUMPFILE (mysql) writes the SERVER filesystem → EXEC;
    # SELECT … INTO <table> → write.
    into = stmt.args.get("into") if isinstance(stmt, (exp.Select, exp.Union)) else None
    if into is not None:
        into_sql = into.sql().upper()
        if "OUTFILE" in into_sql or "DUMPFILE" in into_sql:
            operation = "select_into_outfile"
            reasons.append("SELECT INTO OUTFILE/DUMPFILE writes the server filesystem")
            effect = _max(effect, Effect.EXEC)
        else:
            operation = "select_into"
            effect = _max(effect, Effect.WRITE)

    # A PRAGMA that ASSIGNS a value mutates engine/session state (sqlite
    # `PRAGMA journal_mode = WAL`, `PRAGMA writable_schema = ON`, `PRAGMA synchronous = OFF`,
    # `PRAGMA cache_size = 2000`) — a state change the gate must not wave through as a READ.
    # The query forms stay reads: a bare `PRAGMA journal_mode`, and the introspection call
    # form `PRAGMA table_info('t')` (which sqlglot parses as an EQ whose RHS is a STRING
    # literal — the argument). A quoted setter value (`journal_mode = 'WAL'`) is
    # indistinguishable from a string-arg call form in the parse tree, so the statement
    # text decides it: an ``=`` after the pragma's name is an assignment
    # (DuckDB ``PRAGMA memory_limit = '1GB'``), the call form has none.
    _pragma = getattr(exp, "Pragma", None)
    if isinstance(_pragma, type) and isinstance(stmt, _pragma):
        body = stmt.this
        rhs = body.expression if isinstance(body, exp.EQ) else None
        if rhs is not None and (
            not (isinstance(rhs, exp.Literal) and rhs.is_string)
            or _pragma_assigns(body.this, original_sql)
        ):
            operation = "pragma_set"
            reasons.append("PRAGMA assigns engine/session state (write)")
            effect = _max(effect, Effect.WRITE)

    for drop in stmt.find_all(exp.Drop):
        kind = str(drop.args.get("kind") or "object").lower()
        operation = f"drop_{kind}"
        reasons.append(f"DROP {kind.upper()}")
        effect = _max(effect, Effect.DESTROY)
    if list(stmt.find_all(exp.TruncateTable)):
        operation = "truncate"
        reasons.append("TRUNCATE")
        effect = _max(effect, Effect.DESTROY)
    grants = list(stmt.find_all(exp.Grant))
    revokes = list(stmt.find_all(exp.Revoke))
    if grants or revokes:
        operation = "grant" if grants else "revoke"
        reasons.append("privilege change (GRANT/REVOKE)")
        effect = _max(effect, Effect.DESTROY)
    is_merge = bool(list(stmt.find_all(exp.Merge)))
    if is_merge:
        operation = "merge"
        effect = _max(effect, Effect.WRITE)
    for upd in stmt.find_all(exp.Update):
        if upd.find_ancestor(exp.Merge):
            continue
        operation = "update"
        if _guarded(upd):
            effect = _max(effect, Effect.WRITE)
        else:
            has_where = False
            reasons.append("UPDATE without an effective WHERE (mass mutation)")
            effect = _max(effect, Effect.DESTROY)
    for dele in stmt.find_all(exp.Delete):
        if dele.find_ancestor(exp.Merge):
            continue
        operation = "delete"
        # Deletion is the canonical destructive op (mirrors `rm` in the shell
        # classifier): a DELETE removes rows IRREVERSIBLY, so it's always DESTROY
        # — a WHERE narrows the blast radius but never makes it recoverable, so it
        # stays on the floor (prompts under auto/default; bypass waives it).
        effect = _max(effect, Effect.DESTROY)
        if _guarded(dele):
            reasons.append("DELETE removes rows (irreversible)")
        else:
            has_where = False
            reasons.append("DELETE without an effective WHERE (mass mutation)")
    inserts = list(stmt.find_all(exp.Insert))
    if inserts:
        operation = "merge" if is_merge else "insert"
        effect = _max(effect, Effect.WRITE)
        for ins in inserts:
            # A MERGE's WHEN-NOT-MATCHED-THEN-INSERT is a merge clause, not a standalone
            # INSERT OVERWRITE — it carries neither the `overwrite` arg nor a Directory target.
            if ins.find_ancestor(exp.Merge):
                continue
            if isinstance(ins.this, exp.Directory):
                # INSERT OVERWRITE [LOCAL] DIRECTORY '<path|s3://…>' SELECT … writes the
                # result set OUT to an arbitrary path/object-store URI (databricks/spark/hive)
                # — a server-side egress channel, like COPY/UNLOAD TO, so it must hit the
                # egress floor, not rest at a plain WRITE.
                operation = "insert_overwrite_directory"
                reasons.append(
                    "INSERT OVERWRITE DIRECTORY writes rows to an external path (egress)"
                )
                effect = _max(effect, Effect.EGRESS)
            elif ins.args.get("overwrite"):
                # INSERT OVERWRITE TABLE atomically REPLACES all existing rows (or matched
                # partitions) — irreversible, semantically TRUNCATE+reload — so it's DESTROY
                # and floored, mirroring CREATE OR REPLACE below and TRUNCATE above.
                operation = "insert_overwrite"
                reasons.append("INSERT OVERWRITE replaces existing rows (irreversible)")
                effect = _max(effect, Effect.DESTROY)
    for create in stmt.find_all(exp.Create):
        if UNTRUSTED_ROUTINE.search(create.sql(dialect=dialect or None)):
            # A routine in C or an untrusted language runs on the host as the
            # server user; a later ``SELECT f()`` reads as a plain read.
            operation = "create_untrusted_routine"
            reasons.append("a function in C or an untrusted language runs on the server")
            effect = _max(effect, Effect.EXEC)
            continue
        if create.args.get("replace"):
            operation = "create_or_replace"
            reasons.append("CREATE OR REPLACE (drops the existing object)")
            effect = _max(effect, Effect.DESTROY)
        else:
            operation = "create"
            effect = _max(effect, Effect.WRITE)
    if list(stmt.find_all(exp.Alter)):
        # ALTER … DROP COLUMN is already DESTROY via the Drop walk above; a plain
        # ALTER is a recoverable write.
        if effect != Effect.DESTROY:
            operation = "alter"
            effect = _max(effect, Effect.WRITE)
    for _copy in stmt.find_all(exp.Copy):
        # Render with the connection dialect so a Postgres ``COPY t TO E'/x'`` keeps its OUTBOUND
        # ``TO`` direction/destination — the dialect-less render mangles it to the inbound
        # ``COPY INTO t`` and the destination test never sees the exfil target.
        rendered = _copy.sql(dialect=dialect or None)
        if EXEC_COPY.search(rendered):
            # FROM/TO PROGRAM (RCE) or FROM/TO a local file (server filesystem) —
            # the EXEC tier, checked BEFORE egress (a local-file COPY matches both).
            operation = "copy_program" if "program" in rendered.lower() else "copy_file"
            reasons.append("COPY runs a program / reaches the server filesystem (exec)")
            effect = _max(effect, Effect.EXEC)
        elif EGRESS_COPY.search(rendered):
            operation = "copy"
            reasons.append("COPY/UNLOAD to an external stage or object store (egress)")
            effect = _max(effect, Effect.EGRESS)
        else:
            operation = "copy"
            effect = _max(effect, Effect.WRITE)

    exec_funcs = _exec_funcs(stmt, dialect)
    if exec_funcs:
        operation = "server_exec"
        reasons.append(
            f"{', '.join(exec_funcs)} runs a program or reaches the server "
            "filesystem — the read-only transaction and rollback don't reach it"
        )
        effect = _max(effect, Effect.EXEC)

    file_tables = _file_tables(stmt, dialect)
    if file_tables:
        reasons.append(f"reads a file or object store named as a table: {', '.join(file_tables)}")
        effect = _max(effect, Effect.EGRESS)

    dangerous = _dangerous(stmt, dialect)
    if dangerous:
        reasons.append(f"dangerous function(s): {', '.join(dangerous)}")
        effect = _max(effect, Effect.EGRESS)
        remote = sorted(set(dangerous) & REMOTE_EXEC_FUNCS.get(dialect, set()))
        if remote:
            operation = "remote_exec"
            reasons.append(
                f"{', '.join(remote)} runs SQL on another server — the read-only "
                "transaction and the rollback don't reach it"
            )
            effect = _max(effect, Effect.DESTROY)

    side_effects = _side_effect(stmt, dialect)
    if side_effects and effect == Effect.READ:
        # A local side-effecting function in an otherwise-read statement: floor it
        # to WRITE so the read-only session refuses it (the read-only transaction
        # and the rollback never see a session kill / config reload / stat reset).
        operation = "side_effect_function"
        reasons.append(
            f"{', '.join(side_effects)} changes server state the read-only "
            "transaction and rollback can't undo"
        )
        effect = _max(effect, Effect.WRITE)

    return StatementInfo(
        effect=effect,
        operation=operation,
        targets=_targets(stmt),
        has_where=has_where,
        dangerous_functions=dangerous,
        reasons=reasons,
        heuristic=heuristic,
    )


#: Connection dialect names sqlglot doesn't know, mapped to the grammar the system actually
#: speaks. A Tinybird connection IS a ClickHouse query engine, so its SQL must be parsed with the
#: ClickHouse grammar — the same one the connector's own ``classifier_dialect`` already uses. An
#: alias only ever resolves to a MORE specific grammar than the neutral fallback below, so it
#: narrows what the gate can prove read-only; it can never widen it.
_DIALECT_ALIASES = {"tinybird": "clickhouse"}


def _valid_dialect(dialect: str) -> str:
    """``dialect`` if sqlglot recognizes it, else ``""`` (the neutral parser). A stale or
    differently-spelled dialect name — e.g. ``sqlserver`` (sqlglot spells it ``tsql``) on a
    generic_sql connection — must NOT make ``sqlglot.parse`` raise and drop the whole
    classification to ``unknown``: that desyncs the GATE's classification (built from
    ``conn.dialect``) from the connector's own neutral-dialect classifier, so an approved write's
    cap-token fingerprint never matches and the write is permanently refused. Degrading an unknown
    dialect NAME to neutral keeps the two in lockstep; a real SQL SYNTAX error still parses (with
    the neutral grammar) and falls back fail-closed exactly as before. A name in
    ``_DIALECT_ALIASES`` resolves to the grammar that connection really speaks first, so the gate
    and the connector classify the same statement with the same parser."""
    if not dialect:
        return ""
    dialect = _DIALECT_ALIASES.get(dialect.lower(), dialect)
    try:
        Dialect.get_or_raise(dialect)
        return dialect
    except Exception:
        return ""


def classify_statements(
    sql: str, dialect: str = "", *, _depth: int = 0
) -> tuple[list[StatementInfo], Confidence]:
    """Classify each statement in ``sql`` → ``(statements, confidence)``.
    Fail-closed: parse error / oversize / null-byte → ``write`` + ``unknown``;
    a Command/dynamic inference drops confidence to ``heuristic``."""
    dialect = _valid_dialect(dialect)  # unknown dialect NAME → neutral (not a fail-closed parse)
    if "\x00" in sql or len(sql.encode("utf-8", "ignore")) > _MAX_SQL_BYTES:
        return (
            [
                StatementInfo(
                    effect=Effect.WRITE,
                    operation="unknown",
                    has_where=False,
                    reasons=["rejected: null byte or oversize SQL"],
                    heuristic=True,
                )
            ],
            "unknown",
        )
    try:
        parsed = [s for s in sqlglot.parse(sql, dialect=dialect or None) if s is not None]
    except Exception:
        return fallback_classify(sql, dialect, _depth)
    if not parsed:
        return ([StatementInfo(effect=Effect.READ, operation="empty")], "exact")
    statements: list[StatementInfo] = []
    confidence: Confidence = "exact"
    for stmt in parsed:
        if isinstance(stmt, exp.Command) or isinstance(stmt, _KNOWN_STATEMENTS):
            info = _with_columns(_classify_one(stmt, dialect, _depth, sql), stmt)
            statements.append(info)
            if info.heuristic and confidence == "exact":
                confidence = "heuristic"
        else:
            confidence = "unknown"
            # A statement sqlglot parsed to a node this classifier does not model
            # (MySQL ``LOAD DATA INFILE``, DuckDB ``ATTACH '<file>'`` / ``INSTALL``)
            # still fails closed to write — but if it reaches the server filesystem
            # or loads native code, it is the stricter EXEC tier. Some of these nodes
            # render to an empty ``.sql()`` (DuckDB ``exp.Install``), so the node type
            # is checked as well as the rendered text.
            is_exec_stmt = type(stmt).__name__ in _EXEC_STATEMENT_TYPES or looks_like_exec(
                stmt.sql()
            )
            unknown_effect = Effect.EXEC if is_exec_stmt else Effect.WRITE
            reason = (
                "unrecognized statement reaches the server filesystem / runs a program"
                if unknown_effect is Effect.EXEC
                else "unrecognized statement (not real SQL)"
            )
            statements.append(
                StatementInfo(
                    effect=unknown_effect,
                    operation="server_exec" if unknown_effect is Effect.EXEC else "unknown",
                    has_where=False,
                    reasons=[reason],
                    heuristic=True,
                )
            )
    return statements, confidence


# Recognized typed statement roots (Command handled separately, always).
_KNOWN_STATEMENTS: tuple[type[exp.Expression], ...] = tuple(
    cls
    for cls in (
        getattr(exp, name, None)
        for name in (
            "Select",
            "Union",
            "Subquery",
            "With",
            "Insert",
            "Update",
            "Delete",
            "Merge",
            "Create",
            "Alter",
            "Drop",
            "TruncateTable",
            "Grant",
            "Revoke",
            "Copy",
            "Export",
            "Get",
            "Put",
            "Execute",
            "ExecuteSql",
            "Describe",
            "Show",
            "Use",
            "Set",
            "Pragma",
            "Transaction",
            "Commit",
            "Rollback",
        )
    )
    if isinstance(cls, type)
)


def descriptor_from_sql(
    sql: str,
    *,
    dialect: str = "",
    capability: str = "sql",
    connection: str | None = None,
    classifier: str = "sqlglot",
    _depth: int = 0,
) -> ActionDescriptor:
    """Build the ``ActionDescriptor`` — effect = MAX over all statements; targets
    stamped with the connection."""
    statements, confidence = classify_statements(sql, dialect, _depth=_depth)
    effect = Effect.READ
    reasons: list[str] = []
    operation = statements[0].operation if statements else ""
    targets: list[ResourceRef] = []
    for st in statements:
        if SEVERITY[st.effect] >= SEVERITY[effect]:
            operation = st.operation
        effect = _max(effect, st.effect)
        reasons.extend(st.reasons)
        for t in st.targets:
            targets.append(ResourceRef(kind=t.kind, name=t.name, connection=connection))
    return ActionDescriptor(
        capability=capability,
        effect=effect,
        operation=operation,
        targets=targets,
        raw=sql,
        statements=statements,
        reasons=reasons,
        classifier=classifier,
        confidence=confidence,
    )


__all__ = ["classify_statements", "descriptor_from_sql"]
