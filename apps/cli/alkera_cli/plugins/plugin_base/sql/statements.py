"""Split a multi-statement SQL string into individually-parseable statements.

A single Airflow operator ``sql=`` can hold several statements; one un-tokenizable statement
(a reserved-word target like ``CREATE TABLE out AS …``, a genuine syntax error) must NOT
discard its valid siblings. :func:`parse_isolated` parses the whole string fast on the happy
path, and ONLY on failure falls back to splitting on top-level ``;`` (respecting quotes +
parens, so it never breaks inside a string or a CTE body) and parsing each fragment alone —
so a failing fragment drops only itself. Used in LOCKSTEP by the table-grain (operators) and
column-grain (column_parser) extractors so they agree on statement boundaries.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

_OPEN = "(["
_CLOSE = ")]"
_QUOTES = "'\"`"
#: A PostgreSQL dollar-quote opener: ``$$`` or ``$tag$``. Its body (and any ``;`` inside) is
#: opaque, so a procedural body (``DO $$ … ; … $$``) is never split into phantom fragments.
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_sql_statements(sql: str) -> list[str]:
    """Split ``sql`` on top-level ``;`` boundaries, treating string literals, paren/bracket
    nesting, SQL comments (``-- …`` / ``/* … */``), and dollar-quoted bodies (``$tag$ … $tag$``)
    as OPAQUE — so a ``;`` inside any of them never splits a statement. (Comment + body text is
    preserved in the fragment; only its statement-boundary effect is suppressed.) This matters
    because the splitter runs precisely on the parse-isolation fallback, where a procedural body
    or a comment containing ``;`` would otherwise corrupt, drop, or PHANTOM a sibling."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        # Line comment: consume through end-of-line (the ';' in it is not a boundary).
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            end = n if nl == -1 else nl + 1
            buf.append(sql[i:end])
            i = end
            continue
        # Block comment: consume through the matching */.
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            close = sql.find("*/", i + 2)
            end = n if close == -1 else close + 2
            buf.append(sql[i:end])
            i = end
            continue
        # Dollar-quoted body ($$ … $$ / $tag$ … $tag$): opaque to the matching delimiter.
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                close = sql.find(tag, m.end())
                end = n if close == -1 else close + len(tag)
                buf.append(sql[i:end])
                i = end
                continue
        if ch in _QUOTES:
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            fragment = "".join(buf).strip()
            if fragment:
                out.append(fragment)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    fragment = "".join(buf).strip()
    if fragment:
        out.append(fragment)
    return out


def _parse_all(sql: str, dialect: str | None) -> list[exp.Expression] | None:
    """The whole string parsed at once (fast path), or ``None`` if any statement fails."""
    try:
        parsed = sqlglot.parse(sql, dialect=dialect or None)
    except Exception:
        return None
    return [s for s in parsed if isinstance(s, exp.Expression)]


def _parse_one(fragment: str, dialect: str | None) -> exp.Expression | None:
    """One fragment parsed in isolation, or ``None`` if it fails."""
    try:
        statement = sqlglot.parse_one(fragment, dialect=dialect or None)
    except Exception:
        return None
    return statement if isinstance(statement, exp.Expression) else None


def parse_isolated(sql: str, dialect: str | None) -> list[exp.Expression]:
    """Parse ``sql`` into statements, ISOLATING failures: the whole-string parse is tried
    first (fast, common case), and if it fails, each statement fragment is parsed on its own
    so one bad statement can't blank its valid siblings. Best-effort — never raises."""
    whole = _parse_all(sql, dialect)
    if whole is not None:
        return whole
    return [s for fragment in split_sql_statements(sql) if (s := _parse_one(fragment, dialect))]


__all__ = ["parse_isolated", "split_sql_statements"]
