"""Ending a wake no machine took.

A send to a sleeping chat, a reader opening one, or a move stamps
``wake_requested_at`` on the chat, and the box that serves it takes the chat
on its next routing pass and says ``publishing``, which clears the stamp. When
a box that answers never takes it (its worker is wedged, the chat trips
something the box will not report), nothing cleared the stamp: the chat read
"Waking" for as long as anyone looked, the person's message sat unanswered,
and every later sleep re-took the chat for nobody.

Past ``chat_wake_deadline_seconds``, counted from the later of the wake and
the machine coming up, the compute sweep ends the wait. A chat with nothing
owed goes back to sleep. A chat with a message waiting goes back to sleep
too, and the message is marked as never run with a line saying no machine
picked it up, so the person sends it again knowing it was not run, and no box
runs it behind them later. Nothing a person wrote is removed. A machine that
is not answering is the machine's own wait, and its own bounds end it.

A box that has the chat's message but no free slot says so (``waiting``),
and the chat reads queued. A slot that never frees left the message queued
for good, so past ``chat_queue_deadline_seconds`` the same ending runs with
the reason ``no_slot``.

Run by the compute sweep. Idempotent: an ended wait no longer matches.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, DateTime, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.machines import READY, machine_state
from alkera_core.config import get_settings
from alkera_core.events import BOUND_MACHINE_KEY, Entity, EventType, emit
from alkera_core.logging import get_logger
from alkera_core.models import ComputeAllocation, RealtimeDoc, WorkspaceObject
from alkera_core.objects.chat_end import CHAT_TYPE, lock_chat
from alkera_core.objects.chat_transcript import (
    prompt_cancelled_for,
    publish_server_entries,
    unstarted_prompts,
    write_entries,
)
from alkera_core.objects.chat_turn import turn_end_note
from alkera_core.objects.instants import instant
from alkera_core.schemas.chat import PROMPT_CANCELLED_NO_SLOT, PROMPT_CANCELLED_NOT_TAKEN
from alkera_core.schemas.objects.transcript import system_note_entries

log = get_logger(__name__)

#: The reason a chat no machine took is recorded under.
NOT_TAKEN: Final = "not_taken"
#: The reason a chat queued for a slot that never freed is recorded under.
NO_SLOT: Final = "no_slot"
#: How many wakes one pass ends; the next pass takes the rest.
PAGE: Final = 200

_WAKE: Final = "wake_requested_at"
_QUEUED: Final = "slot_wait_at"

#: The reason each ending is told to the reader as, on a message never run.
_CANCELLED_AS: Final = {NOT_TAKEN: PROMPT_CANCELLED_NOT_TAKEN, NO_SLOT: PROMPT_CANCELLED_NO_SLOT}


def wake_deadline() -> timedelta:
    return timedelta(seconds=get_settings().chat_wake_deadline_seconds)


def queue_deadline() -> timedelta:
    return timedelta(seconds=get_settings().chat_queue_deadline_seconds)


async def end_untaken_wakes(db: AsyncSession, *, now: datetime | None = None) -> int:
    """End every wake a machine that answers has left untaken, and every wait
    for a slot that never freed, past its bound. Returns how many it ended.
    Commits per chat, so one chat's lock never holds the rest."""
    moment = now or datetime.now(UTC)
    due = (
        (
            await db.execute(
                select(WorkspaceObject.id)
                .where(
                    WorkspaceObject.type == CHAT_TYPE,
                    WorkspaceObject.deleted_at == 0,
                    or_(
                        _older_than(_WAKE, moment - wake_deadline()),
                        _older_than(_QUEUED, moment - queue_deadline()),
                    ),
                )
                .order_by(WorkspaceObject.id)
                .limit(PAGE)
            )
        )
        .scalars()
        .all()
    )
    await db.rollback()
    ended = 0
    for chat_id in due:
        if await _end_one(db, chat_id, moment):
            ended += 1
        await db.commit()
    return ended


