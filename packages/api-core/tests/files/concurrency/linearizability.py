"""A Wing-Gong / Porcupine-style history checker for the Files op set.

A stress run against real Postgres produces a *history*: every operation's
invocation and response, stamped from the injected clock. The API is supposed
to behave as one sequential system, so that history must be **linearizable** —
there has to exist some total order of the operations that (a) respects
real-time precedence (an op that returned before another was invoked comes
first) and (b) makes every recorded result the one a tiny sequential reference
model would produce.

The checker searches those orders depth-first, always taking an op that no
remaining op must precede, and backtracks the moment the model diverges from a
recorded result. That search is exponential in the number of *concurrent* ops,
so `check_linearizable` refuses a history wider than `MAX_CONCURRENT_OPS`
(`TooLarge`) and a soak run feeds it overlapping windows instead.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Hashable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from alkera_core.files.clock import Clock

MAX_CONCURRENT_OPS = 12
"""Above this many mutually-overlapping ops the search is refused, not run."""


# The `Error` suffix N818 wants would read as a production failure; this is a
# checker guard a soak run catches to narrow its window.
class TooLarge(AssertionError):  # noqa: N818
    """The history is wider than the checker's concurrency bound."""


@dataclass(frozen=True)
class Op:
    """One completed operation: what was asked, what came back, and when."""

    kind: str
    args: tuple[Any, ...]
    result: Any
    invoke_at: float
    return_at: float
    client: str

    def __str__(self) -> str:
        rendered = ", ".join(repr(a) for a in self.args)
        return f"{self.client}:{self.kind}({rendered}) -> {self.result!r}"


@dataclass
class Call:
    """The in-flight handle `History.record` yields; the body sets `result`."""

    kind: str
    args: tuple[Any, ...]
    client: str
    invoke_at: float
    result: Any = None


class Model(Protocol):
    """A tiny sequential reference implementation of the contract."""

    def apply(self, op: Op) -> Any:
        """Run `op` against this state and return what it should have returned."""
        ...

    def key(self) -> Hashable:
        """A hashable snapshot of the state, so equal states prune the search."""
        ...


@dataclass
class History:
    """The recorded operations, in completion order."""

    ops: list[Op] = field(default_factory=list)
    clock: Clock | None = None

    @asynccontextmanager
    async def record(
        self,
        kind: str,
        args: Iterable[Any] = (),
        *,
        client: str = "c0",
    ) -> AsyncIterator[Call]:
        """Stamp invoke/return around a real call from the injected clock."""
        if self.clock is None:
            msg = "History.record needs a clock; construct History(clock=...)"
            raise ValueError(msg)
        call = Call(kind=kind, args=tuple(args), client=client, invoke_at=self.clock.monotonic())
        try:
            yield call
        finally:
            self.ops.append(
                Op(
                    kind=call.kind,
                    args=call.args,
                    result=call.result,
                    invoke_at=call.invoke_at,
                    return_at=self.clock.monotonic(),
                    client=call.client,
                )
            )


@dataclass(frozen=True)
class LinResult:
    """The verdict, plus the witness order or the op nothing could explain."""

    ok: bool
    order: tuple[Op, ...] = ()
    violating_op: Op | None = None
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


def max_concurrency(ops: Sequence[Op]) -> int:
    """The largest number of ops whose intervals overlap at one instant."""
    events: list[tuple[float, int]] = []
    for op in ops:
        events.append((op.invoke_at, 1))
        events.append((op.return_at, -1))
    # A return at exactly another op's invocation is NOT concurrency, so the
    # -1 sorts ahead of the +1 at an equal timestamp.
    events.sort()
    current = 0
    widest = 0
    for _, delta in events:
        current += delta
        widest = max(widest, current)
    return widest


