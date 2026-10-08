"""Ending a running turn whose worker stopped reporting on it.

A box renews a running turn's stamp every 15 seconds. When a worker dies under
a supervisor that keeps beating, nothing ends the turn: the machine reads
ready, no ending fires, and the chat said "Working…" for as long as anyone
looked. Its status reads "stalled" once the stamp is
``chat_turn_silence_seconds`` old; past ``chat_turn_abandon_seconds`` this ends
it the way any server ending does (:func:`chat_turn.end_turn_nobody_runs`),
and records why, so the chat reads "Stopped because the agent stopped
reporting progress." until a message is sent again. A box that comes back and
stamps again takes the turn back: the box's word is the one the document
keeps.

Run by the compute sweep. Idempotent: an ended turn no longer matches.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import DateTime, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import get_settings
from alkera_core.events import BOUND_MACHINE_KEY, Entity, EventType, emit
from alkera_core.logging import get_logger
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.objects import chat_turn
from alkera_core.objects.chat_end import lock_chat
from alkera_core.objects.instants import instant

log = get_logger(__name__)

#: The reason a turn ended this way is recorded under.
TURN_LOST: Final = "turn_lost"
#: How many turns one pass ends; the next pass takes the rest.
PAGE: Final = 200


def abandon_after() -> timedelta:
    return timedelta(seconds=get_settings().chat_turn_abandon_seconds)


async def end_abandoned_turns(db: AsyncSession, *, now: datetime | None = None) -> int:
    """End every running turn whose stamp is older than the bound. Returns how
    many it ended. Commits per chat, so one chat's lock never holds the rest."""
    moment = now or datetime.now(UTC)
    stamped = cast(RealtimeDoc.turn_state_at, DateTime(timezone=True))
    due = (
        (
            await db.execute(
                select(RealtimeDoc.doc_id)
                .where(
                    RealtimeDoc.doc_type == "chat",
                    RealtimeDoc.turn_state == "working",
                    func.pg_input_is_valid(RealtimeDoc.turn_state_at, "timestamptz"),
                    stamped <= moment - abandon_after(),
                )
                .order_by(stamped)
                .limit(PAGE)
            )
        )
        .scalars()
        .all()
    )
    await db.rollback()
    ended = 0
    for doc_id in due:
        try:
            chat_id = uuid.UUID(doc_id)
        except ValueError:
            continue
        if await _end_one(db, chat_id, moment):
            ended += 1
        await db.commit()
    return ended


async def _end_one(db: AsyncSession, chat_id: uuid.UUID, moment: datetime) -> bool:
    # The chat first, then its document: the order every writer of a chat takes.
    chat = await lock_chat(db, chat_id)
    if chat is None:
        return False
    doc = await db.get(RealtimeDoc, (chat.org_team_id, "chat", str(chat_id)))
    stamp = instant(doc.turn_state_at) if doc is not None else None
    if stamp is None or stamp > moment - abandon_after():
        # Stamped again since the read: the box is back and owns its turn.
        return False
    if not await chat_turn.end_turn_for(db, chat, reason=TURN_LOST, actor=None):
        return False
    await emit(
        db,
        org_id=chat.org_team_id,
        type=EventType.CHAT_UPDATED,
        entity=Entity.CHAT,
        entity_id=str(chat.id),
        version=chat.version,
        payload={
            "team_id": str(chat.team_id) if chat.team_id else None,
            BOUND_MACHINE_KEY: (chat.spec or {}).get("machine_id"),
        },
        actor=None,
    )
    log.info("chat.turn.abandoned", chat_id=str(chat_id), stamped_at=stamp.isoformat())
    return True


__all__ = ["TURN_LOST", "abandon_after", "end_abandoned_turns"]
