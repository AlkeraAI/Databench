"""Document synchronisation: one strategy per document type, one registry
that applies operations under a row lock and announces them.

The server is the single writer of a document's ``epoch`` and ``seq``. An
operation is applied inside ONE transaction: lock the row, check the epoch
the peer speaks about, run the strategy (pure: state in, state and effect
out), persist any side effect outside ``realtime_docs`` (a registered kind
writes through its own domain's service, so every rule that guards a REST
edit guards a live one), bump ``seq``, and write two outbox rows — the ``doc.op``
that every replica rebroadcasts to its subscribers, and the domain event the
portal invalidates on. A rollback leaves no trace of any of it.

A stale epoch is never applied and never silently ignored: the peer is told
the current epoch and re-``hello``s for a fresh snapshot. A rebuild — a
publisher snapshot, or a source of truth the REST path edited behind the
document's back — bumps the epoch, resets ``seq`` and broadcasts ``reload``.

The row is the authority on who may touch it. Every entry point locks (or, for
the ephemeral lane, reads) the row and judges it as it stands: a row of
another org, or one narrowed to a team the caller is not on, is ``not_found``
whatever the caller's grant says, and a write is allowed only to the row's
publisher — its ``owner_user_id`` (for a chat in a workspace of several, the
workspace's owner), or the machine the chat's declaration binds it to when the
socket was opened as that machine (``publishes``; for a chat the rule is
:func:`backend.services.realtime.channels.chat_standing`, the one the
subscribe applies). The grant a socket caches at
subscribe time names the channel and its audience; it is never what decides.

For that to be true the row has to be true. A ``chat``'s audience is written
on the row and nowhere else, so it always is. A registered kind whose source
of truth lives elsewhere (a knowledge item's visibility scope, which a REST
edit moves without the document being told) leaves the row only a copy, and
every ``ensure`` refreshes that copy from the source (``current_scope``)
before judging it. The row still decides; it just stops deciding from a stamp
the source has outgrown.

A document is looked up, and created, within one org: the org is half its key,
because a chat id is chosen by the client and two tenants may name the same
one. That same client-chosen id is why creation is bounded — an org holds at
most ``REALTIME_DOCS_MAX_PER_ORG`` documents of a kind, counted and inserted
under one advisory lock, and a hello past the ceiling is refused in band as
``quota_exceeded`` while every document that exists keeps working.

Strategies: a registered kind brings its own
(:mod:`backend.services.realtime.doc_kinds`); the socket's own is

* ``chat`` — ``{"meta", "events", "ids"}``; ``append`` de-duplicates by
  ``event_id``, refuses event types the chat log never persists, writes each
  accepted event to ``chat_messages`` (the transcript's system of record) and
  keeps the state a bounded live window over it; ``set_meta`` merges;
  ``user_message`` is a relay — a ``prompt`` into a chat that exists is a
  person speaking, admitted by the same SEND decision the REST route makes
  (``chat.access``, verified email included, on record) and then recorded as a
  transcript entry and rebroadcast with the id and sequence the server gave
  it; a server-minted kind from a peer is refused; anything else travels
  unchanged (the publisher receives it on whichever replica it is on).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from alkera_core.authz import (
    ActingContext,
    Decision,
)
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, advisory_key, advisory_xact_lock, lock_rows
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import MAX_PAYLOAD_BYTES, Entity, EventType, emit
from alkera_core.events.outbox import payload_size, unstorable_number
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.ladder import DEFAULT_LADDER
from alkera_core.models import RealtimeDoc, User, WorkspaceObject
from alkera_core.objects import chat_turn
from alkera_core.objects.chat_transcript import (
    compact_window,
    kind_for_event,
    publish_server_entries,
    recorded_sequences,
    role_for_event,
    stamp_sequences,
    state_size,
    window_threshold,
)
from alkera_core.schemas.chat import NON_PERSISTED_EVENT_TYPES
from alkera_core.schemas.objects import PromptRelay
from alkera_core.schemas.objects.transcript import SERVER_ONLY_KINDS, reads_as_prompt
from alkera_core.schemas.realtime import (
    SERVER_PEER_ID,
    DocEnvelope,
    OpPayload,
    ReloadPayload,
)
from pydantic import ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.authz import OutboxDecisionSink
from backend.services.chats import ChatMessageTooLargeError, chat_service, send_decision
from backend.services.realtime import channels as channel_rules
from backend.services.realtime.channels import ChannelGrant, ChatStanding, doc_readable
from backend.services.realtime.doc_kinds import registered_strategy
from backend.services.realtime.filters import EntitlementSnapshot

RejectCode = Literal[
    "forbidden",
    "blocked",
    "unknown_field",
    "invalid_field",
    "doc_too_large",
    "op_too_large",
    "unsupported_kind",
    "not_found",
    "bad_op",
    "quota_exceeded",
    "email_verification_required",
    "draft_moved",
]

logger = structlog.get_logger(__name__)

DOMAIN_ANNOUNCE_INTERVAL_SECONDS = 5.0
"""How often one document's live changes are announced to the org at most.