def check_linearizable(history: History, model: type[Model]) -> LinResult:
    """Search for a linearization of `history` that `model` explains."""
    ops = sorted(history.ops, key=lambda op: (op.invoke_at, op.return_at))
    if not ops:
        return LinResult(ok=True, message="empty history")
    width = max_concurrency(ops)
    if width > MAX_CONCURRENT_OPS:
        msg = (
            f"history has {width} concurrent operations, above the checker's "
            f"bound of {MAX_CONCURRENT_OPS}; check overlapping windows instead"
        )
        raise TooLarge(msg)

    deepest: dict[str, Any] = {"depth": -1, "op": None, "actual": None}
    dead: set[tuple[frozenset[int], Hashable]] = set()

    def search(state: Model, remaining: frozenset[int], depth: int) -> tuple[Op, ...] | None:
        if not remaining:
            return ()
        memo = (remaining, state.key())
        if memo in dead:
            return None
        # An op may go next unless some OTHER remaining op returned before it
        # was invoked. Comparing against the two earliest returns keeps that
        # exclusion exact for a zero-duration op, which is its own minimum.
        returns = sorted(ops[i].return_at for i in remaining)
        earliest = returns[0]
        second = returns[1] if len(returns) > 1 else earliest
        for index in sorted(remaining):
            op = ops[index]
            others_min = second if op.return_at == earliest else earliest
            if others_min < op.invoke_at:
                continue  # another op must return before this one may start
            candidate = copy.deepcopy(state)
            actual = candidate.apply(op)
            if actual != op.result:
                if depth > deepest["depth"]:
                    deepest.update(depth=depth, op=op, actual=actual)
                continue
            tail = search(candidate, remaining - {index}, depth + 1)
            if tail is not None:
                return (op, *tail)
        dead.add(memo)
        return None

    order = search(model(), frozenset(range(len(ops))), 0)
    if order is not None:
        return LinResult(ok=True, order=order, message="linearizable")
    culprit: Op | None = deepest["op"]
    if culprit is None:
        return LinResult(ok=False, message="no linearization exists")
    return LinResult(
        ok=False,
        violating_op=culprit,
        message=(
            f"no linearization explains {culprit}; the model produced "
            f"{deepest['actual']!r} at every reachable position"
        ),
    )


class RegisterModel:
    """A single register: after `write(v)`, `read()` returns `v`."""

    def __init__(self) -> None:
        self.value: int | None = None

    def apply(self, op: Op) -> Any:
        if op.kind == "write":
            self.value = op.args[0]
            return None
        if op.kind == "read":
            return self.value
        msg = f"RegisterModel has no operation {op.kind!r}"
        raise ValueError(msg)

    def key(self) -> Hashable:
        return self.value


@dataclass
class _Node:
    trashed: bool = False
    content: str | None = None
    grants: dict[str, str] = field(default_factory=dict)


class NamespaceModel:
    """Paths to nodes — the contract the namespace routes have to meet.

    Every operation returns a value rather than raising, so an illegal call is
    a recordable outcome (`"conflict"`, `"missing"`) a history can carry.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, _Node] = {}

    def apply(self, op: Op) -> Any:
        """One branch per contract operation; illegal calls return an outcome."""
        kind = op.kind
        if kind == "create":
            (path,) = op.args
            if path in self.nodes:
                return "conflict"
            self.nodes[path] = _Node()
            return path
        if kind in {"rename", "move"}:
            source, target = op.args
            node = self._live(source)
            if node is None:
                return "missing"
            if target in self.nodes:
                return "conflict"
            del self.nodes[source]
            self.nodes[target] = node
            return target
        if kind == "trash":
            (path,) = op.args
            node = self._live(path)
            if node is None:
                return "missing"
            node.trashed = True
            return path
        if kind == "restore":
            (path,) = op.args
            trashed = self.nodes.get(path)
            if trashed is None or not trashed.trashed:
                return "missing"
            trashed.trashed = False
            return path
        if kind == "grant":
            path, principal, role = op.args
            node = self._live(path)
            if node is None:
                return "missing"
            node.grants[principal] = role
            return role
        if kind == "revoke":
            path, principal = op.args
            node = self._live(path)
            if node is None:
                return "missing"
            return node.grants.pop(principal, None)
        if kind == "put_version":
            path, content = op.args
            node = self._live(path)
            if node is None:
                return "missing"
            node.content = content
            return content
        if kind == "read_version":
            (path,) = op.args
            node = self._live(path)
            return None if node is None else node.content
        if kind == "list":
            return tuple(sorted(p for p, n in self.nodes.items() if not n.trashed))
        msg = f"NamespaceModel has no operation {kind!r}"
        raise ValueError(msg)

    def _live(self, path: str) -> _Node | None:
        node = self.nodes.get(path)
        return None if node is None or node.trashed else node

    def key(self) -> Hashable:
        return tuple(
            (path, node.trashed, node.content, tuple(sorted(node.grants.items())))
            for path, node in sorted(self.nodes.items())
        )
