"""Deciding a held channel again, for a socket whose standing may have moved.

A person's socket holds chat channels and live document channels. When
something can move what it may do (a share, a role, a machine taking a folder),
each held channel is decided again by its own rules: a live document by its
type through the CRDT lane, a chat by the chat channel's rules. This is that
one decision; the socket acts on the answer (a fresh ``subscribed`` frame, or
the channel ended).
"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from alkera_core.schemas.realtime import ErrorFrame, SubscribedFrame

from backend.services.realtime.channels import (
    Channel,
    ChannelError,
    ChannelGrant,
    PresenceGrant,
    WorkspaceGrant,
    authorize,
    authorize_workspace,
)
from backend.services.realtime.filters import EntitlementSnapshot
from backend.services.sharing import SharedRungCache


class DocumentLane(Protocol):
    """What this module asks of a socket's live document lane (the CRDT
    gateway's socket), named here so realtime does not reach into it."""

    async def grant(self, channel: Channel) -> ChannelGrant: ...

    def serves(self, doc_type: object) -> bool: ...

    def has_source(self, doc_type: object) -> bool: ...


class HoldsChannels(Protocol):
    """What a person's socket offers this module."""

    channels: dict[str, PresenceGrant]
    user: User
    rungs: SharedRungCache

    @property
    def crdt(self) -> DocumentLane | None: ...

    @property
    def ref(self) -> Any: ...

    @property
    def verified_machine_id(self) -> str | None: ...

    async def send(self, frame: Any) -> None: ...

    async def _unsubscribe(self, key: str) -> None: ...

    async def _redecide(self, key: str, grant: PresenceGrant) -> None: ...


async def fresh_grant(
    grant: ChannelGrant,
    *,
    crdt: DocumentLane | None,
    user: User,
    ent: EntitlementSnapshot,
    agent_id: str | None,
    rungs: SharedRungCache,
) -> ChannelGrant | None:
    """The channel's grant as decided now, or ``None`` for a channel this
    socket does not re-decide. Raises ``ChannelError`` when it is refused."""
    lane = crdt if crdt is not None and crdt.serves(grant.channel.doc_type) else None
    if lane is not None:
        return await lane.grant(grant.channel)
    if grant.channel.doc_type != "chat":
        return None
    async with AsyncSessionLocal() as db:
        return await authorize(db, user, grant.channel, ent=ent, agent_id=agent_id, rungs=rungs)


async def redecide(sock: HoldsChannels, key: str, grant: PresenceGrant) -> None:
    """Decide one held channel again; tell the socket when its answer moved,
    and end the channel when it is refused outright. A workspace channel has
    no write question: it is either still the reader's or it ends."""
    try:
        if isinstance(grant, WorkspaceGrant):
            async with AsyncSessionLocal() as db:
                await authorize_workspace(
                    db, sock.user, grant.channel, org_id=sock.ref.value.org_id
                )
            return
        fresh = await fresh_grant(
            grant,
            crdt=sock.crdt,
            user=sock.user,
            ent=sock.ref.value,
            agent_id=sock.verified_machine_id,
            rungs=sock.rungs,
        )
    except ChannelError as exc:
        if sock.channels.get(key) is grant:
            await sock._unsubscribe(key)
            await sock.send(ErrorFrame(code=exc.code, message=exc.message, channel=key))
        return
    if fresh is None or sock.channels.get(key) is not grant:
        return
    if fresh.can_write == grant.can_write:
        return
    sock.channels[key] = grant.settled(
        owner_user_id=grant.owner_user_id, team_id=grant.team_id, can_write=fresh.can_write
    )
    await sock.send(SubscribedFrame(channel=key, can_write=fresh.can_write))


async def reconsider_documents(sock: HoldsChannels, only: str | None = None) -> None:
    """Decide the held live documents with a source again (all of them, or
    the channel ``only``). A source can stop taking writes with no event
    naming the socket (a machine took the folder) and start again; the
    socket's tick and a saving notice ask. A failure is the next tick's."""
    crdt = sock.crdt
    if crdt is None:
        return
    for key, grant in list(sock.channels.items()):
        if isinstance(grant, WorkspaceGrant):
            continue
        if only in (None, key) and crdt.has_source(grant.channel.doc_type):
            with contextlib.suppress(Exception):
                await sock._redecide(key, grant)


__all__ = ["DocumentLane", "HoldsChannels", "fresh_grant", "reconsider_documents", "redecide"]
