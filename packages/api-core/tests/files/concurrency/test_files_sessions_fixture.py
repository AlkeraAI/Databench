"""The two-session fixture really is two backends at READ COMMITTED."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from alkera_core.config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# One xdist worker for this module: the module-scoped fixtures below are built
# once per worker, so splitting the module per test would rebuild them per worker.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.xdist_group("files_sessions_fixture"),
]


@pytest.fixture(scope="module")
async def _probe_table_name() -> AsyncIterator[str]:
    """A committed table two sessions can fight over.

    Module-scoped on purpose: a `DROP TABLE` needs ACCESS EXCLUSIVE, so it can
    only run once every function-scoped session from `sessions()` has closed.
    """
    name = f"files_conc_probe_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    async with engine.connect() as connection:
        await connection.execute(text(f"create table {name} (id int primary key)"))
        await connection.commit()
    yield name
    async with engine.connect() as connection:
        await connection.execute(text(f"drop table {name}"))
        await connection.commit()
    await engine.dispose()


@pytest.fixture
async def probe_table(_probe_table_name: str) -> AsyncIterator[str]:
    """The probe table, emptied before the test opens any session on it."""
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    async with engine.connect() as connection:
        await connection.execute(text(f"delete from {_probe_table_name}"))
        await connection.commit()
    await engine.dispose()
    yield _probe_table_name


async def test_two_sessions_are_two_backends(
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    one, two = await sessions(2)

    first = (await one.execute(text("select pg_backend_pid()"))).scalar_one()
    second = (await two.execute(text("select pg_backend_pid()"))).scalar_one()

    assert first != second


async def test_the_sessions_come_out_of_the_one_shared_pool(
    concurrency_engine: AsyncEngine,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Asking for sessions borrows connections; it does not fork backends.

    A fresh engine per ask meant a TCP connect, a forked Postgres backend and an
    authentication round-trip for every session of every test in this directory,
    which is what the concurrency modules were really spending their time on. The
    two sessions are still two backends — that is the test above — and this is
    what makes them cheap.
    """
    before = concurrency_engine.pool.checkedout()

    one, two = await sessions(2)
    await one.execute(text("select 1"))
    await two.execute(text("select 1"))

    assert concurrency_engine.pool.checkedout() == before + 2


async def test_a_per_statement_session_keeps_its_own_pool(
    concurrency_engine: AsyncEngine,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Transaction pooling is proved by a pool of exactly one, not a shared one.

    The claim the shape exists for is that a statement hands its connection
    BACK, and a pool with room to spare would let a session that never released
    one satisfy
    :func:`test_a_per_statement_session_releases_its_connection_between_statements`
    anyway.
    """
    before = concurrency_engine.pool.checkedout()

    (pooled,) = await sessions(1, per_statement_connection=True)
    await pooled.execute(text("select 1"))

    assert concurrency_engine.pool.checkedout() == before


async def test_sessions_run_at_read_committed(
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    (session,) = await sessions(1)

    level = (await session.execute(text("show transaction_isolation"))).scalar_one()

    assert level == "read committed"


async def test_an_uncommitted_insert_is_invisible_until_commit(
    sessions: Callable[..., Awaitable[list[Any]]],
    probe_table: str,
) -> None:
    writer, reader = await sessions(2)

    await writer.execute(text(f"insert into {probe_table} (id) values (1)"))
    before = (await reader.execute(text(f"select count(*) from {probe_table}"))).scalar_one()
    await writer.commit()
    after = (await reader.execute(text(f"select count(*) from {probe_table}"))).scalar_one()

    assert before == 0
    assert after == 1


async def test_a_per_statement_session_holds_no_transaction_across_statements(
    sessions: Callable[..., Awaitable[list[Any]]],
    probe_table: str,
) -> None:
    """PgBouncer transaction mode: each statement is its own transaction."""
    (pooled,) = await sessions(1, per_statement_connection=True)
    (observer,) = await sessions(1)

    await pooled.execute(text(f"insert into {probe_table} (id) values (7)"))
    # No commit() on `pooled` — under transaction pooling there is nothing left
    # to commit, so the row is already visible to a different backend.
    seen = (await observer.execute(text(f"select count(*) from {probe_table}"))).scalar_one()

    assert seen == 1


async def test_a_per_statement_session_releases_its_connection_between_statements(
    sessions: Callable[..., Awaitable[list[Any]]],
    run_interleaved: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Two concurrent statements share a one-connection pool.

    A session that held its connection for its whole life would park the second
    statement on the exhausted pool until the checkout timed out.
    """
    (pooled,) = await sessions(1, per_statement_connection=True)

    async def ask() -> Any:
        return (await pooled.execute(text("select 1"))).scalar_one()

    results = await asyncio.wait_for(run_interleaved(ask(), ask()), timeout=10)

    assert results == [1, 1]


async def test_run_interleaved_returns_results_and_exceptions_in_order(
    run_interleaved: Callable[..., Awaitable[list[Any]]],
    barrier: Callable[[int], asyncio.Barrier],
) -> None:
    gate = barrier(2)

    async def winner() -> str:
        await gate.wait()
        return "ok"

    async def loser() -> str:
        await gate.wait()
        msg = "boom"
        raise RuntimeError(msg)

    results = await run_interleaved(winner(), loser())

    assert results[0] == "ok"
    assert isinstance(results[1], RuntimeError)


async def test_a_barrier_makes_both_coroutines_meet(
    run_interleaved: Callable[..., Awaitable[list[Any]]],
    barrier: Callable[[int], asyncio.Barrier],
) -> None:
    gate = barrier(2)
    arrivals: list[str] = []

    async def arrive(name: str) -> str:
        arrivals.append(f"before-{name}")
        await gate.wait()
        arrivals.append(f"after-{name}")
        return name

    results = await run_interleaved(arrive("a"), arrive("b"))

    assert results == ["a", "b"]
    # Neither coroutine may pass the gate until both have reached it.
    assert set(arrivals[:2]) == {"before-a", "before-b"}
    assert set(arrivals[2:]) == {"after-a", "after-b"}
