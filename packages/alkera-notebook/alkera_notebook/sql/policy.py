"""Statement policy: whether a statement runs, needs a person's confirmation,
or is refused.

The broker asks the policy before every statement. By default every statement
runs, whatever it does: the gate is the run, not the SQL in it. A cell runs
only when a person runs it, when a person approved the agent's run, or under a
permission mode that lets the agent run without asking, and a cell's Python
can change data just as freely as its SQL. A deployment that wants a statement
rule on top plugs in its own policy (and a ``Confirmer`` to ask with).
The classifier is a seam too: the kind is still reported with each statement.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from alkera_notebook.sql.provider import Requester

StatementKind = Literal["read", "write", "ddl", "unknown"]
Decision = Literal["allow", "confirm", "refuse"]

Classifier = Callable[[str], StatementKind]


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    kind: StatementKind
    reason: str = ""


class StatementPolicy(Protocol):
    def check(self, statement: str, actor: Requester) -> PolicyDecision: ...


_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_READ_HEADS = frozenset(
    {
        "select",
        "with",
        "show",
        "describe",
        "desc",
        "explain",
        "values",
        "table",
        "from",
        "summarize",
        "pivot",
        "unpivot",
    }
)
_WRITE_HEADS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "merge",
        "upsert",
        "replace",
        "copy",
        "call",
        "truncate",
        "load",
        "unload",
        "put",
        "get",
        "remove",
        "vacuum",
        "analyze",
        "set",
        "use",
        "begin",
        "commit",
        "rollback",
        "export",
        "import",
        "checkpoint",
        "pragma",
        "attach",
        "detach",
        "install",
    }
)
_DDL_HEADS = frozenset({"create", "drop", "alter", "rename", "grant", "revoke", "comment"})
# A read whose body still writes: `WITH ... INSERT`, `SELECT ... INTO`, a
# data-modifying CTE, or a read that writes a file.
_WRITE_INSIDE = re.compile(
    r"\b(insert|update|delete|merge|copy|create|drop|alter|truncate|into\s+(?:outfile|dumpfile))\b",
    re.I,
)


def keyword_classifier(statement: str) -> StatementKind:
    """Classifies by the leading keyword of each statement, after removing
    comments and string literals. Anything it cannot read as plainly a read
    is not a read: a multi-statement text counts as its strongest part, and
    a read that names a writing keyword outside a string is a write."""
    text = _STRING.sub("''", _COMMENT.sub(" ", statement))
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if not parts:
        return "unknown"
    kinds: list[StatementKind] = []
    for part in parts:
        head = re.match(r"\(*\s*([A-Za-z_]+)", part)
        word = head.group(1).lower() if head else ""
        if word in _DDL_HEADS:
            kinds.append("ddl")
        elif word in _WRITE_HEADS:
            kinds.append("write")
        elif word in _READ_HEADS:
            kinds.append("write" if _WRITE_INSIDE.search(part) else "read")
        else:
            kinds.append("unknown")
    order: tuple[StatementKind, ...] = ("ddl", "write", "unknown")
    return next((kind for kind in order if kind in kinds), "read")


class DefaultStatementPolicy:
    """Every statement runs. The kind is classified and reported, never used
    to stop a run that was already approved."""

    def __init__(self, classifier: Classifier = keyword_classifier) -> None:
        self._classify = classifier

    def check(self, statement: str, actor: Requester) -> PolicyDecision:
        return PolicyDecision("allow", self._classify(statement))


class Confirmer(Protocol):
    """Asks a person to confirm a statement that changes data. The engine
    implements it (a prompt in the notebook); the answer covers the rest of
    the run."""

    async def confirm(
        self, run_id: str, actor: Requester, statement: str, kind: StatementKind
    ) -> bool: ...


class NoConfirmer:
    """With no way to ask anyone, nothing is confirmed."""

    async def confirm(
        self, run_id: str, actor: Requester, statement: str, kind: StatementKind
    ) -> bool:
        return False
