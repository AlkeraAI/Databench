"""A chat write takes its two rows in one order, and the code is what says so.

A chat serialises on two rows: its realtime document and the chat object. The
socket's append takes the document first and the chat second, and
``lock_chat_for_write`` exists so every other writer takes them the same way
round. Today that contract lives in one docstring: five writers take the chat
row alone, and the moment one of them also reaches the document — a transcript
line for "X switched to Auto" is the obvious next thing to want — that path is
chat-then-document against the socket's document-then-chat, and Postgres breaks
the cycle by aborting the request. The reader is told their switch failed on a
chat that is working perfectly.

So the invariant is read off the source rather than trusted. The two locks are
recognised by what they actually are — a ``FOR UPDATE`` whose ``select`` names
``RealtimeDoc`` or ``WorkspaceObject`` — and not by the name of the helper that
holds them, so renaming a helper, moving it, or adding a sixth writer changes
nothing here: the scan re-derives the set every run. What fails this test is
a function that reaches BOTH rows and reaches the chat row first.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
_CORE = Path(__file__).resolve().parents[3] / "packages" / "api-core" / "alkera_core"

#: Where the two locks live and where every writer that holds them is written.
#: The routes are deliberately NOT read: a route reaches its rows through these
#: services, and its calls sit on branches (claim the warm spare and answer, or
#: fall through and create) that a straight read of the source cannot tell apart
#: from two locks taken in one transaction. Reading them here would report an
#: order no request can take.
SOURCES = (
    _BACKEND / "services" / "chats" / "chat_service.py",
    _BACKEND / "services" / "objects" / "object_service.py",
    _BACKEND / "services" / "realtime" / "docsync.py",
    # Where the document-then-chat lock every writer above calls is written.
    _CORE / "objects" / "chat_end.py",
)

#: The model each lock names, and the name this test calls that lock by.
LOCKED_ROWS = {"RealtimeDoc": "doc", "WorkspaceObject": "chat"}

#: The one order. A writer that takes both takes them in this sequence.
ORDER = ("doc", "chat")


def _qualnames(tree: ast.Module, module: str) -> Iterator[tuple[str, ast.AST]]:
    """Every function in the module, by the name a call site would use."""

    def walk(node: ast.AST, prefix: str) -> Iterator[tuple[str, ast.AST]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}.{child.name}")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                yield f"{prefix}.{child.name}", child
                yield from walk(child, f"{prefix}.{child.name}")

    yield from walk(tree, module)


def _selected_models(node: ast.AST) -> set[str]:
    """The models a ``select(...)`` in this function names."""
    found: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        name = call.func.id if isinstance(call.func, ast.Name) else None
        if name != "select":
            continue
        for arg in call.args:
            target = arg
            while isinstance(target, ast.Attribute):
                target = target.value
            if isinstance(target, ast.Name) and target.id in LOCKED_ROWS:
                found.add(target.id)
    return found


def _holds_for_update(node: ast.AST) -> bool:
    """Whether the function locks rows: a ``with_for_update`` it spells, or the
    locking module's ``lock_rows``, which every row lock now goes through."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "with_for_update":
            return True
        if isinstance(func, ast.Name) and func.id == "lock_rows":
            return True
    return False


def _callee(call: ast.Call, *, module: str, own: str) -> str | None:
    """The scanned function this call names, spelled as ``_qualnames`` does."""
    func = call.func
    if isinstance(func, ast.Name):
        return f"{module}.{func.id}"
    if not isinstance(func, ast.Attribute):
        return None
    holder = func.value
    if isinstance(holder, ast.Name):
        if holder.id == "self":
            # A method calling its own class's helper: the class is whatever
            # this function is defined inside.
            enclosing = own.rsplit(".", 1)[0]
            return f"{enclosing}.{func.attr}"
        return f"{holder.id}.{func.attr}"
    return None


def _scan() -> dict[str, tuple[ast.AST, str]]:
    """Every scanned function, by qualified name, with the module it is in."""
    functions: dict[str, tuple[ast.AST, str]] = {}
    for path in SOURCES:
        module = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qualname, node in _qualnames(tree, module):
            functions[qualname] = (node, module)
    return functions


def _own_locks(
    qualname: str, node: ast.AST, module: str, functions: dict[str, tuple[ast.AST, str]]
) -> set[str]:
    """Which rows this function's own ``FOR UPDATE`` holds.

    A ``FOR UPDATE`` whose ``select`` is built by a helper (the registry builds
    its document query in one) names no model here, so the model is taken from
    the helpers this function calls.
    """
    if not _holds_for_update(node):
        return set()
    models = _selected_models(node)
    if not models:
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            named = _callee(call, module=module, own=qualname)
            entry = functions.get(named or "")
            if entry is not None:
                models |= _selected_models(entry[0])
    return {LOCKED_ROWS[model] for model in models}


