"""The chat transcript's persisted records and the relay bodies, spelled once.

``chat_messages.payload`` is the most-written persisted JSONB of the workspace:
every transcript row is one of two shapes, written in one process and read in
another.

* :class:`ChatTranscriptEntry` — the envelope the machine publishes for one
  harness event (``{event_id, role, kind, payload}``, the event itself under
  ``payload``). Stored verbatim by the chat document's persist step and read
  back by every browser, by the mirror's catch-up and by the cost roll-ups that
  select on ``kind``.
* :class:`ChatPromptRecord` — a person's message, as the server records it.

The relay bodies are the other half of the same boundary: a route (or the
socket's own relay path) mints one, the box's mirror parses it field by field.
:data:`ChatRelay` is the tagged union of the four the product sends, with
:class:`RawRelay` as the catch-all so a kind a newer server invents reaches an
older box as data to ignore rather than an exception.

All of them are ``VersionedModel``: they are persisted and cross a process
boundary. Every field is defaulted on purpose, so a partial or hostile body
degrades to a record the existing checks refuse rather than a validation error
on a live socket. A row written before the version stamp existed still loads,
which the lineage fixtures prove.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Final, Literal, Union

from pydantic import Discriminator, Field, Tag, TypeAdapter, model_validator

from alkera_core.schemas.chat.events_interaction import (
    PermissionResolved,
    QuestionAnswered,
    QuestionRejected,
)
from alkera_core.schemas.chat.events_signals import PromptCancelled
from alkera_core.schemas.chat.events_transcript import (
    MessageCompleted,
    MessageCreated,
    PartCreated,
)
from alkera_core.schemas.chat.parts import FilePart, TextPart
from alkera_core.schemas.objects.specs import CLOUD_PERMISSION_MODES, CloudPermissionMode
from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator

#: How a transcript row attributes what it holds. A tool result is not the
#: assistant speaking, and the distinction is what lets a reader render a tool
#: card rather than a paragraph.
TranscriptRole = Literal["user", "assistant", "tool", "system"]

#: The relay kind a person's message travels under — the same string the
#: transcript row's ``kind`` column carries for that row.
PROMPT_KIND: Final[Literal["prompt"]] = "prompt"

#: The wire kinds :data:`ChatRelay` knows. An answer to an outstanding ask is
#: NOT one of them: it travels as a ``prompt`` carrying an ``interrupt_id``,
#: which is what the routes have always sent and what the box dispatches on.
RELAY_KINDS = frozenset({PROMPT_KIND, "run_query", "promote", "mode", "model", "stop"})

#: The stances a cloud chat's session may be put in. The HTTP body's own set,
#: aliased rather than re-spelled: a relay is the only other way into the
#: machine, so a mode the route admits must be relayable and one it refuses must
#: not be — and a second spelling is how the two drift into a switch the row
#: records and the running box floors to ``read_only`` instead.
CloudRelayMode = CloudPermissionMode
#: The same set, for the reader that has to recognise one at runtime.
CLOUD_RELAY_MODES: Final[frozenset[str]] = CLOUD_PERMISSION_MODES


class ChatTranscriptEntry(VersionedModel):
    """One published harness event, as the transcript stores it.

    ``payload`` is the harness event as the harness emitted it — itself a
    versioned model, which is why only the envelope was ever unversioned.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    event_id: str = ""
    role: TranscriptRole = "assistant"
    kind: str = ""
    #: The transcript sequence this entry is held under, as a reader is told
    #: it: the chat document's state carries it, and so does the rebroadcast of
    #: the append that recorded it. It is what lets a reader name a row it has
    #: let go of and ask the durable record for it again, so a long chat's
    #: loaded window stays bounded. Unset on a stored entry — there the
    #: ``chat_messages.seq`` column is the record, and a second spelling of it
    #: in the payload is one that could disagree.
    seq: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_what_a_publisher_sends(cls, data: Any) -> Any:
        """Normalize the two shapes a publisher can legally produce.

        A role outside the four falls back to ``assistant`` (the fallback both
        producers already applied before this model existed), and a payload
        that is not an object is wrapped the way the entry-shrinking path
        wraps one. A ``seq`` that is not an ordinal is dropped rather than
        coerced: unstamped means "keep this row", which is the safe answer,
        while believing ``"17"`` or ``17.5`` would let a reader release a row
        under a name the durable record does not answer to. All three are here
        for the same reason — parsing a published entry must not fail on a
        live socket, whatever a writer put on the wire.
        """
        if not isinstance(data, dict):
            return data
        role = data.get("role")
        payload = data.get("payload")
        seq = data.get("seq")
        if role not in ("user", "assistant", "tool", "system"):
            data = {**data, "role": "assistant"}
        if payload is not None and not isinstance(payload, dict):
            data = {**data, "payload": {"value": payload}}
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int)):
            data = {**data, "seq": None}
        return data


