"""What an ``inspect.frame`` request may ask the kernel.

The kernel registers the inspected frame under the table name ``frame`` and
runs ``filter_sql`` as a subquery in a locked DuckDB. The engine refuses,
before anything reaches the kernel, any filter that is not one ``SELECT``
reading only ``frame``: no second statement, no other table, no table
function (``read_csv``, a file path), no schema-qualified name. The kernel
checks again; this is the first wall, and the one that names the problem.
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from alkera_notebook.engine.errors import QueryRefusedError
from alkera_notebook.engine.sort_spec import MAX_SORT_KEYS

FRAME_TABLE = "frame"
#: The most keys a frame page may be ordered by (the kernel takes as many).


def check_frame_filter(sql: str) -> str:
    """``sql`` trimmed (and without a trailing ``;``) if it is one ``SELECT``
    over ``frame``; :class:`QueryRefusedError` otherwise."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise QueryRefusedError("The filter is empty", reason="empty")
    try:
        statements = [s for s in sqlglot.parse(text, read="duckdb") if s is not None]
    except SqlglotError as exc:
        raise QueryRefusedError(f"The filter does not parse: {exc}", reason="parse") from exc
    if len(statements) != 1:
        raise QueryRefusedError("The filter must be a single statement", reason="statements")
    (statement,) = statements
    if not isinstance(statement, exp.Select):
        raise QueryRefusedError(
            f"filter_sql must be a SELECT, not {statement.key.upper()}", reason="not_select"
        )
    tables = list(statement.find_all(exp.Table))
    for table in tables:
        if (
            not isinstance(table.this, exp.Identifier)
            or table.name.lower() != FRAME_TABLE
            or table.args.get("db") is not None
            or table.args.get("catalog") is not None
        ):
            raise QueryRefusedError(
                f"filter_sql may read only the table {FRAME_TABLE!r}, not {table.sql('duckdb')}",
                reason="other_table",
            )
    if not tables:
        raise QueryRefusedError(
            f"filter_sql must select from the table {FRAME_TABLE!r}", reason="no_frame"
        )
    return text


def frame_sort(sort: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """The kernel's sort, a list of ``{column, descending}`` keys in order,
    from a query's list of at most :data:`MAX_SORT_KEYS`. Whether each column
    exists is the kernel's to say: it alone sees the frame."""
    if not sort:
        return None
    if len(sort) > MAX_SORT_KEYS:
        raise QueryRefusedError(f"Sort takes at most {MAX_SORT_KEYS} columns", reason="sort")
    keys: list[dict[str, Any]] = []
    for key in sort:
        column = key.get("column") if isinstance(key, dict) else None
        if not isinstance(column, str) or not column:
            raise QueryRefusedError("Each sort key needs a column name", reason="sort")
        descending = key.get("descending", False)
        if not isinstance(descending, bool):
            raise QueryRefusedError("Descending must be true or false", reason="sort")
        keys.append({"column": column, "descending": descending})
    return keys