Live edits reach the people watching the document through ``doc.op``; the
domain event exists for everyone who is NOT watching, and it costs every
portal in the org a refetch of a whole query prefix. A typist emits an
operation every few hundred milliseconds, so the announcements are throttled
to one per document per window."""
ANNOUNCE_TRACKING_CAP = 4096
"""How many documents' announcement windows one process remembers, counting a
document of each tenant separately (a chat id is the client's to choose, so one
name can belong to several orgs)."""
REBUILD_REASON_REST_EDIT = "rest_edit"
REBUILD_REASON_PUBLISHER = "publisher_snapshot"
REBUILD_REASON_SCHEMA = "schema_upgrade"
#: The key every persisted state names its shape's version under, so a row an
#: older writer left behind is recognised and brought forward on the next
#: read rather than misread. Per strategy: see ``STATE_SCHEMA_VERSION``.
STATE_SCHEMA_VERSION_KEY = "schema_version"

#: The intents that rewrite what the document IS — the transcript itself — and
#: are therefore the publisher's alone, whatever rung a grant hands out. A
#: colleague shared the chat at ``writer`` may steer the live document; nobody
#: but the owner and the machine serving the chat may rewrite its history, so a
#: share can never amount to discarding a chat's transcript.
PUBLISHER_ONLY_INTENTS: frozenset[str] = frozenset({"append"})
#: The intents no write gate judges: a ``user_message`` is a person speaking,
#: relayed to the publisher rather than written by the sender, and it meets the
#: SEND policy (``chat.access``) that ``POST /chats/{id}/messages`` meets —
#: one message through two doors, one decision. Narrowing SEND by rung is a
#: change to THAT policy, so that both doors move together; doing it here alone
#: would make the socket and the route disagree about who may speak.
RELAYED_INTENTS: frozenset[str] = frozenset({"user_message"})
#: What every other intent needs: the ladder's WRITE, which ``writer`` and up
#: allow and ``reader``/``commenter`` do not.
DOC_WRITE_ACTION = FilesAction.WRITE

#: The meta key a chat's shared composer draft rode before it moved to the Loro
#: lane (``doc:chat_draft:<chat>``). A ``set_meta`` that writes it is refused
#: as ``draft_moved``; a value already stored is kept, and read once as the
#: first text of the chat's live draft.
DRAFT_META_KEY = "draft"

#: The meta keys a RUNNING TURN restamps itself under. The box says "still
#: working" every fifteen seconds for as long as a turn lasts — it is the only
#: thing on the wire that tells a reader a quiet box is thinking rather than
#: gone — and between two of those nothing changes but the instant.
TURN_STATE_META_KEY = chat_turn.TURN_STATE_META_KEY
TURN_STATE_AT_META_KEY = chat_turn.TURN_STATE_AT_META_KEY
STAMP_META_KEYS: frozenset[str] = frozenset({TURN_STATE_META_KEY, TURN_STATE_AT_META_KEY})
#: The effect key a restamp leaves behind, so the registry writes the columns
#: and fans the frame out without touching the document's state.
TURN_STAMP_EFFECT = "turn_stamp"
#: What the two columns hold. A stamp longer than this is not a stamp.
MAX_TURN_STATE = 32
MAX_TURN_STATE_AT = 64


def _stamp_only(current: Mapping[str, Any], writes: Mapping[str, Any]) -> dict[str, str] | None:
    """The restamp this ``set_meta`` is, or ``None`` if it changes anything else.

    A restamp is the turn that is already standing saying again that it is
    still running: the same state word, the same budget, a later instant. Only
    then may the document be left alone — a turn that STARTED, ended, or
    changed what it reports is a real change to what the chat says, and goes
    the ordinary way through state, sequence and compaction.
    """
    if not writes or not set(writes) <= STAMP_META_KEYS:
        return None
    proposed = writes.get(TURN_STATE_META_KEY)
    standing = current.get(TURN_STATE_META_KEY)
    if not isinstance(proposed, dict) or not isinstance(standing, dict):
        return None
    if {key: value for key, value in proposed.items() if key != "at"} != {
        key: value for key, value in standing.items() if key != "at"
    }:
        return None
    at = proposed.get("at")
    word = proposed.get("state")
    if not isinstance(at, str) or not isinstance(word, str) or not at or not word:
        return None
    if len(word) > MAX_TURN_STATE or len(at) > MAX_TURN_STATE_AT:
        return None
    return {"state": word, "at": at}


def stamped_state(doc: RealtimeDoc) -> dict[str, Any]:
    """``doc.state`` with the turn's own columns folded back over its meta.

    The restamp writes the columns and leaves the blob alone, so a reader that
    took the blob verbatim would see the instant the turn last CHANGED rather
    than the instant it last SPOKE — a turn running for an hour would read as
    one that died an hour ago. The fold is skipped when the columns describe a
    different turn than the blob does: the blob is the authority on what the
    turn IS, the columns only on when it last said so.
    """
    state = dict(doc.state)
    if not doc.turn_state_at:
        return state
    meta = state.get("meta")
    turn = meta.get(TURN_STATE_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(meta, dict) or not isinstance(turn, dict):
        return state
    if turn.get("state") != doc.turn_state:
        return state
    state["meta"] = {
        **meta,
        TURN_STATE_META_KEY: {**turn, "at": doc.turn_state_at},
        TURN_STATE_AT_META_KEY: doc.turn_state_at,
    }
    return state


class StaleEpochError(Exception):
    """The peer spoke about an epoch that is no longer current."""

    def __init__(self, current_epoch: int) -> None:
        super().__init__(f"stale epoch; the document is at epoch {current_epoch}")
        self.current_epoch = current_epoch


class DocOpRejectedError(Exception):
    """An operation refused; ``code`` is what the peer is told."""

    def __init__(self, code: RejectCode, message: str = "") -> None:
        super().__init__(message or code)
        self.code: RejectCode = code
        self.message = message or code


@dataclass(frozen=True, slots=True)
class AppliedOp:
    """What ``apply_op`` settled: the envelope every other subscriber receives
    (server ``seq`` and ``epoch`` stamped) and what the sender is told."""

    envelope: DocEnvelope
    seq: int
    epoch: int
    changed: bool
    effect: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DocScope:
    """Who a document is for, as its source of truth says NOW: the team it is
    narrowed to (``None`` for the whole org) and the user who owns it."""

    team_id: UUID | None
    owner_user_id: UUID | None


@dataclass(frozen=True, slots=True)
class DocSnapshot:
    doc_type: str
    doc_id: str
    epoch: int
    seq: int
    state: dict[str, Any]
    owner_user_id: UUID | None
    team_id: UUID | None
    #: The registry's verdict on the caller against the row it served: whether
    #: THIS socket may write the document (the write predicate, not the
    #: subscribe's guess).
    can_write: bool = False


def check_size(state: Mapping[str, Any]) -> None:
    size = state_size(state)
    if size > settings.realtime_doc_max_bytes:
        raise DocOpRejectedError(
            "doc_too_large",
            f"the document would be {size} bytes; the cap is {settings.realtime_doc_max_bytes}",
        )


def _check_storable(payload: Mapping[str, Any], what: str) -> None:
    """Refuse a document Postgres could not store.

    ``NaN`` and the infinities are not JSON, but the readers between a client
    and this line accept their literals and the writers emit them again, so one
    travels from a frame into the row's state and into the ``doc.op`` payload —
    where ``jsonb`` refuses the whole document, taking the transaction and the
    socket down with it. A number the store cannot hold is a bad operation, and
    it is answered in band like every other one, before anything moves."""
    number = unstorable_number(payload)
    if number is not None:
        raise DocOpRejectedError("bad_op", f"{what} carries {number!r}, which cannot be stored")


@dataclass(frozen=True, slots=True)
class MachineSubject:
    """A box on its own machine credential, as the registry judges it.

    Nobody is behind it: it owns no document, holds no rung, has no team and
    no draft to sign. What it may do is exactly the publisher's work on the
    chats bound to its machine — the binding is read at every write, as it is
    for a box on its operator's session — inside the orgs its credential
    serves. Everything a person does through the registry that is not the
    publisher's (relay a message, write a draft, open an artifact) is refused
    for it outright.
    """

    ctx: ActingContext

    @property
    def machine_id(self) -> str:
        return self.ctx.acting_principal.id


#: Who a registry call is made for: a person (their live ``users`` row), or a
#: box on its own machine credential.
Subject = User | MachineSubject


def _user_id(subject: Subject) -> UUID | None:
    """The person a subject is, or ``None`` for a machine."""
    return None if isinstance(subject, MachineSubject) else subject.id


class DocStrategy(Protocol):
    """The type-specific half. ``apply`` is pure; ``persist`` and ``seed`` do
    the I/O outside ``realtime_docs``."""

    domain_event: EventType
    domain_entity: Entity
    #: Whether a peer's ``snapshot`` may replace the state. A chat's source of
    #: truth is its publisher; an artifact's is its knowledge item, so it is
    #: rebuilt from the item on a REST edit and never from a peer.
    publisher_may_rebuild: bool
    #: The version of the state shape this strategy writes; stamped on every
    #: state it seeds or rebuilds, checked on every row it reads.
    STATE_SCHEMA_VERSION: str

    def apply(
        self, state: dict[str, Any], op: OpPayload, *, peer_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], bool]: ...

    async def migrate_state(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc
    ) -> dict[str, Any] | None:
        """``doc.state`` brought forward to ``STATE_SCHEMA_VERSION``, or
        ``None`` when this strategy cannot read the version it names."""
        ...

    async def persist(
        self,
        db: AsyncSession,
        *,
        user: Subject,
        doc: RealtimeDoc,
        state: dict[str, Any],
        effect: dict[str, Any],
        announce: bool,
    ) -> dict[str, Any]:
        """Write the change outside ``realtime_docs``. ``announce`` is the
        registry's coalescing decision for this operation: when it is false the
        side effect must not put a domain event on the outbox — the operation
        rides the ``doc.op`` rebroadcast alone."""
        ...

    async def seed(
        self, db: AsyncSession, *, user: Subject, grant: ChannelGrant
    ) -> dict[str, Any] | None: ...

    async def fresh_state_if_moved(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc
    ) -> dict[str, Any] | None: ...

    async def current_scope(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc
    ) -> DocScope | None:
        """The team and owner this document's source of truth gives it now, or
        ``None`` when the row itself is that source and nothing outside it may
        move the audience."""
        ...

    async def relay(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc, op: OpPayload
    ) -> list[dict[str, Any]] | None:
        """Handle a relayed operation — one that changes no state but has to
        reach every peer.

        Returns the events the rebroadcast should carry when handling changed
        them (a person's message gains the id and sequence the server gave it),
        or ``None`` to rebroadcast what arrived. Raises
        :class:`DocOpRejectedError` for a relay this document does not accept.
        """
        ...

    def carry_forward(
        self, previous: dict[str, Any], replacement: dict[str, Any]
    ) -> dict[str, Any]:
        """What survives a rebuild that replaces the state wholesale.

        A rebuild is the source of truth speaking again — a publisher's
        snapshot, a REST edit, a schema upgrade — and it knows only what it
        owns. Anything the document holds that the source has never heard of
        would otherwise be dropped on the floor by a resume, so a strategy
        names it here and the registry folds it back in. Returning
        ``replacement`` unchanged is the correct answer for a document whose
        source knows everything about it.
        """
        ...


def _reject_code_for(decision: Decision) -> RejectCode:
    """How the socket spells a policy's refusal: the opaque not-found when the
    decision is one, the policy's own error code when it names one (the REST
    route puts the same code in its 403 body), ``forbidden`` otherwise."""
    if decision.as_not_found:
        return "not_found"
    if decision.error_code == chat_policy.VERIFY_EMAIL_CODE:
        return "email_verification_required"
    return "forbidden"


async def _admit_send(db: AsyncSession, *, chat: WorkspaceObject, user: Subject) -> None:
    """The socket's SEND gate, which is the REST route's.

    ``POST /chats/{id}/messages`` and a ``prompt`` relayed over the chat
    document are one message through two doors, so they meet one policy
    (``chat.access``) over the same facts, resolved the same way: roles at the
    org root, memberships, and a verified email. The decision goes on record
    like the route's — the allow in the operation's own transaction, the deny
    in a committed session of its own, so it survives the refused operation's
    rollback exactly as a refused ``POST`` does.
    """
    if isinstance(user, MachineSubject):
        # A message drives the agent and is billed to the chat's owner; a box
        # relays for nobody. Refused before any fact is resolved: there is no
        # person whose facts they would be.
        raise DocOpRejectedError("forbidden", "a machine relays no message")
    decided = await send_decision(db, chat=chat, user=user)
    decision, event = decided.decision, decided.event("WS", chat_service.channel_key(chat.id))
    sink = OutboxDecisionSink()
    if decision.allowed:
        await sink.record_allow(db, event)
        return
    await sink.record_deny(db, event)
    raise DocOpRejectedError(_reject_code_for(decision), decision.message)


async def _chat_row(db: AsyncSession, doc: RealtimeDoc) -> WorkspaceObject | None:
    """The workspace object a chat document is named after, trashed or not."""
    try:
        chat_id = UUID(doc.doc_id)
    except ValueError:
        return None
    return (
        await db.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.id == chat_id,
                WorkspaceObject.org_team_id == doc.org_id,
                WorkspaceObject.type == "chat",
            )
        )
    ).scalar_one_or_none()


async def _chat_object(db: AsyncSession, doc: RealtimeDoc) -> WorkspaceObject | None:
    """The workspace object behind a chat document, when there is one.

    A chat created through the REST surface has one and its transcript is
    durable. A document whose id was minted somewhere else (an older session
    id, a test peer) has none, and its state is the only record it gets — the
    window keeps working, nothing is written, and nothing fails.
    """
    chat = await _chat_row(db, doc)
    return None if chat is None or chat.deleted_at != 0 else chat


async def _chat_object_for_write(db: AsyncSession, doc: RealtimeDoc) -> WorkspaceObject | None:
    """The chat a write may land in, or a refusal when it is in the trash.

    Trashing a chat marks its object and leaves the document standing, so both
    write doors used to read the tombstoned chat as the no-object case meant
    for a document nobody's row backs — and that case exists to let a document
    with no transcript keep working, which is exactly the wrong answer here.
    An append was rebroadcast to everyone watching with no row and no sequence
    behind it, so the frame named a transcript entry a reader paging the chat
    could never find; a relay skipped the SEND decision altogether, because
    there was no chat to decide against. Both are a frame the durable log does
    not hold, and a chat that is gone is the one case where the honest answer
    is to write nothing and send nothing: ``not_found``, the same thing the
    reader is told about any chat it may not see.
    """
    chat = await _chat_row(db, doc)
    if chat is not None and chat.deleted_at != 0:
        raise DocOpRejectedError("not_found")
    return chat


class ChatAppendById:
    """A chat transcript: append-by-id, a merged meta map, relayed messages."""

    domain_event = EventType.CHAT_UPDATED
    domain_entity = Entity.CHAT
    publisher_may_rebuild = True
    STATE_SCHEMA_VERSION = "1.0.0"

    def apply(
        self, state: dict[str, Any], op: OpPayload, *, peer_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        if op.intent == "append":
            # Only this server writes under its own peer id, and it is the one
            # writer that may say which member decided an ask.
            return self._append(state, op, from_server=peer_id == SERVER_PEER_ID)
        if op.intent == "set_meta":
            current = state.get("meta") or {}
            writes = dict(op.meta)
            if DRAFT_META_KEY in writes:
                raise DocOpRejectedError(
                    "draft_moved", "this chat's draft is shared live; reload to keep typing"
                )
            stamp = _stamp_only(current, writes)
            if stamp is not None:
                # The state is handed back UNCHANGED and ``changed`` is False,
                # so nothing persists the document: the registry writes the two
                # columns and rebroadcasts the frame. Viewers still hear it —
                # that is the whole point of the beat — they just stop paying
                # for a transcript rewrite to be told the turn is still alive.
                return dict(state), {TURN_STAMP_EFFECT: stamp, "meta_keys": sorted(op.meta)}, False
            meta = {**current, **writes}
            changed = meta != current
            new_state = {**state, "meta": meta}
            check_size(new_state)
            return new_state, {"meta_keys": sorted(op.meta)}, changed
        if op.intent == "user_message":
            if not isinstance(op.events, list) or not op.events:
                raise DocOpRejectedError("bad_op", "a user_message carries at least one event")
            return dict(state), {"relay": True}, False
        if op.intent == "chunk":
            raise DocOpRejectedError("unsupported_kind", "chunk ops ride the ephemeral lane")
        raise DocOpRejectedError("unsupported_kind", f"intent {op.intent!r} is not valid on a chat")

    @staticmethod
    def _append(
        state: dict[str, Any], op: OpPayload, *, from_server: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        events: list[dict[str, Any]] = list(state.get("events") or [])
        ids: dict[str, int] = dict(state.get("ids") or {})
        appended: list[str] = []
        # Every id the operation NAMED, whether or not the window took it: the
        # rebroadcast carries them all, so the sequences a viewer is told have
        # to cover them all too.
        named: list[str] = []
        for event in op.events:
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise DocOpRejectedError("bad_op", "every appended event needs a string event_id")
            # The kind reads through the publisher's envelope as well as a flat
            # event, so this refusal actually fires on what a machine sends.
            kind = kind_for_event(event)
            if kind in NON_PERSISTED_EVENT_TYPES:
                raise DocOpRejectedError("unsupported_kind", f"{kind} is never persisted")
            if kind in SERVER_ONLY_KINDS and not from_server:
                # A mode change names the member who made it; only the server,
                # which knows the roster, may write one.
                raise DocOpRejectedError("forbidden", f"{kind} is written by the server only")
            if reads_as_prompt(role=role_for_event(event), kind=kind) and not from_server:
                # A person's message is recorded by the server, which stamps
                # who sent it. A row that reads back as one is what a box's
                # catch-up runs as the member it names, so a peer publishing
                # one would put words in that member's mouth.
                raise DocOpRejectedError("forbidden", "a person's message is written by the server")
            named.append(event_id)
            if event_id in ids:
                continue
            ids[event_id] = len(events)
            # A resolution a PEER publishes says its harness settled an ask. It
            # does not say which MEMBER decided — a reader's surface renders
            # that as the name of the person who approved a write, so it is the
            # server's to write and nobody else's to claim. The server writes
            # its own resolution through this same door (`publish_server_events`,
            # peer id `SERVER_PEER_ID`) and is the one writer whose claim
            # stands; the other machine door is the publisher's rebuild, which
            # `carry_forward` cleans because it never comes through here.
            admitted = dict(event if from_server else chat_service.without_decider_claim(event))
            events.append(admitted)
            appended.append(event_id)
        new_state = {**state, "events": events, "ids": ids}
        check_size(new_state)
        # What is rebroadcast is what was STORED, never what arrived. Other
        # viewers fold the FRAME, not the window, so a claim cleaned out of the
        # state and left on the wire is the same forgery reaching the same
        # screens by the shorter route — and on a reader that keeps the first
        # name it is given, an unstoppable one.
        admitted_events = [
            dict(event if from_server else chat_service.without_decider_claim(event))
            for event in op.events
        ]
        return (
            new_state,
            {"appended": appended, "named": named, "admitted": admitted_events},
            bool(appended),
        )

    async def persist(
        self,
        db: AsyncSession,
        *,
        user: Subject,
        doc: RealtimeDoc,
        state: dict[str, Any],
        effect: dict[str, Any],
        announce: bool,
    ) -> dict[str, Any]:
        """Write the appended events to the transcript, then trim the window.

        The order matters: ``chat_messages`` is the system of record and the
        document's state is a live window over it, so the durable write
        happens first and the window is compacted afterwards. An event dropped
        from the window is still in the table, and still pages over REST.
        """
        chat = await _chat_object_for_write(db, doc)
        if chat is not None:
            appended = {
                str(event_id)
                for event_id in effect.get("appended", [])
                if isinstance(event_id, str)
            }
            events = [
                event
                for event in state.get("events") or []
                if isinstance(event, dict) and event.get("event_id") in appended
            ]
            try:
                written = await chat_service.persist_published_events(db, chat=chat, events=events)
            except ChatMessageTooLargeError as exc:
                raise DocOpRejectedError("op_too_large", str(exc)) from exc
            sequences = {row.event_id: row.seq for row in written}
            # An id the write did not return is one the transcript already
            # holds — a publisher republishing after a reconnect. It has a
            # sequence, and the rebroadcast carries the entry either way, so
            # the sequence is read back rather than left off: one unnameable
            # row is enough to pin a reader's window open for the whole tab.
            sequences.update(
                await recorded_sequences(
                    db,
                    chat=chat,
                    event_ids=[
                        event_id
                        for event_id in effect.get("named", ())
                        if isinstance(event_id, str) and event_id not in sequences
                    ],
                )
            )
            effect["sequences"] = sequences
            state = stamp_sequences(state, sequences)
        return compact_window(state)

    def carry_forward(
        self, previous: dict[str, Any], replacement: dict[str, Any]
    ) -> dict[str, Any]:
        """A draft stored before the live lane survives a publisher's
        snapshot, and no snapshot may say which member decided an ask.

        A chat that has been asleep comes back on a fresh machine, and the
        first thing that machine does is republish the transcript it restored
        — a rebuild, which replaces the state wholesale. A ``meta.draft`` left
        from before the Loro lane is what the chat's live draft starts from
        when it is first opened there, and it is not the machine's to know
        about, so it is folded back in rather than lost to a resume. A
        replacement that names a draft of its own wins.

        The same rebuild is the OTHER way a machine's words reach every reader,
        and the one that does not go through ``apply``: the state it hands over
        becomes the window each browser opens on, whole. So the claim about who
        decided is taken off every event in it here, for the row's owner and
        the bound machine alike — a rebuild is a machine restoring a transcript
        it read back off disk, and a row it read is not evidence about a
        person however it got there.
        """
        replacement = self._without_decider_claims(replacement)
        draft = (previous.get("meta") or {}).get(DRAFT_META_KEY)
        if draft is None:
            return replacement
        meta = replacement.get("meta") or {}
        if DRAFT_META_KEY in meta:
            return replacement
        return {**replacement, "meta": {**meta, DRAFT_META_KEY: draft}}

    @staticmethod
    def _without_decider_claims(state: dict[str, Any]) -> dict[str, Any]:
        """``state`` with every event's decider claim stripped, or ``state``
        itself when no event carried one."""
        events = state.get("events")
        if not isinstance(events, list):
            return state
        cleaned = [
            chat_service.without_decider_claim(event) if isinstance(event, dict) else event
            for event in events
        ]
        if all(new is old for new, old in zip(cleaned, events, strict=True)):
            return state
        return {**state, "events": cleaned}

    async def seed(
        self, db: AsyncSession, *, user: Subject, grant: ChannelGrant
    ) -> dict[str, Any] | None:
        return {
            STATE_SCHEMA_VERSION_KEY: self.STATE_SCHEMA_VERSION,
            "meta": {"session_id": grant.channel.doc_id},
            "events": [],
            "ids": {},
        }

    async def fresh_state_if_moved(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc
    ) -> dict[str, Any] | None:
        return None

    async def current_scope(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc
    ) -> DocScope | None:
        # A chat's declaration is its workspace object: the row was stamped
        # from it when the document was created and is re-stamped from it on
        # every hello, so an owner or audience changed on the object is what
        # the row says too. A document with no object keeps what its row says.
        chat = await _chat_object(db, doc)
        if chat is None:
            return None
        return DocScope(team_id=chat.team_id, owner_user_id=chat.owner_user_id)

    async def relay(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc, op: OpPayload
    ) -> list[dict[str, Any]] | None:
        """Record a person's message and hand the rebroadcast its identity.

        A relay carries structured payloads. Only ``prompt`` may come from
        a client: ``run_query`` and ``promote`` are minted by a route that
        already decided, so a socket peer sending one would be asking the
        machine to act on a decision nobody made.

        A relay into a chat that exists is a person speaking, and the audience
        rule that admitted them to the document is not the whole gate: SEND is
        decided by the chat policy — the same decision the REST route makes —
        BEFORE anything here reads the ``kind`` the peer chose (see
        :func:`_admit_send`). The order is the point: keyed on the kind, the
        gate would judge only the frames a peer spelled the recognised way, and
        a frame that named an unknown kind (or none at all) would reach every
        other subscriber unjudged and unlogged. So the door comes first, and a
        kind nobody knows is then refused rather than echoed.
        """
        if isinstance(user, MachineSubject):
            raise DocOpRejectedError("forbidden", "a machine relays no message")
        chat = await _chat_object_for_write(db, doc)
        relayed: list[dict[str, Any]] = []
        if chat is not None and op.events:
            await _admit_send(db, chat=chat, user=user)
        for event in op.events:
            kind = event.get("kind")
            if kind in chat_service.SERVER_RELAY_KINDS:
                raise DocOpRejectedError(
                    "forbidden", f"a {kind} relay is minted by the server, not a peer"
                )
            if chat is None:
                # A document nobody's row backs (an older session id, a test
                # peer) has no chat to decide against and nothing to record:
                # its events travel as they always have.
                relayed.append(dict(event))
                continue
            if kind != chat_service.USER_MESSAGE_KIND:
                raise DocOpRejectedError(
                    "unsupported_kind",
                    f"relay kind {kind!r} is not valid on a chat",
                )
            text = event.get("text")
            client_id = event.get("client_id")
            if not isinstance(text, str) or not text:
                raise DocOpRejectedError("bad_op", "a prompt relay carries a non-empty text")
            if not isinstance(client_id, str) or not client_id:
                raise DocOpRejectedError("bad_op", "a prompt relay carries a client_id")
            try:
                message, _ = await chat_service.append_user_message(
                    db, chat=chat, user_id=user.id, text=text, client_id=client_id
                )
            except ChatMessageTooLargeError as exc:
                raise DocOpRejectedError("op_too_large", str(exc)) from exc
            relayed.append(
                PromptRelay(
                    message_id=str(message.id),
                    seq=message.seq,
                    text=message.payload.get("text", text),
                    client_id=client_id,
                    user_id=str(user.id),
                    at=message.created_at,
                ).model_dump(mode="json")
            )
        return relayed

    async def migrate_state(
        self, db: AsyncSession, *, user: Subject, doc: RealtimeDoc
    ) -> dict[str, Any] | None:
        # A transcript has no source but the row: a state written before the
        # version key existed is the 1.0.0 shape and is stamped as such; a
        # version this strategy does not know is not rewritten into its own.
        state = doc.state
        if state.get(STATE_SCHEMA_VERSION_KEY) is not None:
            return None
        if not (
            isinstance(state.get("meta"), dict)
            and isinstance(state.get("events"), list)
            and isinstance(state.get("ids"), dict)
        ):
            return None
        return {**state, STATE_SCHEMA_VERSION_KEY: self.STATE_SCHEMA_VERSION}


@dataclass(frozen=True)
class _SuppressedAnnounce:
    """A change whose announcement the open window swallowed, and everything the
    trailing emit needs to make it once the window elapses.

    Ids only, like every domain event: the frame says a document changed and the
    reader re-reads it through the surface it is authorized for."""

    org_id: UUID
    event_type: EventType
    entity: Entity
    entity_id: str
    version: int
    team_id: str | None
    visibility: str
    actor: dict[str, Any]


class DocRegistry:
    def __init__(
        self,
        *,
        strategies: Mapping[str, DocStrategy] | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        sessions: Callable[[], AbstractAsyncContextManager[AsyncSession]] = AsyncSessionLocal,
    ) -> None:
        self._strategies = dict(strategies) if strategies is not None else None
        self._now = now
        #: How the trailing edge waits out a window, and where it gets the
        #: session to write the announcement the operation could not: it runs
        #: after that operation's transaction is long closed, so it owns one.
        self._sleep = sleep
        self._sessions = sessions
        #: When each document last had its change announced to its org by this
        #: process, keyed by ``(org, channel)``. See ``_announce_due``.
        self._announced: dict[tuple[UUID, str], float] = {}
        #: The last change of each document the open window swallowed, and the
        #: timer that will announce it when the window elapses. Both are keyed
        #: the same way, and at most one timer runs per document.
        self._pending: dict[tuple[UUID, str], _SuppressedAnnounce] = {}
        self._trailing: dict[tuple[UUID, str], asyncio.Task[None]] = {}

    def _announce_due(self, grant: ChannelGrant) -> bool:
        """Whether this document's change is announced to its org NOW.

        Every accepted operation is rebroadcast as ``doc.op``, which is what
        the people with the document open read. The domain event is for
        everyone else — it invalidates a whole query prefix in every portal in
        the org — and one person typing produces an operation every few
        hundred milliseconds. Announcing each one asks the entire org to
        refetch several times a second, so the announcements are coalesced to
        at most one per document per window.

        This is the LEADING edge alone — whether the change is announced as it
        happens. A change it swallows is not lost: the caller arms the trailing
        edge for it (:meth:`_arm_trailing`), which announces the last one once
        the window elapses. Without that, a typist who stopped inside a window
        announced nothing at all and every catalogue in the org kept the title
        it was opened with.

        The window is this process's alone: a replica may not read another's
        clock, and the failure it protects against is per-replica anyway. The
        cost of a lost window (a restart, an eviction) is one extra
        announcement, never a missing one.

        It is also the tenant's alone. A chat id comes from the client, so two
        orgs can name the same document; a window keyed by the name would let
        one tenant's typing swallow the other's announcement, and a shared key
        is never something one org may spend for another.
        """
        key = (grant.org_id, grant.channel.key)
        now = self._now()
        last = self._announced.get(key)
        if last is not None and now - last < DOMAIN_ANNOUNCE_INTERVAL_SECONDS:
            return False
        if len(self._announced) >= ANNOUNCE_TRACKING_CAP:
            self._forget_stale(now)
        self._announced[key] = now
        return True

    def _forget_stale(self, now: float) -> None:
        """Keep the window map bounded. An entry older than the window decides
        nothing, so dropping it changes no answer; if every entry is still
        inside its window the map is cleared instead — that costs one extra
        announcement per document, the safe direction."""
        for key in [
            k for k, t in self._announced.items() if now - t >= DOMAIN_ANNOUNCE_INTERVAL_SECONDS
        ]:
            del self._announced[key]
        if len(self._announced) >= ANNOUNCE_TRACKING_CAP:
            self._announced.clear()

    def _arm_trailing(self, key: tuple[UUID, str], pending: _SuppressedAnnounce) -> None:
        """Remember a swallowed change and make sure a timer will announce it.

        Only the LAST one is kept: they are the same fan-out, and the frame
        carries ids rather than the change, so announcing the burst once when
        it settles says everything announcing each of them would have. One
        timer runs per document — a second change inside the same window
        replaces what the running timer will announce rather than adding one."""
        self._pending[key] = pending
        if key in self._trailing:
            return
        opened = self._announced.get(key)
        delay = (
            DOMAIN_ANNOUNCE_INTERVAL_SECONDS
            if opened is None
            else max(0.0, opened + DOMAIN_ANNOUNCE_INTERVAL_SECONDS - self._now())
        )
        self._trailing[key] = asyncio.ensure_future(self._announce_trailing(key, delay))

    def _cancel_trailing(self, key: tuple[UUID, str]) -> None:
        """Drop whatever the trailing edge was going to say about this document.

        Called when the leading edge announces it instead: the emit that opens
        the new window already carries everything the swallowed changes were
        waiting to report, so letting the timer fire too would ask the org to
        refetch twice for one burst."""
        self._pending.pop(key, None)
        task = self._trailing.pop(key, None)
        if task is not None:
            task.cancel()

    async def _announce_trailing(self, key: tuple[UUID, str], delay: float) -> None:
        """Wait out the window, then announce the last change it swallowed.

        It runs outside every request: the operation's transaction closed long
        ago, so it takes a session of its own and commits it. A failure is
        logged and dropped — the announcement is a hint to refetch, and the
        socket has already delivered the change itself to everyone watching."""
        try:
            await self._sleep(delay)
            pending = self._pending.pop(key, None)
            if pending is None:
                return
            # The trailing emit is an announcement like any other, so it opens
            # the next window: a change arriving right behind it must not ring
            # the org a second time.
            self._announced[key] = self._now()
            async with self._sessions() as db:
                await emit(
                    db,
                    org_id=pending.org_id,
                    type=pending.event_type,
                    entity=pending.entity,
                    entity_id=pending.entity_id,
                    version=pending.version,
                    payload={"team_id": pending.team_id},
                    visibility=pending.visibility,
                    actor=pending.actor,
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("docsync.trailing_announce_failed", channel=key[1], exc_info=True)
        finally:
            if self._trailing.get(key) is asyncio.current_task():
                del self._trailing[key]

    async def aclose(self) -> None:
        """Stop every armed trailing announcement, announcing nothing.

        The process is going away and with it the session the timer would have
        written through; a document whose last change goes unannounced here is
        re-announced by the leading edge of the next change anyone makes to it,
        on whichever replica takes them."""
        tasks = list(self._trailing.values())
        self._trailing.clear()
        self._pending.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def strategy(self, doc_type: str) -> DocStrategy:
        if self._strategies is not None:
            found = self._strategies.get(doc_type)
        else:
            found = ChatAppendById() if doc_type == "chat" else registered_strategy(doc_type)
        if found is None:
            raise DocOpRejectedError("unsupported_kind", f"no strategy for {doc_type!r}")
        return found

    def _select(self, grant: ChannelGrant) -> Select[tuple[RealtimeDoc]]:
        """The grant's document, within the grant's org. The org is half the
        key: a row of another tenant under the same client-chosen id is not
        this document and must never be found here."""
        return select(RealtimeDoc).where(
            RealtimeDoc.org_id == grant.org_id,
            RealtimeDoc.doc_type == grant.channel.doc_type,
            RealtimeDoc.doc_id == grant.channel.doc_id,
        )

    async def _locked(self, db: AsyncSession, grant: ChannelGrant) -> RealtimeDoc | None:
        """The document row, held for this transaction, AS THE LOCK FINDS IT.

        ``populate_existing`` is what makes the second half true. A caller's
        session that already read this row keeps it in its identity map, and
        without the option the locked read hands that older copy back — so the
        writer would take the lock and then build its successor on the very
        window the lock exists to stop it building on, silently dropping
        whatever committed in between.
        """
        return (
            await lock_rows(
                db,
                LockRank.REALTIME_DOC,
                self._select(grant).execution_options(populate_existing=True),
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _hold_creation_lock(db: AsyncSession, grant: ChannelGrant) -> None:
        """Serialise creation for this org and document type until the caller's
        transaction ends. There is no row to lock before the first insert, so
        without this the ceiling below would only be as tight as the number of
        first hellos racing each other."""
        await advisory_xact_lock(
            db, advisory_key("realtime-doc-creation", grant.org_id, grant.channel.doc_type)
        )

    @staticmethod
    async def _check_quota(db: AsyncSession, grant: ChannelGrant) -> None:
        """Refuse a new document once the org holds its ceiling of this kind.
        A chat id is the client's to choose, so this is the only thing standing
        between one member and an unbounded table of 4 MiB rows."""
        ceiling = settings.realtime_docs_max_per_org
        live = (
            await db.execute(
                select(func.count())
                .select_from(RealtimeDoc)
                .where(
                    RealtimeDoc.org_id == grant.org_id,
                    RealtimeDoc.doc_type == grant.channel.doc_type,
                )
            )
        ).scalar_one()
        if int(live) >= ceiling:
            raise DocOpRejectedError(
                "quota_exceeded",
                f"this organisation already holds {ceiling} {grant.channel.doc_type} "
                "documents; no more can be created",
            )

    @staticmethod
    async def _restamp_scope(
        db: AsyncSession, *, strategy: DocStrategy, user: Subject, doc: RealtimeDoc
    ) -> None:
        """Copy the audience a document's source of truth gives it onto the row,
        for the types that keep that source outside ``realtime_docs``.

        An artifact's team and owner are the knowledge item's. The row is
        stamped with them once, when it is created, and a rebuild rewrites only
        epoch, seq and state — so a REST re-scope left the row naming a team the
        entry had left. That copy then decided two things it had no right to:
        the read gate below refused readers the item's own scope admits, and
        every ``doc.op`` went on addressed to the old team, past the people who
        now hold the entry. Nothing here decides who may read — the subscribe's
        scope check and the gate below do that — it only stops the row lying."""
        scope = await strategy.current_scope(db, user=user, doc=doc)
        if scope is None:
            return
        if doc.team_id == scope.team_id and doc.owner_user_id == scope.owner_user_id:
            return
        doc.team_id = scope.team_id
        doc.owner_user_id = scope.owner_user_id
        await db.flush()

    @staticmethod
    def _readable(doc: RealtimeDoc, user: Subject, ent: EntitlementSnapshot) -> RealtimeDoc:
        """``doc`` as the caller may read it, or ``not_found`` — the opaque
        answer for a row of another org or of a team the caller is not on.

        For a box the row's org must be one its credential serves — the
        engine's tenancy floor, asked of the row itself. Whether the box HOLDS
        the chat is the binding's question, and every operation a box makes
        asks it of the chat row at that moment (:meth:`publishes`), so a
        rebinding takes effect on the next frame rather than the next
        subscribe."""
        if isinstance(user, MachineSubject):
            if not user.ctx.serves(doc.org_id):
                raise DocOpRejectedError("not_found")
            return doc
        if not doc_readable(doc, ent=ent):
            raise DocOpRejectedError("not_found")
        return doc

    @staticmethod
    def _doc_op_payload(
        envelope: DocEnvelope, *, team_id: str | None, relay: bool
    ) -> dict[str, Any]:
        """The ``doc.op`` row's payload: the envelope every replica
        rebroadcasts, the team the frame is addressed to, and whether it was
        a relay (no state moved)."""
        return {"envelope": envelope.model_dump(mode="json"), "team_id": team_id, "relay": relay}

    @staticmethod
    async def _standing(
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        user: Subject,
        ent: EntitlementSnapshot,
        agent_id: str | None,
    ) -> ChatStanding:
        """Where ``doc`` leaves this caller as it stands now.

        A chat is judged from its declaration, read at the moment of the write
        through the same seam and the same rule the subscribe uses
        (:func:`channels.chat_standing`), so a rebinding, a workspace demotion
        or a share change takes effect on the next frame rather than the next
        subscribe. Any other document, and a chat nothing declares any more,
        is published by the row's owner alone, and nobody holds a rung on it.
        """
        user_id = _user_id(user)
        scope = (
            await channel_rules.lookup_chat_doc(db, org_id=doc.org_id, chat_id=doc.doc_id)
            if doc.doc_type == "chat"
            else None
        )
        if scope is None:
            return ChatStanding(
                is_owner=doc.owner_user_id is not None and doc.owner_user_id == user_id,
                is_bound_machine=False,
                shared_role=None,
            )
        return await channel_rules.chat_standing(
            db,
            scope,
            chat_id=doc.doc_id,
            user_id=user_id,
            agent_id=agent_id,
            org_id=doc.org_id,
            ent=ent,
        )

    async def publishes(
        self,
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        user: Subject,
        ent: EntitlementSnapshot,
        agent_id: str | None,
    ) -> bool:
        """Whether this socket is the document's PUBLISHER: the chat's owner
        (the workspace's owner when the chat sits in a workspace of several),
        or the machine the chat is bound to when the agent id IS that
        machine's id. A share is not a way in, deliberately: the transcript
        and the ephemeral lane are the publisher's."""
        standing = await self._standing(db, doc=doc, user=user, ent=ent, agent_id=agent_id)
        return standing.publishes

    async def role(
        self,
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        user: Subject,
        ent: EntitlementSnapshot,
        agent_id: str | None,
    ) -> str | None:
        """The rung ``user`` holds on ``doc`` as it stands now.

        The publisher's rung for the two publishers; otherwise the rung that
        decides the chat for this caller (the workspace's in a workspace of
        several, the chat node's otherwise), which is where every deliberate
        grant on a chat is made. ``None`` is a reader: the audience already
        admitted them, and reading is all a rung-less caller may do. The
        publisher of a chat of its own never pays for the ACL read.
        """
        standing = await self._standing(db, doc=doc, user=user, ent=ent, agent_id=agent_id)
        return standing.role

    async def writable(
        self,
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        user: Subject,
        ent: EntitlementSnapshot,
        agent_id: str | None,
    ) -> bool:
        """Whether this socket may WRITE ``doc`` as it stands now — the ladder's
        WRITE, which the publisher and anyone shared the node at ``writer`` or
        above hold, and a ``reader`` or ``commenter`` does not. This is what
        the peer is told as ``can_write``; what a writer may actually do with
        it is narrower than what the publisher may (see
        :data:`PUBLISHER_ONLY_INTENTS`)."""
        return DEFAULT_LADDER.allows(
            await self.role(db, doc=doc, user=user, ent=ent, agent_id=agent_id), DOC_WRITE_ACTION
        )

    async def _require_publisher(
        self,
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        user: Subject,
        ent: EntitlementSnapshot,
        agent_id: str | None,
    ) -> None:
        if not await self.publishes(db, doc=doc, user=user, ent=ent, agent_id=agent_id):
            raise DocOpRejectedError(
                "forbidden", "only the document's owner or the machine serving it may write it"
            )

    async def _require_writable(
        self,
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        user: Subject,
        ent: EntitlementSnapshot,
        agent_id: str | None,
    ) -> None:
        if not await self.writable(db, doc=doc, user=user, ent=ent, agent_id=agent_id):
            raise DocOpRejectedError(
                "forbidden",
                "writing this document needs the document's publisher, "
                "or a share of it at writer or above",
            )

    async def locate(
        self, db: AsyncSession, *, grant: ChannelGrant, user: Subject, ent: EntitlementSnapshot
    ) -> RealtimeDoc:
        """The document row as the caller may read it, without locking or
        creating it: the ephemeral lane's look-up. A missing row is
        ``not_found`` — a stream has nothing to belong to until a ``hello``
        created the document."""
        doc = (await db.execute(self._select(grant))).scalar_one_or_none()
        if doc is None:
            raise DocOpRejectedError("not_found")
        return self._readable(doc, user, ent)

    async def ensure(
        self,
        db: AsyncSession,
        *,
        grant: ChannelGrant,
        user: Subject,
        actor: Mapping[str, Any],
        ent: EntitlementSnapshot,
    ) -> RealtimeDoc:
        """The document row, locked for this transaction; created from the
        strategy's seed when missing; rebuilt when the REST path moved it.
        Judged as the caller may read it NOW (``not_found`` otherwise), whatever
        the grant assumed when the subscribe was made. Creating one is refused
        as ``quota_exceeded`` once the org holds its ceiling of that kind."""
        strategy = self.strategy(grant.channel.doc_type)
        doc = await self._locked(db, grant)
        if doc is None:
            state = await strategy.seed(db, user=user, grant=grant)
            if state is None:
                raise DocOpRejectedError("not_found")
            check_size(state)
            # Nothing exists to lock yet, so creation is serialised per org and
            # document type: whoever holds that lock counts and inserts alone,
            # and the ceiling is a real ceiling rather than one per racer.
            await self._hold_creation_lock(db, grant)
            doc = await self._locked(db, grant)
            if doc is None:
                await self._check_quota(db, grant)
                # Insert-or-nothing then re-lock: two first hellos race here, and
                # the loser must read the winner's row rather than fail — and is
                # judged against it like any other reader, since the winner may
                # be someone the loser must not learn about.
                await db.execute(
                    pg_insert(RealtimeDoc)
                    .values(
                        doc_type=grant.channel.doc_type,
                        doc_id=grant.channel.doc_id,
                        org_id=grant.org_id,
                        team_id=grant.team_id,
                        owner_user_id=grant.owner_user_id,
                        epoch=1,
                        seq=0,
                        state=state,
                    )
                    .on_conflict_do_nothing(index_elements=["org_id", "doc_type", "doc_id"])
                )
                doc = await self._locked(db, grant)
                if doc is None:  # pragma: no cover - the row was just inserted or already there
                    raise DocOpRejectedError("not_found")
            return self._readable(doc, user, ent)
        # Before the row is judged: a row whose audience is decided elsewhere
        # must be brought up to date first, or the gate rules on a stamp that
        # has been wrong since the last REST edit.
        await self._restamp_scope(db, strategy=strategy, user=user, doc=doc)
        self._readable(doc, user, ent)
        if doc.state.get(STATE_SCHEMA_VERSION_KEY) != strategy.STATE_SCHEMA_VERSION:
            # An older writer's row (or a newer one's): bring it forward if
            # the strategy can read it, and tell every subscriber to reload;
            # refuse it rather than misread it if not.
            migrated = await strategy.migrate_state(db, user=user, doc=doc)
            if migrated is None:
                raise DocOpRejectedError(
                    "unsupported_kind",
                    f"the document state names schema version "
                    f"{doc.state.get(STATE_SCHEMA_VERSION_KEY)!r}, which this server cannot read",
                )
            await self._rebuild(
                db, doc=doc, grant=grant, state=migrated, reason=REBUILD_REASON_SCHEMA, actor=actor
            )
        fresh = await strategy.fresh_state_if_moved(db, user=user, doc=doc)
        if fresh is not None:
            await self._rebuild(
                db, doc=doc, grant=grant, state=fresh, reason=REBUILD_REASON_REST_EDIT, actor=actor
            )
        return doc

    async def snapshot(
        self,
        db: AsyncSession,
        *,
        grant: ChannelGrant,
        user: Subject,
        actor: Mapping[str, Any],
        ent: EntitlementSnapshot,
        agent_id: str | None = None,
    ) -> DocSnapshot:
        doc = await self.ensure(db, grant=grant, user=user, actor=actor, ent=ent)
        if isinstance(user, MachineSubject):
            # A person reads what the audience admits them to; a box reads
            # the transcript it publishes and no other. Asked of the binding
            # now, so a chat rebound since the subscribe hands out nothing.
            await self._require_publisher(db, doc=doc, user=user, ent=ent, agent_id=agent_id)
        return DocSnapshot(
            doc_type=doc.doc_type,
            doc_id=doc.doc_id,
            epoch=doc.epoch,
            seq=doc.seq,
            state=stamped_state(doc),
            owner_user_id=doc.owner_user_id,
            team_id=doc.team_id,
            can_write=await self.writable(db, doc=doc, user=user, ent=ent, agent_id=agent_id),
        )

    @staticmethod
    def _stamp(
        doc: RealtimeDoc, new_state: dict[str, Any] | None, effect: Mapping[str, Any]
    ) -> bool:
        """Put the turn's liveness on the row's own columns, in place.

        ``True`` when this operation was a RESTAMP — the document's state was
        left alone and the columns are the only thing that moved, which is what
        tells the caller to fan the frame out on the unsequenced lane. A real
        turn change returns ``False``: the state carries it, and the columns
        are refreshed here only so they never describe the turn before last.
        """
        stamp = effect.get(TURN_STAMP_EFFECT)
        if isinstance(stamp, dict):
            doc.turn_state = str(stamp["state"])
            doc.turn_state_at = str(stamp["at"])
            return True
        if new_state is None or TURN_STATE_META_KEY not in effect.get("meta_keys", ()):
            return False
        meta = new_state.get("meta")
        turn = meta.get(TURN_STATE_META_KEY) if isinstance(meta, dict) else None
        word = turn.get("state") if isinstance(turn, dict) else None
        at = turn.get("at") if isinstance(turn, dict) else None
        doc.turn_state = word[:MAX_TURN_STATE] if isinstance(word, str) else None
        doc.turn_state_at = at[:MAX_TURN_STATE_AT] if isinstance(at, str) else None
        return False

    async def apply_op(
        self,
        db: AsyncSession,
        *,
        grant: ChannelGrant,
        user: Subject,
        envelope: DocEnvelope,
        actor: Mapping[str, Any],
        ent: EntitlementSnapshot,
        agent_id: str | None = None,
    ) -> AppliedOp:
        """Apply one ``op`` envelope in the caller's transaction (see the
        module docstring). Raises :class:`StaleEpochError` or
        :class:`DocOpRejectedError`; the caller rolls back on either.
        ``agent_id`` is the agent the socket was opened as (``None`` for a
        person's own socket); a write is judged against it."""
        if envelope.peer_id == SERVER_PEER_ID:
            # Writing under this id is what says "the server wrote this", and
            # what it buys is the right to name a member. The socket already
            # overwrites a peer id a client sent, so nothing legitimate reaches
            # here claiming it — which is exactly why the refusal belongs at
            # the mechanism rather than at the one caller that happens to guard
            # it today.
            raise DocOpRejectedError("bad_op", "the server's peer id is not a client's to write as")
        strategy = self.strategy(grant.channel.doc_type)
        doc = await self.ensure(db, grant=grant, user=user, actor=actor, ent=ent)
        if envelope.epoch != doc.epoch:
            raise StaleEpochError(doc.epoch)
        try:
            op = OpPayload.model_validate(envelope.payload)
        except ValidationError as exc:
            raise DocOpRejectedError("bad_op", str(exc.errors()[0].get("msg", ""))) from exc
        if op.intent == "chunk":
            raise DocOpRejectedError("unsupported_kind", "chunk ops ride the ephemeral lane")
        if op.intent in PUBLISHER_ONLY_INTENTS:
            await self._require_publisher(db, doc=doc, user=user, ent=ent, agent_id=agent_id)
        elif op.intent not in RELAYED_INTENTS:
            await self._require_writable(db, doc=doc, user=user, ent=ent, agent_id=agent_id)
        # Before the operation is measured, let alone applied: a number the
        # store cannot hold makes the measure below meaningless (it counts only
        # what could be written) and would otherwise surface as a failed INSERT.
        _check_storable(envelope.payload, "the operation")
        team_id = str(doc.team_id) if doc.team_id else None
        # The op travels as a doc.op row whose payload the event log caps well
        # below the socket's frame cap. Size it as that row would carry it —
        # stamped with the next seq and the longer relay flag, so the bound is
        # never under — and refuse in band before anything moves, rather than
        # fail on the emit after the state and the knowledge item changed.
        size = payload_size(
            self._doc_op_payload(
                envelope.model_copy(update={"seq": doc.seq + 1, "epoch": doc.epoch}),
                team_id=team_id,
                relay=False,
            )
        )
        if size > MAX_PAYLOAD_BYTES:
            raise DocOpRejectedError(
                "op_too_large",
                f"the operation would be {size} bytes in the event log; "
                f"the cap is {MAX_PAYLOAD_BYTES}",
            )
        new_state, effect, changed = strategy.apply(dict(doc.state), op, peer_id=envelope.peer_id)
        _check_storable(new_state, "the document state")
        # One decision for the whole operation: the domain announcement here
        # and the one the side effect would make (a knowledge item announcing
        # its own edit) are the same fan-out and are coalesced together.
        announce = changed and self._announce_due(grant)
        # A relay moves no state but may still be recorded (a person's message
        # becomes a transcript entry), and the rebroadcast then carries the
        # identity the record gave it rather than what the peer guessed.
        relayed: list[dict[str, Any]] | None = None
        if op.intent == "user_message":
            relayed = await strategy.relay(db, user=user, doc=doc, op=op)
        if changed:
            new_state = await strategy.persist(
                db, user=user, doc=doc, state=new_state, effect=effect, announce=announce
            )
            doc.seq += 1
            doc.state = new_state
            await db.flush()
        # A turn's liveness is kept out of the blob, so BOTH paths keep the
        # columns current: a restamp writes them alone, and a real turn change
        # writes them alongside the state it just moved. Letting only the
        # restamp write them would leave a stale instant folded over a turn
        # that has since started again.
        stamped = self._stamp(doc, new_state if changed else None, effect)
        # A relay that moved nothing rides seq 0 — the unsequenced lane, the same
        # one the ephemeral ops use. Stamped with the document's CURRENT
        # sequence it would arrive as an op every subscriber has already
        # applied, and an ordering guard would drop it: the relay would work on
        # an empty document and stop working after the first append.
        update: dict[str, Any] = {
            "seq": 0 if (relayed is not None or stamped) and not changed else doc.seq,
            "epoch": doc.epoch,
        }
        if relayed is not None:
            update["payload"] = {**envelope.payload, "events": relayed}
        # An append is rebroadcast carrying the sequences the durable write just
        # assigned, exactly as the document's state carries them. A viewer that
        # folds the frame can then name every row it holds and let go of the
        # ones it can ask for again; without it a reader's window could only
        # ever shed the page it opened on, and grew for the life of the tab.
        #
        # The events it carries are the ones the strategy ADMITTED, not the
        # ones the peer sent: a claim taken out of the stored event has to be
        # out of the frame too, or every other viewer folds it off the wire.
        sequences = effect.get("sequences")
        if op.intent == "append":
            admitted = effect.get("admitted")
            carrier = (
                {**envelope.payload, "events": admitted}
                if isinstance(admitted, list)
                else envelope.payload
            )
            update["payload"] = (
                stamp_sequences(carrier, sequences)
                if isinstance(sequences, dict) and sequences
                else carrier
            )
        rebroadcast = envelope.model_copy(update=update)
        if changed or op.intent == "user_message" or stamped:
            row_payload = self._doc_op_payload(rebroadcast, team_id=team_id, relay=not changed)
            # The pre-flight measure above sized the operation as it arrived;
            # recording it can only have added to it, so the bound is checked
            # once more against what is actually about to be written.
            grown = payload_size(row_payload)
            if grown > MAX_PAYLOAD_BYTES:
                raise DocOpRejectedError(
                    "op_too_large",
                    f"the operation would be {grown} bytes in the event log; "
                    f"the cap is {MAX_PAYLOAD_BYTES}",
                )
            await emit(
                db,
                org_id=doc.org_id,
                type=EventType.DOC_OP,
                entity=Entity.DOC,
                entity_id=grant.channel.key,
                version=doc.seq,
                payload=row_payload,
                visibility=grant.visibility,
                actor=actor,
            )
        if announce:
            await emit(
                db,
                org_id=doc.org_id,
                type=strategy.domain_event,
                entity=strategy.domain_entity,
                entity_id=grant.channel.doc_id,
                version=doc.seq,
                payload={"team_id": team_id},
                visibility=grant.visibility,
                actor=actor,
            )
        # The window is a delay, not a veto. A change it swallowed is armed to
        # be announced when the window elapses, so a typist who stops mid-window
        # still reaches every catalogue in the org; an announcement made here
        # already carries them, so it cancels what was armed instead.
        window = (grant.org_id, grant.channel.key)
        if announce:
            self._cancel_trailing(window)
        elif changed:
            self._arm_trailing(
                window,
                _SuppressedAnnounce(
                    org_id=doc.org_id,
                    event_type=strategy.domain_event,
                    entity=strategy.domain_entity,
                    entity_id=grant.channel.doc_id,
                    version=doc.seq,
                    team_id=team_id,
                    visibility=grant.visibility,
                    actor=dict(actor),
                ),
            )
        return AppliedOp(
            envelope=rebroadcast, seq=doc.seq, epoch=doc.epoch, changed=changed, effect=effect
        )

    async def rebuild(
        self,
        db: AsyncSession,
        *,
        grant: ChannelGrant,
        user: Subject,
        state: dict[str, Any],
        reason: str,
        actor: Mapping[str, Any],
        ent: EntitlementSnapshot,
        agent_id: str | None = None,
    ) -> RealtimeDoc:
        """A publisher's rebuild — replace the state wholesale: ``epoch + 1``,
        ``seq 0``, and a ``reload`` every replica pushes to its subscribers.
        Only for a document type whose source of truth is a peer, and then
        only for its publisher (the row's owner, or the bound machine)."""
        strategy = self.strategy(grant.channel.doc_type)
        if not strategy.publisher_may_rebuild:
            raise DocOpRejectedError(
                "unsupported_kind",
                f"a {grant.channel.doc_type} is rebuilt from its source, never from a snapshot",
            )
        doc = await self.ensure(db, grant=grant, user=user, actor=actor, ent=ent)
        # A rebuild replaces the whole state, which for a chat is its history:
        # the publisher's, never a shared writer's.
        await self._require_publisher(db, doc=doc, user=user, ent=ent, agent_id=agent_id)
        await self._rebuild(db, doc=doc, grant=grant, state=state, reason=reason, actor=actor)
        return doc

    async def _rebuild(
        self,
        db: AsyncSession,
        *,
        doc: RealtimeDoc,
        grant: ChannelGrant,
        state: dict[str, Any],
        reason: str,
        actor: Mapping[str, Any],
    ) -> None:
        # Every state this server writes names the version it writes; a state
        # that names another one is not this server's to write.
        strategy = self.strategy(grant.channel.doc_type)
        current = strategy.STATE_SCHEMA_VERSION
        named = state.get(STATE_SCHEMA_VERSION_KEY)
        if named is not None and named != current:
            raise DocOpRejectedError(
                "unsupported_kind",
                f"the state names schema version {named!r}; this server writes {current}",
            )
        # The source of truth is speaking again, and it knows only what it
        # owns. Whatever the document holds that the source has never heard of
        # — a chat's shared composer draft — is folded back in, so a resume
        # does not quietly discard what people typed while the box was away.
        state = strategy.carry_forward(dict(doc.state), state)
        stamped = {**state, STATE_SCHEMA_VERSION_KEY: current}
        _check_storable(stamped, "the document state")
        check_size(stamped)
        doc.epoch += 1
        doc.seq = 0
        doc.state = stamped
        # The replacement is the source of truth speaking again, so the turn's
        # columns are re-read from it rather than carried: a box that came back
        # and republished an idle chat must not keep a stamp saying a turn from
        # before the restart is still running.
        self._stamp(doc, stamped, {"meta_keys": [TURN_STATE_META_KEY]})
        await db.flush()
        reload = DocEnvelope(
            doc_id=doc.doc_id,
            doc_type=grant.channel.doc_type,
            epoch=doc.epoch,
            peer_id=SERVER_PEER_ID,
            seq=0,
            kind="reload",
            payload=ReloadPayload(epoch=doc.epoch, reason=reason).model_dump(mode="json"),
        )
        await emit(
            db,
            org_id=doc.org_id,
            type=EventType.DOC_OP,
            entity=Entity.DOC,
            entity_id=grant.channel.key,
            version=0,
            payload={
                "envelope": reload.model_dump(mode="json"),
                "team_id": str(doc.team_id) if doc.team_id else None,
                "relay": False,
            },
            visibility=grant.visibility,
            actor=actor,
        )


async def end_turn_nobody_runs(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    user: Subject,
    actor: Mapping[str, Any] | None,
) -> bool:
    """A reader's Stop on a chat no box holds: its machine gone quiet, asleep
    or off the plane, or the box having put the chat to sleep or refused it.
    Nothing could write the turn's end, so the server stamps the document idle
    the way the box would
    (:func:`alkera_core.objects.chat_turn.end_turn_nobody_runs`). True when
    the document changed."""
    del user  # the stamp is the server's own, under its peer id
    # The chat's row lock first, in the one order every writer of a chat takes
    # them in (see :func:`publish_server_events`).
    await chat_service.lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    return await chat_turn.end_turn_nobody_runs(db, chat, actor=actor)


async def publish_server_events(
    db: AsyncSession,
    *,
    chat: WorkspaceObject,
    user: Subject,
    entries: list[dict[str, Any]],
    actor: Mapping[str, Any] | None,
) -> None:
    """Put entries the SERVER authored onto a chat's document, live.

    Some of a chat's transcript is the server's to write, not the machine's:
    the resolution of an ask a person answered in the browser carries who
    decided, and the roster lives here and nowhere the box can see it. Written
    only to the table, that row reached a reader who was watching by no route
    at all — the machine's own later resolution settled their card instead,
    correctly naming nobody — so the name appeared on a reload and never while
    anybody was looking.

    This is the same append a machine makes, under this server's own peer id:
    one event becomes the row, the entry in the live window, and the frame the
    socket carries, with the transcript sequence the write assigned stamped on
    it. An entry the window already holds moves nothing and is not re-announced,
    so a retry is a no-op, and a chat with no document yet has nobody
    subscribed — the row is already durable and the next reader reads it.

    The machine's own resolution for the same ask arrives later, de-named, and
    is a separate event id: readers fold resolutions by ``request_id``, so it
    settles the same card rather than adding one, and it cannot take the name
    off a decision the server recorded.
    """
    if not entries:
        return
    # Both rows this touches, in the one order every writer of a chat takes
    # them in. The caller has almost always been through here already — it
    # recorded the rows it is now publishing — and re-taking a lock this
    # transaction holds costs nothing, so the order is asserted here rather
    # than assumed of whoever called.
    locked = await chat_service.lock_chat_for_write(db, chat.id, org_team_id=chat.org_team_id)
    doc = (
        await db.execute(
            select(RealtimeDoc)
            .where(
                RealtimeDoc.org_id == chat.org_team_id,
                RealtimeDoc.doc_type == "chat",
                RealtimeDoc.doc_id == str(chat.id),
            )
            # Read the row as the lock above finds it, not as this session last
            # saw it: a caller's session has usually touched the document, and
            # the identity map would otherwise hand back that older copy — so
            # the write would build on a window the lock exists to stop it
            # building on.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if doc is None or locked is None:
        return
    for entry in entries:
        _check_storable(entry, "a server entry")
    try:
        await publish_server_entries(db, chat=locked, doc=doc, entries=entries, actor=actor)
    except ChatMessageTooLargeError as exc:
        raise DocOpRejectedError("op_too_large", str(exc)) from exc


__all__ = [
    "REBUILD_REASON_PUBLISHER",
    "REBUILD_REASON_REST_EDIT",
    "REBUILD_REASON_SCHEMA",
    "STATE_SCHEMA_VERSION_KEY",
    "TURN_STATE_AT_META_KEY",
    "TURN_STATE_META_KEY",
    "AppliedOp",
    "ChatAppendById",
    "DocOpRejectedError",
    "DocRegistry",
    "DocScope",
    "DocSnapshot",
    "DocStrategy",
    "RejectCode",
    "StaleEpochError",
    "check_size",
    "compact_window",
    "publish_server_events",
    "stamped_state",
    "state_size",
    "window_threshold",
]
