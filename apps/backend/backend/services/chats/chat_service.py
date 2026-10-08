"""Chats: the durable transcript, the relay onto the chat document, and the
promote that pins a result.

The transcript is a paged domain table. ``chat_messages`` is the system
of record; the realtime document keeps only a bounded live window, and catch-up
is a domain query over this table — "messages after seq N in *this* chat" —
never a global cursor. ``seq`` is dense and assigned under the chat row's lock,
so two writers cannot mint the same one.

There is exactly ONE function that turns a person's message into a transcript
entry — :func:`append_user_message` — and both entry points call it: the REST
POST a browser makes, and the ``user_message`` relay a socket peer sends. That
is deliberate: two spellings of "a person said something" is how a message ends
up in the transcript on one path and not the other.

A relay is not a state change. It carries a structured payload the machine
consumes — a prompt, a re-run, a promote — and rides the ``user_message``
intent of the chat document so the daemon and every other reader see it at
once. The cloud never executes SQL; ``run_query`` is a request to the machine
that owns the chat (R-H).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from alkera_core.chat_refusals import ChatRefusalKind, refusal_is_final
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.events import ACCESS_CHANGED_KEY, BOUND_MACHINE_KEY, Entity, EventType, emit
from alkera_core.events.types import CHAT_DELETED_REASON
from alkera_core.models import (
    ChatAttachment,
    ChatMessage,
    RealtimeDoc,
    User,
    WorkspaceObject,
)
from alkera_core.objects import chat_end, chat_turn
from alkera_core.objects.chat_transcript import (
    MAX_STOPPED_PROMPTS,
    ChatMessageTooLargeError,
    answers_a_waiting_message_clause,
    check_entry_size,
    kind_for_event,
    next_seq,
    prompt_cancelled_for,
    recorded_sequences,
    role_for_event,
    stamp_last_seq,
    unstarted_prompts,
    write_entries,
)
from alkera_core.objects.publisher_report import apply_publisher_report, clear_refusal
from alkera_core.objects.transcript_page import PageReach, PageRow, align_boundary
from alkera_core.schemas.chat import (
    FilePart,
    PermissionResolved,
    QuestionAnswered,
    QuestionRejected,
)
from alkera_core.schemas.objects import (
    PROMPT_KIND,
    ChatModelPin,
    ChatPromptRecord,
    ChatSpec,
    ChatTranscriptEntry,
    CloudPermissionMode,
    MachineStatus,
    PromptRelay,
)
from alkera_core.schemas.objects.transcript import (
    RESOLUTION_KINDS,
    ModeChangeRecord,
    RecordedResolution,
    aside_note_id,
    mode_changed_entry,
    recorded_answer_entry,
    recorded_answer_event_id,
    stopped_turn_entries,
    stopped_turn_note_id,
)
from alkera_core.schemas.realtime import SERVER_PEER_ID, DocEnvelope, OpPayload
from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Uuid,
    case,
    delete,
    func,
    literal,
    null,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import workspaces
from backend.services.chats.activity import ChatActivity, chat_activity
from backend.services.chats.chat_titles import TITLE_MAX_CHARS, title_from_prompt
from backend.services.chats.creation import (
    ClientIdTakenError,
    create_chat,
    find_chat_by_client_id,
)
from backend.services.chats.message_ids import event_id_for_client_message, refuse_reused_id
from backend.services.objects import object_service

#: The structured relay kinds the chat document carries on ``user_message``.
#: Only ``prompt`` may originate from a browser; the other two are minted by a
#: route after it decided, so a socket peer sending one is refused.
RelayKind = Literal["prompt", "run_query", "promote"]
CLIENT_RELAY_KINDS: frozenset[str] = frozenset({"prompt"})
SERVER_RELAY_KINDS: frozenset[str] = frozenset({"run_query", "promote"})

#: The transcript entry a person's message becomes. Spelled in api-core beside
#: the record model, so the machine that parses it and the server that writes it
#: read the same constant.
USER_MESSAGE_KIND = PROMPT_KIND

#: The kind a "never run" entry lands under. Read here so the mark a stop
#: derives its note id from can step over the rows that stop itself wrote.
PROMPT_CANCELLED_KIND = "prompt.cancelled"

#: The row that opens a message. Exactly one is written per message, so a page
#: holding it holds the message from its start — which is what lets a backward
#: page prove it is not showing the end of something.
MESSAGE_CREATED_KIND = "message.created"


async def lock_chat_for_write(
    db: AsyncSession, chat_id: UUID, *, org_team_id: UUID
) -> WorkspaceObject | None:
    """The chat's document, then the chat: the one order every writer to a
    chat takes (:func:`alkera_core.objects.chat_end.lock_chat_for_write`)."""
    return await chat_end.lock_chat_for_write(db, chat_id, org_team_id=org_team_id)


async def _name_from_first_prompt(db: AsyncSession, chat: WorkspaceObject, text: str) -> None:
    """Name a still-unnamed chat after the first thing said in it.

    This is derived, like ``last_seq``: it does not bump ``version``, because a
    client holding version N has not lost a race when the chat acquires the
    name of its own opening prompt. A chat created with a title keeps it.
    """
    if (chat.title or "").strip():
        return
    title = title_from_prompt(text)
    if not title:
        return
    chat.title = title
    await db.flush()


def check_user_message_fits(
    *,
    text: str,
    user_id: UUID,
    client_id: str,
    attachments: Sequence[FilePart] = (),
    context: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """The size guard :func:`append_user_message` applies, asked ahead of time.

    For a caller that must know a message will be accepted BEFORE it does
    something it cannot undo — copying a template's files into a new chat's
    working directory, say. Measures the record the append would build, so the
    answer here and the answer there come from one spelling of the payload.

    Raises :class:`ChatMessageTooLargeError`, exactly as the append would.
    """
    check_entry_size(
        ChatPromptRecord(
            text=text,
            client_id=client_id,
            user_id=str(user_id),
            attachments=list(attachments),
            context=context,
            metadata=dict(metadata or {}),
        ).model_dump(mode="json")
    )


def _relay_payload(
    *, chat_id: UUID, doc: RealtimeDoc | None, events: list[dict[str, Any]]
) -> dict[str, Any]:
    """The outbox payload one ``user_message`` relay becomes.

    Spelled once, so the size guard measures exactly the row
    :func:`broadcast_relay` will write rather than a second spelling of it that
    can drift. The relay is the bigger of the two things a message becomes — the
    same text again, inside a document envelope — so a message sized against the
    transcript entry alone can clear that guard and still be refused on the
    emit, where the bare ``ValueError`` is nobody's published contract.
    """
    envelope = DocEnvelope(
        doc_id=str(chat_id),
        doc_type="chat",
        epoch=doc.epoch if doc else 1,
        peer_id=SERVER_PEER_ID,
        seq=0,
        kind="op",
        payload=OpPayload(
            op_id=f"srv-{uuid4().hex[:12]}",
            intent="user_message",
            events=events,
        ).model_dump(mode="json"),
    )
    return {
        "envelope": envelope.model_dump(mode="json"),
        "team_id": str(doc.team_id) if doc and doc.team_id else None,
        "relay": True,
    }


async def _existing_entry(db: AsyncSession, chat_id: UUID, event_id: str) -> ChatMessage | None:
    return (
        await db.execute(
            select(ChatMessage).where(
                ChatMessage.chat_id == chat_id, ChatMessage.event_id == event_id
            )
        )
    ).scalar_one_or_none()


async def message_recorded(db: AsyncSession, *, chat_id: UUID, client_id: str) -> bool:
    """Whether a person's message with this client id is already on the
    transcript -- asked by a surface that does work BEFORE recording (a Slack
    message whose files are written into the chat first), so a redelivery
    does not redo it."""
    return await _existing_entry(db, chat_id, event_id_for_client_message(client_id)) is not None


#: How many linked nodes one page of the attachments listing carries.
DEFAULT_ATTACHMENT_LIMIT = 50
MAX_ATTACHMENT_LIMIT = 200


#: Where a page of attachments resumes: the place and the node that ended the
#: previous page. The PAIR, not the place alone — the order is taken on both,
#: and rows written before attaches were serialized can still share a place, so
#: a cursor that carried only the place stepped over every link that shared the
#: one a page ended on.
AttachmentCursor = tuple[int, UUID]

#: The widest place a cursor may name: ``position`` is a ``BIGINT``, so a larger
#: number is not a place any row can hold — and not one this service minted.
#: Binding it anyway is a driver-level error, which is how a query string became
#: a 500; the codec refuses it here instead, where "a cursor nobody minted" is
#: already a refusal.
MAX_ATTACHMENT_POSITION = 2**63 - 1


class TooManyAttachmentsError(ValueError):
    """The chat already holds as many nodes as ``CHAT_MAX_ATTACHMENTS`` allows."""


async def _require_chat(db: AsyncSession, chat_id: UUID) -> None:
    """Refuse a link to a chat that is not there, rather than letting the
    foreign key say it as an integrity error nobody can answer."""
    found = (
        await db.execute(select(WorkspaceObject.id).where(WorkspaceObject.id == chat_id))
    ).scalar_one_or_none()
    if found is None:
        raise LookupError("the chat is gone")


async def _hold_attachments(db: AsyncSession, chat_id: UUID) -> None:
    """Serialize this chat's attaches for the rest of the transaction.

    A place is the highest one the chat holds plus one and a ceiling is a
    count, so both are read-then-write: two attaches in flight together read
    the same number and then each writes it, which is how eight files taken
    from one selection settled on one place and how a ceiling of four admitted
    eleven. The lock makes the read and the write one step per chat.

    Per CHAT, and not the chat ROW: a person speaking in the conversation takes
    that row for the length of their message, and an attach has no business
    waiting for it (nor it for an attach). It is an *xact* lock, so it goes
    when the transaction does and a caller that dies mid-attach blocks nobody.
    """
    await advisory_xact_lock(db, advisory_key("chat-attach", chat_id))


async def record_attachment(db: AsyncSession, *, chat_id: UUID, node_id: UUID) -> bool:
    """Link ``node_id`` to the chat, idempotently.

    This is the writer :func:`alkera_core.files.link_attachment` is handed: the
    Files library proves the node is live in the caller's org and this function
    is the only thing that writes the link. ``True`` when the reference was
    newly written, ``False`` when it was already there — an attach is a link,
    so attaching the same node twice is not an error and does not duplicate.

    ONE row, whatever the chat already holds. The links used to be a JSON array
    inside the chat's spec, which meant locking the chat row, reading every id
    back, appending and writing all of them again: the cost of attaching a file
    grew with the number of files already attached, and every concurrent attach
    queued behind the chat's own row. The duplicate is refused by the primary
    key rather than by reading first, and the order is taken from the highest
    position the chat holds — an index lookup, not a scan. What is serialized
    is only this chat's attaches (:func:`_hold_attachments`), and only until the
    transaction ends, so the cost is a place of one's own rather than a rewrite.

    A ceiling, where a deployment sets one, refuses a NEW link and never an
    existing one. Idempotence is the whole contract of this function, and a
    ceiling that turns the second attach of a node the chat already holds into
    an error breaks it exactly when the chat is fullest. The membership read
    that keeps that true is taken only once the count has reached the ceiling,
    so a deployment with no ceiling — and every attach below one — pays nothing
    for it.
    """
    ceiling = settings.chat_max_attachments
    await _require_chat(db, chat_id)
    await _hold_attachments(db, chat_id)
    if (
        ceiling is not None
        and await attachment_count(db, chat_id) >= ceiling
        and node_id not in await linked_among(db, chat_id, [node_id])
    ):
        raise TooManyAttachmentsError(f"a chat holds at most {ceiling} attachments")
    nxt = (
        select(func.coalesce(func.max(ChatAttachment.position), 0) + 1)
        .where(ChatAttachment.chat_id == chat_id)
        .scalar_subquery()
    )
    written = (
        await db.execute(
            pg_insert(ChatAttachment)
            .values(chat_id=chat_id, node_id=node_id, position=nxt)
            .on_conflict_do_nothing(index_elements=["chat_id", "node_id"])
            .returning(ChatAttachment.node_id)
        )
    ).scalar_one_or_none()
    await db.flush()
    return written is not None


async def forget_attachment(db: AsyncSession, *, chat_id: UUID, node_id: UUID) -> bool:
    """Drop the link to ``node_id``, idempotently.

    The inverse of :func:`record_attachment` and the writer
    :func:`alkera_core.files.unlink_attachment` is handed. ``True`` when a
    reference was removed, ``False`` when the chat did not name that node — a
    detach is the absence of a link, so detaching twice is not an error.
    """
    await _require_chat(db, chat_id)
    removed = (
        await db.execute(
            delete(ChatAttachment)
            .where(ChatAttachment.chat_id == chat_id, ChatAttachment.node_id == node_id)
            .returning(ChatAttachment.node_id)
        )
    ).scalar_one_or_none()
    await db.flush()
    return removed is not None


async def attachment_count(db: AsyncSession, chat_id: UUID) -> int:
    """How many nodes this chat holds. One counted index scan, no ids."""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ChatAttachment)
                .where(ChatAttachment.chat_id == chat_id)
            )
        ).scalar_one()
    )


async def attachment_counts(db: AsyncSession, chat_ids: Sequence[UUID]) -> dict[UUID, int]:
    """How many nodes each of these chats holds, in ONE statement.

    A listing page says how many files a conversation carries without carrying
    them: fifty chats must not become fifty counts, and must never become fifty
    lists of ids a rail renders none of.
    """
    if not chat_ids:
        return {}
    rows = await db.execute(
        select(ChatAttachment.chat_id, func.count())
        .where(ChatAttachment.chat_id.in_(list(chat_ids)))
        .group_by(ChatAttachment.chat_id)
    )
    return {UUID(str(chat_id)): int(count) for chat_id, count in rows.all()}


async def linked_attachments(
    db: AsyncSession,
    chat_id: UUID,
    *,
    after: AttachmentCursor | None = None,
    limit: int | None = None,
) -> tuple[list[UUID], AttachmentCursor | None]:
    """A page of the nodes linked to this chat, in attach order.

    Returns the ids and the cursor a next page starts after, or ``None`` when
    the page is the last one. ``limit=None`` reads the whole list, which is
    what the chat's own read does — ids alone, and the caller decides each one
    again before it says a word about the file.

    The page resumes on the same pair it is ordered by. Resuming on the place
    alone was correct only while places were distinct, which they were not: a
    page that ended on one of two links sharing a place asked for everything
    *past* that place and never returned the other — for every reader, on every
    read, while the link and the file both stood. The pair is unique because it
    ends in the primary key, so a tie is paged through rather than over.
    """
    stmt = (
        select(ChatAttachment.node_id, ChatAttachment.position)
        .where(ChatAttachment.chat_id == chat_id)
        .order_by(ChatAttachment.position, ChatAttachment.node_id)
    )
    if after is not None:
        place, node = after
        stmt = stmt.where(
            tuple_(ChatAttachment.position, ChatAttachment.node_id)
            > tuple_(literal(place, BigInteger), literal(node, Uuid))
        )
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    rows = list((await db.execute(stmt)).all())
    more = limit is not None and len(rows) > limit
    if more:
        rows = rows[:limit]
    ids = [UUID(str(node_id)) for node_id, _position in rows]
    return ids, ((int(rows[-1][1]), ids[-1]) if more and rows else None)


async def linked_among(db: AsyncSession, chat_id: UUID, node_ids: Sequence[UUID]) -> set[UUID]:
    """Which of ``node_ids`` this chat holds — the membership question a
    message body asks, answered over the ids it named rather than over every id
    the chat has ever held."""
    if not node_ids:
        return set()
    rows = await db.execute(
        select(ChatAttachment.node_id).where(
            ChatAttachment.chat_id == chat_id,
            ChatAttachment.node_id.in_(list(node_ids)),
        )
    )
    return {UUID(str(node_id)) for node_id in rows.scalars().all()}


async def append_user_message(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    user_id: UUID,
    text: str,
    client_id: str,
    attachments: Sequence[FilePart] = (),
    context: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[ChatMessage, bool]:
    """Persist a person's message as the next transcript entry.

    ``(message, created)``; ``created`` is ``False`` when this exact client id
    was already recorded, so a caller knows not to broadcast it twice.

    ``context`` is recorded beside the words for the model alone (a Slack
    briefing, who is speaking); it never names the chat and never renders as
    the person's message.

    ``metadata`` says where the words came from when it was not somebody
    typing — the template brief a chat opens with. It rides the record and the
    relay alike, so a reader that folds the durable row and one that folds the
    live relay caption the same bubble.

    The chat row is locked first: the lock is what makes ``seq`` dense when two
    people (or a person and a retry) speak at the same moment.
    """
    event_id = event_id_for_client_message(client_id)
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    existing = await _existing_entry(db, chat.id, event_id)
    if existing is not None:
        return existing, False
    provenance = dict(metadata or {})
    payload = ChatPromptRecord(
        text=text,
        client_id=client_id,
        user_id=str(user_id),
        attachments=list(attachments),
        context=context,
        metadata=provenance,
    ).model_dump(mode="json")
    check_entry_size(payload)
    # The identity the relay carries, minted before the guard rather than by it:
    # every caller relays the row it just got back, so the relay measured here is
    # byte-for-byte the one `broadcast_relay` is about to emit.
    message_id = uuid4()
    seq = await next_seq(db, chat.id)
    # Stamped here rather than by the database, so the relay can carry the
    # very stamp the row will be read back with.
    created_at = datetime.now(UTC)
    check_entry_size(
        _relay_payload(
            chat_id=chat.id,
            doc=await db.get(RealtimeDoc, (locked.org_team_id, "chat", str(chat.id))),
            events=[
                PromptRelay(
                    message_id=str(message_id),
                    seq=seq,
                    text=text,
                    client_id=client_id,
                    user_id=str(user_id),
                    attachments=list(attachments),
                    context=context,
                    at=created_at,
                    metadata=provenance,
                ).model_dump(mode="json")
            ],
        ),
        "the relay carrying it",
    )
    message = ChatMessage(
        id=message_id,
        chat_id=chat.id,
        org_team_id=locked.org_team_id,
        seq=seq,
        role="user",
        kind=USER_MESSAGE_KIND,
        event_id=event_id,
        payload=payload,
        created_at=created_at,
    )
    db.add(message)
    await db.flush()
    await stamp_last_seq(db, locked, message.seq)
    await _name_from_first_prompt(db, locked, text)
    return message, True


async def submit_user_message(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    user_id: UUID,
    text: str,
    client_id: str,
    actor: dict[str, Any] | None,
    attachments: Sequence[FilePart] = (),
    context: str = "",
    metadata: dict[str, Any] | None = None,
    announce: bool = True,
) -> tuple[ChatMessage, bool]:
    """A person's message, recorded and handed to the box: the one prompt path.

    Every surface a person speaks from -- the web composer's send, a template's
    brief, a Slack message, a Slack message held for the owner's consent --
    becomes a turn through this function, so each lands as the same transcript
    entry and reaches the box as the same relay. What differs between surfaces
    (how the words were typed, which files ride along, the agent-only context)
    is decided by the caller before it gets here; nothing after it knows or
    cares which surface spoke.

    ``(message, created)``: a client id already recorded is not relayed twice,
    so a redelivered Slack event or a retried web send is one turn. ``announce``
    tells the chat's readers the row moved; a caller that announces for its own
    reasons (a template brief inside a create) leaves it off.
    """
    message, created = await append_user_message(
        db,
        chat=chat,
        user_id=user_id,
        text=text,
        client_id=client_id,
        attachments=attachments,
        context=context,
        metadata=metadata,
    )
    if not created:
        refuse_reused_id(message, text=text, user_id=user_id, client_id=client_id)
        return message, False
    relay = PromptRelay(
        message_id=str(message.id),
        seq=message.seq,
        text=text,
        client_id=client_id,
        user_id=str(user_id),
        attachments=list(attachments),
        context=context,
        at=message.created_at,
    ).model_dump(mode="json")
    if metadata:
        # Where the words came from, on the relay as on the record, so a reader
        # of either captions the bubble the same way.
        relay["metadata"] = metadata
    await broadcast_relay(
        db,
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        events=[relay],
        actor=actor,
    )
    if announce:
        await announce_chat(db, chat=chat, actor=actor)
    return message, True


def event_payload(entry: dict[str, Any]) -> dict[str, Any]:
    """The harness event inside a stored transcript entry.

    The dual of :func:`kind_for_event`, for a reader rather than a writer. A
    machine event is stored as the envelope ``{event_id, role, kind, payload}``
    with the event itself one level down, so a consumer that reads ``part`` or
    ``request_id`` off the row's ``payload`` column finds nothing at all. A
    person's ``prompt`` row is flat and has no nested event, so it is returned
    as it stands.
    """
    nested = entry.get("payload")
    if isinstance(nested, dict) and nested:
        return nested
    return entry


def message_id_for_event(entry: dict[str, Any]) -> str | None:
    """Which message a stored transcript row belongs to, off the row itself.

    A machine event states it flat — ``message.created``, ``part.started``,
    ``message.completed``, ``tool.call`` — and a ``part.created`` states it on
    the part it carries. A person's ``prompt`` row and the session-level events
    belong to no message and say nothing, which is exactly what keeps them from
    holding a page open. Nothing here reads text or timing: two messages
    written in the same millisecond are still two messages.

    A ``prompt.cancelled`` row is the one row that states an id it does NOT
    belong to: it names the PROMPT it cancels — a row of its own, which no
    ``message.created`` ever opens — so reading it as membership would leave
    every page carrying one unable to say it holds that message whole.
    """
    if kind_for_event(entry) == PROMPT_CANCELLED_KIND:
        return None
    event = event_payload(entry)
    flat = event.get("message_id")
    if isinstance(flat, str) and flat:
        return flat
    part = event.get("part")
    if isinstance(part, dict):
        nested = part.get("message_id")
        if isinstance(nested, str) and nested:
            return nested
    return None


async def persist_published_events(
    db: AsyncSession, *, chat: WorkspaceObject, events: list[dict[str, Any]]
) -> list[ChatMessage]:
    """Record the machine's published events as transcript entries.

    Called from the chat document's persist step, inside the operation's own
    transaction, so a rolled-back operation leaves no transcript behind. An
    event id already recorded is skipped rather than duplicated — the publisher
    republishes on reconnect and must be able to do so safely.

    Each entry is read through :class:`ChatTranscriptEntry` — the same model the
    machine writes — so what lands in ``payload`` is the envelope this server
    understands, stamped with its schema version. The row's ``role``/``kind``
    columns are resolved first and validated in, because a publisher may state
    the kind only inside the event, and an entry that named neither would
    otherwise leave the columns a receipt selects on empty.
    """
    if not events:
        return []
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        return []
    seq = await next_seq(db, chat.id)
    written: list[ChatMessage] = []
    for event in events:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            continue
        entry = ChatTranscriptEntry.model_validate(
            {
                **event,
                "event_id": event_id,
                "role": role_for_event(event),
                "kind": kind_for_event(event)[:64],
            }
        ).model_dump(mode="json")
        check_entry_size(entry)
        row = {
            "id": uuid4(),
            "chat_id": chat.id,
            "org_team_id": locked.org_team_id,
            "seq": seq,
            "role": entry["role"],
            "kind": entry["kind"],
            "event_id": event_id,
            "payload": entry,
        }
        result = await db.execute(
            pg_insert(ChatMessage)
            .values(**row)
            .on_conflict_do_nothing(constraint="uq_chat_messages_chat_event")
            .returning(ChatMessage.id)
        )
        if result.scalar_one_or_none() is None:
            continue
        stored = await db.get(ChatMessage, row["id"])
        if stored is not None:
            written.append(stored)
        seq += 1
    if written:
        await stamp_last_seq(db, locked, written[-1].seq)
    return written


# ---------------------------------------------------------------------------
# An ask and its recorded answer
# ---------------------------------------------------------------------------

#: The kinds under which the harness raises an ask a person has to answer.
ASK_KINDS: frozenset[str] = frozenset({"permission.request", "question.request"})

#: Fields on a resolution that say WHICH MEMBER decided. The server writes
#: them and nothing else may: a reader's surface renders them as the name of
#: the person who approved a write, which is an identity claim, and a machine
#: is not a party that gets to make one about the people in the chat.
DECIDER_CLAIM_KEYS: tuple[str, ...] = ("decided_by_user_id", "decided_by_name", "decided_via")


def without_decider_claim(event: dict[str, Any]) -> dict[str, Any]:
    """``event`` with any claim about WHO decided taken off it.

    Applied to everything a machine publishes. A box runs the customer's code
    next to the customer's credentials and is the least trustworthy writer in
    the system; a resolution it publishes is a true record that its harness
    settled an ask, and it is not evidence about a person. Left in, a box that
    is compromised — or merely echoing a row it read back — could put "Allowed
    once by <a colleague>" on the tape over a write nobody approved.

    Only :func:`record_interrupt_answer`, reached through the authenticated
    route, stamps these; every other writer's copy comes back clean.

    Reads through the publisher's ``{event_id, role, kind, payload}`` envelope
    AND the flat event, and strips BOTH levels rather than stopping at the
    first it finds. They are two different readers: a browser folding the live
    frame takes the envelope's ``payload``, while a page catching up over REST
    reads the stored entry itself as the harness event. Cleaning only the
    deeper one leaves the claim standing for the other, which is the whole
    attack again by a different route. Returns ``event`` itself when there is
    nothing to take off, so the common case copies nothing.
    """
    raw = event.get("payload")
    payload = raw if isinstance(raw, dict) else None
    nested = payload is not None and any(key in payload for key in DECIDER_CLAIM_KEYS)
    if not nested and not any(key in event for key in DECIDER_CLAIM_KEYS):
        return event
    cleaned = {key: value for key, value in event.items() if key not in DECIDER_CLAIM_KEYS}
    if payload is not None and nested:
        cleaned["payload"] = {
            key: value for key, value in payload.items() if key not in DECIDER_CLAIM_KEYS
        }
    return cleaned


class AskNotFoundError(LookupError):
    """The transcript holds no ask under that id."""


class AskAlreadyAnsweredError(ValueError):
    """The ask was resolved — by a reader, or by the machine — already."""


class AnswerDoesNotFitError(ValueError):
    """The answer is not one this ask can take: a question's answers for a
    permission, an option the ask never offered, and so on."""


class TranscriptAsk:
    """One ask as the transcript holds it: its kind, the event the harness
    raised it with, and whether anything has resolved it since."""

    __slots__ = ("event", "kind", "resolved")

    def __init__(self, kind: str, event: dict[str, Any], *, resolved: bool) -> None:
        self.kind = kind
        self.event = event
        self.resolved = resolved


async def find_ask(db: AsyncSession, *, chat_id: UUID, request_id: str) -> TranscriptAsk | None:
    """The ask ``request_id`` names in this chat's transcript, or ``None``.

    Both the ask and its resolution are read off the rows the machine (or the
    server) wrote, so what a reader may answer is exactly what the transcript
    says was asked — never an id a body invented.
    """
    nested = ChatMessage.payload["payload"]["request_id"].astext
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.kind.in_(sorted(ASK_KINDS | RESOLUTION_KINDS)),
                    nested == request_id,
                )
                .order_by(ChatMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    ask: TranscriptAsk | None = None
    for row in rows:
        if row.kind in ASK_KINDS and ask is None:
            ask = TranscriptAsk(row.kind, event_payload(row.payload), resolved=False)
        elif row.kind in RESOLUTION_KINDS and ask is not None:
            ask.resolved = True
    return ask


def _resolution_for(
    ask: TranscriptAsk,
    *,
    request_id: str,
    session_id: str,
    option_id: str | None,
    answers: list[list[str]] | None,
    reject: bool,
    reason: str | None,
    now: datetime,
    note: str | None = None,
    decided_by_user_id: str | None = None,
    decided_by_name: str | None = None,
    decided_via: str | None = None,
) -> RecordedResolution:
    """The resolution event a reader's answer to ``ask`` becomes, or
    :class:`AnswerDoesNotFitError` when the answer is not one the ask takes.

    ``decided_by_user_id`` / ``decided_by_name`` name the person, when the
    caller knows the roster. A rejection carries no decider: declining to
    answer records that the ask went unanswered, not a decision anyone made."""
    event_id = recorded_answer_event_id(request_id)
    try:
        if ask.kind == "permission.request":
            if option_id is None:
                raise AnswerDoesNotFitError("a permission ask is answered with one of its options")
            offered = [
                choice.get("option_id")
                for choice in ask.event.get("options") or []
                if isinstance(choice, dict)
            ]
            if option_id not in offered:
                raise AnswerDoesNotFitError(
                    f"the ask offered {', '.join(str(o) for o in offered) or 'nothing'}"
                )
            # Validated from a dict: the option is a str off the wire, and the
            # event's own literal is what decides whether it is a real one.
            return PermissionResolved.model_validate(
                {
                    "event_id": event_id,
                    "time": now,
                    "session_id": session_id,
                    "request_id": request_id,
                    "option_id": option_id,
                    "decided_by": "user",
                    "decided_by_user_id": decided_by_user_id,
                    "decided_by_name": decided_by_name,
                    "decided_via": decided_via,
                }
            )
        if option_id is not None:
            raise AnswerDoesNotFitError("a question is answered with answers, or declined")
        if reject:
            return QuestionRejected(
                event_id=event_id,
                time=now,
                session_id=session_id,
                request_id=request_id,
                reason=reason,
            )
        return QuestionAnswered(
            event_id=event_id,
            time=now,
            session_id=session_id,
            request_id=request_id,
            answers=answers or [],
            decided_by="user",
            decided_by_user_id=decided_by_user_id,
            decided_by_name=decided_by_name,
            note=note,
        )
    except ValidationError as exc:
        raise AnswerDoesNotFitError(
            f"the answer could not be recorded ({exc.error_count()} bad field(s))"
        ) from exc


async def record_interrupt_answer(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    request_id: str,
    option_id: str | None = None,
    answers: list[list[str]] | None = None,
    reject: bool = False,
    reason: str | None = None,
    note: str | None = None,
    decided_by: User | None = None,
    via: Literal["web", "slack"] | None = None,
    now: datetime | None = None,
) -> ChatMessage:
    """Record a reader's answer to an ask as the transcript's resolution of it.

    An answer is durable: the ask is state of the chat, and so is its answer.
    With a box holding the ask, the relay reaches it first and the row is the
    record; with none — the chat asleep, its box gone — the row is what the
    next box reads when it opens the chat, and it acts on it instead of asking
    the person again. The row is the very event the machine would have
    published (``permission.resolved`` / ``question.answered`` /
    ``question.rejected``, decided by the user), under
    :data:`RECORDED_ANSWER_ROLE` so a box can tell it from one its harness
    already settled.

    ``decided_by`` is the member who answered. A chat many people can read
    records who made the call, not only what was chosen: the caller is the one
    place that knows the roster, so it stamps the id and the display name onto
    the event here. Omitting it records the decision unattributed, which is
    what an answer from a caller with no user behind it has to be.

    Raises :class:`AskNotFoundError` for an id the transcript never asked,
    :class:`AskAlreadyAnsweredError` once anything resolved it, and
    :class:`AnswerDoesNotFitError` for an answer the ask cannot take.
    """
    ask = await find_ask(db, chat_id=chat.id, request_id=request_id)
    if ask is None:
        raise AskNotFoundError(f"no ask {request_id!r} is outstanding in this chat")
    if ask.resolved:
        raise AskAlreadyAnsweredError(f"ask {request_id!r} has already been answered")
    event = _resolution_for(
        ask,
        request_id=request_id,
        session_id=str(chat.id),
        option_id=option_id,
        answers=answers,
        reject=reject,
        reason=reason,
        now=now or datetime.now(UTC),
        note=note,
        decided_by_user_id=str(decided_by.id) if decided_by is not None else None,
        decided_by_name=(decided_by.display_name or None) if decided_by is not None else None,
        decided_via=via,
    )
    written = await persist_published_events(db, chat=chat, events=[recorded_answer_entry(event)])
    if not written:  # pragma: no cover — the resolved check above holds the row's id
        raise AskAlreadyAnsweredError(f"ask {request_id!r} has already been answered")
    return written[0]


async def record_turn_stopped(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    who: str,
    now: datetime | None = None,
) -> list[ChatMessage]:
    """Record what a reader's Stop did to this chat: the messages it means will
    never run, then the line naming who ended the turn.

    The relay is how the box hears the stop; this is how a reader who was not
    watching finds out what happened to a turn that ends mid-answer. Only the
    server knows which member pressed Stop — the box sees a user id and has no
    roster to resolve it — so the line is written here.

    A message the box has not begun answering is a second, separate fact, and
    it is durable for the same reason the line is. The relay is the only thing
    that reaches a box mid-turn, and it reaches nobody at all when the box is
    still starting, resubscribing, or asleep — yet the message itself is a row,
    and a row a reader wrote and nothing answered is exactly what the next box
    to open the chat picks up and runs. So a stop that lands in the window
    between a message being taken and a machine saying anything about it says
    on the transcript that the message was not sent: the reader sees it, and no
    box starts it later.

    The note's id is derived from the turn, so the same stop is the same note
    wherever it is met: live off the socket, read back off the record, or
    echoed by a mirror repeating what it heard. A second press on the same turn
    writes nothing and returns nothing; a press on a later turn takes an id of
    its own.

    The turn is named by the person's last message, because that is the only
    name for it both ends can read off the same rows — the box mints its turn
    id inside a process the server never sees. Naming it by how far the
    transcript had got instead is what a mid-turn press breaks: the box appends
    a part every few tens of milliseconds, so a second press a moment later
    stood somewhere new and drew a second "Stopped by" line for one stop.
    """
    when = now or datetime.now(UTC)
    # Under the chat's own row lock, so the watermark this reads and the rows
    # it writes cannot straddle a publish: the box's persist step takes the
    # same lock, and without it a message recorded as never sent by this could
    # belong to a turn that had in fact begun. The same lock is what makes the
    # position below a stable reading rather than a guess.
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        return []
    # The turn this Stop is about: the person's last ROW. A message is not the
    # only thing that starts a turn — answering an ask the agent was blocked on
    # restarts one with nothing new typed — and a reader's row is what
    # `answers_a_waiting_message` steps over for exactly that reason: it is the
    # asking, not the reporting. So the two sides agree on where one turn ends
    # and the next begins without spelling the rule twice.
    turn_seq = int(
        (
            await db.execute(
                select(func.max(ChatMessage.seq)).where(
                    ChatMessage.chat_id == chat.id,
                    ChatMessage.role == "user",
                )
            )
        ).scalar_one()
        or 0
    )
    waiting = await unstarted_prompts(db, chat_id=chat.id)
    return await write_entries(
        db,
        chat=locked,
        events=[
            *(prompt_cancelled_for(row, session_id=str(chat.id), now=when) for row in waiting),
            *stopped_turn_entries(
                session_id=str(chat.id),
                note_id=stopped_turn_note_id(turn_seq),
                who=who,
                now=when,
            ),
        ],
    )


async def oldest_seq(db: AsyncSession, chat_id: UUID) -> int:
    """The lowest transcript sequence still held for this chat."""
    return int(
        (
            await db.execute(
                select(func.min(ChatMessage.seq)).where(ChatMessage.chat_id == chat_id)
            )
        ).scalar_one()
        or 0
    )


async def list_messages(
    db: AsyncSession, *, chat_id: UUID, after_seq: int, limit: int
) -> tuple[list[ChatMessage], int, int | None]:
    """``(messages, next_after_seq, resync_from)``.

    ``resync_from`` is the in-band reset marker: when the caller asks
    for messages older than the oldest one still held, the page itself says
    "start again from here" rather than raising an error the client needs a
    branch for. Nothing prunes this table today, so it is quiet until something
    does — which is the point of designing the reset path first.
    """
    after_seq = max(after_seq, 0)
    rows = list(
        (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == chat_id, ChatMessage.seq > after_seq)
                .order_by(ChatMessage.seq)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    oldest = await oldest_seq(db, chat_id)
    resync_from = oldest if oldest and after_seq + 1 < oldest else None
    next_after = rows[-1].seq if rows else after_seq
    return rows, next_after, resync_from


def page_row_of(row: ChatMessage) -> PageRow:
    """One ``chat_messages`` row as the shared page rule sees it.

    Only a ``prompt`` row starts a turn here: the box's own ``message.created``
    for the same message repeats the row, and is repeated again from inside the
    answer every time the session moves on. Every message the transcript names
    counts as a message, whoever published it — the machine's answers, the
    box's echo of the person's message, the notes the server writes into a turn.
    """
    return PageRow(
        seq=row.seq,
        starts_turn=row.kind == USER_MESSAGE_KIND,
        message_id=message_id_for_event(row.payload),
        opens_message=row.kind == MESSAGE_CREATED_KIND,
    )


def message_id_column() -> ColumnElement[str | None]:
    """:func:`message_id_for_event` as SQL, so the page rule can be handed a
    row's message WITHOUT its payload.

    The descent looks over every row inside its budget but SHOWS none of them:
    all it needs per row is the sequence, whether the row starts a turn, and
    which message it belongs to. Reading those three as columns instead of
    hydrating a thousand transcript entries is the difference between a page
    that costs a few milliseconds and one that costs tens of them.

    The Python rule is the definition and this is its one transcription; they
    are held together by a test that runs both over the same real transcript.
    """
    event = ChatMessage.payload["payload"]
    return case(
        (ChatMessage.kind == PROMPT_CANCELLED_KIND, null()),
        else_=func.nullif(
            func.coalesce(
                event["message_id"].astext,
                event["part"]["message_id"].astext,
                ChatMessage.payload["message_id"].astext,
                ChatMessage.payload["part"]["message_id"].astext,
            ),
            "",
        ),
    )


async def _align_page_boundary(
    db: AsyncSession, *, chat_id: UUID, rows: list[ChatMessage], limit: int, oldest: int
) -> tuple[list[ChatMessage], int, bool]:
    """``(rows, low, cut)`` — the page lowered to where the shared rule says it
    may open, and what that rule says the page is admitting to.

    The rule is :func:`alkera_core.objects.transcript_page.align_boundary`,
    which the daemon's local-log pager drives too, so a reader gets the same
    page — and the same ``cut`` — whichever side served it. It is handed the
    whole stretch it may look at in ONE read rather than being walked down a
    step at a time: deciding on the budget and nothing else is what keeps two
    readers of a chat neither can see all of from drifting apart.
    """
    reach = PageReach(
        limit=limit,
        turn=max(settings.chat_page_turn_reach, 1),
        message=max(settings.chat_page_message_reach, 1),
    )
    page_low = rows[0].seq
    floor = max(page_low - reach.budget, oldest)
    below: list[PageRow] = []
    if floor < page_low:
        below = [
            PageRow(
                seq=seq,
                starts_turn=kind == USER_MESSAGE_KIND,
                message_id=message_id,
                opens_message=kind == MESSAGE_CREATED_KIND,
            )
            for seq, kind, message_id in (
                await db.execute(
                    select(ChatMessage.seq, ChatMessage.kind, message_id_column())
                    .where(
                        ChatMessage.chat_id == chat_id,
                        ChatMessage.seq >= floor,
                        ChatMessage.seq < page_low,
                    )
                    .order_by(ChatMessage.seq)
                )
            ).all()
        ]
    answer = align_boundary(
        [*below, *(page_row_of(row) for row in rows)],
        page_low=page_low,
        oldest=oldest,
        reach=reach,
    )
    if answer.seq >= page_low:
        return rows, answer.seq, answer.cut
    # Only now, and only the rows the page actually gained, are read whole.
    gained = (
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.chat_id == chat_id,
                    ChatMessage.seq >= answer.seq,
                    ChatMessage.seq < page_low,
                )
                .order_by(ChatMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    return [*gained, *rows], answer.seq, answer.cut


#: The transcript rows that name a tool call and may carry its input.
_CALL_KINDS: tuple[str, ...] = ("tool.call", "tool.call_update", "part.created", "part.updated")


async def gated_call(
    db: AsyncSession, *, chat_id: UUID, call_ids: Sequence[str], before_seq: int | None = None
) -> tuple[str, dict[str, Any]] | None:
    """``(tool name, input)`` of the call a permission ask gates, read from the
    rows (before ``before_seq``, when given), or ``None`` when the transcript
    never named it.

    An ask may reach the transcript naming only the call it gates -- a harness
    that hosts a tool over MCP raises it with no arguments -- and the call's own
    rows are the only place its input exists. The input is the latest one a
    row carried: a streaming call opens with none and fills it in.
    """
    ids = [call_id for call_id in call_ids if call_id]
    if not ids:
        return None
    event = ChatMessage.payload["payload"]
    query = select(ChatMessage.payload).where(
        ChatMessage.chat_id == chat_id,
        ChatMessage.kind.in_(_CALL_KINDS),
        (
            event["tool_call_id"].astext.in_(ids)
            | event["provider_call_id"].astext.in_(ids)
            | event["part"]["call_id"].astext.in_(ids)
        ),
    )
    if before_seq is not None:
        query = query.where(ChatMessage.seq < before_seq)
    rows = (await db.execute(query.order_by(ChatMessage.seq))).scalars()
    name = ""
    tool_input: dict[str, Any] = {}
    for stored in rows:
        payload = event_payload(stored or {})
        part = payload.get("part")
        source = part if isinstance(part, dict) else payload
        named = source.get("name") if isinstance(part, dict) else payload.get("tool_name")
        if isinstance(named, str) and named:
            name = named
        given = source.get("input")
        if isinstance(given, dict) and given:
            tool_input = given
    if not name and not tool_input:
        return None
    return name, tool_input


async def list_messages_before(
    db: AsyncSession, *, chat_id: UUID, before: int | None, limit: int
) -> tuple[list[ChatMessage], int | None, bool, bool]:
    """``(messages, prev_before, has_older, cut)`` — the page BELOW ``before``
    (exclusive), or the newest page when ``before`` is ``None``.

    The page is the ``limit`` rows nearest ``before``, returned ascending, then
    lowered until its first row opens a turn AND belongs to no message that
    began below it. A reader opening cold holds no page above to repair a cut
    with: a turn shown from its middle is a turn whose opening rows are
    missing, and a message shown from its middle is parts with no message to
    hang them on — which is how a fresh open rendered an answer without its
    thinking part, after the reader sent the next prompt while that answer was
    still being written and the prompt row landed INSIDE it.

    So two reaches run alternately, because neither settles the boundary on
    its own: down to the prompt row a turn began at, within
    ``limit * chat_page_turn_reach`` rows, and down past any message the
    boundary would split, within ``limit * chat_page_message_reach`` rows.
    Lowering onto a prompt row can land inside an answer; lowering past that
    answer lands on the box's echo of the person's message, which begins one
    row UNDER the prompt that turn started at. Only a prompt row starts a
    turn: the box's own ``message.created`` for the same message repeats the
    row, and is repeated again from inside the answer every time the session
    moves on.

    What counts as a message here, for anyone keeping a reader in step with
    this: EVERY message the transcript names, whoever published it — the
    machine's answers, the box's echo of the person's message, the notes the
    server writes into a turn. A row belongs to one by the message id it
    states and by nothing else: not its text, not its timing, not its role.
    The single exception is ``prompt.cancelled``, whose id names the prompt it
    cancels rather than a message it is part of. A reader that FOLDS these
    rows may well narrow that — an echo is the person's turn, not a message of
    its own — but a page that split one would hand it the pieces to fold.

    A read that reached however far down it had to would pull a whole
    ten-thousand-event turn for one page of it, so the descent stops
    ``limit * (chat_page_turn_reach + chat_page_message_reach)`` rows under the
    page — one page is therefore at most ``limit`` plus that, 1400 rows at the
    limit a reader opens a chat on — and past it the page says ``cut``: it
    opens mid-turn, or it carries a message whose opening row it could not
    reach, and the page below carries the rest.
    A message is held whole only when the page holds the row that OPENED it —
    a message whose rows are scattered further apart than this read goes is
    reported, never waved through. ``prev_before`` is the page's lowest
    sequence — the next ``before`` — and ``has_older`` says whether anything
    is held below it, so the page below resumes exactly where this one opens:
    no gap and no repeated row.

    Every read is a range scan of ``uq_chat_messages_chat_seq``: the page is a
    backward scan of at most ``limit`` rows, each turn probe a bounded range
    with a filter, and the descent reads only the rows it has not read
    already — so no second index carries this.
    """
    clauses = [ChatMessage.chat_id == chat_id]
    if before is not None:
        clauses.append(ChatMessage.seq < before)
    rows = list(
        (
            await db.execute(
                select(ChatMessage).where(*clauses).order_by(ChatMessage.seq.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return [], None, False, False
    rows.reverse()
    oldest = await oldest_seq(db, chat_id)
    rows, low, cut = await _align_page_boundary(
        db, chat_id=chat_id, rows=rows, limit=limit, oldest=max(oldest, 1)
    )
    return rows, low, low > oldest, cut


# ---------------------------------------------------------------------------
# The chat document relay
# ---------------------------------------------------------------------------


def channel_key(chat_id: UUID) -> str:
    return f"doc:chat:{chat_id}"


async def broadcast_relay(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    chat_id: UUID,
    events: list[dict[str, Any]],
    actor: dict[str, Any] | None,
) -> None:
    """Put one ``user_message`` relay on the chat's channel.

    A relay changes no state, so it does not move the document's sequence and
    carries none: it rides seq 0 — the unsequenced lane — stamped with the
    document's current epoch, exactly as the socket path stamps one. Given the
    document's current sequence instead it would arrive as an op every
    subscriber has already applied, and an ordering guard would drop it: the
    relay would work on an empty chat and stop working after the machine's
    first append. When no document exists yet nobody is subscribed, and the row
    is harmless.
    """
    doc = await db.get(RealtimeDoc, (org_team_id, "chat", str(chat_id)))
    await emit(
        db,
        org_id=org_team_id,
        type=EventType.DOC_OP,
        entity=Entity.DOC,
        entity_id=channel_key(chat_id),
        version=doc.seq if doc else 0,
        payload=_relay_payload(chat_id=chat_id, doc=doc, events=events),
        actor=actor,
    )


async def announce_chat(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    actor: dict[str, Any] | None,
    access_changed: bool = False,
) -> None:
    """The chat list's invalidation: a doorbell naming the chat, no content.

    ``access_changed`` says this is not an ordinary edit: the chat came or went
    (deleted, trashed, restored), so anyone watching it is decided again; a
    flag on the same doorbell, so a viewer's socket pays a re-decision here and
    not for every title change and streamed turn.
    """
    payload: dict[str, Any] = {
        "team_id": str(chat.team_id) if chat.team_id else None,
        # The box that runs the chat (the announcement that binds a chat is the
        # first frame it must hear), which reads the reason as the deletion.
        BOUND_MACHINE_KEY: chat_spec_of(chat).machine_id,
        **({"reason": CHAT_DELETED_REASON} if chat.deleted_at else {}),
    }
    if access_changed:
        payload[ACCESS_CHANGED_KEY] = True
    await emit(
        db,
        org_id=chat.org_team_id,
        type=EventType.CHAT_UPDATED,
        entity=Entity.CHAT,
        entity_id=str(chat.id),
        version=chat.version,
        payload=payload,
        actor=actor,
    )


async def delete_chat(
    db: AsyncSession, *, chat: WorkspaceObject, actor: dict[str, Any] | None = None
) -> WorkspaceObject:
    """Tombstone the chat: ``deleted_at`` is stamped, the row and its transcript
    stay. Every reader — the list, ``find_chat_by_client_id``, the socket's
    lookup — already hides a tombstone, so nothing else has to learn the chat
    is gone; the caller announces and commits.

    A deletion is an ending first: the chat is put to sleep through the one
    transition every ending takes — its folder's lease ended, its box told —
    and only then trashed, so the trash never meets a live lease.
    """
    await chat_end.end_chat(
        db, chat.id, chat_end.ChatEndReason.DELETED, actor=actor, announce=False
    )
    await object_service.tombstone(db, chat)
    # The workspace of one that IS this chat goes with it; a workspace that
    # holds other chats keeps standing.
    await workspaces.tombstone_adopted_for_chat(db, chat_id=chat.id)
    return chat


async def set_publisher_state(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    state: str,
    reason: str = "",
    actor: Mapping[str, Any] | None = None,
    kind: ChatRefusalKind | None = None,
) -> WorkspaceObject:
    """Record what the chat's machine says about it (``apply_publisher_report``
    has the rule). A derived flag like ``last_seq``: it does not bump
    ``version``, so nobody editing the chat has lost a race because its machine
    spoke. Returns the locked row; the caller announces and commits.

    A box whose refusal is final (``alkera_core.chat_refusals``) will not
    finish the chat's turn either, so that refusal ends the running turn for
    every reader and says why. A wait, or a refusal of no known kind, leaves
    the turn alone: the box tries again on its own and may still finish it.

    Locked the way every writer to a chat locks it (:func:`lock_chat_for_write`):
    a report that ends the chat goes on to touch its document, and taking the
    chat first would hold what a socket's op is waiting for while waiting for
    the document that op holds."""
    ends_turn = state == "refused" and refusal_is_final(kind)
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    spec = apply_publisher_report(locked.spec, state=state, reason=reason, now=now_iso(), kind=kind)
    if spec != locked.spec:
        locked.spec = spec
        await db.flush()
    if ends_turn:
        await chat_turn.end_turn_for(db, locked, reason=chat_turn.REFUSED, actor=actor)
    return locked


async def request_wake(db: AsyncSession, *, chat: WorkspaceObject) -> bool:
    """A reader opened a chat that is asleep: stamp the wake on the row so the
    box's discovery — which reads the row — re-takes it on its next pass.
    Once per sleep: a request already standing is left as it is, and the
    caller announces nothing. Returns whether anything was written."""
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    if locked.spec.get("mirror_state") != "asleep" or locked.spec.get("wake_requested_at"):
        return False
    spec = dict(locked.spec)
    spec["wake_requested_at"] = now_iso()
    locked.spec = spec
    await db.flush()
    return True


async def rebind_machine(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    machine_id: str | None,
    machine_status: MachineStatus,
) -> WorkspaceObject:
    """Move a chat onto a different machine, or off every machine.

    The binding was made once, at creation, which was right while a box lived
    for as long as the org did. It is wrong now: a box is a thing that sleeps,
    is replaced, or simply dies, and a chat whose machine is gone was stranded
    reading ``none`` forever — no box serves a chat bound to another machine, so
    nothing would ever pick it up again however many boxes the org had.

    A derived fact like ``last_seq`` and ``publisher_refusal``: it does not bump
    ``version``, because moving a chat to a live box is not an edit anybody can
    lose a race to. The publisher refusal is cleared with the move — it was the
    OLD machine's reason, and leaving it would light a banner about a box that
    is no longer serving this chat. So is the old box's word on the chat's
    session (``mirror_state``): a chat that box parked would otherwise read
    ``asleep`` on a ready box that never held it, and be counted as parked
    there. A standing wake stays — it says a reader is waiting, which the next
    box needs to know. Returns the locked row; the caller announces and
    commits.
    """
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    spec = dict(locked.spec)
    if (spec.get("machine_id") or None) == (machine_id or None):
        return locked
    spec["machine_id"] = machine_id
    spec["machine_status"] = machine_status
    clear_refusal(spec)
    spec.pop("mirror_state", None)
    locked.spec = spec
    await db.flush()
    return locked


async def set_permission_mode(
    db: AsyncSession, *, chat: WorkspaceObject, mode: CloudPermissionMode
) -> WorkspaceObject:
    """Record the stance the chat's session runs in. Returns the locked row.

    A derived setting like ``last_seq`` and ``publisher_refusal``: it does not
    bump ``version``, so a reader switching the mode has not made whoever is
    editing the chat lose a race. The caller announces, relays and commits.
    """
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    spec = dict(locked.spec)
    spec["permission_mode"] = mode
    locked.spec = spec
    await db.flush()
    return locked


async def record_mode_change(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    previous_mode: str | None,
    mode: str,
    by: User | None,
    via: Literal["web", "slack"] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Write a permission-mode change onto the transcript; the entry, or
    ``None`` when the mode did not change (nothing happened to show).

    The caller publishes the returned entry live (``publish_server_events``)
    so an open browser folds it at once; the row written here is what a page
    read over REST, and the Slack relay, read.
    """
    if previous_mode == mode:
        return None
    entry = mode_changed_entry(
        ModeChangeRecord(
            event_id=aside_note_id(f"mode-{uuid4().hex}"),
            time=now or datetime.now(UTC),
            session_id=str(chat.id),
            mode=mode,
            previous_mode=previous_mode,
            decided_by_user_id=str(by.id) if by is not None else None,
            decided_by_name=(by.display_name or None) if by is not None else None,
            decided_via=via,
        )
    )
    await persist_published_events(db, chat=chat, events=[entry])
    return entry


