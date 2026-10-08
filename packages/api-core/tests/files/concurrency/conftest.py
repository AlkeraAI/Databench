"""Two-session fixtures: real Postgres, real isolation, no sleeps.

Every concurrency claim in the Files spec is about two *backends* touching one
row, so a test that shares one `AsyncSession` proves nothing — SQLAlchemy would
serialize the statements on a single connection and the interleaving under test
could never happen. `sessions(n)` therefore hands out n sessions each pinned to
its own checked-out connection.

The connections come from ONE engine per test session, not from a fresh engine
per call. A fresh engine could not reuse anything: every `sessions(n)` was n TCP
connects, n Postgres backends forked and n authentication round-trips, and the
engine was disposed at the end of the test so the next one paid for them again —
about 340 real connects for the lock-ordering module alone. On Linux a backend is
a `fork`; on Windows it is a `CreateProcess`, and with every xdist worker doing
this at once that churn, not the interleaving under test, is what the concurrency
modules spent their time on. Pooled, the whole directory costs one poolful per
worker. Isolation is unchanged — a test still gets its own `AsyncSession` on its
own connection — because a connection returning to the pool is rolled back, and
`pool_pre_ping` replaces one the server dropped in between.

The pool is sized to the most connections one test here holds at once and takes
no overflow, so a test that asks for more than the directory accounts for waits
on the pool instead of quietly opening a backend nobody counted.

`per_statement_connection=True` returns sessions that open a fresh connection
per statement and commit it there: PgBouncer in transaction mode, where nothing
session-scoped (a `pg_advisory_lock`, a temp table, an open transaction)
survives from one statement to the next. The lease/shard tests use it to prove
mutual exclusion does not depend on connection affinity. That shape keeps its
own engine sized `pool_size=n, max_overflow=0`: the claim it exists to prove is
that a statement gives its connection BACK, and a pool with room to spare would
let a session that never released one pass anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol

import pytest
from alkera_core.config import settings
from sqlalchemy import Executable
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine


class ConcurrentSession(Protocol):
    """What a concurrency test needs from a session, whichever kind it got."""

    async def execute(self, statement: Executable, /) -> Any:
        """Run one statement."""
        ...

    async def commit(self) -> None:
        """Make this session's work visible to the others."""
        ...

    async def close(self) -> None:
        """Release the underlying resources."""
        ...


class _MaterializedResult:
    """Rows read before the per-statement connection went away."""

    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        (row,) = self._rows
        return row[0]

    def scalar(self) -> Any:
        return None if not self._rows else self._rows[0][0]


class PerStatementSession:
    """A session whose every statement runs on a fresh connection.

    Mimics PgBouncer transaction pooling: the statement runs, commits, and the
    connection goes back to the pool, so the next statement may land on a
    different backend entirely.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def execute(self, statement: Executable, /) -> Any:
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            rows = result.all() if result.returns_rows else []
            await connection.commit()
        return _MaterializedResult(rows)

    async def commit(self) -> None:
        """A no-op: each statement already committed on its own connection."""
        return None

    async def close(self) -> None:
        return None


SessionFactory = Callable[..., Awaitable[list[ConcurrentSession]]]

#: The most connections one test in this directory holds at once: the
#: eight-writer soak takes ten, and a pair test takes three for each of the two
#: orders it drives without closing the first order's. Two spare, and no
#: overflow, so a test that asks for more than that waits instead of opening a
#: backend the directory never accounted for.
MAX_CONCURRENT_SESSIONS = 12

#: Long enough to outlast a checkout queued behind a neighbour on a loaded box,
#: short enough that a test leaking connections fails on the pool rather than
#: hanging to the per-test timeout with nothing to read.
POOL_TIMEOUT_SECONDS = 30.0


@pytest.fixture(scope="session")
async def concurrency_engine() -> AsyncIterator[AsyncEngine]:
    """The one engine every `sessions(n)` checkout in this directory borrows from.

    Session-scoped, which under xdist is one per worker process — each already
    pointed at its own database by the repo-root conftest.
    """
    engine = create_async_engine(
        settings.database_url,
        pool_size=MAX_CONCURRENT_SESSIONS,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def sessions(concurrency_engine: AsyncEngine) -> AsyncIterator[SessionFactory]:
    """`await sessions(n)` → n sessions, each on its own connection."""
    engines: list[AsyncEngine] = []
    connections: list[AsyncConnection] = []
    opened: list[ConcurrentSession] = []

    async def factory(
        n: int,
        *,
        per_statement_connection: bool = False,
    ) -> list[ConcurrentSession]:
        if per_statement_connection:
            engine = create_async_engine(settings.database_url, pool_size=n, max_overflow=0)
            engines.append(engine)
            return [PerStatementSession(engine) for _ in range(n)]
        made: list[ConcurrentSession] = []
        for _ in range(n):
            connection = await concurrency_engine.connect()
            connections.append(connection)
            session = AsyncSession(bind=connection)
            opened.append(session)
            made.append(session)
        return made

    yield factory

    for session in opened:
        await session.close()
    for connection in connections:
        await connection.close()
    for engine in engines:
        await engine.dispose()


@pytest.fixture
def barrier() -> Callable[[int], asyncio.Barrier]:
    """`barrier(n)` → an `asyncio.Barrier` n coroutines meet at."""

    def make(n: int) -> asyncio.Barrier:
        return asyncio.Barrier(n)

    return make


@pytest.fixture
def run_interleaved() -> Callable[..., Awaitable[list[Any]]]:
    """Start every coroutine and return results/exceptions in argument order."""

    async def run(*coros: Awaitable[Any]) -> list[Any]:
        return list(await asyncio.gather(*coros, return_exceptions=True))

    return run
