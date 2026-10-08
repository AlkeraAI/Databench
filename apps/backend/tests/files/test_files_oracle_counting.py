"""The statement counter behind the Files cost pins counts the request, not the process.

The backend suite runs every test of a worker on one session-scoped event loop
against one process-wide engine, so work another test left running -- a
debounced announcement, an email dispatch, a runtime sweeper -- executes on the
same engine while a later test measures a request. A counter listening to the
whole engine charged that work to whichever request was open at the time, and
a long request (a two-thousand-entry tree report takes seconds on a loaded
runner) was open long enough to catch one: "one entry cost 52 statements and
two thousand cost 53". These tests pin that the counter records what the
measured code path runs, including a second connection and a task it spawns,
and nothing that was already running beside it.
"""

from __future__ import annotations

import asyncio

import pytest
from _oracle import counting
from alkera_core.db.session import AsyncSessionLocal, engine
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

SYNC_ENGINE = engine.sync_engine


async def _run(marker: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text(f"SELECT '{marker}'"))


def _markers(statements: list[str]) -> list[str]:
    return sorted(s.split("'")[1] for s in statements if s.startswith("SELECT '"))


async def test_work_already_running_beside_the_measurement_is_not_counted() -> None:
    go = asyncio.Event()

    async def bystander() -> None:
        await go.wait()
        await _run("bystander")

    # Started before the measurement, like a task an earlier test left behind.
    task = asyncio.create_task(bystander())
    try:
        with counting(SYNC_ENGINE) as seen:
            go.set()
            await task
            await _run("measured")
    finally:
        await task
    assert _markers(seen) == ["measured"]


async def test_every_connection_and_task_the_measured_path_opens_is_counted() -> None:
    """The filter must not narrow to one connection: a request reserves inos on
    a side session and may spawn work of its own, and both are its cost."""
    with counting(SYNC_ENGINE) as seen:
        await _run("first-connection")
        async with AsyncSessionLocal() as a, AsyncSessionLocal() as b:
            await a.execute(text("SELECT 'session-a'"))
            await b.execute(text("SELECT 'session-b'"))
        await asyncio.create_task(_run("spawned"))
    assert _markers(seen) == ["first-connection", "session-a", "session-b", "spawned"]


async def test_two_overlapping_measurements_each_count_only_their_own() -> None:
    async def measure(marker: str, gate: asyncio.Event, release: asyncio.Event) -> list[str]:
        with counting(SYNC_ENGINE) as seen:
            gate.set()
            await release.wait()
            await _run(marker)
        return _markers(seen)

    a_in, b_in, both = asyncio.Event(), asyncio.Event(), asyncio.Event()
    a = asyncio.create_task(measure("a", a_in, both))
    b = asyncio.create_task(measure("b", b_in, both))
    await a_in.wait()
    await b_in.wait()
    both.set()
    assert await a == ["a"]
    assert await b == ["b"]
