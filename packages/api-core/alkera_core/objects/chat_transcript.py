"""A chat's transcript as the server writes it: the rows, and the live window.

A chat has two copies of what was said. ``chat_messages`` is the system of
record: one row per entry, with a dense per-chat sequence. The chat's realtime
document holds a window over its newest entries, which is what a reader opens
on and what every frame folds into. Both the API and the worker write to it:
a box publishes through the API's socket, and a turn the server ends (a
machine stopped for credit, a box that left, a worker that went silent) is
ended by whichever process noticed. So the rules for both copies live here,
once, below every caller.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import settings
from alkera_core.events import Entity, EventType, emit
from alkera_core.events.outbox import MAX_PAYLOAD_BYTES, payload_size
from alkera_core.models import ChatMessage, RealtimeDoc, WorkspaceObject
from alkera_core.schemas.chat import PROMPT_CANCELLED_STOPPED, PromptCancelled
from alkera_core.schemas.objects import PROMPT_KIND, ChatPromptRecord
from alkera_core.schemas.objects.transcript import (
    ASIDE_NOTE_PREFIX,
    ChatTranscriptEntry,
    prompt_cancelled_entry,
    prompt_cancelled_event_id,
    prompt_entry_id,
)
from alkera_core.schemas.realtime import SERVER_PEER_ID, DocEnvelope, OpPayload


class ChatMessageTooLargeError(ValueError):
    """One transcript entry is larger than the row (and the outbox) accept."""


async def next_seq(db: AsyncSession, chat_id: UUID) -> int:
    highest = (
        await db.execute(select(func.max(ChatMessage.seq)).where(ChatMessage.chat_id == chat_id))
    ).scalar_one()
    return int(highest or 0) + 1


async def stamp_last_seq(db: AsyncSession, chat: WorkspaceObject, seq: int) -> None:
    """Keep the chat's ``last_seq`` current without calling it an edit.

    It is a derived counter, so it does not bump ``version`` — a client holding
    version N has not lost a race because somebody spoke.
    """
    if int(chat.spec.get("last_seq") or 0) >= seq:
        return
    chat.spec = {**chat.spec, "last_seq": seq}
    await db.flush()


def check_entry_size(payload: dict[str, Any], what: str = "a transcript entry") -> None:
    size = payload_size(payload)
    if size > MAX_PAYLOAD_BYTES:
        raise ChatMessageTooLargeError(
            f"{what} would be {size} bytes; the cap is {MAX_PAYLOAD_BYTES}"
        )


#: How an event published by the machine maps onto a transcript role. A tool
#: result is not the assistant speaking, and the distinction is what lets a
#: reader render a tool card rather than a paragraph.
def role_for_event(event: dict[str, Any]) -> str:
    declared = event.get("role")
    if declared in ("user", "assistant", "tool", "system"):
        return str(declared)
    if kind_for_event(event).startswith("tool"):
        return "tool"
    return "assistant"


def kind_for_event(event: dict[str, Any]) -> str:
    """The transcript row's ``kind`` — the event type, wherever it is stated.

    The machine does not publish a harness event flat: it publishes an
    envelope, ``{event_id, role, kind, payload}``, with the event itself under
    ``payload``. Reading only a top-level ``event_type`` therefore left the
    ``kind`` column empty on every row a machine ever published, and a receipt
    or a cost roll-up that selects ``kind = 'message.completed'`` found
    nothing. Both spellings are accepted; a relayed prompt is still flat.
    """
    declared = event.get("kind")
    if isinstance(declared, str) and declared:
        return declared
    payload = event.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("event_type")
        if isinstance(nested, str) and nested:
            return nested
    return str(event.get("event_type") or "")


async def recorded_sequences(
    db: AsyncSession, *, chat: WorkspaceObject, event_ids: Sequence[str]
) -> dict[str, int]:
    """The sequence each of ``event_ids`` is already recorded under here.

    The dual of :func:`persist_published_events`' de-duplication. That write
    skips an event id the table already holds and so returns no row for it,
    while a reader still has to be told where the entry lives: a row a reader
    cannot name is a row it can never ask for again, and so one it can never
    let go of. A publisher that reconnects and republishes is exactly the case.
    """
    if not event_ids:
        return {}
    rows = await db.execute(
        select(ChatMessage.event_id, ChatMessage.seq).where(
            ChatMessage.chat_id == chat.id,
            ChatMessage.event_id.in_(list(event_ids)),
        )
    )
    return {event_id: seq for event_id, seq in rows.all()}


async def write_entries(
    db: AsyncSession, *, chat: WorkspaceObject, events: list[dict[str, Any]]
) -> list[ChatMessage]:
    """Record a batch of entries for a caller that already holds the chat's row
    lock (the backend's ``persist_published_events`` is the publisher's form).

    The difference is the number of statements, not the result: the
    publisher's form walks the batch a row at a time so a republished event can
    collide and be skipped, which is right for a publisher on a socket and
    wrong for a reply written under the lock — a reader's Stop would otherwise cost one insert
    per waiting message with the chat held the whole time. Here the ids already
    recorded are read ONCE and dropped, and what is left cannot collide
    (nothing else may write this chat while the lock is held), so the rows go
    in as one flush with dense sequences.
    """
    if not events:
        return []
    entries = [
        ChatTranscriptEntry.model_validate(
            {
                **event,
                "event_id": str(event.get("event_id") or ""),
                "role": role_for_event(event),
                "kind": kind_for_event(event)[:64],
            }
        ).model_dump(mode="json")
        for event in events
        if event.get("event_id")
    ]
    for entry in entries:
        check_entry_size(entry)
    already = await recorded_sequences(db, chat=chat, event_ids=[e["event_id"] for e in entries])
    seq = await next_seq(db, chat.id)
    written: list[ChatMessage] = []
    for entry in entries:
        if entry["event_id"] in already:
            continue
        written.append(
            ChatMessage(
                id=uuid4(),
                chat_id=chat.id,
                org_team_id=chat.org_team_id,
                seq=seq,
                role=entry["role"],
                kind=entry["kind"],
                event_id=entry["event_id"],
                payload=entry,
            )
        )
        seq += 1
    if not written:
        return []
    db.add_all(written)
    await db.flush()
    await stamp_last_seq(db, chat, written[-1].seq)
    return written


def state_size(state: Mapping[str, Any]) -> int:
    return len(json.dumps(state, separators=(",", ":")).encode("utf-8"))


def dumped_size(value: Any) -> int:
    """The bytes ``value`` occupies inside a serialised state — the same writer
    :func:`state_size` uses, so the two compose."""
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def window_threshold() -> int:
    """The size at which the live window is trimmed.

    Half the document cap, so compaction happens well before the hard refusal
    and a burst of events cannot walk the state into ``doc_too_large`` between
    two trims. It reads the setting on every call because the setting is
    configuration, not a constant.
    """
    return max(settings.realtime_doc_max_bytes // 2, 1)


def window_cut(state: Mapping[str, Any], events: list[Any], target: int) -> int:
    """How many of the oldest events the window has to drop to fit ``target``.

    The size of every candidate window is DERIVED — each event and each event id
    is serialised once, and the sizes of the states they would make are read off
    those numbers — rather than re-serialising the whole document once per
    dropped event. That earlier shape was quadratic in the window: at the real
    threshold a publisher fills the window in two frames and the trim then runs
    for minutes on the event loop, with every socket and request on the worker
    waiting behind it.

    The arithmetic is exact, not an estimate: it reproduces byte for byte what
    ``json.dumps`` writes for ``{**state, "events": kept, "ids": rebuilt}``, so
    the window this cuts to is the one the naive loop would have arrived at.
    """
    total = len(events)
    # Everything but the two keys the window owns: its contribution is the same
    # whatever the cut is, so it is measured once. `{...}` around the items costs
    # two braces and one comma per extra item; the two window keys then add their
    # own quotes, colons and the commas joining the three groups.
    others = {key: value for key, value in state.items() if key not in ("events", "ids")}
    others_size = len(json.dumps(others, separators=(",", ":")).encode("utf-8"))
    fixed = others_size + 17 if others else 18

    keys: list[str | None] = [
        event.get("event_id")
        if isinstance(event, dict) and isinstance(event.get("event_id"), str)
        else None
        for event in events
    ]
    # ``ids`` is a dict, so a repeated event id keeps only its LAST position —
    # whichever cut the window takes, as long as that position survives it.
    last_at: dict[str, int] = {key: index for index, key in enumerate(keys) if key is not None}
    event_bytes = [0] * (total + 1)
    id_bytes = [0] * (total + 1)
    id_count = [0] * (total + 1)
    for index in range(total - 1, -1, -1):
        key = keys[index]
        indexed = key is not None and last_at[key] == index
        event_bytes[index] = event_bytes[index + 1] + dumped_size(events[index])
        id_bytes[index] = id_bytes[index + 1] + (dumped_size(key) + 1 if indexed else 0)
        id_count[index] = id_count[index + 1] + (1 if indexed else 0)

    def size_at(cut: int) -> int:
        events_size = 2 + event_bytes[cut] + (total - cut) - 1
        present = id_count[cut]
        if present == 0:
            return fixed + events_size + 2
        # Every index is at least one digit; one more for each index that reaches
        # the next power of ten, which is the count of ids from that offset on.
        digits = present
        step = 10
        while cut + step < total:
            digits += id_count[cut + step]
            step *= 10
        return fixed + events_size + 2 + id_bytes[cut] + digits + present - 1

    # The window is never emptied, so the last event is never a candidate to
    # drop; sizes fall as the cut moves forward, so the first fit is the answer.
    low, high = 1, total - 1
    while low < high:
        middle = (low + high) // 2
        if size_at(middle) <= target:
            high = middle
        else:
            low = middle + 1
    return low


def stamp_sequences(carrier: dict[str, Any], sequences: Mapping[str, int]) -> dict[str, Any]:
    """``carrier`` — a document state, or the payload of the operation that
    moved it — with each entry under ``events`` named in ``sequences`` carrying
    the transcript ``seq`` the durable write gave it.

    A snapshot is a suffix of the transcript, and a reader that opened the chat
    on its newest page holds only a suffix too; the stamp is what lets it tell
    an entry below its page (which an older page will bring, in order) from
    one above it. The live append is stamped for the same reason: a row a
    reader can name is a row it can ask for again, and so one it may let go of
    when its loaded window is full. An entry the write did not return — one
    with no id — keeps whatever it carried.
    """
    if not sequences:
        return carrier
    events = [
        {**event, "seq": sequences[event["event_id"]]}
        if isinstance(event, dict) and event.get("event_id") in sequences
        else event
        for event in carrier.get("events") or []
    ]
    return {**carrier, "events": events}


def compact_window(state: dict[str, Any]) -> dict[str, Any]:
    """``state`` with the oldest events dropped until it fits the window.

    Only the WINDOW is trimmed — every event dropped here is already in
    ``chat_messages`` and still pages over REST. ``ids`` is rebuilt from what
    survives, so a dropped event id can be appended again (and lands once in
    the table, which de-duplicates on its own).
    """
    events = list(state.get("events") or [])
    if not events or state_size(state) <= window_threshold():
        return state
    if len(events) == 1:
        return state
    kept = events[window_cut(state, events, window_threshold()) :]
    return {
        **state,
        "events": kept,
        "ids": {
            str(event.get("event_id")): index
            for index, event in enumerate(kept)
            if isinstance(event, dict) and isinstance(event.get("event_id"), str)
        },
    }


async def publish_server_entries(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    doc: RealtimeDoc,
    entries: list[dict[str, Any]],
    actor: Mapping[str, Any] | None,
) -> list[ChatMessage]:
    """Put entries the SERVER authored onto a chat, durably and live.

    Some of a chat's transcript is the server's to write: who answered an ask
    or pressed Stop (only the server holds the roster), and why a turn the
    server ended stopped (no box is left to say it). Each entry becomes its
    row, its place in the document's window, and the frame every viewer folds,
    under the server's own peer id and stamped with the sequence the row got,
    the same append a box makes over its socket.

    The caller holds the chat's document and then the chat, locked, and passes
    the document as the lock found it. An entry already recorded is not
    recorded again, and one the window already holds is not re-announced, so a
    retry is a no-op. Returns the rows written.
    """
    if not entries:
        return []
    written = await write_entries(db, chat=chat, events=entries)
    sequences = {row.event_id: row.seq for row in written}
    named = [str(entry.get("event_id") or "") for entry in entries]
    sequences.update(
        await recorded_sequences(
            db, chat=chat, event_ids=[name for name in named if name and name not in sequences]
        )
    )
    state = dict(doc.state or {})
    events: list[Any] = list(state.get("events") or [])
    ids: dict[str, int] = dict(state.get("ids") or {})
    appended = False
    for entry in entries:
        event_id = entry.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in ids:
            continue
        ids[event_id] = len(events)
        events.append(dict(entry))
        appended = True
    if not appended:
        return written
    window = compact_window(stamp_sequences({**state, "events": events, "ids": ids}, sequences))
    size = state_size(window)
    if size > settings.realtime_doc_max_bytes:
        raise ChatMessageTooLargeError(
            f"the chat's window would be {size} bytes; the cap is {settings.realtime_doc_max_bytes}"
        )
    op = OpPayload(op_id=f"srv-{uuid4().hex[:12]}", intent="append", events=entries)
    envelope = DocEnvelope(
        doc_id=str(chat.id),
        doc_type="chat",
        epoch=doc.epoch,
        peer_id=SERVER_PEER_ID,
        seq=doc.seq + 1,
        kind="op",
        payload=stamp_sequences(op.model_dump(mode="json"), sequences),
    )
    frame = {
        "envelope": envelope.model_dump(mode="json"),
        "team_id": str(doc.team_id) if doc.team_id else None,
        "relay": False,
    }
    check_entry_size(frame, "the frame announcing it")
    doc.state = window
    doc.seq += 1
    await db.flush()
    await emit(
        db,
        org_id=doc.org_id,
        type=EventType.DOC_OP,
        entity=Entity.DOC,
        entity_id=f"doc:chat:{chat.id}",
        version=doc.seq,
        payload=frame,
        actor=dict(actor) if actor is not None else None,
    )
    return written


def answers_a_waiting_message_clause() -> ColumnElement[bool]:
    """:func:`~alkera_core.schemas.objects.transcript.answers_a_waiting_message`
    as SQL, so the server selects exactly the rows the box steps over.

    The Python rule is the definition and this is its one transcription; they
    are held together by a test that runs both over the same real transcript.
    It lives beside the query rather than beside the model because the model
    module is imported by the migration environment, which must not drag the
    schemas package (and the settings singleton behind it) in at import time.
    """
    return and_(
        ChatMessage.role != "user",
        not_(ChatMessage.event_id.startswith(ASIDE_NOTE_PREFIX, autoescape=True)),
    )


#: How many waiting messages one Stop reports on. The lane the box runs them
#: through is one at a time, so a chat with more than this waiting is a client
#: that lost its composer, not a person typing — and the cost of the reply has
#: to be bounded because every row is written while this chat's row is locked.
#: Past it the note still lands and the box still drains its own queue; what is
#: given up is the durable record for the oldest of an unreasonable backlog.
MAX_STOPPED_PROMPTS = 200


async def unstarted_prompts(
    db: AsyncSession, *, chat_id: UUID, limit: int = MAX_STOPPED_PROMPTS
) -> list[ChatMessage]:
    """The person's messages no machine has begun answering, newest last.

    A box answering a message publishes something for it — its ``running``
    status first of all. Which rows count as that answer is not decided here:
    :func:`answers_a_waiting_message_clause` is the transcript-reading rule the
    BOX applies to the same rows when it decides what it still owes a turn, and
    the server asks it the same question so the two cannot disagree. The last
    row that answers is the watermark: a prompt above it has had nothing come
    back, and a prompt below it was answered, is being answered, or was
    stopped mid-answer.

    At most ``limit`` are returned, and they are the NEWEST — a reader whose
    backlog is past the cap is told about the messages they last typed rather
    than about ones they have scrolled away from.
    """
    answered_to = (
        await db.execute(
            select(func.max(ChatMessage.seq)).where(
                ChatMessage.chat_id == chat_id, answers_a_waiting_message_clause()
            )
        )
    ).scalar_one() or 0
    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.role == "user",
                    ChatMessage.kind == PROMPT_KIND,
                    ChatMessage.seq > answered_to,
                )
                .order_by(ChatMessage.seq.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return rows[::-1]


def prompt_cancelled_for(
    row: ChatMessage,
    *,
    session_id: str,
    now: datetime,
    reason: str = PROMPT_CANCELLED_STOPPED,
) -> dict[str, Any]:
    """The "never run" entry for one recorded prompt, named the way the reader
    already holds that message: by the id minted from the sender's own client
    id, falling back to the row when a message arrived without one."""
    record = ChatPromptRecord.model_validate(row.payload)
    named = prompt_entry_id(record.client_id) if record.client_id else str(row.id)
    return prompt_cancelled_entry(
        PromptCancelled(
            event_id=prompt_cancelled_event_id(named),
            time=now,
            session_id=session_id,
            message_id=named,
            client_id=record.client_id,
            reason=reason,
        )
    )


__all__ = [
    "MAX_STOPPED_PROMPTS",
    "ChatMessageTooLargeError",
    "answers_a_waiting_message_clause",
    "check_entry_size",
    "compact_window",
    "dumped_size",
    "kind_for_event",
    "next_seq",
    "prompt_cancelled_for",
    "publish_server_entries",
    "recorded_sequences",
    "role_for_event",
    "stamp_last_seq",
    "stamp_sequences",
    "state_size",
    "unstarted_prompts",
    "window_cut",
    "window_threshold",
    "write_entries",
]
