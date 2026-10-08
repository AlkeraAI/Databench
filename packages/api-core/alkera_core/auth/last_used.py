"""Stamping a credential's ``last_used_at`` without making requests queue on it.

Every token shape the backend and the model gateway resolve (machine
credential, CI token, proxy token, personal access token) records when it was
last used. Writing that on
every request makes the credential's row the one row every request of that
caller updates and commits, so a busy caller's requests line up behind each
other's row lock: under a slow ``COMMIT`` a box's heartbeats waited seconds on
its own credential and timed out, and the box read as unreachable while alive.

The stamp is a usage trace, not an access decision, so it gives up both of the
properties that cost the queue:

* **Resolution.** The row is written at most once per
  :data:`LAST_USED_RESOLUTION`; a request inside that window writes nothing,
  which also spares the commit its WAL flush.
* **No waiting.** The row is claimed with ``FOR UPDATE SKIP LOCKED``. When
  another request is stamping it right now, this one skips the write instead of
  waiting for that request's commit: the other request is recording the same
  moment to within the resolution.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

#: How stale ``last_used_at`` may grow before a request writes it again.
LAST_USED_RESOLUTION = timedelta(seconds=60)


async def stamp_last_used(
    session: AsyncSession,
    model: Any,
    row_id: uuid.UUID,
    now: datetime,
    *,
    resolution: timedelta = LAST_USED_RESOLUTION,
) -> bool:
    """Set ``model.last_used_at = now`` on ``row_id`` when it is due, never waiting.

    ``model`` is a mapped class with ``id`` and ``last_used_at`` columns.
    Returns whether this call wrote the row. The caller commits.
    """
    due = (
        select(model.id)
        .where(
            model.id == row_id,
            or_(model.last_used_at.is_(None), model.last_used_at < now - resolution),
        )
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    result = await session.execute(
        update(model)
        .where(model.id == due)
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(getattr(result, "rowcount", 0))


__all__ = ["LAST_USED_RESOLUTION", "stamp_last_used"]
