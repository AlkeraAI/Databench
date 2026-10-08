"""``alkera.test``: pytest-native data tests (re-exported from ``alkera``).

This is the full-expressiveness authoring tier: plain Python functions in the
user's own test directory, registered by the ``@alkera.test`` decorator
with explicit URN bindings, collected by plain pytest AND by ``alkera test``
identically::

    import alkera

    @alkera.test(refs=["duckdb://jaffle_shop/main.orders#amount"],
                 severity="blocking")
    def test_orders_amount_positive(conn: alkera.Connection):
        bad = conn.sql("select * from {orders} where amount < 0")
        assert bad.is_empty(), bad.sample(5)

The decorator only REGISTERS the function + its bindings (so the gate can
select it like any other test); execution flows through the governed runner
seam in the product's test runner -- the connection a test receives is
budgeted/timeout-enforced there, never a raw driver handle.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

TestFunction = typing.Callable[..., object]


class RowSet:
    """The result of a governed ``conn.sql(...)`` call -- failing rows, ready for
    the ``is_empty()`` assertion and a ``sample(n)`` failure message."""

    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self.rows: list[dict[str, object]] = list(rows)

    def is_empty(self) -> bool:
        return not self.rows

    def sample(self, n: int = 5) -> list[dict[str, object]]:
        return self.rows[:n]

    def __len__(self) -> int:
        return len(self.rows)


@runtime_checkable
class Connection(Protocol):
    """The governed connection a pytest-native test receives. ``{label}``
    placeholders in the query resolve to the relations bound via ``refs``
    (keyed by relation name), so tests never hardcode physical names."""

    def sql(self, query: str) -> RowSet:
        """Run a SELECT and return its rows."""
        ...


@dataclass(frozen=True)
class RegisteredTest:
    """One ``@alkera.test``-registered function + its declared bindings."""

    __test__ = False  # not a pytest test class despite the Test* name

    fn: TestFunction
    refs: tuple[str, ...]
    severity: str = "blocking"
    enabled: bool = True
    name: str = ""
    source_file: str = ""
    params: dict[str, object] = field(default_factory=dict)


_REGISTRY: list[RegisteredTest] = []


def test(
    *,
    refs: Sequence[str],
    severity: str = "blocking",
    enabled: bool = True,
    name: str | None = None,
) -> Callable[[TestFunction], TestFunction]:
    """Register a pytest-native data test bound to ``refs`` (every input URN --
    multi-target bindings are mandatory so the gate selects the test when ANY
    input changes). Returns the function unchanged, so plain pytest still
    collects and runs it."""
    if not isinstance(refs, (list, tuple)):
        # A bare string char-iterates into one-letter refs; a mapping iterates its
        # KEYS (discarding the URN values), a set orders nondeterministically.
        # Demand a real ordered list/tuple of URN strings.
        raise ValueError("@alkera.test refs= must be a list of URN strings, e.g. refs=[...]")
    if not refs:
        raise ValueError("@alkera.test refs= must be a list of URN strings, e.g. refs=[...]")
    bad = next((r for r in refs if not isinstance(r, str) or not r.strip()), None)
    if bad is not None:
        raise ValueError(f"@alkera.test refs= must be non-empty URN strings, got {bad!r}")
    # `enabled` is typed bool but nothing enforces it: `enabled="false"` would be
    # kept as a truthy str, then Pydantic-coerced to False and skip the test.
    if not isinstance(enabled, bool):
        raise ValueError(f"@alkera.test enabled= must be true or false, got {enabled!r}")
    if not isinstance(severity, str) or not severity.strip():
        raise ValueError(f"@alkera.test severity= must be a non-empty string, got {severity!r}")
    # `name` is typed `str | None`, but `name=[] or fn.__name__` silently fell
    # back to the function name, and `name=123` registered an int. When given, it
    # must be a real non-empty string.
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError(f"@alkera.test name= must be a non-empty string, got {name!r}")

    def register(fn: TestFunction) -> TestFunction:
        code = getattr(fn, "__code__", None)
        registered = RegisteredTest(
            fn=fn,
            refs=tuple(refs),
            severity=severity,
            enabled=enabled,
            name=name or fn.__name__,
            source_file=code.co_filename if code is not None else "",
        )
        _REGISTRY.append(registered)
        return fn

    return register


def registered_tests() -> list[RegisteredTest]:
    """A copy of everything registered so far (the loader's read surface)."""
    return list(_REGISTRY)


def reset_registered() -> None:
    """Clear the registry -- loader/test isolation between collection passes."""
    _REGISTRY.clear()


__test__ = False  # keep pytest from collecting the module-level `test` symbol

__all__ = [
    "Connection",
    "RegisteredTest",
    "RowSet",
    "registered_tests",
    "reset_registered",
    "test",
]
