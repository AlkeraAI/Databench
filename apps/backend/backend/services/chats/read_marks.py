"""Per-person read state of a chat: unread, needs you, and the marks behind them.

Each person keeps one mark per chat (:class:`~alkera_core.models.ChatReadMark`),
the transcript sequence of the last event they have seen. A chat is unread for
a person when the transcript holds activity after their mark that they did not
author (the agent ended a turn, or someone else sent a message), or when they
marked it unread themselves. Events streamed inside a turn are not activity, so
a chat is not unread while its turn is still running, and a person's own
messages never make a chat unread for them.

A chat needs a person when the agent is waiting on an ask in it and that
person may answer it. Who may answer is the chat policy's ``SEND``, the gate the
answer route decides on, so the listing passes the ``can_send`` it already
decided for the row; nothing here re-derives it.

Only a person has read state. A box on its own credential and an agent speaking
on its operator's session read every chat as neither unread nor waiting on
them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from alkera_core.authz.principal import ActingContext
from alkera_core.models import ChatMessage, ChatReadMark, WorkspaceObject
from alkera_core.schemas.objects import PROMPT_KIND
from alkera_core.schemas.objects.transcript import RESOLUTION_KINDS
from sqlalchemy import (
    ColumnElement,
    and_,
    exists,
    false,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.services.chats import chat_service

#: The transcript kind the agent ends a turn with.
TURN_FINISHED_KIND = "turn.finished"


@dataclass(frozen=True)
class ReadState:
    """What a chat row says to the person reading it."""

    unread: bool = False
    needs_you: bool = False


NO_READ_STATE = ReadState()


def reader_id(ctx: ActingContext) -> UUID | None:
    """The person whose read state a request reads or moves, or ``None`` when
    no person is reading: a box on its credential, or an agent on its
    operator's session (the box reading the chat it runs, not the operator)."""
    if ctx.is_machine or ctx.is_agent:
        return None
    return ctx.effective_user_id


def _mark_join(user_id: UUID) -> ColumnElement[bool]:
    return and_(ChatReadMark.chat_id == WorkspaceObject.id, ChatReadMark.user_id == user_id)


def _unread(user_id: UUID) -> ColumnElement[bool]:
    """The unread rule over ``WorkspaceObject`` outer-joined to the reader's
    mark: marked unread, or activity after the mark the reader did not author."""
    seen = func.coalesce(ChatReadMark.last_read_seq, 0)
    activity = exists().where(
        ChatMessage.chat_id == WorkspaceObject.id,
        ChatMessage.seq > seen,
        or_(
            ChatMessage.kind == TURN_FINISHED_KIND,
            and_(
                ChatMessage.role == "user",
                ChatMessage.kind == PROMPT_KIND,
                func.coalesce(ChatMessage.payload["user_id"].astext, "") != str(user_id),
            ),
        ),
    )
    return or_(func.coalesce(ChatReadMark.marked_unread, false()), activity)


async def unread_chats(db: AsyncSession, *, user_id: UUID, chat_ids: Sequence[UUID]) -> set[UUID]:
    """Which of these chats are unread for this person. One statement for a
    whole page."""
    if not chat_ids:
        return set()
    rows = await db.execute(
        select(WorkspaceObject.id)
        .outerjoin(ChatReadMark, _mark_join(user_id))
        .where(WorkspaceObject.id.in_(chat_ids), _unread(user_id))
    )
    return set(rows.scalars().all())


async def waiting_on_answer(db: AsyncSession, chat_ids: Sequence[UUID]) -> set[UUID]:
    """Which of these chats have an ask the agent is still waiting on: raised,
    not resolved under the same ``request_id``, and not left behind by a turn
    that has since ended. One statement for a whole page."""
    if not chat_ids:
        return set()
    ask = aliased(ChatMessage)
    settled = aliased(ChatMessage)
    ended = aliased(ChatMessage)
    asked_id = ask.payload["payload"]["request_id"].astext
    rows = await db.execute(
        select(ask.chat_id)
        .where(
            ask.chat_id.in_(chat_ids),
            ask.kind.in_(sorted(chat_service.ASK_KINDS)),
            ~exists().where(
                settled.chat_id == ask.chat_id,
                settled.kind.in_(sorted(RESOLUTION_KINDS)),
                settled.payload["payload"]["request_id"].astext == asked_id,
            ),
            ~exists().where(
                ended.chat_id == ask.chat_id,
                ended.kind == TURN_FINISHED_KIND,
                ended.seq > ask.seq,
            ),
        )
        .distinct()
    )
    return set(rows.scalars().all())


