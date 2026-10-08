"""Watching one backend wait on another's lock, for the two-task lock tests.

A lock-order test has to stop one transaction exactly where it is queued on a
lock the other holds, and only then let the other take its next lock. Polling
``pg_blocking_pids`` is how a test knows the first one got there, without a
sleep that is either too short on a loaded box or wastes time on a quiet one.
"""

from __future__ import annotations

import asyncio

from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def backend_pid(session: AsyncSession) -> int:
    """The Postgres backend ``session``'s connection runs on."""
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def until_waiting_on(pid: int) -> None:
    """Return once another backend is queued on a lock ``pid`` holds; raise
    ``TimeoutError`` if none is within ten seconds."""
    async with asyncio.timeout(10), AsyncSessionLocal() as probe:
        while True:
            waiting = (
                await probe.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :pid = ANY(pg_blocking_pids(pid))"
                    ),
                    {"pid": pid},
                )
            ).scalar_one()
            await probe.rollback()
            if waiting:
                return
            await asyncio.sleep(0.02)
