"""Two first saves of one settings row at once, as two requests make them.

Each save runs in a session of its own and holds its transaction until both
have written (or a second has passed). A save that serializes on its row takes
both, one after the other; one that reads, then inserts when missing, has one
of them refused as a duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

Save = Callable[[AsyncSession, OrgWithAdmin, int], Awaitable[Any]]


async def refused_saves(save: Save, org: OrgWithAdmin) -> list[BaseException]:
    """Run two first saves at once; what either raised."""
    saved = 0
    both = asyncio.Event()

    async def one(n: int) -> None:
        nonlocal saved
        async with AsyncSessionLocal() as db:
            await save(db, org, n)
            saved += 1
            if saved == 2:
                both.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both.wait(), 1.0)
            await db.commit()

    outcomes = await asyncio.wait_for(asyncio.gather(one(1), one(2), return_exceptions=True), 30)
    return [outcome for outcome in outcomes if isinstance(outcome, BaseException)]


async def rows_for(model: Any, column: Any, org: OrgWithAdmin) -> int:
    """How many ``model`` rows ``column`` ties to the org."""
    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(column == org.org_id)
                )
            ).scalar_one()
        )
