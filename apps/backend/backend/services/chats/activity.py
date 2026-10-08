"""What a listing says about each chat's transcript: whether it owes a turn,
and when anything was last written to it. One read for a whole page."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from alkera_core.models import ChatMessage, RealtimeDoc, WorkspaceObject
from alkera_core.objects.instants import instant
from alkera_core.schemas.objects import PROMPT_KIND
from alkera_core.schemas.objects.transcript import ASIDE_NOTE_PREFIX
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ChatActivity:
    """What a listing says about a chat's transcript: whether it owes a turn,
    and when anything was last written to it."""

    pending_turn: bool
    last_activity_at: datetime | None
    #: Whether the chat's document says a turn is running, and when its
    #: worker last renewed that word (``None`` from a box that stamps none).
    turn_working: bool = False
    turn_stamped_at: datetime | None = None
    #: When the last message was sent, and whether nothing has answered it.
    prompt_at: datetime | None = None
    prompt_owed: bool = False


async def chat_activity(db: AsyncSession, chat_ids: Sequence[UUID]) -> dict[UUID, ChatActivity]:
    """One plain read for a whole page: the transcript's last question, last
    answer and last write per chat, and whether its document still says a
    turn is working. A question above the last answer is a message nothing
    took up (the rule :func:`answers_a_waiting_message` states once); a
    working document with no process behind it is an interrupted turn.

    A snapshot read: it takes no row lock and so never waits on, or holds up,
    a writer appending to a transcript or stamping a document."""
    if not chat_ids:
        return {}
    transcript = (
        select(
            ChatMessage.chat_id.label("chat_id"),
            func.max(ChatMessage.seq)
            .filter(ChatMessage.role == "user", ChatMessage.kind == PROMPT_KIND)
            .label("asked"),
            func.max(ChatMessage.seq)
            .filter(
                ChatMessage.role != "user",
                ChatMessage.event_id.not_like(f"{ASIDE_NOTE_PREFIX}%"),
            )
            .label("answered"),
            func.max(ChatMessage.created_at).label("last_at"),
            func.max(ChatMessage.created_at)
            .filter(ChatMessage.role == "user", ChatMessage.kind == PROMPT_KIND)
            .label("asked_at"),
        )
        .where(ChatMessage.chat_id.in_(chat_ids))
        .group_by(ChatMessage.chat_id)
        .subquery()
    )
    working = (
        select(
            RealtimeDoc.doc_id.label("doc_id"),
            RealtimeDoc.turn_state_at.label("stamped_at"),
        )
        .where(
            RealtimeDoc.doc_type == "chat",
            RealtimeDoc.doc_id.in_([str(chat_id) for chat_id in chat_ids]),
            # The live column first; a document stamped before the column
            # existed carries the word in its meta.
            (RealtimeDoc.turn_state == "working")
            | (
                RealtimeDoc.turn_state.is_(None)
                & (RealtimeDoc.state["meta"]["turn_state"]["state"].astext == "working")
            ),
        )
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                WorkspaceObject.id,
                transcript.c.asked,
                transcript.c.answered,
                transcript.c.last_at,
                working.c.doc_id.is_not(None),
                working.c.stamped_at,
                transcript.c.asked_at,
            )
            .select_from(WorkspaceObject)
            .outerjoin(transcript, transcript.c.chat_id == WorkspaceObject.id)
            .outerjoin(working, working.c.doc_id == func.cast(WorkspaceObject.id, String))
            .where(WorkspaceObject.id.in_(chat_ids))
        )
    ).all()
    out: dict[UUID, ChatActivity] = {}
    for chat_id, asked, answered, last_at, is_working, stamped_at, asked_at in rows:
        waiting = asked is not None and (answered is None or asked > answered)
        out[chat_id] = ChatActivity(
            pending_turn=bool(waiting or is_working),
            last_activity_at=last_at,
            turn_working=bool(is_working),
            turn_stamped_at=instant(stamped_at),
            prompt_at=asked_at,
            prompt_owed=bool(waiting),
        )
    return out


__all__ = ["ChatActivity", "chat_activity"]