#: The kinds under which an ask's resolution lands in the transcript, whoever
#: wrote it: the machine when its harness settled the ask, or the server when a
#: reader answered while no machine was holding it.
RESOLUTION_KINDS: Final[frozenset[str]] = frozenset(
    {"permission.resolved", "question.answered", "question.rejected"}
)

#: The role a resolution a READER recorded through the server carries. The
#: machine files the ones it publishes itself under ``assistant`` (its harness
#: already acted on them), so this is what lets a box reading the transcript
#: back tell a decision nobody has acted on yet from one that is done — and it
#: is ``user`` because that is who decided.
RECORDED_ANSWER_ROLE: Final[Literal["user"]] = "user"

RecordedResolution = PermissionResolved | QuestionAnswered | QuestionRejected


def recorded_answer_event_id(request_id: str) -> str:
    """The event id a reader's recorded answer to ``request_id`` carries — one
    per ask, so a second record of the same answer is the same row."""
    return f"answer-{request_id}"


#: What every id belonging to a Stop note starts with. A reader tells the
#: note's own rows from the transcript around it by this.
STOPPED_NOTE_ID_PREFIX = "stop-"


def stopped_turn_note_id(turn_seq: int) -> str:
    """The note id a Stop on the turn the message at ``turn_seq`` started.

    Derived, not minted, for the same reason an answer's id is: the note goes
    out live AND is read back off the record, and a mirror may echo the stop it
    heard, so a reader meets the same note more than once and must recognise it
    rather than draw the sentence twice.

    What it is derived FROM is the person's last ROW, because that is what a
    Stop is about and it is the only name for a turn both ends of the chat can
    read off the same rows: the box's own turn id is minted inside a process
    and a server that never saw it cannot re-derive it. A row of the reader's
    is what `answers_a_waiting_message` steps over — it is the asking, not the
    reporting — and both things that start a turn are one: a message, and an
    answer to an ask the agent was blocked on. So two presses on one turn — a
    double click, a retried request, two readers a moment apart — spell one id
    however much the box wrote between them, while the next thing a person
    does starts a turn a later Stop names for itself.
    """
    return f"{STOPPED_NOTE_ID_PREFIX}{turn_seq}"


def stopped_turn_terminal_id(turn: str) -> str:
    """The id of the terminal a box stamps on the turn ``turn`` when a reader
    ended it.

    Derived for the same reason the note's id is, and it is the one row of a
    stop a MACHINE writes: a box restarted mid-stop, a chat re-provisioned onto
    a second box, or a re-hello replaying the stop all stamp it again, and a
    minted id would put a second terminal for one turn on every reader's
    transcript — the exact thing the box's own once-per-turn barrier cannot
    prevent, because that barrier lives in one process's memory.
    """
    return f"stopped-{turn}"


