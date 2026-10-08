"""Which Alembic chain a revision belongs to: the platform's, or an extension's.

A distribution that extends the platform with tables of its own keeps them in a
chain of its own, with its own version table, run after the platform's. The
early revisions are replaced by one baseline per chain. Every revision after
:data:`LAST_UNSIDED_REVISION` declares its chain at module level::

    SIDE: str = "open"  # creates or changes only platform tables
    SIDE: str = "private"  # creates or changes an extension's table

A revision is one side or the other, never both:

* an ``open`` revision changes no extension table and names none in any string
  (raw SQL included), because the platform chain must run with no extension
  table present;
* a ``private`` revision changes no platform table. It may name platform tables
  (a foreign key into ``users``, a join in a back-fill), since its chain runs
  after the platform's.

The functions here read a revision's source without importing it, so the check
needs no database. Which side each table is on is the caller's input.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass

OPEN = "open"
PRIVATE = "private"
SIDES = (OPEN, PRIVATE)

#: The revision the baselines are generated from. Revisions up to it are
#: replaced by the baselines, whatever they declare; every later one must
#: declare ``SIDE``.
LAST_UNSIDED_REVISION = "0220"

#: Calls that create or change a table, and where the table's name sits: the
#: positional indexes and the keyword names that carry it. ``op.*`` and the
#: ``backend.migration_safety`` helpers alike.
_TABLE_ARGUMENTS: Mapping[str, tuple[tuple[int, ...], tuple[str, ...]]] = {
    "create_table": ((0,), ("table_name",)),
    "drop_table": ((0,), ("table_name",)),
    "rename_table": ((0, 1), ("old_table_name", "new_table_name")),
    "add_column": ((0,), ("table_name",)),
    "drop_column": ((0,), ("table_name",)),
    "alter_column": ((0,), ("table_name",)),
    "batch_alter_table": ((0,), ("table_name",)),
    "create_table_comment": ((0,), ("table_name",)),
    "drop_table_comment": ((0,), ("table_name",)),
    "create_index": ((1,), ("table_name",)),
    "drop_index": ((1,), ("table_name",)),
    "create_foreign_key": ((1,), ("source_table",)),
    "create_unique_constraint": ((1,), ("table_name",)),
    "create_check_constraint": ((1,), ("table_name",)),
    "create_primary_key": ((1,), ("table_name",)),
    "drop_constraint": ((1,), ("table_name",)),
    "add_check_not_valid": ((0,), ("table",)),
    "drop_constraint_if_exists": ((0,), ("table",)),
    "create_index_concurrently": ((1,), ("table",)),
    "in_batches": ((0,), ("table",)),
}


@dataclass(frozen=True)
class RevisionFacts:
    """What a revision's source says about its chain."""

    revision: str | None
    #: The ``SIDE`` value as written, or None when the module declares none.
    side: str | None
    #: Tables a call in the revision creates or changes.
    changed_tables: frozenset[str]
    #: Every string the code holds (SQL included), docstrings and bare
    #: expression strings left out.
    strings: tuple[str, ...]


def read_revision(source: str) -> RevisionFacts:
    tree = ast.parse(source)
    assigned = _module_assignments(tree)
    return RevisionFacts(
        revision=assigned.get("revision"),
        side=assigned.get("SIDE"),
        changed_tables=frozenset(_changed_tables(tree)),
        strings=tuple(_code_strings(tree)),
    )


def needs_side(revision: str) -> bool:
    """Whether a revision id comes after :data:`LAST_UNSIDED_REVISION`."""
    return int(revision) > int(LAST_UNSIDED_REVISION)


def side_problems(facts: RevisionFacts, table_sides: Mapping[str, str]) -> list[str]:
    """Why a revision cannot go into one chain, or nothing when it can.

    ``table_sides`` maps a table name to ``open`` or ``private``. A table it
    does not know (one this revision creates, say) has no side to compare."""
    if facts.side is None:
        return [f'declares no SIDE; add `SIDE: str = "{OPEN}"` or `"{PRIVATE}"`']
    if facts.side not in SIDES:
        return [f"SIDE is {facts.side!r}; it must be {OPEN!r} or {PRIVATE!r}"]
    other = PRIVATE if facts.side == OPEN else OPEN
    article = "an" if facts.side == OPEN else "a"
    problems = [
        f"{article} {facts.side} revision changes the {other} table {table!r}"
        for table in sorted(facts.changed_tables)
        if table_sides.get(table) == other
    ]
    if facts.side == OPEN:
        private_tables = sorted(t for t, side in table_sides.items() if side == PRIVATE)
        problems.extend(
            f"an open revision names the private table {table!r}"
            for table in private_tables
            if table not in facts.changed_tables
            and any(_names(text, table) for text in facts.strings)
        )
    return problems


def _module_assignments(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            found[target.id] = value.value
    return found


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _changed_tables(tree: ast.Module) -> set[str]:
    tables: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name is None or name not in _TABLE_ARGUMENTS:
            continue
        positions, keywords = _TABLE_ARGUMENTS[name]
        candidates = [node.args[i] for i in positions if i < len(node.args)]
        candidates += [kw.value for kw in node.keywords if kw.arg in keywords]
        tables.update(
            c.value for c in candidates if isinstance(c, ast.Constant) and isinstance(c.value, str)
        )
    return tables


def _code_strings(tree: ast.Module) -> list[str]:
    bare = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare
    ]


def _names(text: str, table: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(table)}(?!\w)", text) is not None
