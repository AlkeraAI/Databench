"""Shared compute over a tabular result blob — the DuckDB / Arrow plumbing that
``blob.query`` and ``blob.derive`` both build on.

A rows blob's cells are JSON values (already ``json_restore``-d to real floats by
the caller). We expose them to DuckDB as an in-memory Arrow table named
``result``, run a single read-only SELECT with external access disabled (no file /
network / extension reach), and spill a large output to its own handle so it pages
exactly like ``sql.query``. Keeping this in one module is the only way the two
tools can't drift on the lockdown or the spill contract.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic_core import to_jsonable_python

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.delivery import SqlQueryResult, deliver_rows
from alkera_cli.plugins.plugin_base.duckdb_confine import confine_duckdb
from alkera_cli.plugins.plugin_base.permissions import descriptor_from_sql
from alkera_cli.plugins.plugin_base.result_blob import (
    ResultBlobEnvelope,
    load_rows_envelope,
    read_envelope,
)
from alkera_cli.plugins.plugin_base.tool import ToolError

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore


#: Shared "this handle is gone" message — the same wording fetch_result uses, so
#: every blob tool reports a GC'd / unknown handle identically.
GONE_MSG = "no result blob {handle!r} (it may have been garbage-collected)"


def load_rows_or_error(blobs: BlobStore, handle: str) -> ResultBlobEnvelope:
    """``load_rows_envelope`` with the framework's clean error mapping (missing
    blob → GC'd; bad sha / non-tabular → invalid handle). The single reader every
    rows-consuming tool shares."""
    try:
        return load_rows_envelope(blobs, handle)
    except FileNotFoundError as exc:
        raise ToolError(GONE_MSG.format(handle=handle)) from exc
    except ValueError as exc:
        raise ToolError(f"invalid blob handle: {exc}") from exc


def load_result_or_error(blobs: BlobStore, handle: str) -> ResultBlobEnvelope:
    """Like :func:`load_rows_or_error` but accepts ANY result-blob kind — tabular
    (``rows``) OR ``text`` (a spilled tool result, e.g. a large ``web.fetch``
    page). The reader for tools that operate over both; the same clean error
    mapping (missing → GC'd, bad sha / opaque → invalid handle)."""
    try:
        env = read_envelope(blobs, handle)
    except FileNotFoundError as exc:
        raise ToolError(GONE_MSG.format(handle=handle)) from exc
    except ValueError as exc:
        raise ToolError(f"invalid blob handle: {exc}") from exc
    if env is None:
        raise ToolError(f"invalid blob handle: {handle!r} is not a result blob (rows or text)")
    return env


def unique_names(columns: list[str]) -> list[str]:
    """DuckDB registration needs unique column names; disambiguate duplicates,
    re-checking each suffixed name so a name like ``x_1`` can't collide with a
    generated one."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in columns:
        base = str(raw) or "col"
        name = base
        i = 1
        while name in seen:
            name = f"{base}_{i}"
            i += 1
        seen.add(name)
        out.append(name)
    return out


def to_arrow_table(columns: list[str], rows: list[list[Any]]) -> Any:
    """Build a pyarrow Table from columns + rows. A heterogeneous column (mixed
    types — common in a JSON result) falls back to a string column so the query
    still runs rather than raising on type inference."""
    import pyarrow as pa

    names = unique_names(columns)
    arrays = []
    for i in range(len(names)):
        vals = [row[i] if i < len(row) else None for row in rows]
        try:
            arrays.append(pa.array(vals))
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError):
            arrays.append(
                pa.array(
                    [
                        None
                        if v is None
                        else v
                        if isinstance(v, str)
                        else json.dumps(v, default=str)
                        for v in vals
                    ],
                    type=pa.string(),
                )
            )
    return pa.Table.from_arrays(arrays, names=names)


def validate_read_only_sql(sql: str) -> None:
    """Raise ``ToolError`` unless ``sql`` is a confident read-only statement. The
    in-memory DuckDB ALSO disables external access (defense in depth), but refusing
    non-READ / unparseable SQL keeps the read-only contract clean."""
    descriptor = descriptor_from_sql(sql, dialect="duckdb", capability="blob")
    if descriptor.effect != Effect.READ or descriptor.confidence == "unknown":
        op = descriptor.operation or descriptor.effect.value
        raise ToolError(
            f"read-only only: couldn't confirm {op!r} is a safe SELECT over `result`. "
            "Rewrite as a single SELECT, or use blob.materialize to write data out."
        )


def run_read_only_sql_over(
    tables: dict[str, tuple[list[str], list[list[Any]]]], sql: str, *, max_rows: int | None = None
) -> tuple[list[str], list[list[Any]]]:
    """Execute ``sql`` over one or more named tables in a locked-down in-memory
    DuckDB (external access off). Each ``tables`` entry registers its
    ``(columns, rows)`` under that name, so a join spans sources one SQL statement
    can't reach on its own. ``max_rows`` bounds the output: past it the call fails
    rather than materializing an unbounded (e.g. cross-join) result. Caller must
    ``validate_read_only_sql`` first."""
    import duckdb

    con = duckdb.connect()
    try:
        # No file, no network, no extension — and locked, so the SQL cannot lift
        # the memory ceiling or the thread count either (`SET memory_limit`
        # classifies as a READ, so it reaches this engine).
        confine_duckdb(con)
        for name, (columns, rows) in tables.items():
            con.register(name, to_arrow_table(columns, rows))
        cur = con.execute(sql)
        out_columns = [d[0] for d in cur.description] if cur.description else []
        if max_rows is None:
            return out_columns, [list(r) for r in cur.fetchall()]
        fetched = cur.fetchmany(max_rows + 1)
        if len(fetched) > max_rows:
            raise ToolError(
                f"the join produced more than {max_rows} rows — add an aggregate or a "
                "filter so the combined result is bounded"
            )
        return out_columns, [list(r) for r in fetched]
    except duckdb.Error as exc:
        raise ToolError(f"query failed: {exc}") from exc
    finally:
        con.close()


def run_read_only_sql(
    columns: list[str], rows: list[list[Any]], sql: str
) -> tuple[list[str], list[list[Any]]]:
    """Single-table :func:`run_read_only_sql_over`, exposing the rows as ``result``."""
    return run_read_only_sql_over({"result": (columns, rows)}, sql)


def build_rows_result(
    blobs: BlobStore, columns: list[str], rows: list[list[Any]], *, result_name: str = ""
) -> SqlQueryResult:
    """Coerce DuckDB output to JSON-safe rows, spill to a blob when rows are
    withheld or the payload exceeds the inline cap, and return the same
    ``SqlQueryResult`` shape ``sql.query`` does — so the editor chip +
    ``fetch_result`` paging are identical for every producer."""
    coerced = to_jsonable_python(rows, bytes_mode="base64")
    base = SqlQueryResult(columns=columns, row_count=len(rows), result_name=result_name)
    return deliver_rows(blobs, coerced, base=base)


__all__ = [
    "GONE_MSG",
    "build_rows_result",
    "load_result_or_error",
    "load_rows_or_error",
    "run_read_only_sql",
    "run_read_only_sql_over",
    "to_arrow_table",
    "unique_names",
    "validate_read_only_sql",
]