def recorded_answer_entry(event: RecordedResolution) -> dict[str, Any]:
    """The transcript entry the server writes for an answer a reader gave to an
    ask no machine was holding: the resolution event the box's mirror already
    understands, in the envelope every other row wears, under
    :data:`RECORDED_ANSWER_ROLE`."""
    return ChatTranscriptEntry(
        event_id=event.event_id,
        role=RECORDED_ANSWER_ROLE,
        kind=event.event_type,
        payload=event.model_dump(mode="json"),
    ).model_dump(mode="json")


#: What a stopped turn's transcript note says, with the person who stopped it.
#: One line: who ended the turn is the only fact the reader did not already
#: have from the turn going quiet.
STOPPED_NOTE = "Stopped by {who}."

#: What the note says when the stopper has no name to show. The row still
#: carries their user id; this is only what is rendered.
STOPPED_BY_A_MEMBER = "a member of this workspace"


#: The id a note the machine writes ABOUT ITSELF is built from — "the
#: workspace restarted while it was answering", "the schema is still loading".
#: Every row of such a note (its message, its text, its close) takes its event
#: id from that id, so the prefix is on the row itself and survives the restart
#: that wrote it. That is the whole point: which rows answer nothing was a set
#: held in ONE process's memory, so the box that came back and the server never
#: agreed on it, and the two readings of one transcript disagreed about whether
#: a message was still waiting.
ASIDE_NOTE_PREFIX: Final[str] = "aside-"


def aside_note_id(token: str) -> str:
    """The id for a note that answers nothing, from a token of the writer's."""
    return f"{ASIDE_NOTE_PREFIX}{token}"


def answers_a_waiting_message(*, role: str, event_id: str) -> bool:
    """Whether this transcript row means the machine took up the messages above
    it — the one rule for reading a transcript back.

    Two readers ask it of the same rows and must get the same answer: the box,
    deciding which of a reader's messages it still owes a turn, and the server,
    deciding which of them a Stop means were never run. A rule spelled twice is
    how a message ends up cancelled on one side and re-run on the other.

    A reader's own row answers nothing — it IS the question. Everything else
    was written by the machine and stands for work on the messages before it,
    except a note the machine wrote about itself, which reports on the machine
    and leaves every question above it exactly as waiting as it was.
    """
    return role != "user" and not event_id.startswith(ASIDE_NOTE_PREFIX)


def reads_as_prompt(*, role: str, kind: str | None) -> bool:
    """Whether a transcript row reads back as a person's message.

    A user row of kind ``prompt``, or of no kind at all (a row from before
    rows carried one). The box's catch-up runs such a row as the person it
    names, so the server writes them and no peer may publish one. A user row
    under a harness kind (the echo of a message the harness accepted) is not
    a question and is published like any other event.
    """
    return role == "user" and (not kind or kind == PROMPT_KIND)


def prompt_cancelled_event_id(message_id: str) -> str:
    """The event id "this message was never run" is recorded under.

    Derived from the message it is about, because BOTH ends write it: the box
    when a Stop empties the lane it was queued in, and the server when a Stop
    lands on a message no machine has begun answering yet. A stop that reaches
    both is one line, not two, because they spell the same id.
    """
    return f"cancelled-{message_id}"


def prompt_cancelled_entry(event: PromptCancelled) -> dict[str, Any]:
    """The transcript row for a message a Stop means was never run.

    ``system`` because it is nobody speaking — the same role the box's own
    publish gives this event, so a reader cannot tell which end noticed first.
    """
    return ChatTranscriptEntry(
        event_id=event.event_id,
        role="system",
        kind=event.event_type,
        payload=event.model_dump(mode="json"),
    ).model_dump(mode="json")


def stopped_turn_entries(
    *, session_id: str, note_id: str, who: str, now: datetime
) -> list[dict[str, Any]]:
    """The transcript rows a reader's Stop leaves behind: one system message
    naming who ended the turn. Written by the SERVER because only the server
    knows which member pressed Stop."""
    return system_note_entries(
        session_id=session_id,
        note_id=note_id,
        text=STOPPED_NOTE.format(who=who or STOPPED_BY_A_MEMBER),
        now=now,
    )


