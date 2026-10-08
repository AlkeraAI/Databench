"""Resolve the org an event belongs to.

Outbox rows are keyed by the ORG root team, not by whichever team the mutation
happened on: subscriptions are per user and a user belongs to one org, so the
row's ``org_id`` is what a reader filters on and the team lives in the payload.
Producers that only know a team id (a connection, a pool, a membership) resolve
the root here. It lives in api-core rather than the backend's ``team_service``
because the worker emits too and has no ``backend`` package.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from alkera_core.models.team import Team

#: Hard bound on the walk. It is recursive SQL, so a malformed row (a
#: ``parent_team_id`` cycle) costs a bounded number of rows and a truncated
#: answer instead of an unbounded loop inside the event loop.
_MAX_WALK_DEPTH = 32


async def org_root_for_team(session: AsyncSession, team_id: UUID) -> UUID:
    """The root team ``team_id`` hangs under.

    ``team_id`` itself when it is a root, when it does not resolve, or when the
    chain is malformed and cannot be walked further. Stops at the first repeated
    id so a cycle yields the last distinct ancestor rather than spinning.
    """
    anchor = (
        select(
            Team.id.label("id"),
            Team.parent_team_id.label("parent_team_id"),
            literal(0).label("depth"),
        )
        .where(Team.id == team_id)
        .cte("event_scope_ancestors", recursive=True)
    )
    parent = aliased(Team)
    walk = anchor.union_all(
        select(parent.id, parent.parent_team_id, anchor.c.depth + 1).where(
            parent.id == anchor.c.parent_team_id,
            anchor.c.depth < _MAX_WALK_DEPTH,
        )
    )
    ids = (await session.execute(select(walk.c.id).order_by(walk.c.depth))).scalars().all()
    root = team_id
    seen: set[UUID] = set()
    for candidate in ids:
        if candidate in seen:
            break
        seen.add(candidate)
        root = candidate
    return root


__all__ = ["org_root_for_team"]
