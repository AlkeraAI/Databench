"""The channels a box's socket holds besides its own machine channel.

A box holds the document of every chat bound to its machine, as the chat's
publisher, and follows the live document of every notebook in a folder it
holds, as a reader. Both are decided here, and re-decided on every tick:
the socket (:class:`~backend.services.realtime.session.MachineSocketSession`)
carries the answers and tells a box it lost a channel.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent

from backend.services.crdt import CRDT_UPDATE_EVENT_TYPE, relay_frames
from backend.services.notebooks import NOTEBOOK_DOC_TYPE, box_follows
from backend.services.realtime.channels import (
    Channel,
    ChannelError,
    ChannelGrant,
    PresenceGrant,
    WorkspaceChannel,
    authorize_machine_chat,
)
from backend.services.realtime.filters import MachineScopeRef, load_machine_scope


async def grant(
    runtime: Any, ctx: ActingContext, scope: MachineScopeRef, channel: Channel | WorkspaceChannel
) -> ChannelGrant:
    """The grant for the box ``ctx`` subscribing to ``channel``, or
    ``ChannelError`` (``not_found``, as a stranger is told). A granted
    channel joins ``scope`` at once, so its frames are not refused until the
    next tick re-reads it."""
    if isinstance(channel, WorkspaceChannel):
        raise ChannelError("not_found")
    if channel.doc_type == NOTEBOOK_DOC_TYPE:
        return await _notebook_grant(runtime, ctx, scope, channel)
    async with AsyncSessionLocal() as db:
        found = await authorize_machine_chat(db, ctx, channel)
    held = scope.value
    if channel.doc_id not in held.chat_ids:
        scope.value = replace(
            held,
            chat_ids=held.chat_ids | {channel.doc_id},
            org_ids=held.org_ids | {found.org_id},
        )
    return found


async def _notebook_grant(
    runtime: Any, ctx: ActingContext, scope: MachineScopeRef, channel: Channel
) -> ChannelGrant:
    """Follow a notebook's live document as the machine holding its folder
    (:func:`box_follows`). The grant reads: the box writes the notebook
    through its ops route under its lease's fence, never on its socket."""
    if getattr(runtime, "crdt", None) is None:
        raise ChannelError("crdt_unsupported", "this server runs no CRDT lane")
    org_id = await _follows(ctx, channel)
    if org_id is None:
        raise ChannelError("not_found")
    held = scope.value
    scope.value = replace(held, notebook_channels=held.notebook_channels | {channel.key})
    return ChannelGrant(
        channel=channel, org_id=org_id, can_write=False, owner_user_id=None, team_id=None
    )


async def _follows(ctx: ActingContext, channel: Channel) -> UUID | None:
    try:
        item_id = UUID(channel.doc_id)
    except ValueError:
        return None
    async with AsyncSessionLocal() as db:
        return await box_follows(db, ctx, item_id)


async def followed_frames(
    runtime: Any, channels: Mapping[str, PresenceGrant], event: HubEvent, *, own_peer: str
) -> list[Any] | None:
    """A committed update, a reload or a saving notice on a notebook the box
    follows, as a reader is sent it; ``None`` for any other event. People's
    carets stay off a box's socket: it has no presence and is told of nobody."""
    if event.type != CRDT_UPDATE_EVENT_TYPE:
        return None
    held = channels.get(event.channel or "")
    docs = getattr(runtime, "crdt", None)
    if not isinstance(held, ChannelGrant) or docs is None or event.org_id != held.org_id:
        return []
    return await relay_frames(docs, held.org_id, event, own_peer=own_peer)


async def rescope(
    ctx: ActingContext, scope: MachineScopeRef, channels: Mapping[str, PresenceGrant]
) -> list[str]:
    """Re-read the chats bound to the box and re-decide every notebook it
    follows, into ``scope``; answers the held channels it no longer holds."""
    async with AsyncSessionLocal() as db:
        fresh = await load_machine_scope(db, ctx)
    followed: set[str] = set()
    lost: list[str] = []
    for key, held in list(channels.items()):
        doc_type = held.channel.doc_type
        if doc_type == "chat" and held.channel.doc_id not in fresh.chat_ids:
            lost.append(key)
        elif doc_type == NOTEBOOK_DOC_TYPE and isinstance(held.channel, Channel):
            if await _follows(ctx, held.channel) == held.org_id:
                followed.add(key)
            else:
                lost.append(key)
    scope.value = replace(fresh, notebook_channels=frozenset(followed))
    return lost


__all__ = ["followed_frames", "grant", "rescope"]