def system_note_entries(
    *, session_id: str, note_id: str, text: str, now: datetime
) -> list[dict[str, Any]]:
    """One system message saying ``text``, as transcript rows.

    It is the same three-event system note the box writes for anything it does
    outside a turn (a message, its text, its close), so it renders in every
    reader with no new renderer. ``note_id`` makes the three event ids, so a
    note recorded twice under the same id is one note.
    """
    return [
        ChatTranscriptEntry(
            event_id=f"{note_id}-created",
            role="system",
            kind="message.created",
            payload=MessageCreated(
                event_id=f"{note_id}-created",
                time=now,
                session_id=session_id,
                message_id=note_id,
                role="system",
            ).model_dump(mode="json"),
        ).model_dump(mode="json"),
        ChatTranscriptEntry(
            event_id=f"{note_id}-text",
            role="system",
            kind="part.created",
            payload=PartCreated(
                event_id=f"{note_id}-text",
                time=now,
                session_id=session_id,
                part=TextPart(
                    part_id=f"{note_id}-part",
                    message_id=note_id,
                    text=text,
                    synthetic=True,
                ),
            ).model_dump(mode="json"),
        ).model_dump(mode="json"),
        ChatTranscriptEntry(
            event_id=f"{note_id}-done",
            role="system",
            kind="message.completed",
            payload=MessageCompleted(
                event_id=f"{note_id}-done",
                time=now,
                session_id=session_id,
                message_id=note_id,
                finish_reason="stop",
            ).model_dump(mode="json"),
        ).model_dump(mode="json"),
    ]


#: The transcript kind of a permission-mode change. Written by the server only
#: -- it names the member who changed the mode, and a machine is not a party
#: that gets to make that claim -- so the document refuses it from anyone else.
MODE_CHANGED_KIND: Final[Literal["mode.changed"]] = "mode.changed"

#: The transcript kind of a model or effort change. Server-only for the same
#: reason: it names who made the change.
MODEL_CHANGED_KIND: Final[Literal["model.changed"]] = "model.changed"

#: Transcript kinds only the server may append.
SERVER_ONLY_KINDS: Final[frozenset[str]] = frozenset({MODE_CHANGED_KIND, MODEL_CHANGED_KIND})


class ModeChangeRecord(VersionedModel):
    """A permission-mode change, as the transcript records it.

    A row rather than only the chat's ``permission_mode`` so that every reader
    -- the web transcript, a Slack thread -- sees WHEN the agent's stance
    changed, who changed it and from where, in order with everything else.
    The decider fields share their names with a resolution's so the one
    decider-claim strip covers both. The row's id is an aside
    (:func:`aside_note_id`): a mode change answers no waiting message.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["mode.changed"] = MODE_CHANGED_KIND
    event_id: str
    time: datetime | None = None
    session_id: str = ""
    mode: str
    previous_mode: str | None = None
    decided_by_user_id: str | None = None
    decided_by_name: str | None = None
    decided_via: Literal["web", "slack"] | None = None


def mode_changed_entry(record: ModeChangeRecord) -> dict[str, Any]:
    """The transcript row a mode change is written as."""
    return ChatTranscriptEntry(
        event_id=record.event_id,
        role="system",
        kind=MODE_CHANGED_KIND,
        payload=record.model_dump(mode="json"),
    ).model_dump(mode="json")


class ModelChangeRecord(VersionedModel):
    """A change of the model or the reasoning effort a chat runs on, as the
    transcript records it.

    Modelled on :class:`ModeChangeRecord`: a row, so every reader sees when
    the chat moved, who moved it and from where, in order with the turns
    around it. It applies from the next turn the box starts; the turn records
    themselves carry the model that actually ran each turn. A change of effort
    alone is a change too (``model_id == previous_model_id``). The row's id is
    an aside: it answers no waiting message.

    ``decided_via`` is a plain string rather than a closed set: a reader of an
    older build meets the value a newer surface wrote (an editor, the CLI) and
    must still load the row.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["model.changed"] = MODEL_CHANGED_KIND
    event_id: str
    time: datetime | None = None
    session_id: str = ""
    model_id: str = ""
    effort: str | None = None
    display_name: str = ""
    previous_model_id: str | None = None
    previous_effort: str | None = None
    previous_display_name: str | None = None
    decided_by_user_id: str | None = None
    decided_by_name: str | None = None
    decided_via: str | None = None


