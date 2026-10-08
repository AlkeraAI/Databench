"""The HTTP shapes of the chat and object surface.

These only exist in flight (a request body, a response model), so they are
plain ``BaseModel``s. The persisted half (the specs and the result envelope) is
versioned.

Three rules the shapes encode:

* ``expected_version`` is a request field, never a column, and ``0`` is not
  "no precondition". A write naming it against a live row is refused with 409.
* Every free-form JSON field carries an explicit ``title``. The OpenAPI
  generators name an inline object after its property, so three fields called
  ``spec`` would collide and the generator would silently drop their models.
* Nothing here carries a credential. A receipt names a connection and a role;
  a machine binding names an id and a status. No field can hold a secret.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from alkera_core.authz.chat_scope import SCOPE_ORG, SCOPE_PRIVATE, SCOPE_TEAM_PREFIX, team_of_scope
from alkera_core.chat_refusals import ChatRefusalKind, refusal_kind_of
from alkera_core.config import get_settings
from alkera_core.files.providers.registry import object_web_path
from alkera_core.models.workspace_object import ObjectType
from alkera_core.schemas.objects.specs import (
    MAX_EFFORT_LENGTH,
    MAX_MODEL_ID_LENGTH,
    MAX_TEMPLATE_BRIEF_LENGTH,
    ChatModelPin,
    CloudPermissionMode,
    MachineStatus,
    SessionState,
)
from alkera_core.schemas.org_machines import MachineUnavailableRead
from alkera_core.status import StatusFact

#: Page sizes for the two listings and the row reader.
#:
#: A page size is a transport unit, not a ceiling on what anyone may see: a
#: caller reads everything by following ``next_cursor``, and the listings cut to
#: what the reader may see AFTER the page is taken, so a page can come back
#: short with more behind it. These are sized so the common case — every chat an
#: org has — is one round trip and paging is the exception rather than the rule.
DEFAULT_LIST_LIMIT = 1000
MAX_LIST_LIMIT = 5000
DEFAULT_MESSAGE_LIMIT = 200
MAX_MESSAGE_LIMIT = 500
DEFAULT_ROW_LIMIT = 50
MAX_ROW_LIMIT = 1000

#: The longest title a caller may PROPOSE, in characters — a title is display
#: text with no filesystem under it, so it is bounded in characters rather than
#: the bytes a name is bounded in, and a three-byte script gets the same room as
#: English. It bounds a heading, not a document: every surface that shows a
#: title shows it on one line beside other things. Only incoming titles are
#: judged; no read shape carries this bound, so a row stored under an older,
#: looser ceiling still loads for the person who owns it.
MAX_TITLE_LENGTH = 200
#: The longest message a person may send, and the ABSOLUTE ceiling the schema
#: publishes. It is deliberately far above anything anyone types or pastes: over
#: it the POST is refused and the message is LOST — there is no draft to go back
#: to — so the number exists to bound one request, never to judge a prompt. The
#: operational ceiling is ``settings.chat_message_max_chars``, which defaults to
#: this and which a deployment may set lower.
MAX_MESSAGE_TEXT_LENGTH = 1_000_000
MAX_CLIENT_ID_LENGTH = 128
#: ``team:`` plus a UUID is 41 characters; the bound keeps a garbage scope short.
MAX_VISIBILITY_SCOPE_LENGTH = 64
#: How many Files nodes one message may name. A message is a relay, not a
#: transfer: a body that named a thousand nodes would be a thousand authorization
#: decisions on one request.
MAX_MESSAGE_ATTACHMENTS = 20

#: Bounds on an answer to an ask. An ask's id and option ids are minted by the
#: harness, and a question's answers are the labels it offered, so these cap a
#: forged relay rather than describe anything a real client sends.
MAX_INTERRUPT_ID_LENGTH = 255
MAX_OPTION_ID_LENGTH = 64
MAX_ANSWER_PROMPTS = 32
MAX_ANSWER_CHOICES = 32
#: An answer is a line typed into a card rather than a document, and one body
#: may carry ``MAX_ANSWER_PROMPTS`` of them, so it keeps its own bound instead of
#: riding the message ceiling — a body sized like thirty-two messages is one
#: nobody sends and every process has to buffer.
MAX_ANSWER_LENGTH = 100_000
MAX_REASON_LENGTH = 1000
#: A note a reader sends with an answer: guidance for the model, typed into the
#: card, so a few paragraphs rather than a document.
MAX_ANSWER_NOTE_LENGTH = 4000
#: A machine's reason for not delivering a promoted result's payload.
MAX_FAILURE_REASON_LENGTH = 500


class ChatSessionRead(BaseModel):
    """A conversation as a list or a detail view shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    #: Who publishes the chat — the daemon binds every relay to this user.
    owner_user_id: UUID
    #: The owner's name as people see it. A box names the chat's agent by it
    #: ("Alkera agent for <name>") wherever its work shows live. Set on the
    #: listing and the single read; empty from a server that predates it.
    owner_display_name: str = ""
    #: The org the chat belongs to: the root team id every org-scoped row,
    #: token and policy names it by. A box serving several orgs keys every
    #: per-tenant store, cache and credential on it, so it reads the org off
    #: this row and from nowhere else. The server sets it on every read;
    #: ``None`` only from a server that predates the field, which a box treats
    #: as a chat it cannot place in any tenant.
    org_id: UUID | None = None
    machine_id: str | None
    machine_status: MachineStatus
    #: Why the bound machine cannot publish this chat, in the gateway's words,
    #: while ``machine_status`` is ``refused``; ``None`` otherwise.
    machine_refusal_reason: str | None = None
    #: The kind of that refusal, which is what a reader is shown in place of
    #: the reason; ``None`` when there is none or the box sent no kind.
    machine_refusal_kind: ChatRefusalKind | None = None
    #: Whether that refusal is final: no box will finish a turn the chat shows
    #: running. ``False`` for a wait, an unknown kind, and no refusal.
    machine_refusal_final: bool = False
    #: When a reader opened this chat while it was asleep and nothing has
    #: answered yet. The box reads it off the row and re-takes the chat; it is
    #: cleared the moment the box reports the chat awake.
    wake_requested_at: datetime | None = None
    #: How many times the server has ended this chat's service out from under
    #: its box. A box that took the chat at a lower value drops it.
    end_seq: int = 0
    created_at: datetime
    updated_at: datetime
    last_seq: int
    #: Whether the chat owes a turn: a person's message nothing has answered
    #: yet, or a turn the chat's document still shows working. Carried by the
    #: LISTING, which is what a box walks when it takes chats: it takes these
    #: first, so the chat somebody is waiting on is not queued behind hundreds
    #: that are asleep. ``False`` on a single read.
    pending_turn: bool = False
    #: When anything was last written to the chat's transcript; ``None`` for a
    #: chat with none. Listing only, like ``pending_turn``.
    last_activity_at: datetime | None = None
    #: The chat's sandbox limits: vCPUs and memory in MiB for the sandbox a box
    #: opens this chat in — the org's override where staff set one, else its
    #: plan tier's figure. ``None`` = the box's own default (an enterprise org
    #: with no override: its dedicated box's leak guard). On every read, not
    #: only the listing: the box sizes the chat from whichever read it opens
    #: it on.
    sandbox_vcpu: int | None = None
    sandbox_memory_mb: int | None = None
    #: The model this chat was started on, as the gateway catalog described it.
    #: ``None`` for a chat created before the picker existed — the box runs its
    #: own default, and the composer says so rather than naming a model nothing
    #: pinned. The whole pin rides the wire, not just an id: the BOX reads this
    #: row to open the chat, and it needs the catalog's facts (the wire, the
    #: offered efforts, the limits) to file the model under the right provider.
    model: ChatModelPin | None = None
    #: The permission stance the box opens this chat's session in.
    permission_mode: CloudPermissionMode = "read_only"
    #: What a permission card says in place of Allow on a write in this
    #: chat's stance, where the box would discard an approval; ``None`` when it
    #: acts on one. The server's verdict, so no surface keeps the stance table.
    approval_refusal: str | None = None
    #: Where this chat's agent session stands (``asleep`` / ``starting`` /
    #: ``waking`` / ``queued`` / ``awake`` / ``working``), for the reader's
    #: banner and the rail. Read
    #: from what the box last reported, the machine it is bound to, a pending
    #: wake and the turn the transcript owes. A field an older reader ignores.
    session_state: SessionState = "asleep"
    #: Where the chat stands as every surface draws it: the pill's word, its
    #: tone, why, one sentence and since when, all written here. Decided on
    #: what the chat's worker reports (the stamp a running turn renews, how
    #: long a sent message has waited) before where the chat was placed, so
    #: a turn nobody is running does not read as working. ``None`` for a chat
    #: that has never run and owes nothing.
    status: StatusFact | None = None
    #: Whether THIS caller may speak in the chat, decided by the same policy
    #: over the same facts the send route decides on. Reading a chat and
    #: driving its agent are different rungs, so a composer that renders on a
    #: read alone offers a box that answers 403 — and a reader shared at "Can
    #: view" finds that out by typing. Published on the list and on the single
    #: read; it is a capability hint, not a grant, and every send is decided
    #: again on its own request.
    can_send: bool = False
    #: Whether THIS caller may delete the chat, decided by the policy the
    #: delete route decides on: the owner's or an org admin's, never a rung a
    #: share grants. A hint for hiding the control, like ``can_send``.
    can_delete: bool = False
    #: Whether THIS caller may answer an ask with a standing option ("Always
    #: allow"), decided by the policy the answer route decides on: the chat's
    #: own owner. A hint for offering the option, like ``can_send``.
    can_answer_always: bool = False
    #: Whether the chat is unread for THIS caller: activity after their own
    #: read mark that they did not author (the agent ended a turn, or someone
    #: else sent a message), or they marked it unread. Per person, set by the
    #: listings; ``False`` on a single read and for a box.
    unread: bool = False
    #: Whether the agent is waiting on an ask in this chat that THIS caller
    #: may answer. Shown above ``unread``. Listings only, like ``unread``.
    needs_you: bool = False
    #: The Files node this chat IS — a chat is a folder in the drive, and this
    #: is the id of that folder. The box mounts the chat's working folder BY
    #: THIS ID rather than by rebuilding a path from a name, which would break
    #: the moment anyone renamed the chat (and would guess wrong for two chats
    #: with the same title). ``None`` where Files is disabled, or for a chat
    #: created before the drive existed.
    files_node_id: UUID | None = None
    #: The drive that folder is on. A person's chats are all on their org's
    #: drive, which their credential already names; a box on its own machine
    #: credential serves chats across the orgs it is assigned to, has no drive
    #: of its own, and addresses every Files node by the drive AND the node —
    #: so the chat says which drive its folder is on. ``None`` exactly when
    #: ``files_node_id`` is.
    files_drive_id: UUID | None = None
    #: The Files nodes linked to this chat, in attach order. Ids only: a name,
    #: a size and a mime type are read back through
    #: ``GET /chats/{id}/attachments``, which decides each node per request.
    #: Carried by the SINGLE-chat read, which a reader has opened and is about
    #: to render. A LISTING leaves it empty and publishes ``attachment_count``
    #: instead — fifty conversations that each hold a thousand files would
    #: otherwise be a fifty-thousand-id response a rail renders none of.
    attachments: list[str] = Field(default_factory=list)
    #: How many Files nodes are linked to this chat. On every read, list and
    #: single alike, so a rail can say "3 files" without being handed three ids
    #: it does not use — or a thousand it cannot.
    attachment_count: int = Field(default=0, ge=0)
    #: The ``.alkerareport`` / ``.alkeraquery`` folder this chat was started
    #: from, when it was started from one. A replication context, not an
    #: attachment: the box reads the folder's ``spec.json`` and ``README.md``
    #: into the chat's context so the agent asks the questions the context
    #: declares before it re-runs anything.
    source_node_id: UUID | None = None
    #: The saved object that folder IS. Both are published because they answer
    #: different questions: a browser links to the NODE (that is where the
    #: folder lives in the drive), and the box reads the context through the
    #: OBJECT, which survives the node being renamed, moved or trashed.
    source_object_id: UUID | None = None
    #: Whether the chat's folder is in the trash. ``files_node_id`` goes back to
    #: ``None`` the moment the folder is trashed, which on its own reads exactly
    #: like a chat that never had a folder at all — so a Files panel would show
    #: the same empty state for "this chat has no drive" and for "someone threw
    #: your working directory away". This separates the two.
    files_node_trashed: bool = False
    #: The workspace this chat is in (``GET /api/v1/workspaces/{id}``). Every
    #: chat has one; ``None`` only for a row the migration that adopts every
    #: chat into a workspace has not reached yet.
    workspace_id: UUID | None = None
    #: What the workspace's folder is: ``native`` for a workspace that owns a
    #: ``.alkeraworkspace`` folder (a shared ``files/`` tree every chat in it
    #: works in, and each chat's records under ``.chats/``), ``adopted`` for a
    #: workspace of one whose folder is the chat's own, ``None`` when unknown.
    #: A box serves an ``adopted`` chat exactly as a chat with no workspace.
    workspace_layout: Literal["native", "adopted"] | None = None
    #: The native workspace's folder, the node a box leases to serve every chat
    #: in it. ``None`` for an adopted workspace, and for a chat whose folder is
    #: no longer inside its workspace's folder (moved out), which a box refuses.
    workspace_node_id: UUID | None = None
    #: The native workspace's shared ``files/`` tree: the root every chat in it
    #: sees as its home. Same drive as the chat's folder. ``None`` exactly when
    #: ``workspace_node_id`` is.
    workspace_files_node_id: UUID | None = None
    #: The tree this chat's agent works in, the one to open as the chat's
    #: files: the workspace's shared ``files/`` for a member of a native
    #: workspace, the chat's own working directory otherwise. ``None`` when
    #: the chat has no folder (or its folder is in the trash).
    working_node_id: UUID | None = None


