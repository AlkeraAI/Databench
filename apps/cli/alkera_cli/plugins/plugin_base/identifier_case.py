"""Per-system COLUMN identifier case rules, backed by sqlglot.

A column's URN identity must fold the way its own system resolves an identifier.
Otherwise two producers split one column into two nodes. Snowflake folds an
unquoted reference UPPER and stores a quoted one exactly. Postgres folds lower and
keeps a quoted one exactly. DuckDB and BigQuery match columns case-insensitively
even when quoted. The rule comes from sqlglot's ``Dialect.normalize_identifier``,
the same engine the column parser resolves SQL with, keyed by the URN scheme's
system.

Systems with no SQL dialect (a BI cell, a Fivetran field, the legacy
``warehouse://`` namespace) keep the blanket lowercase fold column URNs have
always used, regardless of quoting.
"""

from __future__ import annotations

import re
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlglot.dialects.dialect import Dialect

#: Mirrors ``urns._STRIP``. Quote, bracket, and backslash characters and the column
#: separator ``#`` can never survive into a URN part.
_STRIP = re.compile(r"""["'`\[\]#\\]""")

#: Quote characters whose presence marks a name as a QUOTED identifier when the
#: caller didn't say (``quoted=None``).
_QUOTE_MARKS = re.compile(r"""["'`\[\]]""")

#: Maps a canonical system, plus its raw engine aliases, to the sqlglot dialect that
#: owns the system's identifier case behavior. The aliases mirror ``urns._SYSTEM_ALIASES``
#: so an un-canonicalized caller still lands on the right rule. A system absent here
#: (hex, sigma, fivetran, looker, ``warehouse``) folds blanket-lowercase.
_SQLGLOT_DIALECTS = {
    "snowflake": "snowflake",
    "oracle": "oracle",
    "postgres": "postgres",
    "postgresql": "postgres",
    "athena": "athena",
    "glue": "athena",
    "clickhouse": "clickhouse",
    "duckdb": "duckdb",
    "bigquery": "bigquery",
    "sqlite": "sqlite",
    "redshift": "redshift",
    "databricks": "databricks",
    "druid": "druid",
    "spark": "spark",
    "hive": "hive",
    "trino": "trino",
    "presto": "trino",
    "sqlserver": "tsql",
    "mssql": "tsql",
    "tsql": "tsql",
    # sqlglot's mysql rule preserves case (its TABLE rule). The fold-lower override
    # below wins for columns. Mapped so deleting the override is a caught mutation
    # rather than a silent fall-through to the same blanket-lower result.
    "mysql": "mysql",
    "mariadb": "mysql",
}

#: MySQL and MariaDB columns match case-insensitively, so their column identity folds
#: lower regardless of quoting. Their tables differ. A table's sensitivity follows the
#: host filesystem, which is the rule sqlglot encodes.
_FOLD_LOWER_OVERRIDE = frozenset({"mysql", "mariadb"})


@cache
def _dialect(name: str) -> Dialect:
    # Deferred so importing this module (and, through it, URN minting) doesn't pay
    # sqlglot's import cost until a dialect-backed system actually folds.
    from sqlglot.dialects.dialect import Dialect

    return Dialect.get_or_raise(name)


def strip_identifier_quotes(name: str) -> str:
    """``name`` with quote/bracket/escape characters (and ``#``) removed, case KEPT.
    Use it for names whose case is already resolved, like a sqlglot-normalized leaf or
    a stored schema name kept as a display spelling."""
    return _STRIP.sub("", name).strip()


def fold_column_identifier(system: str, name: str, quoted: bool | None = None) -> str:
    """``name`` folded to its column-URN identity under ``system``'s case rule.

    ``quoted=True`` marks a quoted SQL identifier or a system-canonical stored name,
    folded by the system's stored-name rule (Snowflake/Postgres keep case, DuckDB /
    BigQuery / tsql fold lower even quoted). ``False`` marks an unquoted SQL
    reference, folded by the unquoted rule (Snowflake/Oracle UPPER, most others
    lower). ``None`` infers ``True`` when quote characters are embedded in ``name``.
    A system with no SQL dialect folds lower regardless of quoting.

    A system's canonical stored form is a fixed point. Re-folding it with
    ``quoted=True`` returns it unchanged, so an already-recorded name re-mints onto
    the same URN. Quote/bracket characters and ``#`` are always stripped for URN
    safety, and the result is ``""`` when nothing survives.
    """
    if quoted is None:
        quoted = bool(_QUOTE_MARKS.search(name))
    part = _STRIP.sub("", name).strip()
    if not part:
        return ""
    s = system.strip().lower()
    from alkera_cli.plugins.plugin_base.urns import OPAQUE_AUTHORITY_SYSTEMS

    if s in OPAQUE_AUTHORITY_SYSTEMS:
        return part
    if s in _FOLD_LOWER_OVERRIDE:
        return part.lower()
    dialect = _SQLGLOT_DIALECTS.get(s)
    if dialect is None:
        return part.lower()
    from sqlglot import exp

    return _dialect(dialect).normalize_identifier(exp.to_identifier(part, quoted=quoted)).name


__all__ = ["fold_column_identifier", "strip_identifier_quotes"]
