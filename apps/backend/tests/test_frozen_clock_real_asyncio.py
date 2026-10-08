"""Every frozen clock inside a coroutine lets the event loop keep real time.

``freeze_time`` without ``real_asyncio=True`` freezes the loop's own clock too, and
the root conftest lists ``sqlalchemy`` among the modules freezegun answers with real
time. A database connection opened inside such a block therefore arms asyncpg's
connect timeout on the REAL monotonic clock (the call is made from SQLAlchemy's
greenlet bridge) while the loop checks it against the FROZEN one, decades later: the
timeout has already passed, and the connect dies with a bare ``TimeoutError`` on its
first turn. A test only reaches that connect when the pool has no idle connection
for it — the first test on a worker, or the one after a test that leaked or
invalidated its connection — so the same test passed or failed by xdist placement.

The first test pins the hazard itself, so the day freezegun stops mixing the two
clocks the guard can go; the second pins the remedy; the third keeps every async
frozen block in the repo on the remedy.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alkera_core.config import settings
from alkera_core.db.tls import asyncpg_connect_args
from freezegun import freeze_time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Where the suites live; ``vendor/`` is not ours to police.
_TEST_ROOTS = ("apps", "packages", "ops")


@pytest.fixture
async def fresh_engine() -> AsyncIterator[AsyncEngine]:
    """An engine of this test's own with an empty pool, so the query below must
    open a brand-new connection — the path a warm shared pool hides."""
    engine = create_async_engine(settings.database_url, connect_args=asyncpg_connect_args())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_connection_opened_under_a_frozen_loop_clock_times_out_at_once(
    fresh_engine: AsyncEngine,
) -> None:
    """The premise. If this starts passing, freezegun no longer hands the connect
    two different clocks, and the rule below has nothing left to guard."""
    with freeze_time("2026-09-05T12:00:00Z", real_asyncio=False), pytest.raises(TimeoutError):
        async with fresh_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))


@pytest.mark.asyncio
async def test_a_connection_opened_under_real_asyncio_connects_and_sees_the_frozen_time(
    fresh_engine: AsyncEngine,
) -> None:
    with freeze_time("2026-09-05T12:00:00Z", real_asyncio=True) as frozen:
        async with fresh_engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        # Driving the clock across a boundary still works with the loop on real time.
        frozen.move_to("2026-09-06T12:00:00Z")
        async with fresh_engine.connect() as conn:
            assert (await conn.execute(text("SELECT 2"))).scalar_one() == 2


def _is_freeze_time(call: ast.Call) -> bool:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "freeze_time"


def offenders(source: str) -> list[tuple[int, str]]:
    """``(line, coroutine)`` for each ``freeze_time(...)`` inside or decorating an
    ``async def`` that does not say ``real_asyncio=`` explicitly. An explicit
    ``real_asyncio=False`` is a decision and passes; silence is the default that
    freezes the loop."""
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for root in (*fn.decorator_list, *fn.body):
            for node in ast.walk(root):
                if (
                    isinstance(node, ast.Call)
                    and _is_freeze_time(node)
                    and id(node) not in seen
                    and not any(k.arg == "real_asyncio" for k in node.keywords)
                ):
                    seen.add(id(node))
                    found.append((node.lineno, fn.name))
    return found


def _python_test_files() -> Iterator[Path]:
    for top in _TEST_ROOTS:
        for path in (REPO_ROOT / top).rglob("*.py"):
            parts = path.relative_to(REPO_ROOT).parts
            if "tests" in parts and "node_modules" not in parts and ".venv" not in parts:
                yield path


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "async def t():\n    with freeze_time('2026-01-01'):\n        pass\n",
            [(2, "t")],
            id="with-block-in-a-coroutine",
        ),
        pytest.param(
            "@freeze_time('2026-01-01')\nasync def t():\n    pass\n",
            [(1, "t")],
            id="decorator-on-a-coroutine",
        ),
        pytest.param(
            "async def t():\n    with freezegun.freeze_time('2026-01-01', tick=True):\n"
            "        pass\n",
            [(2, "t")],
            id="attribute-spelling-with-other-keywords",
        ),
        pytest.param(
            "async def t():\n    async def inner():\n"
            "        with freeze_time('2026-01-01'):\n            pass\n",
            [(3, "t")],
            id="nested-coroutine-counts-once",
        ),
        pytest.param(
            "async def t():\n    with freeze_time('2026-01-01', real_asyncio=True):\n"
            "        pass\n",
            [],
            id="real-asyncio-true",
        ),
        pytest.param(
            "async def t():\n    with freeze_time('2026-01-01', real_asyncio=False):\n"
            "        pass\n",
            [],
            id="explicit-false-is-a-decision",
        ),
        pytest.param(
            "def t():\n    with freeze_time('2026-01-01'):\n        pass\n",
            [],
            id="sync-test-has-no-loop",
        ),
    ],
)
def test_the_scan_names_exactly_the_silent_frozen_blocks_in_coroutines(
    source: str, expected: list[tuple[int, str]]
) -> None:
    assert offenders(source) == expected


def test_every_frozen_clock_in_a_coroutine_keeps_the_loop_on_real_time() -> None:
    bad = [
        f"{path.relative_to(REPO_ROOT)}:{line} ({fn})"
        for path in _python_test_files()
        if "freeze_time" in (source := path.read_text(encoding="utf-8"))
        for line, fn in offenders(source)
    ]
    assert not bad, (
        "these frozen clocks run inside a coroutine without real_asyncio=True, so a "
        "database connection opened in them times out at once (see this module's "
        "docstring):\n  " + "\n  ".join(bad)
    )