async def set_model(
    db: AsyncSession, *, chat: WorkspaceObject, pin: ChatModelPin
) -> WorkspaceObject:
    """Record the model this chat's session runs on. Returns the locked row.

    The pin is the durable answer, the same way the permission mode is: the box
    reads it off the row when it opens the session, so a chat resumed on a fresh
    machine comes back on the model the reader picked rather than on whatever
    the box would have defaulted to. Like the mode it does not bump ``version``
    — switching the model is not an edit of the chat's content, so it cannot
    make whoever is renaming the chat lose a race.

    The caller has already resolved the pick against the catalog; this writes
    what it resolved, and announces, relays and commits.
    """
    locked = await lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    if locked is None:
        raise LookupError("the chat is gone")
    spec = dict(locked.spec)
    spec["model"] = pin.model_dump(mode="json")
    locked.spec = spec
    await db.flush()
    return locked


def chat_spec_of(chat: WorkspaceObject) -> ChatSpec:
    """``chat.spec`` as a :class:`ChatSpec`, whatever a past writer left."""
    return ChatSpec.model_validate(chat.spec or {})


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "CLIENT_RELAY_KINDS",
    "MAX_ATTACHMENT_POSITION",
    "MAX_STOPPED_PROMPTS",
    "SERVER_RELAY_KINDS",
    "TITLE_MAX_CHARS",
    "USER_MESSAGE_KIND",
    "AnswerDoesNotFitError",
    "AskAlreadyAnsweredError",
    "AskNotFoundError",
    "AttachmentCursor",
    "ChatActivity",
    "ChatMessageTooLargeError",
    "ClientIdTakenError",
    "RelayKind",
    "TooManyAttachmentsError",
    "TranscriptAsk",
    "announce_chat",
    "answers_a_waiting_message_clause",
    "append_user_message",
    "attachment_count",
    "attachment_counts",
    "broadcast_relay",
    "channel_key",
    "chat_activity",
    "chat_spec_of",
    "check_user_message_fits",
    "create_chat",
    "delete_chat",
    "event_id_for_client_message",
    "find_ask",
    "find_chat_by_client_id",
    "gated_call",
    "kind_for_event",
    "linked_among",
    "linked_attachments",
    "list_messages",
    "lock_chat_for_write",
    "message_recorded",
    "now_iso",
    "oldest_seq",
    "persist_published_events",
    "record_attachment",
    "record_interrupt_answer",
    "record_mode_change",
    "recorded_sequences",
    "request_wake",
    "role_for_event",
    "set_publisher_state",
    "submit_user_message",
    "title_from_prompt",
    "unstarted_prompts",
    "without_decider_claim",
]
