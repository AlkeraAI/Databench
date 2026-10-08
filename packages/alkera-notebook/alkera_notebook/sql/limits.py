"""The notebook's ``sql_row_limit``, as marimo applies its default limit:
only to a statement that has no row limit of its own, and the output says
the result was cut."""

from __future__ import annotations

import re

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`")
_TOKEN = re.compile(r"[()]|[A-Za-z_]+")


def has_own_limit(sql: str) -> bool:
    """Whether the last statement limits its rows at its top level: ``LIMIT``,
    ``FETCH FIRST/NEXT`` or ``SELECT TOP``. A limit inside a subquery does
    not count; it bounds the subquery, not the result."""
    text = _STRING.sub("''", _COMMENT.sub(" ", sql))
    statements = [s for s in text.split(";") if s.strip()]
    if not statements:
        return False
    depth = 0
    previous = ""
    for part in _TOKEN.findall(statements[-1]):
        if part == "(":
            depth += 1
        elif part == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            word = part.lower()
            if word == "limit" or (previous == "fetch" and word in ("first", "next")):
                return True
            if previous == "select" and word == "top":
                return True
            previous = word
    return False


def cut_notice(limit: int) -> str:
    return (
        f"Showing the first {limit:,} rows: the query returned more. "
        "Add a LIMIT to the query, or change sql_row_limit in the notebook's settings."
    )
