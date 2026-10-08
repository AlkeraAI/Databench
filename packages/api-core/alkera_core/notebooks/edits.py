"""Who edited which cell, and when last (``notebook_edits``).

One row per cell and actor, upserted on every edit: the peer route asks it
whether someone else edited a cell lately (a ``concurrent_edit`` notice), and
the activity digest reads what changed since a moment.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.notebooks.models import NotebookEdit

ActorKind = Literal["person", "agent", "system"]

#: How long another actor's edit to a cell counts as concurrent with yours.
CONCURRENT_EDIT_SECONDS: Final = 15.0


def actor_key(
    *, user_id: uuid.UUID | None = None, agent_id: str | None = None, machine_id: str | None = None
) -> str:
    """The key an actor's edits are filed under: an agent before the person it
    acts for (an agent's edits are its own), a machine before nobody."""
    if agent_id:
        return f"agent:{agent_id}"
    if user_id is not None:
        return f"user:{user_id}"
    if machine_id:
        return f"machine:{machine_id}"
    raise ValueError("an edit is made by a person, an agent or a machine")


async def record_edits(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    cell_ids: Iterable[str],
    actor_key: str,
    actor_kind: ActorKind,
    user_id: uuid.UUID | None,
    agent_id: str | None,
    actor_display: str,
    submit_id: str | None,
    at: datetime,
) -> None:
    """Record that ``actor_key`` edited ``cell_ids`` at ``at``: one statement,
    a new row per cell it never edited, ``last_at`` moved and the count raised
    on the rest. A ``last_at`` never moves back (an edit recorded late)."""
    cells = sorted(set(cell_ids))
    if not cells:
        return
    rows = [
        {
            "org_id": org_id,
            "item_id": item_id,
            "cell_id": cell,
            "actor_key": actor_key,
            "actor_kind": actor_kind,
            "user_id": user_id,
            "agent_id": agent_id,
            "actor_display": actor_display[:255],
            "first_at": at,
            "last_at": at,
            "edits": 1,
            "last_submit_id": submit_id,
        }
        for cell in cells
    ]
    statement = pg_insert(NotebookEdit).values(rows)
    excluded = statement.excluded
    await db.execute(
        statement.on_conflict_do_update(
            constraint="uq_notebook_edits_cell_actor",
            set_={
                "last_at": func.greatest(NotebookEdit.last_at, excluded.last_at),
                "first_at": func.least(NotebookEdit.first_at, excluded.first_at),
                "edits": NotebookEdit.edits + 1,
                "actor_display": excluded.actor_display,
                "last_submit_id": excluded.last_submit_id,
            },
        )
    )


async def recent_editors(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    cell_ids: Iterable[str],
    since: datetime,
    exclude_actor_key: str | None = None,
) -> dict[str, list[NotebookEdit]]:
    """``cell id -> the other actors who edited it at or after since``, most
    recent first. Cells nobody else edited are absent."""
    cells = sorted(set(cell_ids))
    if not cells:
        return {}
    query = (
        select(NotebookEdit)
        .where(
            NotebookEdit.org_id == org_id,
            NotebookEdit.item_id == item_id,
            NotebookEdit.cell_id.in_(cells),
            NotebookEdit.last_at >= since,
        )
        .order_by(NotebookEdit.cell_id, NotebookEdit.last_at.desc())
    )
    if exclude_actor_key is not None:
        query = query.where(NotebookEdit.actor_key != exclude_actor_key)
    found: dict[str, list[NotebookEdit]] = {}
    for row in (await db.execute(query)).scalars():
        found.setdefault(row.cell_id, []).append(row)
    return found


async def concurrent_editors(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    cell_ids: Iterable[str],
    actor_key: str,
    now: datetime | None = None,
) -> dict[str, list[NotebookEdit]]:
    """The other actors who edited each of ``cell_ids`` within
    :data:`CONCURRENT_EDIT_SECONDS` of ``now`` (the wall clock by default):
    what a ``concurrent_edit`` notice names."""
    since = (now or datetime.now(UTC)) - timedelta(seconds=CONCURRENT_EDIT_SECONDS)
    return await recent_editors(
        db,
        org_id=org_id,
        item_id=item_id,
        cell_ids=cell_ids,
        since=since,
        exclude_actor_key=actor_key,
    )


async def edits_since(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    since: datetime,
    exclude_actor_key: str | None = None,
    limit: int = 500,
) -> list[NotebookEdit]:
    """Every cell and actor whose last edit is at or after ``since``, oldest
    first: the activity digest's edit half."""
    query = (
        select(NotebookEdit)
        .where(
            NotebookEdit.org_id == org_id,
            NotebookEdit.item_id == item_id,
            NotebookEdit.last_at >= since,
        )
        .order_by(NotebookEdit.last_at, NotebookEdit.id)
        .limit(limit)
    )
    if exclude_actor_key is not None:
        query = query.where(NotebookEdit.actor_key != exclude_actor_key)
    return list((await db.execute(query)).scalars())


__all__ = [
    "CONCURRENT_EDIT_SECONDS",
    "ActorKind",
    "actor_key",
    "concurrent_editors",
    "edits_since",
    "recent_editors",
    "record_edits",
]
