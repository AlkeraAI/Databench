"""Presence on a channel, and the ephemeral lane presence rides on.

A peer joins a channel, heartbeats every keepalive tick and leaves; a row
unheard from for longer than the presence TTL is gone — the roster hides it
and the sweeper deletes it. Every read of the table is scoped to one org: a
channel name carries a client-chosen document id two tenants may name, so a
read that only matched the channel would show one tenant the other's peers.
Every change is fanned out through
``pg_notify('alkera_rt', …)``: small, disposable, delivered to every replica's
listener and never replayed. The emitting replica receives its own
notification too, so local and remote sockets see identical ordering and
there is no local loopback to keep consistent.

Every function takes an explicit ``now`` for the reads that compare against
the TTL and the writes that stamp ``last_seen_at``, defaulting to the
database clock, so a test can drive the roster across the TTL boundary
without touching the database's idea of time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.config import settings
from alkera_core.events import HubEvent, ephemeral_payload
from alkera_core.events.listener import EPHEMERAL_CHANNEL
from alkera_core.logging import get_logger
from alkera_core.models import RealtimePresence, User
from alkera_core.schemas.realtime import PresenceCursor, PresenceFrame, PresencePeer
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.realtime.channels import (
    PresenceChannel,
    PresenceGrant,
    presence_grant_of_row,
)

#: Ephemeral event types on the hub (not outbox types: nothing persists them).
PRESENCE_EVENT_TYPE = "doc.presence"
#: The box publishing a chat's document came onto its channel or went from it.
PUBLISHER_EVENT_TYPE = "doc.publisher"
CHUNK_EVENT_TYPE = "doc.chunk"
EPHEMERAL_ENTITY = "doc"


class EphemeralTooLargeError(ValueError):
    """An ephemeral message over ``REALTIME_EPHEMERAL_MAX_BYTES``."""


def _ttl() -> timedelta:
    return timedelta(seconds=settings.realtime_presence_ttl_seconds)


def _now(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now


async def join(
    db: AsyncSession,
    *,
    channel: PresenceChannel,
    peer_id: str,
    user_id: UUID,
    org_id: UUID,
    now: datetime | None = None,
) -> None:
    """Upsert the (channel, peer) row; a re-join refreshes ``last_seen_at``."""
    stamp = _now(now)
    stmt = pg_insert(RealtimePresence).values(
        doc_type=channel.doc_type,
        doc_id=channel.doc_id,
        peer_id=peer_id,
        user_id=user_id,
        org_id=org_id,
        joined_at=stamp,
        last_seen_at=stamp,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["doc_type", "doc_id", "peer_id"],
            set_={"last_seen_at": stmt.excluded.last_seen_at},
        )
    )


async def heartbeat(
    db: AsyncSession, *, channel: PresenceChannel, peer_id: str, now: datetime | None = None
) -> bool:
    """Refresh ``last_seen_at``; ``False`` when the row is gone (swept) and
    the caller must re-join."""
    result = await db.execute(
        update(RealtimePresence)
        .where(
            RealtimePresence.doc_type == channel.doc_type,
            RealtimePresence.doc_id == channel.doc_id,
            RealtimePresence.peer_id == peer_id,
        )
        .values(last_seen_at=_now(now))
    )
    return bool(getattr(result, "rowcount", 0))


async def leave(db: AsyncSession, *, channel: PresenceChannel, peer_id: str) -> None:
    await db.execute(
        delete(RealtimePresence).where(
            RealtimePresence.doc_type == channel.doc_type,
            RealtimePresence.doc_id == channel.doc_id,
            RealtimePresence.peer_id == peer_id,
        )
    )


async def roster(
    db: AsyncSession, *, channel: PresenceChannel, org_id: UUID, now: datetime | None = None
) -> list[PresencePeer]:
    """Every peer of ``org_id`` heard from within the TTL, oldest join first.

    The org is not optional. A channel name carries a client-chosen document id
    that two tenants may name, and this table is deliberately not keyed to
    ``realtime_docs`` — so an unfiltered read would put another tenant's peer
    and user ids in the roster frame a subscribe is answered with."""
    cutoff = _now(now) - _ttl()
    # The name is joined rather than denormalised onto the presence row: a
    # person who changes their name should not have to re-join every chat they
    # are reading for the roster to catch up. The join is OUTER so that the
    # roster can never SHRINK because of a name: today the foreign key makes a
    # row without its user impossible, but the count of who is reading is the
    # thing a second person joining a chat is told, and losing a reader over a
    # missing name would be the worse failure.
    rows = await db.execute(
        select(RealtimePresence, User.first_name, User.last_name, User.email)
        .outerjoin(User, User.id == RealtimePresence.user_id)
        .where(
            RealtimePresence.org_id == org_id,
            RealtimePresence.doc_type == channel.doc_type,
            RealtimePresence.doc_id == channel.doc_id,
            RealtimePresence.last_seen_at > cutoff,
        )
        .order_by(RealtimePresence.joined_at, RealtimePresence.peer_id)
    )
    return [
        PresencePeer(
            peer_id=row.peer_id,
            user_id=str(row.user_id),
            last_seen_at=row.last_seen_at,
            email=(email or "")[:320],
            display_name=display_name_of(first, last, email),
        )
        for row, first, last, email in rows
    ]


async def sweep_expired(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete every row unheard from past the TTL, and tell each row's channel
    the peer left; returns how many. The caller commits, which is when the
    leaves go out.

    Whatever removes presence announces it. A peer whose process died without
    a graceful leave (a crash, an OOM, a leave the busy database refused)
    otherwise stayed on every other reader's roster, caret and all, until they
    reloaded: the roster read hides a lapsed row, but no frame ever told an
    open tab to drop it."""
    stamp = _now(now)
    cutoff = stamp - _ttl()
    rows = await db.execute(
        delete(RealtimePresence)
        .where(RealtimePresence.last_seen_at < cutoff)
        .returning(
            RealtimePresence.doc_type,
            RealtimePresence.doc_id,
            RealtimePresence.peer_id,
            RealtimePresence.user_id,
            RealtimePresence.org_id,
        )
    )
    swept = rows.all()
    for doc_type, doc_id, peer_id, user_id, org_id in swept:
        await publish_ephemeral(
            db,
            ephemeral_event(
                grant=presence_grant_of_row(doc_type, doc_id, org_id),
                type=PRESENCE_EVENT_TYPE,
                body={
                    "event": "leave",
                    "peer": presence_peer(peer_id, user_id, stamp).model_dump(mode="json"),
                },
            ),
        )
    return len(swept)


