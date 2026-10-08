"""The one door a chat's publisher alone may open: the machine that runs the
chat reports its state, mints its gateway credential and asks what it needs to
run it, and nobody else may.
"""

from __future__ import annotations

from alkera_core.authz import ActingContext, Action, ResourceType
from alkera_core.compute.machines import current_machine
from alkera_core.models import User, WorkspaceObject
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.authz import enforce, role_resolver
from backend.services import sharing


async def enforce_publisher(
    request: Request,
    db: AsyncSession,
    ctx: ActingContext,
    chat: WorkspaceObject,
    reader: sharing.Reader,
) -> None:
    """Admit the machine that publishes ``chat`` and nobody else: an agent
    whose asserted id is the chat's bound machine or the org's current
    workspace machine (``WRITE`` on ``chat.access``). A person, or any other
    agent, is refused. One helper for every door a publisher alone may open —
    its state report and its gateway credential — so the two cannot drift."""
    # The chat's org, not the caller's: a machine principal has no user, and a
    # pool box serves chats of several orgs.
    bound = sharing.bound_machine(chat, reader)  # a personal box counts its owner's chats only
    current = await current_machine(db, org_id=chat.org_team_id)
    machines = frozenset(
        machine_id
        for machine_id in (bound, str(current.id) if current is not None else None)
        if machine_id
    )
    await enforce(
        request,
        db,
        ctx,
        Action.WRITE,
        sharing.object_resource(chat, type=ResourceType.CHAT),
        {**await sharing.chat_attrs(db, chat, reader), "publisher_machine_ids": machines},
    )


async def chat_reader(
    request: Request, db: AsyncSession, ctx: ActingContext, user: User | None
) -> sharing.Reader:
    """The caller's facts: a person's from their roles and memberships, a box's
    from nothing but its credential (``principal_user`` hands ``None`` for a
    machine and a user for everything else)."""
    if ctx.is_machine or user is None:
        return sharing.machine_reader(ctx)
    return await sharing.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )
