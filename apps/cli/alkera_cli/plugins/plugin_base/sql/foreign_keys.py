"""Foreign-key introspection for the shared SQL base: the catalog's declared
parent-to-child relationships.

Two halves, deliberately split so the interesting part needs no server:

* **Dialect SQL** — one query per engine (``pg_constraint`` for Postgres, the ANSI
  ``information_schema`` triple for everything standard, ``KEY_COLUMN_USAGE`` for
  MySQL, ``SHOW IMPORTED KEYS`` for Snowflake, ``PRAGMA foreign_key_list`` for
  SQLite). Every query is written to return the SAME canonical row shape
  (:data:`CANONICAL_FK_COLUMNS`), so one parser serves them all.
* **Pure parsers**: rows in, :class:`ForeignKeyConstraint` out, no I/O.

A dialect that cannot answer (no FK concept, no catalog view, permission denied) simply
contributes nothing: the reader returns an uncaptured snapshot and the seed proceeds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The canonical row every dialect's FK query must produce, in order:
#: ``(constraint_name, src_catalog, src_schema, src_table, src_column,
#: ref_catalog, ref_schema, ref_table, ref_column, ordinal, enforced)``.
#: One row per constrained COLUMN; ``ordinal`` orders a composite key's columns and
#: pairs each constrained column with the referenced column at the same position.
CANONICAL_FK_COLUMNS = 11

#: Catalog spellings of "this constraint is enforced by the engine". Anything else
#: (``NO``, ``0``, an empty cell) reads as informational — declared intent the engine
#: does not police (Redshift, Snowflake, Unity Catalog, BigQuery).
_ENFORCED_TOKENS = frozenset({"yes", "y", "true", "t", "1", "enforced"})


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _enforced_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return _text(value).lower() in _ENFORCED_TOKENS


def _ordinal(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True, slots=True)
class ForeignKeyConstraint:
    """One declared foreign key, columns in declaration order.

    ``source_*`` is the REFERENCING (child) relation that carries the constraint;
    ``referenced_*`` is the parent whose key it points at. ``source_columns[i]``
    references ``referenced_columns[i]``. ``enforced`` distinguishes a constraint the
    engine actually polices (Postgres, MySQL/InnoDB, SQLite with ``PRAGMA
    foreign_keys=ON``) from an INFORMATIONAL one the warehouse only records for the
    optimizer (Redshift, Snowflake, Unity Catalog, BigQuery).
    """

    constraint_name: str
    source_catalog: str
    source_schema: str
    source_table: str
    source_columns: tuple[str, ...]
    referenced_catalog: str
    referenced_schema: str
    referenced_table: str
    referenced_columns: tuple[str, ...]
    enforced: bool = True

    @property
    def source_key(self) -> tuple[str, str, str]:
        """The referencing relation as ``(catalog, schema, table)``."""
        return (self.source_catalog, self.source_schema, self.source_table)

    @property
    def referenced_key(self) -> tuple[str, str, str]:
        """The referenced relation as ``(catalog, schema, table)``."""
        return (self.referenced_catalog, self.referenced_schema, self.referenced_table)

    @property
    def self_referencing(self) -> bool:
        return self.source_key == self.referenced_key

    @property
    def column_pairs(self) -> tuple[tuple[str, str], ...]:
        """``(referenced column, referencing column)`` — upstream first, matching the
        edge direction. A ragged catalog answer (more constrained columns than
        referenced ones) pairs only what lines up rather than guessing."""
        return tuple(zip(self.referenced_columns, self.source_columns, strict=False))


@dataclass(frozen=True, slots=True)
class ForeignKeySnapshot:
    """The result of one FK introspection pass.

    ``captured`` is the exact-GC gate: True only when the read AUTHORITATIVELY
    completed, so an empty ``constraints`` genuinely means "this schema declares no
    foreign keys" and the reconcile may retire the ones it saw before. A failed,
    truncated, denied, or unsupported read leaves it False — the graph is then never
    reconciled against a snapshot that isn't one.
    """

    constraints: tuple[ForeignKeyConstraint, ...] = ()
    captured: bool = False


#: A dialect's raw result rows → the parsed constraints. The default
#: :func:`parse_canonical_fk_rows` serves every engine whose query emits the canonical
#: shape; an engine whose metadata command has its own shape (Snowflake's ``SHOW``)
#: supplies its own.
FkRowParser = Callable[[Sequence[Sequence[Any]]], list[ForeignKeyConstraint]]

#: The grouping identity of one constraint: ``(name, child catalog/schema/table, parent
#: catalog/schema/table)`` — two constraints with the same name on different relations
#: (a per-schema naming convention) must never merge.
_ConstraintKey = tuple[str, str, str, str, str, str, str]


def parse_canonical_fk_rows(rows: Sequence[Sequence[Any]]) -> list[ForeignKeyConstraint]:
    """Canonical FK rows → constraints, one per ``(constraint, child, parent)`` triple.

    Groups a composite key's rows back together and orders its columns by the catalog's
    ordinal, so ``source_columns[i]`` truly references ``referenced_columns[i]``. Rows
    missing an endpoint (a self-join fan-out that produced NULLs) are dropped, and a
    duplicated ordinal keeps its first occurrence — a join against three catalog views
    can legitimately fan out.
    """
    grouped: dict[_ConstraintKey, list[tuple[int, str, str]]] = {}
    enforced: dict[_ConstraintKey, bool] = {}
    for index, raw in enumerate(rows):
        row = list(raw)
        if len(row) < CANONICAL_FK_COLUMNS:
            # A dialect answered a shape we don't understand — drop the row rather than
            # mis-pair columns from a query that isn't the canonical contract.
            continue
        name, s_cat, s_sch, s_tab, s_col, r_cat, r_sch, r_tab, r_col, ordinal, is_enforced = row[
            :CANONICAL_FK_COLUMNS
        ]
        src_col, ref_col = _text(s_col), _text(r_col)
        if not _text(s_tab) or not _text(r_tab) or not src_col or not ref_col:
            continue
        key: _ConstraintKey = (
            _text(name),
            _text(s_cat),
            _text(s_sch),
            _text(s_tab),
            _text(r_cat),
            _text(r_sch),
            _text(r_tab),
        )
        grouped.setdefault(key, []).append((_ordinal(ordinal, index), src_col, ref_col))
        enforced.setdefault(key, _enforced_flag(is_enforced))
    out: list[ForeignKeyConstraint] = []
    for group_key, entries in grouped.items():
        seen: set[int] = set()
        ordered: list[tuple[int, str, str]] = []
        for position, src_col, ref_col in sorted(entries, key=lambda e: e[0]):
            if position in seen:
                continue
            seen.add(position)
            ordered.append((position, src_col, ref_col))
        name, s_cat, s_sch, s_tab, r_cat, r_sch, r_tab = group_key
        out.append(
            ForeignKeyConstraint(
                constraint_name=name,
                source_catalog=s_cat,
                source_schema=s_sch,
                source_table=s_tab,
                source_columns=tuple(e[1] for e in ordered),
                referenced_catalog=r_cat,
                referenced_schema=r_sch,
                referenced_table=r_tab,
                referenced_columns=tuple(e[2] for e in ordered),
                enforced=enforced[group_key],
            )
        )
    return out


def parse_snowflake_imported_keys(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[ForeignKeyConstraint]:
    """``SHOW IMPORTED KEYS`` output → constraints. Column ORDER isn't contractual for a
    ``SHOW`` result, so every field is resolved by name (``pk_*`` = the referenced side,
    ``fk_*`` = the referencing side, ``key_sequence`` = the composite ordinal).

    Snowflake never ENFORCES a foreign key — it records it for the optimizer — so every
    constraint here is informational.
    """
    lower = [str(c).lower() for c in columns]

    def index_of(*names: str) -> int | None:
        for name in names:
            if name in lower:
                return lower.index(name)
        return None

    slots = {
        key: index_of(*names)
        for key, names in {
            "name": ("fk_name",),
            "s_cat": ("fk_database_name",),
            "s_sch": ("fk_schema_name",),
            "s_tab": ("fk_table_name",),
            "s_col": ("fk_column_name",),
            "r_cat": ("pk_database_name",),
            "r_sch": ("pk_schema_name",),
            "r_tab": ("pk_table_name",),
            "r_col": ("pk_column_name",),
            "seq": ("key_sequence",),
        }.items()
    }
    if any(slots[k] is None for k in ("s_tab", "s_col", "r_tab", "r_col")):
        return []

    def cell(row: Sequence[Any], key: str, default: Any = "") -> Any:
        position = slots[key]
        if position is None or position >= len(row):
            return default
        return row[position]

    canonical = [
        (
            cell(row, "name"),
            cell(row, "s_cat"),
            cell(row, "s_sch"),
            cell(row, "s_tab"),
            cell(row, "s_col"),
            cell(row, "r_cat"),
            cell(row, "r_sch"),
            cell(row, "r_tab"),
            cell(row, "r_col"),
            cell(row, "seq", 1),
            False,
        )
        for row in rows
    ]
    return parse_canonical_fk_rows(canonical)


def parse_sqlite_foreign_key_list(
    table: str,
    rows: Sequence[Sequence[Any]],
    *,
    container: str,
    schema: str = "main",
    enforced: bool = False,
    primary_key_columns: Mapping[str, Sequence[str]] | None = None,
) -> list[ForeignKeyConstraint]:
    """``PRAGMA foreign_key_list(<table>)`` rows → constraints for ONE table.

    Row shape is ``(id, seq, table, from, to, on_update, on_delete, match)``; ``id``
    groups a composite key and ``seq`` orders it. SQLite doesn't name its constraints,
    so the name is synthesized from the table + id — stable across introspections, which
    is all the edge attribute needs. A NULL ``to`` means "the parent's primary key":
    resolved from ``primary_key_columns`` when the caller supplies it, dropped otherwise
    (a guessed column would mint a wrong column edge). ``enforced`` reflects the
    session's ``PRAGMA foreign_keys`` — SQLite declares FKs whether or not it polices
    them.
    """
    pk_lookup = primary_key_columns or {}
    grouped: dict[int, list[tuple[int, str, str]]] = {}
    targets: dict[int, str] = {}
    for index, raw in enumerate(rows):
        row = list(raw)
        if len(row) < 5:
            continue
        fk_id = _ordinal(row[0], index)
        seq = _ordinal(row[1], 0)
        parent = _text(row[2])
        child_column = _text(row[3])
        parent_column = _text(row[4])
        if not parent or not child_column:
            continue
        if not parent_column:
            pk = list(pk_lookup.get(parent, ()))
            if seq >= len(pk):
                continue
            parent_column = _text(pk[seq])
            if not parent_column:
                continue
        grouped.setdefault(fk_id, []).append((seq, child_column, parent_column))
        targets.setdefault(fk_id, parent)
    out: list[ForeignKeyConstraint] = []
    for fk_id, entries in grouped.items():
        ordered = sorted(entries, key=lambda e: e[0])
        out.append(
            ForeignKeyConstraint(
                constraint_name=f"{table}_fk_{fk_id}",
                source_catalog=container,
                source_schema=schema,
                source_table=table,
                source_columns=tuple(e[1] for e in ordered),
                referenced_catalog=container,
                referenced_schema=schema,
                referenced_table=targets[fk_id],
                referenced_columns=tuple(e[2] for e in ordered),
                enforced=enforced,
            )
        )
    return out


#: Postgres (and any PG-compatible engine that exposes the real catalog):
#: ``pg_constraint`` joined to ``pg_attribute`` on BOTH sides, with the composite key's
#: column positions unnested WITH ORDINALITY so a multi-column key keeps its declared
#: order. Read from the catalog rather than by re-parsing ``pg_get_constraintdef`` text,
#: whose target is schema-qualified only when it's off the search_path (which would mint
#: URNs for the wrong schema).
PG_FOREIGN_KEYS_SQL = (
    "SELECT c.conname, "
    "current_database(), sn.nspname, st.relname, sa.attname, "
    "current_database(), rn.nspname, rt.relname, ra.attname, "
    "k.ord, CASE WHEN c.convalidated THEN 'YES' ELSE 'NO' END "
    "FROM pg_constraint c "
    "JOIN pg_class st ON st.oid = c.conrelid "
    "JOIN pg_namespace sn ON sn.oid = st.relnamespace "
    "JOIN pg_class rt ON rt.oid = c.confrelid "
    "JOIN pg_namespace rn ON rn.oid = rt.relnamespace "
    "JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(src_attnum, ref_attnum, ord) "
    "ON TRUE "
    "JOIN pg_attribute sa ON sa.attrelid = c.conrelid AND sa.attnum = k.src_attnum "
    "JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = k.ref_attnum "
    "WHERE c.contype = 'f' "
    "ORDER BY sn.nspname, st.relname, c.conname, k.ord"
)

#: The ANSI/portable form — ``TABLE_CONSTRAINTS`` ⋈ ``KEY_COLUMN_USAGE`` ⋈
#: ``REFERENTIAL_CONSTRAINTS``, joining ``KEY_COLUMN_USAGE`` a second time on the
#: referenced UNIQUE/PRIMARY constraint so the parent's columns come back in the order
#: ``position_in_unique_constraint`` pairs them. The default for anything without a
#: dedicated query (DuckDB, an unknown engine behind the generic connector).
#: Redshift's variant. Its ``KEY_COLUMN_USAGE`` has NO ``position_in_unique_constraint``
#: column (verified against a live serverless cluster: the ANSI query fails with
#: "column kcu.position_in_unique_constraint does not exist", which degrades to zero FK
#: edges), so the parent's columns are paired by ``ordinal_position`` on both sides
#: instead. That is exact whenever the FK's column order matches the referenced key's
#: order, which is how a composite FK is declared in practice; a deliberately re-ordered
#: composite FK would pair its columns differently, and Redshift exposes nothing that
#: would let us tell. Everything else — the tables joined, the emitted row shape — is the
#: ANSI query's. Redshift never enforces a foreign key, so the flag is a literal 'NO'.
REDSHIFT_FOREIGN_KEYS_SQL = (
    "SELECT tc.constraint_name, kcu.table_catalog, kcu.table_schema, kcu.table_name, "
    "kcu.column_name, rcu.table_catalog, rcu.table_schema, rcu.table_name, rcu.column_name, "
    "kcu.ordinal_position, 'NO' "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.key_column_usage kcu "
    "ON kcu.constraint_catalog = tc.constraint_catalog "
    "AND kcu.constraint_schema = tc.constraint_schema "
    "AND kcu.constraint_name = tc.constraint_name "
    "JOIN information_schema.referential_constraints rc "
    "ON rc.constraint_catalog = tc.constraint_catalog "
    "AND rc.constraint_schema = tc.constraint_schema "
    "AND rc.constraint_name = tc.constraint_name "
    "JOIN information_schema.key_column_usage rcu "
    "ON rcu.constraint_catalog = rc.unique_constraint_catalog "
    "AND rcu.constraint_schema = rc.unique_constraint_schema "
    "AND rcu.constraint_name = rc.unique_constraint_name "
    "AND rcu.ordinal_position = kcu.ordinal_position "
    "WHERE tc.constraint_type = 'FOREIGN KEY' "
    "ORDER BY tc.constraint_name, kcu.ordinal_position"
)

ANSI_FOREIGN_KEYS_SQL = (
    "SELECT tc.constraint_name, "
    "kcu.table_catalog, kcu.table_schema, kcu.table_name, kcu.column_name, "
    "rcu.table_catalog, rcu.table_schema, rcu.table_name, rcu.column_name, "
    "kcu.ordinal_position, 'NO' "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.key_column_usage kcu "
    "ON kcu.constraint_catalog = tc.constraint_catalog "
    "AND kcu.constraint_schema = tc.constraint_schema "
    "AND kcu.constraint_name = tc.constraint_name "
    "JOIN information_schema.referential_constraints rc "
    "ON rc.constraint_catalog = tc.constraint_catalog "
    "AND rc.constraint_schema = tc.constraint_schema "
    "AND rc.constraint_name = tc.constraint_name "
    "JOIN information_schema.key_column_usage rcu "
    "ON rcu.constraint_catalog = rc.unique_constraint_catalog "
    "AND rcu.constraint_schema = rc.unique_constraint_schema "
    "AND rcu.constraint_name = rc.unique_constraint_name "
    "AND rcu.ordinal_position = kcu.position_in_unique_constraint "
    "WHERE tc.constraint_type = 'FOREIGN KEY' "
    "ORDER BY kcu.table_schema, kcu.table_name, tc.constraint_name, kcu.ordinal_position"
)

#: MySQL / MariaDB: ``KEY_COLUMN_USAGE`` already carries the referenced side, so the
#: referential join only confirms the row is a foreign key. A 2-level engine — the
#: DATABASE goes in the catalog slot with an empty sub-schema, matching
#: ``TWO_LEVEL_RELATIONS_SQL`` so the edge endpoints fold onto the introspected nodes.
MYSQL_FOREIGN_KEYS_SQL = (
    "SELECT k.CONSTRAINT_NAME, "
    "k.TABLE_SCHEMA, '', k.TABLE_NAME, k.COLUMN_NAME, "
    "k.REFERENCED_TABLE_SCHEMA, '', k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, "
    "k.ORDINAL_POSITION, 'YES' "
    "FROM information_schema.KEY_COLUMN_USAGE k "
    "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
    "ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
    "AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
    "AND r.TABLE_NAME = k.TABLE_NAME "
    "WHERE k.REFERENCED_TABLE_NAME IS NOT NULL "
    "ORDER BY k.TABLE_SCHEMA, k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
)

#: Databricks / Unity Catalog: ``referential_constraints`` ⋈ ``key_column_usage`` for
#: the child columns, ⋈ ``constraint_column_usage`` for the parent's relation, with the
#: parent's column order taken from ``key_column_usage`` on the referenced key. Unity
#: Catalog's foreign keys are INFORMATIONAL (declared ``NOT ENFORCED``), hence the
#: literal 'NO'.
DATABRICKS_FOREIGN_KEYS_SQL = (
    "SELECT rc.constraint_name, "
    "kcu.table_catalog, kcu.table_schema, kcu.table_name, kcu.column_name, "
    "ccu.table_catalog, ccu.table_schema, ccu.table_name, rcu.column_name, "
    "kcu.ordinal_position, 'NO' "
    "FROM information_schema.referential_constraints rc "
    "JOIN information_schema.key_column_usage kcu "
    "ON kcu.constraint_catalog = rc.constraint_catalog "
    "AND kcu.constraint_schema = rc.constraint_schema "
    "AND kcu.constraint_name = rc.constraint_name "
    "JOIN information_schema.key_column_usage rcu "
    "ON rcu.constraint_catalog = rc.unique_constraint_catalog "
    "AND rcu.constraint_schema = rc.unique_constraint_schema "
    "AND rcu.constraint_name = rc.unique_constraint_name "
    "AND rcu.ordinal_position = kcu.position_in_unique_constraint "
    "JOIN information_schema.constraint_column_usage ccu "
    "ON ccu.constraint_catalog = rc.unique_constraint_catalog "
    "AND ccu.constraint_schema = rc.unique_constraint_schema "
    "AND ccu.constraint_name = rc.unique_constraint_name "
    "AND ccu.column_name = rcu.column_name "
    "ORDER BY kcu.table_schema, kcu.table_name, rc.constraint_name, kcu.ordinal_position"
)

#: Snowflake's free metadata command (account-wide, with a current-scope fallback the
#: caller applies). Its result is a named SHOW grid, not the canonical shape — parse it
#: with :func:`parse_snowflake_imported_keys`.
SNOWFLAKE_IMPORTED_KEYS_SHOW = "IMPORTED KEYS"

__all__ = [
    "ANSI_FOREIGN_KEYS_SQL",
    "CANONICAL_FK_COLUMNS",
    "DATABRICKS_FOREIGN_KEYS_SQL",
    "MYSQL_FOREIGN_KEYS_SQL",
    "PG_FOREIGN_KEYS_SQL",
    "REDSHIFT_FOREIGN_KEYS_SQL",
    "SNOWFLAKE_IMPORTED_KEYS_SHOW",
    "FkRowParser",
    "ForeignKeyConstraint",
    "ForeignKeySnapshot",
    "parse_canonical_fk_rows",
    "parse_snowflake_imported_keys",
    "parse_sqlite_foreign_key_list",
]