def model_changed_entry(record: ModelChangeRecord) -> dict[str, Any]:
    """The transcript row a model or effort change is written as."""
    return ChatTranscriptEntry(
        event_id=record.event_id,
        role="system",
        kind=MODEL_CHANGED_KIND,
        payload=record.model_dump(mode="json"),
    ).model_dump(mode="json")


class ChatPromptRecord(VersionedModel):
    """A person's message, as ``chat_messages.payload`` records it.

    ``client_id`` is the browser's own idempotency key (it makes the entry's
    event id), and ``user_id`` is who the server decided was speaking — never
    who the body claimed.

    ``context`` is what the model reads before the words without it being the
    person's words: the briefing and channel history a Slack thread starts
    with, or who is speaking when it is not the chat's owner. The box hands it
    to the model on the hidden per-turn channel; a transcript reader shows
    ``text`` alone, so the person's bubble is what they typed.
    """

    # 1.1.0: ``attachments`` (additive) — one :class:`FilePart` per Files node
    #        the sender linked to the chat and named on this message.
    # 1.2.0: ``context`` (additive) — agent-only context recorded beside the
    #        words, never rendered as them.
    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    kind: Literal["prompt"] = PROMPT_KIND
    text: str = ""
    client_id: str = ""
    user_id: str = ""
    attachments: list[FilePart] = Field(default_factory=list)
    context: str = ""


def prompt_entry_id(client_id: str) -> str:
    """The transcript id a person's message gets.

    Derived from the client's own id so a retried send — the browser resending
    after a dropped response — lands on the entry it already made instead of
    saying the same thing twice. Spelled here because the server mints it, the
    box names messages by it, and a reader matches what it is showing on it:
    three places that have to agree on one string.
    """
    return f"usr:{client_id}"


class PromptRelay(VersionedModel):
    """A person speaking, on its way to the box that runs the chat.

    ``message_id`` and ``seq`` are the recorded row's identity, so a message
    the box catches up on runs through exactly the path a live one does.
    """

    # 1.1.0: ``attachments`` (additive) — the same parts the recorded entry
    #        carries, so the box materializes the files before the turn starts.
    # 1.2.0: ``context`` (additive) — the recorded entry's agent-only context,
    #        so a live relay and a caught-up row compose the same prompt.
    # 1.3.0: ``at`` (additive) — the recorded row's ``created_at``, so a reader
    #        who hears the relay shows the same time a reader of the row does.
    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    kind: Literal["prompt"] = PROMPT_KIND
    message_id: str = ""
    seq: int = 0
    text: str = ""
    client_id: str = ""
    user_id: str = ""
    attachments: list[FilePart] = Field(default_factory=list)
    context: str = ""
    at: datetime | None = None


