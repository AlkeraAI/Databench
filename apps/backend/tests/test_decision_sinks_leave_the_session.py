"""Gate: recording a denial never touches the caller's session.

A permission check runs in the middle of the caller's unit of work — a listing
that refuses one row and goes on to the next, a batch that refuses one item
after earlier ones wrote. A sink that ended, flushed or wrote through the
caller's session to record a DENY expired every row the caller had loaded and
threw away its writes; the trash listing answered 500 for it. So the rule is
structural: every ``record_deny`` in the product may hand the caller's session
on to another sink's ``record_deny`` and do nothing else with it. Its own row
goes through a session of its own.

The scan reads the real tree, so a sink added tomorrow is held to the rule the
day it lands; the decoys prove the scan catches what it is for.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spread]

REPO = Path(__file__).resolve().parents[3]
ROOTS = (REPO / "apps" / "backend" / "backend", REPO / "packages" / "api-core" / "alkera_core")


#: The recorders that commit a row of their own: a denial, and a batch summary.
OWN_SESSION_RECORDERS = frozenset({"record_deny", "record_batch"})


def _record_denies(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in OWN_SESSION_RECORDERS
        ):
            yield node


def _session_param(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The caller's session: the first parameter after ``self``."""
    params = [arg.arg for arg in fn.args.posonlyargs + fn.args.args]
    if params and params[0] == "self":
        params = params[1:]
    return params[0] if params else None


def _forwarded(fn: ast.AST, name: str) -> set[int]:
    """Ids of the ``name`` loads that are handed straight to another recorder."""
    passed: set[int] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in OWN_SESSION_RECORDERS
        ):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name) and arg.id == name:
                    passed.add(id(arg))
    return passed


def violations(source: str, label: str) -> list[str]:
    """Every use of a ``record_deny``'s caller session other than forwarding it."""
    found: list[str] = []
    for fn in _record_denies(ast.parse(source)):
        name = _session_param(fn)
        if name is None:
            continue
        allowed = _forwarded(fn, name)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id == name and id(node) not in allowed:
                if isinstance(node.ctx, ast.Load):
                    found.append(f"{label}:{node.lineno} uses the caller's session `{name}`")
    return found


def _tree_violations() -> tuple[list[str], int]:
    found: list[str] = []
    sinks = 0
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if not any(name in source for name in OWN_SESSION_RECORDERS):
                continue
            sinks += sum(1 for _ in _record_denies(ast.parse(source)))
            found.extend(violations(source, str(path.relative_to(REPO))))
    return found, sinks


def test_no_sink_records_a_denial_through_the_callers_session() -> None:
    found, sinks = _tree_violations()
    assert not found, "a DENY recorded through the caller's session:\n" + "\n".join(found)
    assert sinks >= 4, f"the scan found only {sinks} recorder implementations"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("await db.rollback()", id="ends-the-callers-transaction"),
        pytest.param("await db.commit()", id="commits-the-callers-work"),
        pytest.param("await emit(db, event)", id="writes-into-the-callers-session"),
        pytest.param("db.expire_all()", id="expires-the-callers-rows"),
        pytest.param("session = db\n        await session.flush()", id="through-an-alias"),
    ],
)
def test_the_scan_catches_a_sink_that_touches_the_callers_session(body: str) -> None:
    planted = f"class Sink:\n    async def record_deny(self, db, event):\n        {body}\n"
    assert violations(planted, "planted")


def test_the_scan_catches_a_batch_recorder_that_touches_the_callers_session() -> None:
    planted = (
        "class Sink:\n    async def record_batch(self, db, event):\n        await db.flush()\n"
    )
    assert violations(planted, "planted")


def test_the_scan_admits_a_sink_that_only_forwards_the_session() -> None:
    planted = (
        "class Wrapping:\n"
        "    async def record_deny(self, db, event):\n"
        "        self._seen.clear()\n"
        "        async with DecisionSessionLocal() as own:\n"
        "            await emit(own, event)\n"
        "        await self._inner.record_deny(db, event)\n"
    )
    assert violations(planted, "planted") == []