async def _end_one(db: AsyncSession, chat_id: uuid.UUID, moment: datetime) -> bool:
    org_id = (
        await db.execute(select(WorkspaceObject.org_team_id).where(WorkspaceObject.id == chat_id))
    ).scalar_one_or_none()
    if org_id is None:
        return False
    # The document first, then the chat, the order every writer of a chat's
    # transcript takes them in (:func:`lock_chat`); the document is then read
    # under the lock this transaction holds.
    chat = await lock_chat(db, chat_id)
    doc = (
        await db.execute(
            select(RealtimeDoc)
            .where(
                RealtimeDoc.org_id == org_id,
                RealtimeDoc.doc_type == "chat",
                RealtimeDoc.doc_id == str(chat_id),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    reason = await _overdue(db, chat, moment) if chat is not None else None
    if chat is None or reason is None:
        return False
    spec = dict(chat.spec or {})
    spec.pop(_WAKE, None)
    spec.pop(_QUEUED, None)
    waiting = await unstarted_prompts(db, chat_id=chat.id)
    if waiting:
        spec["turn_end_reason"] = reason
        spec["turn_end_at"] = moment.isoformat()
    chat.spec = spec
    await db.flush()
    if waiting:
        entries = [
            *(
                prompt_cancelled_for(
                    row, session_id=str(chat.id), now=moment, reason=_CANCELLED_AS[reason]
                )
                for row in waiting
            ),
            # Not an aside: the line stands for the messages above it, which
            # are not to be run now that the person has been told they were not.
            *system_note_entries(
                session_id=str(chat.id),
                note_id=f"{reason.replace('_', '-')}-{waiting[-1].seq}",
                text=turn_end_note(reason),
                now=moment,
            ),
        ]
        if doc is not None:
            await publish_server_entries(db, chat=chat, doc=doc, entries=entries, actor=None)
        else:
            await write_entries(db, chat=chat, events=entries)
    await emit(
        db,
        org_id=chat.org_team_id,
        type=EventType.CHAT_UPDATED,
        entity=Entity.CHAT,
        entity_id=str(chat.id),
        version=chat.version,
        payload={
            "team_id": str(chat.team_id) if chat.team_id else None,
            BOUND_MACHINE_KEY: spec.get("machine_id"),
        },
        actor=None,
    )
    log.info("chat.wait.ended", chat_id=str(chat.id), reason=reason, messages=len(waiting))
    return True


def _older_than(key: str, bound: datetime) -> ColumnElement[bool]:
    stamp = WorkspaceObject.spec[key].astext
    return and_(
        func.pg_input_is_valid(stamp, "timestamptz"),
        cast(stamp, DateTime(timezone=True)) <= bound,
    )


async def _overdue(db: AsyncSession, chat: WorkspaceObject, moment: datetime) -> str | None:
    """Which wait of ``chat``'s has outlived its bound on a machine that
    answers (``no_slot`` or ``not_taken``), read again under the chat's lock,
    or ``None``."""
    spec = chat.spec or {}
    if spec.get("publisher_refusal"):
        # A refusal the chat already reads as; the box has named its wait.
        return None
    machine = await _machine(db, spec.get("machine_id"))
    if machine is None or machine_state(machine, now=moment) != READY:
        return None
    queued = instant(spec.get(_QUEUED))
    if queued is not None:
        # The box has the message and is waiting for a slot: its wait, not
        # the wake's, is what is measured.
        return NO_SLOT if moment - queued >= queue_deadline() else None
    asked = instant(spec.get(_WAKE))
    if asked is None:
        return None
    since = max(asked, machine.ready_at) if machine.ready_at is not None else asked
    return NOT_TAKEN if moment - since >= wake_deadline() else None


async def _machine(db: AsyncSession, machine_id: object) -> ComputeAllocation | None:
    try:
        key = uuid.UUID(str(machine_id))
    except ValueError:
        return None
    return await db.get(ComputeAllocation, key)


__all__ = ["NOT_TAKEN", "NO_SLOT", "end_untaken_wakes", "queue_deadline", "wake_deadline"]