class AnswerRelay(VersionedModel):
    """An answer to an ask the running turn is blocked on.

    It travels under the ``prompt`` kind — an answer is speaking in the chat,
    and the gate it passes is the same — and is told apart by naming the ask.
    Exactly one of ``option_id`` (a permission choice), ``answers`` (a
    question's fields) or ``reject`` is meaningful; nothing is decided here, so
    all three are optional and the box checks what it got against the ask it
    actually has outstanding.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    kind: Literal["prompt"] = PROMPT_KIND
    interrupt_id: str = ""
    user_id: str = ""
    option_id: str | None = None
    answers: list[list[str]] | None = None
    reject: bool = False
    reason: str | None = None
    #: What the reader wrote for the model alongside ``answers``. Added in 1.1.0.
    note: str | None = None


class ModeRelay(VersionedModel):
    """The stance a person put this chat's session into.

    The mode is DECIDED and recorded by the route before this is minted — the
    chat row is the durable answer, and this is how the box running the session
    right now hears about it. A box that is not running the chat simply never
    sees it and reads the row when it next opens the session, which is why the
    relay carries no acknowledgement.

    ``user_id`` is the member who switched, stamped by the route from the
    authenticated caller and never taken from a request body. It is what lets
    the box attribute the switch, and what a non-member's forged relay cannot
    produce.

    The mode is constrained to the stances a cloud chat may be in, and a body
    naming anything else — a word a newer server invented, a retired one, a
    number — degrades to ``read_only`` rather than raising. Both halves of that
    matter: this is parsed on a live socket, where an exception would take the
    chat down instead of refusing one relay; and the direction it degrades in is
    the FLOOR, so the failure mode of an unreadable mode is a session that asks
    about everything, never one that asks about nothing.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["mode"] = "mode"
    mode: CloudRelayMode = "read_only"
    user_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _unknown_mode_is_the_floor(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("mode") in CLOUD_RELAY_MODES:
            return data
        return {**data, "mode": "read_only"}


class ModelRelay(VersionedModel):
    """The model a person moved this chat's session onto.

    The same shape as the mode switch, for the same reason: the chat row is the
    durable answer the box reads when it opens the session, and this is how a
    box already running the chat hears about the switch without waiting for a
    resume. The route resolves the pick against the model catalog BEFORE minting
    this, so what arrives here has already been confirmed to be a model this
    workspace can run — the box files it under the right provider rather than
    re-deciding.

    ``pin`` is the resolved :class:`ChatModelPin` as the chat row now stores it,
    carried whole rather than as a bare id: the box files a model under its
    provider from the catalog's facts (the wire, the offered efforts, the
    limits), and it cannot look those up itself on a gateway it may not reach.
    Carrying the same dict the row carries means the box translates a switch
    through the one translation it already applies to the row it read at open
    time, instead of a second, divergent one.

    An empty ``pin`` is refused by the box rather than guessed at: the
    alternative is a reader who believes they moved a chat onto a cheaper model
    and a box that silently stayed on the expensive one. ``user_id`` is stamped
    by the route from the authenticated caller, never taken from a request body.
    """

    # 1.1.0: ``previous_model_id``, ``decided_via`` and ``ledger`` (additive) —
    #        the server's view of the reasoning formats the chat's history
    #        carries, so the box re-checks the switch against it.
    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    kind: Literal["model"] = "model"
    pin: dict[str, Any] = Field(default_factory=dict)
    user_id: str = ""
    previous_model_id: str = ""
    decided_via: str = "web"
    #: ``None`` from a server that sends no ledger: the box then skips its check.
    ledger: list[str] | None = None


class StopRelay(VersionedModel):
    """Stop the turn this chat's session is running right now.

    The same shape as the mode and model switches, and for the same reason: a
    box that is running the chat cancels its turn the moment this arrives, and
    a box that is not running it never sees the relay and loses nothing. There
    is no durable row to fall back on because there is nothing to fall back to
    — a turn nobody is running has already ended.

    ``user_id`` is the member who pressed Stop, stamped by the route from the
    authenticated caller and never taken from a request body. It is what lets a
    reader coming back to the transcript see who ended the turn.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["stop"] = "stop"
    user_id: str = ""


class RunQueryRelay(VersionedModel):
    """Re-run a saved query, minted by the route that already decided.

    ``params`` are the values the reader chose; they are never interpolated —
    the box compiles them to bound parameters through the same compiler the
    route validated them with.

    ``user_id`` is the member who pressed Run. Without it the machine has no
    one to attribute the result to and falls back to the account the BOX is
    logged in as — the operator — so a member's own re-run came back with a
    receipt naming somebody else. It is stamped by the route from the
    authenticated caller, never taken from a request body.
    """

    # 1.1.0: ``user_id`` (additive).
    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    kind: Literal["run_query"] = "run_query"
    object_id: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""
    user_id: str = ""


class PromoteRelay(VersionedModel):
    """Promote a tool result out of the transcript into a workspace object."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal["promote"] = "promote"
    object_id: str = ""
    event_id: str = ""


class RawRelay(VersionedModel):
    """A relay whose kind this reader does not know.

    A publisher's own relay carries no kind at all and travels unchanged; a
    kind a newer server invents lands here too. Either way the body rides along
    intact instead of raising, and the box logs that it ignored it.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: str = ""


_relay_kind_tag = make_unknown_tag_discriminator(set(RELAY_KINDS), field="kind")


def _relay_tag(value: Any) -> str:
    """The union's tag: the wire ``kind``, except that a ``prompt`` naming an
    outstanding ask is an answer. The routes have always sent an answer under
    the prompt kind, so the tag is read from the body rather than from ``kind``
    alone — changing the wire here would strand every box in the field."""
    tag = _relay_kind_tag(value)
    if tag != PROMPT_KIND:
        return tag
    interrupt = value.get("interrupt_id") if isinstance(value, dict) else None
    if interrupt is None:
        interrupt = getattr(value, "interrupt_id", None)
    return "answer" if isinstance(interrupt, str) and interrupt else PROMPT_KIND


ChatRelay = Annotated[
    Union[  # noqa: UP007 — a Tag-annotated union needs the explicit form
        Annotated[PromptRelay, Tag(PROMPT_KIND)],
        Annotated[AnswerRelay, Tag("answer")],
        Annotated[ModeRelay, Tag("mode")],
        Annotated[ModelRelay, Tag("model")],
        Annotated[StopRelay, Tag("stop")],
        Annotated[RunQueryRelay, Tag("run_query")],
        Annotated[PromoteRelay, Tag("promote")],
        Annotated[RawRelay, Tag("__unknown__")],
    ],
    Discriminator(_relay_tag),
]
"""Every body a chat relay can carry, tagged by its wire ``kind``."""

CHAT_RELAY_ADAPTER: TypeAdapter[
    PromptRelay
    | AnswerRelay
    | ModeRelay
    | ModelRelay
    | StopRelay
    | RunQueryRelay
    | PromoteRelay
    | RawRelay
] = TypeAdapter(ChatRelay)
"""Validate a relay body: ``CHAT_RELAY_ADAPTER.validate_python(body)``."""


__all__ = [
    "ASIDE_NOTE_PREFIX",
    "CHAT_RELAY_ADAPTER",
    "CLOUD_RELAY_MODES",
    "MODEL_CHANGED_KIND",
    "MODE_CHANGED_KIND",
    "PROMPT_KIND",
    "RECORDED_ANSWER_ROLE",
    "RELAY_KINDS",
    "RESOLUTION_KINDS",
    "SERVER_ONLY_KINDS",
    "STOPPED_BY_A_MEMBER",
    "STOPPED_NOTE",
    "STOPPED_NOTE_ID_PREFIX",
    "AnswerRelay",
    "ChatPromptRecord",
    "ChatRelay",
    "ChatTranscriptEntry",
    "CloudRelayMode",
    "ModeChangeRecord",
    "ModeRelay",
    "ModelChangeRecord",
    "ModelRelay",
    "PromoteRelay",
    "PromptRelay",
    "RawRelay",
    "RecordedResolution",
    "RunQueryRelay",
    "StopRelay",
    "TranscriptRole",
    "answers_a_waiting_message",
    "aside_note_id",
    "mode_changed_entry",
    "model_changed_entry",
    "prompt_cancelled_entry",
    "prompt_cancelled_event_id",
    "prompt_entry_id",
    "reads_as_prompt",
    "recorded_answer_entry",
    "recorded_answer_event_id",
    "stopped_turn_entries",
    "stopped_turn_note_id",
    "stopped_turn_terminal_id",
    "system_note_entries",
]