def _order_taken(
    qualname: str,
    functions: dict[str, tuple[ast.AST, str]],
    memo: dict[str, tuple[str, ...]],
    seen: frozenset[str],
) -> tuple[str, ...]:
    """The rows this function takes, in the order its source takes them."""
    if qualname in memo:
        return memo[qualname]
    if qualname in seen or qualname not in functions:
        return ()
    node, module = functions[qualname]
    mine = _own_locks(qualname, node, module, functions)
    events: list[tuple[int, str]] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "with_for_update"
            and len(mine) == 1
        ):
            events.append((call.lineno, next(iter(mine))))
            continue
        if isinstance(call.func, ast.Name) and call.func.id == "lock_rows":
            # The row this one call locks: the model its own select names,
            # else the function's one locked row when the select is a helper's.
            named_here = {LOCKED_ROWS[model] for model in _selected_models(call)}
            locked_here = named_here or (mine if len(mine) == 1 else set())
            events.extend((call.lineno, row) for row in sorted(locked_here))
            continue
        named = _callee(call, module=module, own=qualname)
        if named is None or named == qualname:
            continue
        for row in _order_taken(named, functions, memo, seen | {qualname}):
            events.append((call.lineno, row))
    taken = tuple(row for _, row in sorted(events, key=lambda event: event[0]))
    memo[qualname] = taken
    return taken


def _takers() -> dict[str, tuple[str, ...]]:
    functions = _scan()
    memo: dict[str, tuple[str, ...]] = {}
    return {
        qualname: _order_taken(qualname, functions, memo, frozenset()) for qualname in functions
    }


def _first(rows: tuple[str, ...], row: str) -> int:
    return rows.index(row)


def test_the_scan_finds_both_locks_and_a_writer_that_takes_them_both() -> None:
    """The invariant is only worth asserting if the scan can see the mechanism.

    A scan that recognised nothing would pass the order check vacuously, so the
    locks and at least one writer holding both are proven present first — by
    the ``FOR UPDATE`` each one is, never by the name of the helper around it.
    """
    taken = _takers()
    assert {name for name, rows in taken.items() if rows == ("chat",)}, (
        "no function takes the chat row alone: the scan is not recognising the lock"
    )
    assert {name for name, rows in taken.items() if rows == ("doc",)}, (
        "no function takes the document row alone: the scan is not recognising the lock"
    )
    assert {name for name, rows in taken.items() if set(rows) == {"doc", "chat"}}, (
        "no function takes both rows: the order this test enforces is unobservable"
    )


def test_every_writer_that_takes_both_rows_takes_the_document_first() -> None:
    """The one order, over every function in the chat write path.

    A writer that takes the chat row and then reaches the document holds
    exactly what a box appending over its socket is waiting for while the box
    holds what it is waiting for. Postgres aborts one of them, and the one it
    aborts is the reader's request.
    """
    wrong = {
        qualname: rows
        for qualname, rows in _takers().items()
        if set(rows) == {"doc", "chat"} and _first(rows, "chat") < _first(rows, "doc")
    }
    assert not wrong, (
        "these take the chat row before the document row, against the order "
        f"the socket's append takes them in: {wrong}. Route the write through "
        "lock_chat_for_write, which holds both the one way round."
    )


@pytest.mark.parametrize(
    ("rows", "sound"),
    [
        pytest.param(("doc", "chat"), True, id="the-one-order"),
        pytest.param(("doc", "chat", "doc", "chat"), True, id="re-taken-under-the-same-order"),
        pytest.param(("chat",), True, id="the-chat-row-alone"),
        pytest.param(("doc",), True, id="the-document-row-alone"),
        pytest.param(("chat", "doc"), False, id="the-deadlocking-order"),
        pytest.param(("chat", "chat", "doc"), False, id="the-document-reached-last"),
    ],
)
def test_the_order_check_is_the_one_a_deadlock_needs(rows: tuple[str, ...], sound: bool) -> None:
    """What the check above calls wrong, stated on rows instead of on source.

    Holding one row alone is sound whichever it is — a cycle needs two — and
    re-taking a lock the transaction already holds costs nothing, so only the
    first time each row is reached decides.
    """
    both = set(rows) == {"doc", "chat"}
    assert (not both or _first(rows, "doc") < _first(rows, "chat")) is sound