def display_name_of(first: str | None, last: str | None, email: str | None) -> str:
    """What to call somebody on a roster.

    The name they gave, or the half of their email in front of the ``@`` when
    they have not given one — a profile is completed after signup, so a chat
    opened in between must still say who is in it. Never the user id: a sliced
    UUID identifies nobody and puts a raw identifier on screen for the
    privilege. Empty means "we could not say", which a surface draws as an
    anonymous reader.
    """
    named = " ".join(part for part in (first or "", last or "") if part.strip()).strip()
    if named:
        return named[:128]
    local = (email or "").split("@", 1)[0].strip()
    return local[:128]


def presence_peer(
    peer_id: str,
    user_id: UUID,
    now: datetime | None = None,
    *,
    email: str = "",
    display_name: str = "",
    cursor: PresenceCursor | None = None,
) -> PresencePeer:
    return PresencePeer(
        peer_id=peer_id,
        user_id=str(user_id),
        last_seen_at=_now(now),
        email=email[:320],
        display_name=display_name,
        cursor=cursor,
    )


def ephemeral_event(
    *,
    grant: PresenceGrant,
    type: str,
    body: Mapping[str, Any],
) -> HubEvent:
    """The hub event an ephemeral message becomes on every replica. The
    grant's team travels in the payload so the per-frame visibility rule
    applies to it exactly as to a durable row."""
    payload: dict[str, Any] = dict(body)
    if grant.team_id is not None:
        payload["team_id"] = str(grant.team_id)
    return HubEvent(
        lane="ephemeral",
        org_id=grant.org_id,
        type=type,
        entity=EPHEMERAL_ENTITY,
        entity_id=grant.channel.key,
        version=0,
        visibility=grant.visibility,
        payload=payload,
        id=None,
        channel=grant.channel.key,
    )


def presence_frame(event: HubEvent) -> PresenceFrame | None:
    """The frame a presence event becomes on a socket, or ``None`` for one a
    newer or broken writer sent in a shape this reader cannot draw."""
    try:
        peer = PresencePeer.model_validate(event.payload.get("peer"))
        return PresenceFrame.model_validate(
            {
                "channel": str(event.channel),
                "event": event.payload.get("event"),
                "peers": [peer.model_dump(mode="json")],
            }
        )
    except ValidationError:
        get_logger(__name__).warning("realtime.ws.bad_presence", channel=event.channel)
        return None


async def publish_ephemeral(db: AsyncSession, event: HubEvent) -> None:
    """Fan ``event`` out through ``pg_notify``; the caller commits. Raises
    :class:`EphemeralTooLargeError` above the configured cap."""
    try:
        encoded = ephemeral_payload(event)
    except ValueError as exc:
        raise EphemeralTooLargeError(str(exc)) from exc
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": EPHEMERAL_CHANNEL, "payload": encoded},
    )


async def count_for_channel(db: AsyncSession, channel: PresenceChannel, org_id: UUID) -> int:
    """Rows one org holds on ``channel`` regardless of TTL (tests and
    diagnostics). Scoped like :func:`roster`, for the same reason."""
    value = (
        await db.execute(
            select(func.count())
            .select_from(RealtimePresence)
            .where(
                RealtimePresence.org_id == org_id,
                RealtimePresence.doc_type == channel.doc_type,
                RealtimePresence.doc_id == channel.doc_id,
            )
        )
    ).scalar_one()
    return int(value)


__all__ = [
    "CHUNK_EVENT_TYPE",
    "EPHEMERAL_ENTITY",
    "PRESENCE_EVENT_TYPE",
    "PUBLISHER_EVENT_TYPE",
    "EphemeralTooLargeError",
    "count_for_channel",
    "ephemeral_event",
    "heartbeat",
    "join",
    "leave",
    "presence_frame",
    "presence_peer",
    "publish_ephemeral",
    "roster",
    "sweep_expired",
]
