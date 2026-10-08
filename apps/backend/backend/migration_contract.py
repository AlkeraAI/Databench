"""Expand, then contract: what a revision may take away from a release still serving.

A deploy migrates the database first and replaces the servers after, and a
box or a self-hosted server can stay a release behind for much longer. For
that whole time the previous release reads and writes the new schema. A
revision that only adds (a table, a nullable column, an index) is invisible to
it. One that takes something away or makes it stricter breaks it, unless an
earlier revision that has already shipped moved every reader and writer off
the thing first. That earlier revision is the expand, and this one is the
contract that completes it.

So a revision whose ``upgrade()`` contracts must say which expand it
completes, at module level beside ``revision``::

    contract_of = "0198"

and the name must be a revision this one descends from. :func:`lint_versions`
reads a versions directory and reports every revision above a given number
that contracts without saying so, names an expand it does not descend from,
or declares one while taking nothing away.

What counts as contracting (:func:`contracting_operations`): dropping a table,
a column or a constraint, renaming a table, a column or a constraint, making a
column required, and adding a required column with no server default.

What the same ``upgrade()`` adds is not something a previous release uses, so
the revision may take it away again or finish it:

* a table it creates (a scratch table for a back-fill) may be dropped;
* a column it adds may be dropped, and may be made required once the same
  ``upgrade()`` has given it a server default (an insert from the previous
  release, which does not know the column, then gets the default);
* a constraint it adds over a column it adds may be dropped, whichever comes
  first. No release before this one can hold such a constraint. Dropped
  after, it was a scaffold (a ``CHECK`` validated to make ``SET NOT NULL``
  cheap). Dropped before, it is cleared so a second run of the revision finds
  nothing in its way. A constraint over columns that were already there is
  not excused, even when the revision adds one back under the same name:
  the previous release may be relying on the one it had.

The table and the name must both be readable from the revision's own text
(literals and module-level names). Anything this cannot resolve stays a
contraction.

Two things this cannot read, and review still owns: whether a new or replaced
constraint is one a previous release's writes can violate, and whether the named expand
shipped in an earlier release rather than this one (the repository does not
record where a release ended).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

#: Alembic operations that take something away from a previous release.
CONTRACTING_OPS: Final = frozenset({"drop_column", "drop_table", "drop_constraint", "rename_table"})
#: The helpers of :mod:`backend.migration_safety` that do the same.
CONTRACTING_HELPERS: Final = frozenset({"drop_constraint_if_exists"})

_CONTRACTING_SQL: Final = (
    (re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE), "drops a column"),
    (re.compile(r"\bDROP\s+CONSTRAINT\b", re.IGNORECASE), "drops a constraint"),
    (re.compile(r"\bRENAME\s+(TO|COLUMN|CONSTRAINT)\b", re.IGNORECASE), "renames"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE), "makes a column required"),
)
_NAME: Final = r"((?:\"[^\"]+\"|\w+)(?:\.(?:\"[^\"]+\"|\w+))?)"
_CREATE_TABLE_SQL: Final = re.compile(
    r"\bCREATE\s+(?:(?:TEMP|TEMPORARY|UNLOGGED)\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _NAME,
    re.IGNORECASE,
)
_DROP_TABLE_SQL: Final = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.IGNORECASE)

_ALTER_TABLE_SQL: Final = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _NAME, re.IGNORECASE
)
_ADD_COLUMN_SQL: Final = re.compile(
    r"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _NAME, re.IGNORECASE
)
_ADD_CONSTRAINT_SQL: Final = re.compile(r"\bADD\s+CONSTRAINT\s+" + _NAME, re.IGNORECASE)
_SET_DEFAULT_SQL: Final = re.compile(
    r"\bALTER\s+(?:COLUMN\s+)?" + _NAME + r"\s+SET\s+DEFAULT\b", re.IGNORECASE
)
_DROP_COLUMN_SQL: Final = re.compile(
    r"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.IGNORECASE
)
_DROP_CONSTRAINT_SQL: Final = re.compile(
    r"\bDROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?" + _NAME, re.IGNORECASE
)
_SET_NOT_NULL_SQL: Final = re.compile(
    r"\bALTER\s+(?:COLUMN\s+)?" + _NAME + r"\s+SET\s+NOT\s+NULL\b", re.IGNORECASE
)
#: Where one action of an ``ALTER TABLE`` ends and the next begins.
_NEXT_ACTION_SQL: Final = re.compile(r",\s*(?:ADD|DROP|ALTER|RENAME|VALIDATE)\b", re.IGNORECASE)
#: Alembic operations that add a named constraint: ``op.name(constraint, table, ...)``.
_CONSTRAINT_OPS: Final = frozenset(
    {
        "create_check_constraint",
        "create_foreign_key",
        "create_primary_key",
        "create_unique_constraint",
    }
)
#: What stands in for a piece of text the revision's own source does not spell.
#: It is not a word character, so no name can be read across it.
_UNREAD: Final = "\x00"

#: ``(table, column)`` or ``(table, constraint)``, both bare and lowercase.
_Key = tuple[str, str]


@dataclass(frozen=True)
class RevisionHeader:
    """What a revision says about itself at module level."""

    revision: str | None
    parents: tuple[str, ...]
    contract_of: str | None


def _assigned(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level ``NAME = value`` and ``NAME: T = value`` assignments."""
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                found[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                found[node.target.id] = node.value
    return found


def _text(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def read_header(source: str) -> RevisionHeader:
    """A revision's own id, the revisions it follows and the expand it names."""
    assigned = _assigned(ast.parse(source))
    down = assigned.get("down_revision")
    if isinstance(down, ast.Tuple | ast.List):
        parents = tuple(text for item in down.elts if (text := _text(item)) is not None)
    else:
        parents = tuple(text for text in (_text(down),) if text is not None)
    return RevisionHeader(
        revision=_text(assigned.get("revision")),
        parents=parents,
        contract_of=_text(assigned.get("contract_of")),
    )


def _method(call: ast.Call) -> str | None:
    """``name`` for ``op.name(...)`` or ``batch_op.name(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr
    return None


def _function(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _required_column_without_default(call: ast.Call) -> bool:
    """``add_column(table, Column(..., nullable=False))`` with no ``server_default``:
    an insert from the previous release, which does not know the column, fails."""
    for arg in call.args:
        if isinstance(arg, ast.Call) and _is_false(_keyword(arg, "nullable")):
            return _keyword(arg, "server_default") is None
    return False


def _value(
    node: ast.expr | None, assigned: Mapping[str, ast.expr], seen: frozenset[str] = frozenset()
) -> str | None:
    """The text ``node`` spells: a literal, a module-level name for one, or an
    f-string of those. ``None`` when any part of it is not in the source."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name) and node.id in assigned and node.id not in seen:
        return _value(assigned[node.id], assigned, seen | {node.id})
    if isinstance(node, ast.JoinedStr):
        text = "".join(_field(part, assigned, seen) for part in node.values)
        return None if _UNREAD in text else text
    return None


def _field(part: ast.expr, assigned: Mapping[str, ast.expr], seen: frozenset[str]) -> str:
    """One part of an f-string: its fixed text, or the value of its field."""
    if isinstance(part, ast.FormattedValue):
        value = _value(part.value, assigned, seen)
        return _UNREAD if value is None else value
    return _value(part, assigned, seen) or ""


def _texts(
    node: ast.AST, assigned: Mapping[str, ast.expr], seen: frozenset[str] = frozenset()
) -> Iterator[str]:
    """Every piece of text under ``node``, following module-level names. An
    f-string comes out whole, each field filled in where the source spells it."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            yield node.value
    elif isinstance(node, ast.JoinedStr):
        yield "".join(_field(part, assigned, seen) for part in node.values)
    elif isinstance(node, ast.Name):
        if node.id in assigned and node.id not in seen:
            yield from _texts(assigned[node.id], assigned, seen | {node.id})
    else:
        for child in ast.iter_child_nodes(node):
            yield from _texts(child, assigned, seen)


def _statements(call: ast.Call, assigned: Mapping[str, ast.expr]) -> str:
    """The SQL an ``execute`` call carries."""
    return " ".join(text for arg in call.args for text in _texts(arg, assigned))


def _bare(name: str) -> str:
    return name.replace('"', "").rsplit(".", 1)[-1].lower()


#: Words a name pattern can land on when the name itself could not be read.
_NOT_A_NAME: Final = frozenset({"if", "not", "exists", "only", "column", "constraint"})


def _key(table: str | None, name: str | None) -> _Key | None:
    if table is None or name is None:
        return None
    key = (_bare(table), _bare(name))
    return None if _NOT_A_NAME.intersection(key) else key


def _alterations(sql: str) -> Iterator[tuple[str, str]]:
    """Each ``ALTER TABLE`` in ``sql``: its table, and its actions up to the end
    of the statement."""
    found = list(_ALTER_TABLE_SQL.finditer(sql))
    ends = [*(following.start() for following in found[1:]), len(sql)]
    for match, end in zip(found, ends, strict=False):
        yield match.group(1), sql[match.end() : end].split(";", 1)[0]


def _keys(pattern: re.Pattern[str], table: str, actions: str) -> list[_Key]:
    """The ``(table, name)`` of every match of ``pattern`` whose name can be read."""
    keys = (_key(table, match.group(1)) for match in pattern.finditer(actions))
    return [key for key in keys if key is not None]


def _given(node: ast.expr | None) -> bool:
    return node is not None and not (isinstance(node, ast.Constant) and node.value is None)


_REFERENCES_SQL: Final = re.compile(r"\bREFERENCES\b", re.IGNORECASE)


@dataclass
class _Made:
    """What one ``upgrade()`` adds, which it may therefore take away again."""

    tables: set[str] = field(default_factory=set)
    columns: set[_Key] = field(default_factory=set)
    #: Columns the upgrade gives a server default, on adding them or after.
    defaulted: set[_Key] = field(default_factory=set)
    #: Constraints the upgrade adds over a column it adds, so none can predate it.
    constraints: set[_Key] = field(default_factory=set)

    def may_require(self, key: _Key | None) -> bool:
        return key in self.columns and key in self.defaulted

    def _over_own_column(self, table: str, definition: str) -> bool:
        return any(
            owner == _bare(table) and re.search(rf"\b{re.escape(column)}\b", definition, re.I)
            for owner, column in self.columns
        )

    def _constraint(self, table: str | None, name: str | None, definition: str) -> None:
        key = _key(table, name)
        if key is not None and table is not None and self._over_own_column(table, definition):
            self.constraints.add(key)

    def read_columns(self, call: ast.Call, assigned: Mapping[str, ast.expr]) -> None:
        """The tables and columns ``call`` adds, and the defaults it gives."""
        method, args = _method(call), call.args
        if method == "execute":
            sql = _statements(call, assigned)
            self.tables.update(_bare(m.group(1)) for m in _CREATE_TABLE_SQL.finditer(sql))
            for table, actions in _alterations(sql):
                self._sql_columns(table, actions)
        elif method == "create_table" and args:
            if (created := _value(args[0], assigned)) is not None:
                self.tables.add(_bare(created))
        elif method == "add_column" and len(args) >= 2 and isinstance(args[1], ast.Call):
            column = args[1]
            name = _value(column.args[0], assigned) if column.args else None
            if (key := _key(_value(args[0], assigned), name)) is not None:
                self.columns.add(key)
                if _given(_keyword(column, "server_default")):
                    self.defaulted.add(key)
        elif method == "alter_column" and len(args) >= 2:
            key = _key(_value(args[0], assigned), _value(args[1], assigned))
            if key is not None and _given(_keyword(call, "server_default")):
                self.defaulted.add(key)

    def _sql_columns(self, table: str, actions: str) -> None:
        for match in _ADD_COLUMN_SQL.finditer(actions):
            key = _key(table, match.group(1))
            if key is None:
                continue
            self.columns.add(key)
            definition = _NEXT_ACTION_SQL.split(actions[match.end() :], maxsplit=1)[0]
            if re.search(r"\bDEFAULT\b", definition, re.IGNORECASE):
                self.defaulted.add(key)
        self.defaulted.update(_keys(_SET_DEFAULT_SQL, table, actions))

    def read_constraints(self, call: ast.Call, assigned: Mapping[str, ast.expr]) -> None:
        """The constraints ``call`` adds over this upgrade's own columns. Read
        after every call's columns, since nothing orders the two in the source."""
        method, args = _method(call), call.args
        if method == "execute":
            for table, actions in _alterations(_statements(call, assigned)):
                for match in _ADD_CONSTRAINT_SQL.finditer(actions):
                    definition = _NEXT_ACTION_SQL.split(actions[match.end() :], maxsplit=1)[0]
                    # A foreign key is over its own columns, not the ones it points at.
                    own = _REFERENCES_SQL.split(definition, maxsplit=1)[0]
                    self._constraint(table, match.group(1), own)
        elif method in _CONSTRAINT_OPS and len(args) >= 3:
            # op.create_*(constraint, table, ...); a foreign key names the
            # table it points at before its own columns.
            rest = args[3:] if method == "create_foreign_key" else args[2:]
            definition = " ".join(text for arg in rest for text in _texts(arg, assigned))
            self._constraint(_value(args[1], assigned), _value(args[0], assigned), definition)
        elif _function(call) == "add_check_not_valid" and len(args) >= 3:
            definition = " ".join(_texts(args[2], assigned))
            self._constraint(_value(args[0], assigned), _value(args[1], assigned), definition)


def _sql_contractions(sql: str, made: _Made) -> Iterator[str]:
    """What ``sql`` takes away, less what the same upgrade added."""
    excused = dict.fromkeys((what for _, what in _CONTRACTING_SQL), 0)
    for table, actions in _alterations(sql):
        excused["drops a column"] += sum(
            key in made.columns for key in _keys(_DROP_COLUMN_SQL, table, actions)
        )
        excused["drops a constraint"] += sum(
            key in made.constraints for key in _keys(_DROP_CONSTRAINT_SQL, table, actions)
        )
        excused["makes a column required"] += sum(
            made.may_require(key) for key in _keys(_SET_NOT_NULL_SQL, table, actions)
        )
    for pattern, what in _CONTRACTING_SQL:
        # Every occurrence must be excused: one the reader could not tie to a
        # table and a name this upgrade added still counts.
        if len(pattern.findall(sql)) > excused[what]:
            yield what
    if any(_bare(m.group(1)) not in made.tables for m in _DROP_TABLE_SQL.finditer(sql)):
        yield "drops a table"


def contracting_operations(source: str) -> list[str]:
    """What ``upgrade()`` takes away or tightens, one sentence each, in source order."""
    tree = ast.parse(source)
    assigned = _assigned(tree)
    upgrade = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
    )
    if upgrade is None:
        return []
    calls = sorted(
        (n for n in ast.walk(upgrade) if isinstance(n, ast.Call)),
        key=lambda call: (call.lineno, call.col_offset),
    )
    made = _Made()
    for call in calls:
        made.read_columns(call, assigned)
    for call in calls:
        made.read_constraints(call, assigned)

    def named(call: ast.Call, table: int, name: int) -> _Key | None:
        if len(call.args) <= max(table, name):
            return None
        return _key(_value(call.args[table], assigned), _value(call.args[name], assigned))

    found: list[str] = []
    for call in calls:
        method = _method(call)
        where = f"line {call.lineno}"
        if method == "drop_table":
            name = _value(call.args[0], assigned) if call.args else None
            if name is None or _bare(name) not in made.tables:
                found.append(f"{where}: op.drop_table() drops a table")
        elif method == "drop_column":
            if named(call, 0, 1) not in made.columns:
                found.append(f"{where}: op.drop_column() takes something away")
        elif method == "drop_constraint":
            if named(call, 1, 0) not in made.constraints:
                found.append(f"{where}: op.drop_constraint() takes something away")
        elif method in CONTRACTING_OPS:
            found.append(f"{where}: op.{method}() takes something away")
        elif method == "alter_column":
            if _is_false(_keyword(call, "nullable")) and not made.may_require(named(call, 0, 1)):
                found.append(f"{where}: op.alter_column(nullable=False) makes a column required")
            if _keyword(call, "new_column_name") is not None:
                found.append(f"{where}: op.alter_column(new_column_name=...) renames a column")
        elif method == "add_column" and _required_column_without_default(call):
            found.append(f"{where}: op.add_column() adds a required column with no server default")
        elif method == "execute":
            sql = _statements(call, assigned)
            found.extend(f"{where}: SQL {what}" for what in _sql_contractions(sql, made))
        elif _function(call) in CONTRACTING_HELPERS:
            if named(call, 0, 1) not in made.constraints:
                found.append(f"{where}: {_function(call)}() drops a constraint")
    return found


def ancestors(revision: str, parents: Mapping[str, tuple[str, ...]]) -> frozenset[str]:
    """Every revision ``revision`` descends from, itself excluded."""
    seen: set[str] = set()
    pending = list(parents.get(revision, ()))
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(parents.get(current, ()))
    return frozenset(seen)


def lint_contract(source: str, parents: Mapping[str, tuple[str, ...]]) -> list[str]:
    """What is wrong with one revision under the expand-then-contract rule, as
    sentences; empty when nothing is. ``parents`` maps every revision in the
    tree to the revisions it follows."""
    header = read_header(source)
    operations = contracting_operations(source)
    if not operations:
        if header.contract_of is None:
            return []
        return [
            f'declares contract_of = "{header.contract_of}" but upgrade() takes nothing away; '
            "remove the declaration"
        ]
    if header.contract_of is None:
        return [
            *operations,
            "a previous release still reads and writes this schema while it is replaced: say "
            'which earlier revision moved it off what this one removes (contract_of = "NNNN"), '
            "or ship that expand first",
        ]
    if header.contract_of not in ancestors(header.revision or "", parents):
        return [f'contract_of = "{header.contract_of}" is not a revision this one descends from']
    return []


def lint_versions(versions: Path, *, above: int) -> dict[str, list[str]]:
    """Every revision in ``versions`` numbered above ``above`` that breaks the
    rule, by file name. Revisions at or below it predate the rule and are read
    only for the graph."""
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(versions.glob("*.py"))
    }
    headers = {name: read_header(source) for name, source in sources.items()}
    parents = {header.revision: header.parents for header in headers.values() if header.revision}
    broken: dict[str, list[str]] = {}
    for name, source in sources.items():
        revision = headers[name].revision
        if revision is None or not revision.isdigit() or int(revision) <= above:
            continue
        if found := lint_contract(source, parents):
            broken[name] = found
    return broken