class ChatSessionList(BaseModel):
    items: list[ChatSessionRead]
    next_cursor: str | None = None


#: The ABSOLUTE ceiling the schema publishes for a refusal's reason. The
#: operational one is ``settings.chat_refusal_reason_max_chars``, which defaults
#: to this and which a deployment may set lower.
MAX_REFUSAL_REASON_LENGTH = 500


class ChatGatewayTokenRead(BaseModel):
    """The chat's gateway credential, minted for the machine that publishes
    it: a token the gateway bills as the box's own session and the API refuses
    on every route, and when it lapses. The box hands the agent this and
    nothing else."""

    token: str
    expires_at: datetime


class ChatPublisherStateUpdate(BaseModel):
    """What the machine bound to a chat reports about it: ``refused`` (with
    the gateway's reason) when it cannot publish, ``publishing`` when it holds
    the chat's session (again), ``asleep`` when it closed that session — the
    folder pushed and released, the chat resumable by any box. Only a machine
    may say it — the route decides who. ``waiting``: it has the chat's
    message but no free slot to open it in yet."""

    state: Literal["refused", "publishing", "asleep", "waiting"]
    reason: str = Field(default="", max_length=MAX_REFUSAL_REASON_LENGTH)
    #: The kind of a ``refused`` report (``alkera_core.chat_refusals``),
    #: what a reader is shown. A kind this server does not know reads as none.
    refusal_kind: ChatRefusalKind | None = None
    #: Why the box closed the chat, with ``asleep``: it went quiet, or the box
    #: needed its slot. The report is the chat-end transition's, so the
    #: server records it in the same words every other ending uses.
    ending: Literal["idle", "evicted", "drained"] = "idle"
    #: The sandbox of the chat's workspace, as the box running it sees it
    #: (``waking``, ``awake`` or ``asleep``), from a box that runs every chat of
    #: a workspace in one sandbox. ``None`` from a box on the previous build,
    #: which says nothing about a workspace; the workspace's state is then
    #: derived from its chats, as it always was.
    workspace_sandbox: Literal["waking", "awake", "asleep"] | None = None
    #: What the workspace's sandbox holds in memory right now, in MiB, for the
    #: same box; ``None`` when it did not read it.
    workspace_memory_mb: int | None = Field(default=None, ge=0)

    @field_validator("refusal_kind", mode="before")
    @classmethod
    def _an_unknown_kind_is_none(cls, value: object) -> object:
        """A box newer than this server may name a kind it does not know;
        refusing the whole report over it would leave the chat saying nothing."""
        return refusal_kind_of(value)

    @field_validator("reason", mode="before")
    @classmethod
    def _keep_the_head_of_a_long_reason(cls, value: object) -> object:
        """Cut a long reason rather than refuse the whole report.

        A refusal is the only thing that explains an empty chat, and a start
        failure quoting a provider can run long. Refusing the report over its
        length left the banner saying nothing at all, which is strictly worse
        than saying the first few hundred characters of why.
        """
        if not isinstance(value, str):
            return value
        cap = min(get_settings().chat_refusal_reason_max_chars, MAX_REFUSAL_REASON_LENGTH)
        return value[:cap]