async def read_states(
    db: AsyncSession, ctx: ActingContext, page: Sequence[tuple[UUID, bool]]
) -> dict[UUID, ReadState]:
    """Each chat's read state for the person reading, keyed by chat id.

    ``page`` pairs each chat with whether this caller may answer its asks
    (the policy's ``SEND``, as the listing decided it). Every chat is absent
    from the map for a caller with no read state."""
    user_id = reader_id(ctx)
    if user_id is None or not page:
        return {}
    ids = [chat_id for chat_id, _ in page]
    unread = await unread_chats(db, user_id=user_id, chat_ids=ids)
    answerable = [chat_id for chat_id, may_answer in page if may_answer]
    waiting = await waiting_on_answer(db, answerable)
    return {
        chat_id: ReadState(unread=chat_id in unread, needs_you=chat_id in waiting)
        for chat_id in ids
    }


async def workspace_unread_counts(
    db: AsyncSession, ctx: ActingContext, workspace_ids: Sequence[UUID]
) -> dict[UUID, int]:
    """How many live, claimed chats in each workspace are unread for the person
    reading. Counted over the workspace's chats the way its ``chat_count`` is,
    in one grouped statement for the page."""
    user_id = reader_id(ctx)
    if user_id is None or not workspace_ids:
        return {}
    workspace_key = WorkspaceObject.spec["workspace_id"].astext
    rows = await db.execute(
        select(workspace_key, func.count())
        .select_from(WorkspaceObject)
        .outerjoin(ChatReadMark, _mark_join(user_id))
        .where(
            WorkspaceObject.type == "chat",
            WorkspaceObject.deleted_at == 0,
            func.coalesce(WorkspaceObject.spec["spare"].astext, "false") != "true",
            workspace_key.in_([str(key) for key in workspace_ids]),
            _unread(user_id),
        )
        .group_by(workspace_key)
    )
    counts: dict[UUID, int] = {}
    for key, count in rows.all():
        counts[UUID(str(key))] = int(count)
    return counts


def _last_seq(chat: WorkspaceObject) -> int:
    return chat_service.chat_spec_of(chat).last_seq


async def mark_read(db: AsyncSession, *, user_id: UUID, chat: WorkspaceObject, seq: int) -> None:
    """Move this person's mark on ``chat`` forward to ``seq`` and clear their
    "Mark as unread". Never backward: a lower ``seq`` (a late request from an
    older page) leaves the mark where it is. Capped at the chat's last
    sequence, so no request marks read what has not been written yet."""
    await _upsert_marks(db, user_id=user_id, marks=[(chat, min(seq, _last_seq(chat)))])


async def mark_all_read(
    db: AsyncSession, *, user_id: UUID, chats: Iterable[WorkspaceObject]
) -> None:
    """Mark every one of ``chats`` read up to its last sequence."""
    await _upsert_marks(db, user_id=user_id, marks=[(chat, _last_seq(chat)) for chat in chats])


async def _upsert_marks(
    db: AsyncSession, *, user_id: UUID, marks: Sequence[tuple[WorkspaceObject, int]]
) -> None:
    if not marks:
        return
    insert = pg_insert(ChatReadMark).values(
        [
            {
                "chat_id": chat.id,
                "user_id": user_id,
                "org_team_id": chat.org_team_id,
                "last_read_seq": max(seq, 0),
                "marked_unread": False,
            }
            for chat, seq in marks
        ]
    )
    await db.execute(
        insert.on_conflict_do_update(
            index_elements=[ChatReadMark.chat_id, ChatReadMark.user_id],
            set_={
                "last_read_seq": func.greatest(
                    ChatReadMark.last_read_seq, insert.excluded.last_read_seq
                ),
                "marked_unread": False,
                "read_at": func.now(),
            },
            where=or_(
                ChatReadMark.last_read_seq < insert.excluded.last_read_seq,
                ChatReadMark.marked_unread,
            ),
        )
    )


async def mark_unread(db: AsyncSession, *, user_id: UUID, chat: WorkspaceObject) -> None:
    """Set this person's "Mark as unread" on ``chat``. Their mark stays where
    it is, so reading the chat again clears it."""
    insert = pg_insert(ChatReadMark).values(
        chat_id=chat.id,
        user_id=user_id,
        org_team_id=chat.org_team_id,
        last_read_seq=0,
        marked_unread=True,
    )
    await db.execute(
        insert.on_conflict_do_update(
            index_elements=[ChatReadMark.chat_id, ChatReadMark.user_id],
            set_={"marked_unread": True, "read_at": func.now()},
        )
    )


__all__ = [
    "NO_READ_STATE",
    "TURN_FINISHED_KIND",
    "ReadState",
    "mark_all_read",
    "mark_read",
    "mark_unread",
    "read_states",
    "reader_id",
    "unread_chats",
    "waiting_on_answer",
    "workspace_unread_counts",
]
