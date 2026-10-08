"""Opening a chat wakes it: the one wake a person's open asks for.

A slept chat used to wake only when somebody sent it a message, so a notebook
in its workspace could not run until the person typed something they did not
mean to say. Opening the chat page, a notebook tab in it, or pressing Run in
one now asks for the same wake a message gives: placement runs (a chat whose
box is gone moves to a live one), a stopped machine is started under the same
admission a message's start takes, and a chat the box put to sleep has its
wake stamped for the box's discovery.

Who may ask is the route's question, decided by the chat policy's ``SEND``:
waking a box spends the org's compute, which only someone who may run the chat
may do. A reader who may only follow the conversation opens it asleep.

An open that would change nothing (the chat is up, or was opened moments
ago) is answered by :func:`wake_standing` without a wake, so the route files a
decision only for a wake it tries.

One wake per chat per :data:`WAKE_INTENT_INTERVAL`, held on the chat's row
(``wake_intent_at``) under its write lock, so several tabs, a re-render or a
second person opening the chat moments later ask once. A refused start is
throttled the same way: the person is told once, not on every focus.

No message is recorded. A wake nobody takes ends the way any untaken wake
does, and with nothing waiting there is nothing to mark as never run. An idle
woken chat goes back to sleep by the box's ordinary idle rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from alkera_core.authz import ActingContext
from alkera_core.compute.machines import ASLEEP, STARTING
from alkera_core.models import WorkspaceObject
from alkera_core.schemas.objects import ChatWakeOutcome
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.chats import chat_service
from backend.services.chats.reads import instant
from backend.services.compute import (
    UNSERVEABLE,
    ComputeRefusedError,
    bound_machine_state,
    rebind_if_stranded,
)

#: The least time between two wakes one chat's opening asks for.
WAKE_INTENT_INTERVAL: Final = timedelta(seconds=30)

_STAMP: Final = "wake_intent_at"


class WakeRefusedError(Exception):
    """The chat's stopped machine was not started: admission refused it."""

    def __init__(self, refusal: ComputeRefusedError) -> None:
        super().__init__(refusal.message)
        self.refusal = refusal


async def wake_standing(
    db: AsyncSession, *, chat: WorkspaceObject, now: datetime | None = None
) -> ChatWakeOutcome | None:
    """What an open of ``chat`` is answered with no wake tried, or ``None``
    when a wake is due. Reads only, so the caller can answer the opens that
    change nothing (a focus moments after the last, a chat already up) without
    filing a decision for each."""
    spec = chat.spec or {}
    if _within_interval(spec.get(_STAMP), now or datetime.now(UTC)):
        return "throttled"
    machine_id = chat_service.chat_spec_of(chat).machine_id
    state = await bound_machine_state(db, org_team_id=chat.org_team_id, machine_id=machine_id)
    if state in UNSERVEABLE:
        return None
    if spec.get("mirror_state") != "asleep":
        return "awake"
    return "waking" if spec.get("wake_requested_at") else None


async def wake_on_open(
    db: AsyncSession, *, chat: WorkspaceObject, ctx: ActingContext, now: datetime | None = None
) -> ChatWakeOutcome:
    """Ask for the wake opening ``chat`` stands for, once per interval.

    The caller has decided the caller may ``SEND``. Commits. Raises
    :class:`WakeRefusedError` when the machine the chat needs was refused a
    start; the chat is left as it was, with no wake stamped on it."""
    moment = now or datetime.now(UTC)
    if not await _claim(db, chat, moment):
        await db.rollback()
        return "throttled"
    # The stamp stands before any wake is tried, so a second open arriving
    # while this one starts a machine is throttled instead of starting it too.
    await db.commit()
    chat, refused = await rebind_if_stranded(db, chat=chat, ctx=ctx, org_team_id=chat.org_team_id)
    if refused is not None:
        await db.commit()
        raise WakeRefusedError(refused)
    state = await bound_machine_state(
        db, org_team_id=chat.org_team_id, machine_id=chat_service.chat_spec_of(chat).machine_id
    )
    woke = await chat_service.request_wake(db, chat=chat)
    if woke:
        await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    await db.commit()
    return "waking" if woke or state in {ASLEEP, STARTING} else "awake"


async def _claim(db: AsyncSession, chat: WorkspaceObject, moment: datetime) -> bool:
    """Stamp this open on the chat unless one inside the interval stands."""
    locked = await chat_service.lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    if _within_interval(locked.spec.get(_STAMP), moment):
        return False
    spec = dict(locked.spec)
    spec[_STAMP] = moment.isoformat()
    locked.spec = spec
    await db.flush()
    return True


def _within_interval(stamp: object, moment: datetime) -> bool:
    last = instant(stamp) if isinstance(stamp, str) else None
    return last is not None and timedelta(0) <= moment - last < WAKE_INTENT_INTERVAL


__all__ = ["WAKE_INTENT_INTERVAL", "WakeRefusedError", "wake_on_open", "wake_standing"]
