"""The routing feed: which chats are bound to a box's machine, in which org,
and in what state, for the box's root process.

The root process of a box that serves several orgs routes each chat to that
org's own process and holds nothing of any tenant's, so what it reads is ids
and states, never a title or a message. Each row is admitted by the same
decision the chat's own route makes (the machine holds the chat, inside the
engine's tenancy floor), exactly as the chat listing admits it, so the feed
names no chat the listing would not.
"""

from __future__ import annotations

from collections.abc import Sequence

from alkera_core.authz.principal import ActingContext
from alkera_core.models import WorkspaceObject
from alkera_core.objects import chat_spares
from alkera_core.objects.chat_session_state import chat_session_state
from alkera_core.schemas.machine_routing import MachineRoutingEntry, MachineRoutingRead
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.chats import chat_service
from backend.services.chats.reads import machine_status
from backend.services.compute import live_machines
from backend.services.objects import list_chats_on_machine
from backend.services.sharing import machine_reader, readable_chat_facts
from backend.utils.ids import uuid_or_none


async def routing_page(
    db: AsyncSession, ctx: ActingContext, *, limit: int, cursor: str | None
) -> MachineRoutingRead:
    """One page of the chats bound to ``ctx``'s machine, newest first, keyset
    paged with the chat listing's cursor (bound to this machine's listing)."""
    reader = machine_reader(ctx)

    async def _routable(rows: list[WorkspaceObject]) -> list[WorkspaceObject]:
        # A spare is no one's chat until its owner's first send claims it, so
        # nothing may start an agent for it; cut before paging so no cursor
        # names one.
        return [
            chat
            for chat, _facts in await readable_chat_facts(db, rows, reader)
            if not chat_spares.is_spare(chat)
        ]

    page = await list_chats_on_machine(
        db, machine_id=ctx.acting_principal.id, limit=limit, cursor=cursor, cut=_routable
    )
    return MachineRoutingRead(items=await _entries(db, page.items), next_cursor=page.next_cursor)


async def _entries(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> list[MachineRoutingEntry]:
    orgs = {chat.org_team_id for chat in chats}
    machines_of = {org_id: await live_machines(db, org_id=org_id) for org_id in orgs}
    activity = await chat_service.chat_activity(db, [chat.id for chat in chats])
    entries = []
    for chat in chats:
        spec = chat_service.chat_spec_of(chat)
        status = machine_status(spec, machines_of[chat.org_team_id])
        owed = activity.get(chat.id)
        pending = bool(owed and owed.pending_turn)
        entries.append(
            MachineRoutingEntry(
                chat_id=chat.id,
                org_id=chat.org_team_id,
                workspace_id=uuid_or_none(spec.workspace_id),
                state=chat_session_state(spec, status, pending),
                pending_turn=pending,
                wake_requested=bool(spec.wake_requested_at),
                end_seq=spec.end_seq,
            )
        )
    return entries


__all__ = ["routing_page"]