class ChatCreate(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    #: The client's own id, so a retried create lands on the same chat.
    client_id: str | None = Field(default=None, max_length=MAX_CLIENT_ID_LENGTH)
    #: The model the reader picked, by the gateway catalog's id. Resolved
    #: against the catalog at create time — an id the catalog does not offer is
    #: refused rather than pinned, because a chat pinned to a model the box
    #: cannot reach fails on its first turn with nothing to say why.
    model: str | None = Field(default=None, max_length=MAX_MODEL_ID_LENGTH)
    #: The reasoning-effort variant, which must be one the chosen model offers.
    effort: str | None = Field(default=None, max_length=MAX_EFFORT_LENGTH)
    #: The stance the chat opens in. Omitted means the reader's saved default —
    #: a composer that offers no stance control must not have to name one, and
    #: a client that names one must not have to create the chat and then
    #: immediately correct it (which is two states the box can observe, and a
    #: window in which the first turn runs in a stance nobody chose). The same
    #: five stances ``PUT /chats/{id}/permission-mode`` accepts and the chat's
    #: mode menu offers, ``auto`` and ``bypass`` included: those stop the agent
    #: ASKING, never widen what it can reach (see ``CloudPermissionMode``).
    permission_mode: CloudPermissionMode | None = None
    #: A ``.alkerareport`` / ``.alkeraquery`` folder to start this chat FROM.
    #: The caller must be able to READ that node — the same Files decision any
    #: other read of it makes, so an unreadable one is the same opaque 404 —
    #: and it must name a report or a saved query; any other node is a 422,
    #: because there is no replication context to hand the agent.
    source_node_id: UUID | None = None
    #: Take the chat warmed ahead for this caller instead of creating one: the
    #: empty composer's first send says so, and gets the spare — its session
    #: already open on the box — re-pinned to the picks in this body where they
    #: differ. With no spare to take (none warmed yet, the last one just taken,
    #: its machine gone) the create proceeds exactly as without the flag, so the
    #: caller sees the same 201 either way. A chat started FROM a context, by
    #: Slack, or by a copy never claims one.
    claim_spare: bool = False
    #: The workspace to start this chat in. Omitted: a workspace of its own
    #: (while a workspace holds one chat) or the caller's main workspace.
    #: Named: the caller needs edit access to it, and it must be a workspace
    #: made as one; refused (409) while ``workspaces_multi_chat`` is off. A
    #: chat started in a workspace never claims a spare.
    workspace_id: UUID | None = None


ChatWakeOutcome = Literal["waking", "awake", "throttled", "machine_unavailable"]


class ChatWakeRead(BaseModel):
    """What opening a chat did to wake it. ``waking``: a wake was asked of the
    chat's box or its machine. ``awake``: nothing slept, so nothing was asked.
    ``throttled``: the chat was opened moments ago and that open's wake stands.
    ``machine_unavailable``: the workspace's own machine is gone, nothing was
    woken, and ``machine_unavailable`` says what was lost and where the opener
    may wake it instead. A refused start is not an outcome here; it is the
    compute refusal's own ``402`` / ``429``."""

    outcome: ChatWakeOutcome
    machine_unavailable: MachineUnavailableRead | None = None


class ChatReadMarkUpdate(BaseModel):
    """``POST /chats/{id}/read``: the highest transcript sequence the caller's
    page has shown. The mark only moves forward and never past the chat's
    ``last_seq``."""

    seq: int = Field(ge=0)


class ChatReadStateRead(BaseModel):
    """The caller's own read state of one chat after a mark moved."""

    chat_id: UUID
    unread: bool
    needs_you: bool


class ChatSendAdmission(BaseModel):
    """Whether a message's author may still drive the chat, asked by the box
    before it starts the turn (a share can be revoked between the send and the
    pickup). Decided by the same rule as a send; ``code`` and ``message`` are
    the refusal's, ``None`` when allowed."""

    allowed: bool
    code: str | None = None
    message: str | None = None


class ChatShareScopeRead(BaseModel):
    """Where a "shared" note the agent writes in this chat lands, asked by the
    box that runs it: a ``team:<uuid>`` or ``org:<uuid>`` scope token, or
    ``None`` when the person the chat acts for is on no team of its org."""

    scope: str | None = None


class ChatSpareState(BaseModel):
    """What the warm call left behind for the caller.

    ``warm`` when a spare stands for them (its session opened, or opening);
    ``none`` when nothing could be warmed — no live machine, the drive refusing
    a create, the catalog unreachable — which is a state the page never
    surfaces: the first message then takes the ordinary create path.
    """

    state: Literal["warm", "none"]


class ChatPermissionModeUpdate(BaseModel):
    """The stance a reader puts a chat's session in.

    Only a stance a cloud chat may run in is spellable (``CloudPermissionMode``).
    A body naming anything else — a word a newer client invented, a retired one,
    a different casing — is a 422, not a silent downgrade to something the
    reader did not ask for.
    """

    mode: CloudPermissionMode


class ChatModelUpdate(BaseModel):
    """The model a reader moves an open chat onto.

    The same two fields a create takes, and resolved against the same catalog by
    the same rule: an id the workspace cannot run a chat on is a 422, never a
    quiet no-op. Naming no model is not spellable here — a create may decline to
    pick one (the box falls back to its own default), but a SWITCH that pins
    nothing would leave the chat on the model it was already on while telling
    the reader it had moved.
    """

    model: str = Field(min_length=1, max_length=MAX_MODEL_ID_LENGTH)
    #: The reasoning-effort variant, which must be one the chosen model offers.
    #: An effort the model does not offer falls back to that model's default
    #: rather than refusing the switch — the reader picked a MODEL.
    effort: str | None = Field(default=None, max_length=MAX_EFFORT_LENGTH)
    #: The model the caller believes the chat is on. When it no longer is
    #: (someone switched in between), the switch is refused with a 409
    #: ``model_changed`` rather than applied over the other person's pick.
    expected_model_id: str | None = Field(default=None, max_length=MAX_MODEL_ID_LENGTH)


class ChatMessageRead(BaseModel):
    """One transcript entry. ``payload`` is the harness event as published —
    capped, so a large tool result arrives as a preview plus a handle."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    chat_id: UUID
    seq: int
    role: Literal["user", "assistant", "tool", "system"]
    kind: str
    event_id: str
    payload: dict[str, Any] = Field(title="ChatMessagePayload")
    created_at: datetime


class ChatMessageList(BaseModel):
    """A page of transcript, with an in-band reset marker.

    ``resync_from`` is set when the caller asked for messages older than the
    oldest one still held: rather than an error and a protocol branch, the page
    itself says "start again from here" and the client re-reads.
    """

    items: list[ChatMessageRead]
    next_after_seq: int
    resync_from: int | None = None
    #: The page's lowest sequence on a backward read (``tail`` / ``before``):
    #: hand it back as ``before`` for the next older page. ``None`` on an empty
    #: page and on a forward read.
    prev_before: int | None = None
    #: Whether a row older than ``prev_before`` is still held. Always ``False``
    #: on a forward read.
    has_older: bool = False
    #: Whether a backward page opens mid-turn or mid-message: its first turn's
    #: prompt row, or the start of a message it carries the end of, sat further
    #: down than the read reaches, so the page below carries the rest and the
    #: reader keeps going. Always ``False`` on a forward read.
    #:
    #: ``False`` is a near-guarantee, with a named bound: the page opens on a
    #: turn and holds every message it carries whole, unless a message has a
    #: row below the read's floor and none of its rows within
    #: ``limit * CHAT_PAGE_MESSAGE_REACH`` rows above it — a gap a real
    #: transcript does not write.
    cut: bool = False


class ChatMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_TEXT_LENGTH)
    client_id: str = Field(min_length=1, max_length=MAX_CLIENT_ID_LENGTH)
    #: Files node ids to send with this message. Each MUST already be linked to
    #: this chat (``POST /chats/{id}/attachments``); naming one that is not is
    #: refused, so a message body is never the thing that attaches a file.
    attachments: list[str] = Field(default_factory=list, max_length=MAX_MESSAGE_ATTACHMENTS)

    @field_validator("text")
    @classmethod
    def _within_the_deployment_ceiling(cls, value: str) -> str:
        """The settings ceiling, read per request so a deployment can tighten it.

        ``max_length`` above is the absolute bound the schema publishes and the
        one a generated client sees; this is the one an operator owns, and it
        only ever sits at or below it.
        """
        cap = get_settings().chat_message_max_chars
        if len(value) > cap:
            raise ValueError(f"a message is at most {cap} characters")
        return value

    @field_validator("attachments")
    @classmethod
    def _attachments_within_the_deployment_ceiling(cls, value: list[str]) -> list[str]:
        """The operator's ceiling on named nodes, read per request.

        ``max_length`` above is the absolute bound a generated client sees; this
        is the one a deployment owns. Unset means the published bound is the
        only one, so widening is a schema decision and tightening is an
        operator's.
        """
        cap = get_settings().chat_message_max_attachments
        if cap is not None and len(value) > cap:
            raise ValueError(f"a message names at most {cap} attachments")
        return value


class ChatAttachmentCreate(BaseModel):
    """The body that links one Files node to a chat."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    node_id: UUID


class ChatAttachmentRead(BaseModel):
    """One linked node, as this caller sees it right now.

    ``state`` is what a reader can do with it *at this moment*: ``available``
    carries the node's name, size and type; ``unavailable`` carries none of
    them. A chat member already knows the chat holds this reference — it is in
    the chat's own spec — so withholding the row entirely would only make an
    attachment they cannot open look like one that was never there, while
    naming it would hand them a file's name they have no claim to.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    node_id: UUID
    name: str
    size: int
    mime: str
    state: Literal["available", "unavailable"] = "available"


class ChatAttachmentList(BaseModel):
    items: list[ChatAttachmentRead]
    #: Where the next page resumes; ``None`` on the last one. The links are
    #: unbounded over a chat's life and each one is decided per request, so the
    #: tab reads a page at a time rather than a year of them at once.
    next_cursor: str | None = None


class ChatInterruptAnswer(BaseModel):
    """An answer to an ask the agent is blocked on.

    Not a message: an ask is answered, not said, so this makes no transcript
    entry — the machine records the resolution as the harness settles it. The
    shape is the relay the daemon validates: it names the outstanding ask by id
    and carries exactly one answer, which is either a permission option the ask
    itself offered, a set of answers to a question's prompts, or a rejection.

    The server does not judge whether the answer is allowed to have that effect;
    the machine does (it refuses an option the ask never offered, an id that is
    not outstanding, and any approval of a write on a read-only session). What
    the shape enforces is that exactly one answer arrives, so an ambiguous relay
    never reaches the machine to be resolved by field order.
    """

    interrupt_id: str = Field(min_length=1, max_length=MAX_INTERRUPT_ID_LENGTH)
    #: A permission ask's chosen option (``allow_once``, ``reject_once``, …).
    option_id: str | None = Field(default=None, min_length=1, max_length=MAX_OPTION_ID_LENGTH)
    #: A question ask's answers: one list of chosen labels per prompt.
    answers: list[list[str]] | None = Field(default=None, max_length=MAX_ANSWER_PROMPTS)
    #: Decline a question outright.
    reject: bool = False
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    #: What the reader wrote for the model alongside a question's answers — on a
    #: plan, the guidance the run starts under, or why it was sent back.
    note: str | None = None

    @model_validator(mode="after")
    def _exactly_one_answer(self) -> ChatInterruptAnswer:
        given = [self.option_id is not None, self.answers is not None, self.reject]
        if sum(given) != 1:
            raise ValueError("an answer carries exactly one of option_id, answers or reject")
        if self.note is not None:
            if self.answers is None:
                raise ValueError("a note rides only with a question's answers")
            if len(self.note) > MAX_ANSWER_NOTE_LENGTH:
                raise ValueError(f"a note is at most {MAX_ANSWER_NOTE_LENGTH} characters")
            self.note = self.note.strip() or None
        if self.answers is not None:
            if not self.answers:
                raise ValueError("answers carries at least one prompt's answer")
            # The operator's ceiling, read per request; it only ever sits at or
            # below the absolute one the schema publishes.
            choices = min(get_settings().chat_answer_max_choices, MAX_ANSWER_CHOICES)
            for prompt in self.answers:
                if len(prompt) > choices:
                    raise ValueError(f"a prompt carries at most {choices} answers")
                for choice in prompt:
                    if len(choice) > MAX_ANSWER_LENGTH:
                        raise ValueError(f"an answer is at most {MAX_ANSWER_LENGTH} characters")
        return self


class PromoteColumn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)


class ChatPromoteRequest(BaseModel):
    """Pin a result the chat produced: the cloud creates the object now and the
    daemon uploads the payload behind it."""

    event_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    columns: list[PromoteColumn] | None = None
    chart_spec: dict[str, Any] | None = Field(default=None, title="PromoteChartSpec")


class WorkspaceObjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    logical_id: str
    namespace: str
    #: The model's own vocabulary, not a copy of it: every type the column
    #: admits is one this shape can carry, the retired kinds that still load
    #: included. What may be CREATED stays narrower (``WorkspaceObjectCreate``).
    type: ObjectType
    title: str
    version: int
    status: str
    spec: dict[str, Any] = Field(title="WorkspaceObjectSpec")
    #: The owner the audience rules narrow to; for a promoted result, the
    #: owner of the chat it came out of.
    owner_user_id: UUID
    visibility_scope: str
    created_at: datetime
    updated_at: datetime
    content_updated_at: float

    #: Where this object opens in the web app, root-relative: the same answer
    #: a Files node's object facet carries, from the one registry, so a client
    #: that reached an object by its id (an old ``/objects/<id>`` link to a
    #: chat or a workspace) is sent to its page rather than guessing it.
    #: Optional on the wire so a client reading an older server still parses.
    web_url: str | None = None

    @model_validator(mode="after")
    def _name_the_page(self) -> WorkspaceObjectRead:
        if self.web_url is None:
            self.web_url = object_web_path(self.type, str(self.id))
        return self


class WorkspaceObjectList(BaseModel):
    items: list[WorkspaceObjectRead]
    next_cursor: str | None = None


class WorkspaceObjectCreate(BaseModel):
    #: A chat is created by the chat routes and a chat template by its own; a
    #: board and an app have no author-facing create yet. What this route mints
    #: is what a reader saves out of a chat, and that is a result. A saved
    #: query and a report were replaced by chat templates, so neither is
    #: spelled here any more — the route answers a create that still names one
    #: by name (``410 object_type_retired``) before this shape is read, so a
    #: client that has not caught up learns the kind is gone rather than that
    #: its literal was unexpected.
    type: Literal["result"]
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    spec: dict[str, Any] = Field(default_factory=dict, title="WorkspaceObjectCreateSpec")
    client_id: str = Field(min_length=1, max_length=MAX_CLIENT_ID_LENGTH)
    #: Who may read the result: ``private`` (the creator alone, the default),
    #: ``org``, or ``team:<uuid>`` naming a team of the creator's org. A result
    #: is its creator's until they say otherwise, the way a chat and a chat
    #: template are; the route refuses a team that is not the org's with the
    #: same not-found a team that does not exist gets.
    visibility_scope: str | None = Field(default=None, max_length=MAX_VISIBILITY_SCOPE_LENGTH)

    @field_validator("visibility_scope")
    @classmethod
    def _audience_is_one_the_grammar_knows(cls, value: str | None) -> str | None:
        if value is None or value in (SCOPE_PRIVATE, SCOPE_ORG):
            return value
        if value.startswith(SCOPE_TEAM_PREFIX) and team_of_scope(value) is not None:
            return value
        raise ValueError("visibility_scope must be 'private', 'org' or 'team:<uuid>'")


class WorkspaceObjectUpdate(BaseModel):
    """``expected_version`` is required and never optional: a write that does
    not say which row it read is a write that did not read one."""

    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    spec: dict[str, Any] | None = Field(default=None, title="WorkspaceObjectUpdateSpec")
    expected_version: int


class ObjectRowsPage(BaseModel):
    """One page of a result's rows, mediated by authz — never a storage URL.

    ``columns`` are the display labels and ``keys`` the stable column names
    behind them, in the same order: a chart's encoding names a key, so a
    renderer binds by ``keys`` and shows ``columns``, and a rename can never
    detach the chart from its data.
    """

    columns: list[str]
    keys: list[str]
    rows: list[list[Any]]
    total: int


class ObjectRerunRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict, title="ObjectRerunParams")


class ObjectRerunAccepted(BaseModel):
    """The run was handed to the machine that owns the chat; the cloud never
    executes SQL."""

    run_id: UUID
    chat_id: UUID


class ObjectPayloadUpload(BaseModel):
    """The daemon's promote upload: the envelope and the receipt that proves it."""

    envelope: dict[str, Any] = Field(title="ResultBlobEnvelopeDocument")
    receipt: dict[str, Any] = Field(title="ResultReceiptDocument")


class ObjectPayloadFailure(BaseModel):
    """The daemon saying the payload behind a promoted result is not coming.

    Without it the object would wait in ``pending_upload`` forever, reading as
    "Saving…" on its page and "0 rows" in the list, so the reason is recorded
    on the object itself.
    """

    reason: str = Field(min_length=1, max_length=MAX_FAILURE_REASON_LENGTH)


class ChatTemplateRead(BaseModel):
    """A chat template as a list row or a detail view shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    version: int
    #: Who saved it. The owner and an org admin may delete it; who may EDIT it
    #: is decided on its Files node, so sharing a folder shares the template.
    owner_user_id: UUID
    created_at: datetime
    updated_at: datetime
    #: The Files node this template IS — a template is a folder in the drive,
    #: holding the files a chat started from it opens with. ``None`` where
    #: Files is disabled.
    files_node_id: UUID | None = None
    #: The prose its author wrote for whoever starts the next chat.
    brief: str = ""
    #: The model the source chat ran on, when it pinned one. A suggestion: a
    #: chat started from this template falls back to the starter's own
    #: preference when the catalog no longer offers it.
    model: ChatModelPin | None = None
    #: The stance the source chat ran in. A chat started from this template
    #: gets the STRICTER of this and the starter's own, never the wider one.
    permission_mode: CloudPermissionMode = "read_only"
    #: The chat it was saved out of, and how far that chat had got. Both are
    #: provenance: reading the source is authorized per request, and the source
    #: may have been trashed since.
    source_chat_id: UUID | None = None
    saved_from_seq: int = 0


class ChatTemplateList(BaseModel):
    items: list[ChatTemplateRead]
    next_cursor: str | None = None


class SaveAsTemplate(BaseModel):
    """Save a chat as the starting point for the next one.

    Everything but the chat is optional because the server can answer for all
    of it: the title from the chat, the brief from its transcript, and the
    destination from the caller's own ``Chat Templates`` folder. A reader who
    wants none of those defaults overrides the one they care about.
    """

    source_chat_id: UUID
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    #: Overrides the digest the server writes from the transcript.
    brief: str | None = Field(default=None, max_length=MAX_TEMPLATE_BRIEF_LENGTH)
    #: Where the template folder lands. ``None`` is the caller's own
    #: ``Chat Templates`` folder, created on demand.
    destination_id: UUID | None = None
    client_id: str | None = Field(default=None, max_length=MAX_CLIENT_ID_LENGTH)


class ChatTemplateUpdate(BaseModel):
    """``expected_version`` is required and never optional: a write that does
    not say which row it read is a write that did not read one.

    Only the two fields a person writes are here. A template's files are
    ordinary files in its folder and are edited there; its spec is not editable
    through the object surface at all.
    """

    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    brief: str | None = Field(default=None, max_length=MAX_TEMPLATE_BRIEF_LENGTH)
    expected_version: int


class PlacesRead(BaseModel):
    """The named folders in a drive that a client opens things into.

    Every field is nullable because a pure read never creates: a caller that
    has not asked for a folder to be ensured learns that it does not exist
    rather than causing it to.
    """

    # The Files surface answers in camelCase, and these are Files folders: a
    # client that reads a drive reads this with the same casing rule.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    home_id: UUID | None = None
    chats_id: UUID | None = None
    chat_templates_id: UUID | None = None


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_MESSAGE_LIMIT",
    "DEFAULT_ROW_LIMIT",
    "MAX_CLIENT_ID_LENGTH",
    "MAX_FAILURE_REASON_LENGTH",
    "MAX_LIST_LIMIT",
    "MAX_MESSAGE_LIMIT",
    "MAX_ROW_LIMIT",
    "MAX_TEMPLATE_BRIEF_LENGTH",
    "MAX_TITLE_LENGTH",
    "ChatCreate",
    "ChatMessageCreate",
    "ChatMessageList",
    "ChatMessageRead",
    "ChatPromoteRequest",
    "ChatSessionList",
    "ChatSessionRead",
    "ChatTemplateList",
    "ChatTemplateRead",
    "ChatTemplateUpdate",
    "ObjectPayloadFailure",
    "ObjectPayloadUpload",
    "ObjectRerunAccepted",
    "ObjectRerunRequest",
    "ObjectRowsPage",
    "PlacesRead",
    "PromoteColumn",
    "SaveAsTemplate",
    "WorkspaceObjectCreate",
    "WorkspaceObjectList",
    "WorkspaceObjectRead",
    "WorkspaceObjectUpdate",
]
