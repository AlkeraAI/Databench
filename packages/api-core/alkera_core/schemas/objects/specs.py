"""The type-specific body of a workspace object, one ``VersionedModel`` per type.

The row carries the shape common to every object (id, title, version, owner,
audience); the type-specific half lives in the JSONB ``spec`` column and is one
of the models here. Persisted ⇒ versioned: a spec written by today's server
must still load in a year, so every change bumps ``SCHEMA_VERSION``, registers
a migration when it breaks, and leaves a fixture behind as evidence.

* :class:`ChatSpec` — where the conversation runs and how far it has got.
* :class:`QuerySpec` — saved SQL with ``{slot}`` parameters. The template is
  never interpolated; the slots compile to real bound parameters through
  ``alkera_core.schemas.objects.query_params`` — the ONE compiler the re-run
  route validates with and the daemon binds with — which is the whole point of
  storing a template rather than a string of SQL.
* :class:`ResultSpec` — a receipted table. The :class:`Receipt` is the trust
  surface: it is what makes an answer credible, so it is immutable, it is
  rendered inline rather than behind a menu, and it names the connection and
  the ROLE, never a credential.
"""

from __future__ import annotations

from typing import Any, ClassVar, Final, Literal

from pydantic import Field

from alkera_core.schemas.objects.result_blob import BlobHandle
from alkera_core.versioning import VersionedModel

#: The machine states a chat's binding can be in. ``none`` means the org has no
#: machine at all: an actionable message, not an error. ``refused``
#: means the machine the chat is bound to is running but the gateway would not
#: let it publish this chat — the machine said so itself, and the reason is on
#: the chat. ``asleep`` means no box holds a session for this chat — the box
#: put the chat to sleep (its folder pushed and released), or the machine
#: itself is asleep — and the next message, or a reader opening it, wakes it.
#: ``stranded`` means the machine the chat was on has left service and no box
#: could take the chat: it waits, bound to the box that last had it, for the
#: next box that serves its org or for its next message to place it.
MachineStatus = Literal[
    "starting", "ready", "draining", "unreachable", "none", "refused", "asleep", "stranded"
]
#: What the box last said about the chat's session: ``awake`` while it holds
#: one, ``asleep`` after it closed it. ``None`` is a chat no box has reported
#: on since the field existed, which reads as the binding always did.
MirrorState = Literal["awake", "asleep"]
#: Where the chat's own agent session stands, as a reader shows it: ``asleep``
#: (no agent running for it), ``starting`` (a message waits and no box has ever
#: held its session: it is being opened for the first time), ``waking`` (a
#: message or a person asked for it and no box has opened it yet), ``queued``
#: (its box has the message but no free slot to open it in yet), ``awake``
#: (a box holds its session, nothing running) and ``working`` (a box holds it
#: and a turn is in flight). Distinct
#: from the machine's status: a box can be ready while this chat sleeps.
SessionState = Literal["asleep", "starting", "waking", "queued", "awake", "working"]
#: The parameter types a saved query may declare — the compiler's own
#: vocabulary (``query_params.PARAM_TYPES``; a test pins the two equal). Each
#: one validates a value before it is bound; there is no "raw" type on purpose.
ParamType = Literal[
    "string", "integer", "number", "boolean", "date", "datetime", "daterange", "enum"
]
#: The engines a saved query may target: exactly those the compiler has a
#: bound-parameter path for (``query_params.ENGINE_STYLES``; pinned equal by a
#: test). An engine with no such path is refused, never interpolated.
QueryEngine = Literal[
    "postgres", "redshift", "clickhouse", "tinybird", "duckdb", "duckdb_local", "sqlite"
]

#: Bounds on the model a chat is pinned to. The ids and effort names are the
#: gateway catalog's own, so these cap a forged create rather than describe
#: anything a real picker sends.
MAX_MODEL_ID_LENGTH = 200
MAX_EFFORT_LENGTH = 32
MAX_MODEL_EFFORTS = 16

#: How long a chat template's brief may be. It is prose its author wrote for
#: whoever starts the next chat, and it rides the box's hidden per-turn channel
#: once, so it is bounded at the row rather than at the wire.
MAX_TEMPLATE_BRIEF_LENGTH = 32_000

#: How a pinned model is reached — the gateway's own ``wire`` field, which
#: decides which provider the box files the model under. A plain literal rather
#: than an import: this package must not depend on the harness.
ModelWire = Literal["anthropic", "openai"]

#: The permission stances a CLOUD chat may be put in — the harness's five.
#: What ``bypass`` hands over is the ASKING, never the boundary: a cloud chat's
#: writes are fenced to its own folder in every mode, and its connected data is
#: read-only in every mode, so a stance that stops asking still cannot reach
#: anything the others could not — and a reader leaving a long job running
#: unattended is exactly who needs it.
#:
#: ``auto`` is the ceiling below ``bypass``: its write and egress middle is
#: cleared by the grounded safety judge the box builds at start, so a request
#: the analyst stances refuse outright — a page no search of the model's own
#: returned — is judged against the turn's goal rather than refused.
CloudPermissionMode = Literal["read_only", "default", "auto", "plan", "bypass"]
#: The stance a spec written before the control existed reads as, and the floor
#: for a saved preference no door could have set. Never a new chat's start.
DEFAULT_CLOUD_PERMISSION_MODE: CloudPermissionMode = "read_only"
#: The stance a new chat starts in when its person saved no default: ask before
#: each change, the same stance the desktop starts in.
NEW_CHAT_PERMISSION_MODE: CloudPermissionMode = "default"
#: The same five as a set, for a caller that has to test a runtime string
#: against them — a saved preference, say, that is stored as a plain string.
CLOUD_PERMISSION_MODES: Final[frozenset[str]] = frozenset(
    {"read_only", "default", "auto", "plan", "bypass"}
)

MAX_SQL_LENGTH = 20_000
MAX_PARAM_NAME_LENGTH = 64
#: A parameter name must be an identifier: it becomes a bound-parameter name in
#: the driver's own namespace, so anything else is a way to smuggle syntax.
PARAM_NAME_PATTERN = r"^[a-z_][a-z0-9_]*$"


class ChatModelPin(VersionedModel):
    """The model a chat was started on, as the gateway catalog described it.

    A chat's model is chosen once, when it is created, and the box that runs the
    chat has to file it under the right provider — so the pin carries the
    catalog FACTS the box needs (``wire``, the offered ``efforts``, the context
    limits), not a model id the box would have to look up again on a gateway it
    may not be able to reach at open time.

    It is deliberately NOT the harness's manifest shape: the server records what
    the catalog said and the box translates it (``build_manifest_model``), so
    the pinning shape stays owned by the harness and the server never invents a
    provider id.
    """

    # 1.1.0: ``reasoning_format`` / ``reads_reasoning_formats`` (additive) — the
    #        reasoning the model emits and reads, copied from the catalog so the
    #        box can re-check a switch without reaching the gateway.
    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    id: str = Field(default="", max_length=MAX_MODEL_ID_LENGTH)
    display_name: str = Field(default="", max_length=MAX_MODEL_ID_LENGTH)
    wire: ModelWire = "anthropic"
    #: The reasoning-effort variants the catalog offered for this model.
    efforts: list[str] = Field(default_factory=list, max_length=MAX_MODEL_EFFORTS)
    #: The variant the chat runs at; ``None`` when the model offers none.
    effort: str | None = Field(default=None, max_length=MAX_EFFORT_LENGTH)
    context_window: int = Field(default=0, ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    reasoning_format: str | None = Field(default=None, max_length=128)
    reads_reasoning_formats: list[str] = Field(default_factory=list, max_length=64)


class ChatSpec(VersionedModel):
    """A conversation: the machine it runs on and the last transcript seq.

    ``machine_id`` is null until the org has a machine; ``machine_status``
    records what the binding looked like when it was made, so a list can render
    without asking the compute plane per row (the live truth is
    ``GET /api/v1/machines/current``). ``last_seq`` mirrors the highest
    ``chat_messages.seq`` so a list can show a chat's length without counting.
    ``publisher_refusal`` is set by the machine itself when the gateway would
    not let it publish this chat (the reason, in the gateway's words) and
    cleared when it publishes again; while set, the chat reads as ``refused``
    whatever the binding said.
    """

    # 1.1.0: ``publisher_refusal`` (additive).
    # 1.2.0: ``attachments`` (additive).
    # 1.3.0: ``model`` (additive) — the model the chat was started on, and
    #        ``permission_mode`` — the stance the box opens its session in.
    # 1.4.0: ``source_node_id`` / ``source_object_id`` (additive) — the
    #        replication context this chat was started from.
    # 1.5.0: ``mirror_state`` / ``wake_requested_at`` (additive) — whether the
    #        box holds a session for the chat, and a reader's request to wake
    #        a slept one.
    # 1.6.0: ``spare`` / ``spare_active_at`` (additive) — a chat warmed ahead
    #        of its first message, and when its owner was last seen on the page.
    # 1.7.0: ``attachments`` deprecated — the links are rows in
    #        ``chat_attachments`` now. The field stays so a spec written by an
    #        older server still reads, and so the back-fill remains auditable
    #        against what it was built from; nothing writes it any more.
    # 1.8.0: ``machine_status`` admits ``stranded`` (additive) — a chat whose
    #        machine left service with nothing to take it.
    # 1.9.0: ``end_seq`` / ``ended_reason`` (additive) — the server ended the
    #        chat's service (alkera_core.objects.chat_end), and why.
    # 1.10.0: ``workspace_id`` (additive): the workspace the chat is in.
    # 1.11.0: ``slot_wait_at`` (additive) — the bound box has the chat's
    #        message but no free slot to open it in yet.
    # 1.12.0: ``turn_end_reason`` / ``turn_end_at`` (additive) — the server
    #        ended a turn that was running, why and when.
    # 1.13.0: ``wake_intent_at`` (additive) — when a person who may run the
    #        chat last opened it, the stamp the open's wake is throttled on.
    # 1.14.0: ``publisher_refusal_at`` (additive): when the box said its
    #        refusal, so one said before a wake or a restart reads as stale.
    # 1.15.0: ``publisher_refusal_kind`` (additive): the kind of the box's
    #        refusal, which is what a reader is shown.
    SCHEMA_VERSION: ClassVar[str] = "1.15.0"

    machine_id: str | None = None
    machine_status: MachineStatus = "none"
    last_seq: int = Field(default=0, ge=0)
    publisher_refusal: str | None = None
    #: When the box last said ``publisher_refusal``, ISO-8601. ``None`` on a
    #: row written before the stamp existed, whose refusal is read as current.
    publisher_refusal_at: str | None = None
    #: The kind of ``publisher_refusal`` (``alkera_core.chat_refusals``).
    #: ``None`` from a box that sends none, or one this server does not know.
    publisher_refusal_kind: str | None = None
    #: Whether the bound box holds a live session for this chat, as the box
    #: itself last reported. The machine's heartbeat says the BOX is alive;
    #: only the box can say the CHAT is — a slept chat on a live box is
    #: otherwise indistinguishable from a served one.
    mirror_state: MirrorState | None = None
    #: When a reader last opened the chat while it was asleep, ISO-8601.
    #: The box's discovery reads it off the row and re-takes the chat; the
    #: box clears it when it reports the chat awake.
    wake_requested_at: str | None = None
    #: When a person who may run the chat last asked, by opening it, for it to
    #: be woken, ISO-8601. One such wake per chat per interval
    #: (``backend.services.chats.wake_intent``), however many tabs open it.
    wake_intent_at: str | None = None
    #: When the bound box said it has this chat's message but no free slot to
    #: open it in, ISO-8601. Cleared by the box's next report on the chat
    #: (serving it, sleeping it, or refusing it).
    slot_wait_at: str | None = None
    #: The model this chat runs on, pinned at creation. ``None`` for a chat
    #: created before the picker existed, or by a client that chose nothing —
    #: the box then runs its own default, which is what it always did.
    model: ChatModelPin | None = None
    #: The permission stance the box opens this chat's session in. ``read_only``
    #: is the floor a cloud chat starts at, so a chat whose spec predates the
    #: control reads exactly as it behaved.
    permission_mode: CloudPermissionMode = "read_only"
    #: Files nodes linked to this chat, as a server before ``chat_attachments``
    #: wrote them. DEPRECATED and no longer read: a chat's links are unbounded
    #: over its life, and a JSONB array is locked, read and rewritten whole on
    #: every attach — the cost of adding the ten-thousandth file grew with the
    #: nine thousand nine hundred and ninety-nine before it. Migration 0144
    #: back-filled the rows from this list and left it where it was, so the
    #: back-fill stays auditable against its source.
    attachments: list[str] = Field(default_factory=list, deprecated=True)
    #: The Files node of the chat template this chat was started FROM, and the
    #: object that folder stands for; on a chat saved before templates existed,
    #: the saved query or report it was started from instead. Both are recorded
    #: because they answer different questions: the node is what the box reads
    #: the folder's members through (and what a rename cannot break), the object
    #: is what the folder IS, which is what survives the node being trashed.
    #: Like ``attachments`` this is a REFERENCE and never a grant — the box
    #: re-reads it under its own credential, and a chat shared wider than the
    #: template does not widen the template.
    source_node_id: str | None = None
    source_object_id: str | None = None
    #: A chat warmed for its owner before they said anything: placed, its
    #: folder leased and its session opened by the box, but hidden from every
    #: surface a person reads until the owner's first message claims it. The
    #: flag is cleared by the claim; a spare nobody claims is reaped whole.
    spare: bool = False
    #: When the owner was last seen on the chat page, ISO-8601 — the page's
    #: heartbeat stamps it, and the sweep reaps a spare whose owner has been
    #: away past the idle cutoff. Meaningful only while ``spare`` is set.
    spare_active_at: str | None = None
    #: Bumped each time the server ends this chat's service out from under a
    #: holder (alkera_core.objects.chat_end). A box records the value it took
    #: the chat at; a higher one on a later read means the chat it is serving
    #: was ended, and it drops it without pushing.
    end_seq: int = Field(default=0, ge=0)
    #: Why the last ending happened, in ``ChatEndReason``'s spelling.
    ended_reason: str | None = None
    #: Why and when the server last ended a turn that was still running, with
    #: no box left to end it: an ending's reason, or the lifecycle's own
    #: (the turn's worker stopped reporting, no machine took the chat). The
    #: chat reads "stopped" for this reason until a message is sent after it.
    turn_end_reason: str | None = None
    turn_end_at: str | None = None
    #: The workspace this chat is in: the object whose owner's connections the
    #: chat uses and whose sharing reaches it. ``None`` only on a row written
    #: before workspaces existed and not yet adopted by the migration that
    #: gives every chat one.
    workspace_id: str | None = None


#: How a workspace's folder came to be. ``native``: the workspace was created
#: as one and owns a ``<Name>.alkeraworkspace`` folder holding ``files/`` and
#: ``.chats/``. ``adopted``: a workspace of one made for a chat that already
#: had a folder, which it points at instead of moving a byte; the chat's
#: folder IS the workspace's folder until the box is keyed by workspace and the
#: tree is re-laid.
WorkspaceLayout = Literal["native", "adopted"]
#: Whose fields say where a workspace runs. ``chat``: derived from its
#: chats, as before. ``workspace``: a box that runs workspaces has reported
#: on it, and its word on which box holds the workspace and whether its
#: sandbox is awake is read off the workspace; the rest stays the chats'.
BindingAuthority = Literal["chat", "workspace"]
#: A workspace sandbox's state as its box reports it.
SandboxState = Literal["waking", "awake", "asleep"]
#: What a workspace is FOR, apart from what its folder is. ``main``: a
#: member's go-to workspace, one per member, where a chat lands when nobody
#: named a place for it. ``project``: made for one piece of work, which every
#: workspace of one adopted for an existing chat also is.
WorkspaceKind = Literal["main", "project"]


class WorkspaceSpec(VersionedModel):
    """A workspace: chats that share one file tree.

    Where it runs is not stored here: each chat's own spec says where that chat
    runs, and :func:`alkera_core.objects.workspaces.effective_binding` derives
    the workspace's binding from its chats. A box that runs workspaces
    reports the two facts only it knows (1.1.0): which box holds the
    workspace and its sandbox's state; the binding reads those off here.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    kind: WorkspaceKind = "project"
    layout: WorkspaceLayout = "native"
    #: The chat whose folder an ``adopted`` workspace points at; ``None`` on a
    #: native one. A reference, never a grant.
    adopted_chat_id: str | None = None
    #: ``workspace`` once a box that runs workspaces reported on it (1.1.0).
    binding_authority: BindingAuthority = "chat"
    #: The box that last reported holding the workspace (1.1.0).
    machine_id: str | None = None
    #: The workspace's sandbox as the box running it last said: ``waking``
    #: while it is being started, ``awake`` while it runs (whether or not a
    #: chat in it is answering), ``asleep`` once it was put away. ``None``
    #: until a box that runs workspaces said anything (1.1.0).
    sandbox_state: SandboxState | None = None
    #: What the sandbox held in memory when the box last said, in MiB (1.1.0).
    sandbox_memory_used_mb: int | None = Field(default=None, ge=0)
    #: When the box last said either, ISO-8601 (1.1.0).
    sandbox_reported_at: str | None = None
    #: The org machine this workspace runs on, by id; ``None`` runs it where
    #: the org's default placement puts it (1.2.0). Written only by a move.
    machine_pin: str | None = None
    #: The org machine the pin named when that machine was deleted, so the next
    #: wake asks where to run instead of silently landing elsewhere (1.3.0).
    #: Cleared by the next move.
    lost_machine_id: str | None = None
    #: When a waker that is not a person moved the workspace off its lost
    #: machine onto the default placement, ISO-8601 (1.3.0). The workspace
    #: shows the move until the next one.
    fell_back_at: str | None = None


class ChatTemplateSpec(VersionedModel):
    """A chat saved as a starting point for the next one.

    A template is not a record of a run and not a replication context: it is a
    brief plus the files a chat should open with. ``brief`` is prose its author
    wrote for whoever starts from it — the box is handed it verbatim and asks
    the starter what should differ this time.

    ``model`` and ``permission_mode`` are a SUGGESTION, not a grant. The reader
    who starts a chat gets their own stance narrowed by this one where this one
    is stricter, never widened by it, so a template cannot hand anybody a
    permission they did not already have.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    #: What the template's author wants the next chat to be told, verbatim.
    brief: str = Field(default="", max_length=MAX_TEMPLATE_BRIEF_LENGTH)
    #: The model the source chat ran on, if it pinned one. ``None`` means the
    #: starter's own preference decides, which is what it does anyway when the
    #: catalog no longer offers the pinned model.
    model: ChatModelPin | None = None
    #: The stance the source chat ran in. ``read_only`` is the floor, so a
    #: template written before this field existed reads as the safest one.
    permission_mode: CloudPermissionMode = "read_only"
    #: The chat this template was saved out of, for provenance. A REFERENCE:
    #: the source may be trashed, and reading it is authorized per request.
    source_chat_id: str | None = None
    #: How far the source chat's transcript had got when the template was
    #: saved, so a later save can say what is new since this one.
    saved_from_seq: int = Field(default=0, ge=0)


class QueryParam(VersionedModel):
    """One declared parameter of a saved query.

    ``name`` is the slot's name in the template and the bound parameter's name
    in the driver. ``enum_values`` is meaningful only for ``type="enum"`` and is
    the closed set a value must belong to.
    """

    # 1.1.0: ``type`` admits ``number`` and ``boolean`` (additive).
    # 1.2.0: ``type`` admits ``datetime`` — the kind a ``{{DateTime(name)}}``
    #        slot declares in the statement itself (additive).
    # 1.3.0: ``prompt`` (additive).
    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    name: str = Field(pattern=PARAM_NAME_PATTERN, max_length=MAX_PARAM_NAME_LENGTH)
    type: ParamType = "string"
    label: str = ""
    required: bool = True
    enum_values: list[str] = Field(default_factory=list)
    #: The question to ASK before re-running — "Which date range?", "Which
    #: subjects of interest?". ``label`` is a form label beside a filled-in
    #: field; this is what an agent handed the saved object says out loud to a
    #: reader who has none of the conversation it was saved out of. Empty means
    #: nobody wrote one, and the agent falls back to ``label`` and the name.
    prompt: str = ""


class QuerySpec(VersionedModel):
    """Saved, parameterised SQL.

    ``sql_template`` carries slots in either spelling — ``{name}`` (and
    ``{name.start}`` / ``{name.end}`` for a date range) or Tinybird's own
    ``{{String(name)}}``, which is what an agent writes when it parameterises a
    statement for that engine. It is compiled — never formatted — into the
    engine's own placeholder syntax with the values bound alongside.

    ``defaults`` is what the statement was run with when it was saved, so the
    re-run form opens on the answer the reader was just looking at and one
    field can be changed rather than all of them. They are values, not SQL:
    they are validated and bound exactly like a value someone typed.

    A saved query is a REPLICATION CONTEXT, not a record of one run: the
    questions a fresh reader has to answer before it can be re-run are the
    ``prompt`` on each :class:`QueryParam`, and a value baked into
    ``sql_template`` instead of declared as a parameter is a question nobody
    will be asked.
    """

    # 1.1.0: ``engine`` admits every engine the compiler binds for (additive).
    # 1.2.0: ``defaults`` (additive).
    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    sql_template: str = Field(default="", max_length=MAX_SQL_LENGTH)
    params: list[QueryParam] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None
    engine: QueryEngine = "postgres"
    #: The chat this query was saved out of. A re-run is relayed onto that
    #: chat's channel, because the machine bound to that chat is the thing that
    #: executes SQL — the cloud never does (R-H).
    source_chat_id: str | None = None


class ResultColumn(VersionedModel):
    """A column of a result, with the label the author chose for it."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    name: str
    label: str = ""


class Receipt(VersionedModel):
    """Why a result is believable — immutable, and rendered inline.

    Every field is provenance a reader can check: the SQL that ran, which
    connection and which ROLE it ran as (never a credential), the
    engine, the full acting-principal chain, when it ran, how much it returned
    and how long it took, the parameter values it was given, and the shared
    definitions it leaned on.

    ``principal_chain`` is an ``ActorChainRecord`` dump rather than the typed
    model, so this schema module stays free of an import from the authz package
    that persists it; the writer validates it as one before it is stored.

    ``role``, ``executed_at`` and ``duration_ms`` admit null because the
    producer — the machine's own receipt, ``alkera_cli.cloud.receipt`` — sends
    null when it could not learn them, and a receipt that cannot be delivered
    is worse than one that says "unknown": the upload would 422 and the result
    would wait for a payload forever. A reader renders null as "—", which is
    the honest thing to show. The parity test in ``test_receipt_seam.py`` is
    what keeps the two spellings one shape.
    """

    # 1.1.0: ``role`` / ``executed_at`` / ``duration_ms`` admit null (a
    # widening — every value an older writer emitted still validates).
    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    sql: str = ""
    connection_id: str | None = None
    connection_name: str = ""
    role: str | None = None
    engine: str = ""
    principal_chain: dict[str, Any] = Field(default_factory=dict)
    executed_at: str | None = None
    row_count: int = 0
    duration_ms: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    definitions_referenced: list[str] = Field(default_factory=list)


#: The fields of a :class:`ResultSpec` only the server writes — the payload's
#: placement and the receipt that describes it — and so a create may not carry
#: (``server_owned_field``). ``envelope_columns`` is the placement's record of
#: the producer's own column names.
RESULT_SERVER_OWNED_FIELDS = frozenset(
    {"receipt", "payload", "inline", "total_rows", "envelope_columns", "failure_reason"}
)
#: The fields an edit may not change once a result exists: everything the
#: server owns plus the provenance that says where it came from. A receipted
#: answer is not rewritable; what an edit changes is the title, the column
#: labels and the chart.
RESULT_IMMUTABLE_FIELDS = RESULT_SERVER_OWNED_FIELDS | frozenset(
    {"source_chat_id", "source_event_id", "source_query_id"}
)


class ResultSpec(VersionedModel):
    """A promoted result: the columns, the receipt, an optional chart, and the
    payload.

    The payload is where it is, and the spec says so honestly. Under the inline
    cap the whole envelope rides in ``inline`` and no spill rows exist; above
    it, ``inline`` is null and the rows are paged into ``object_payload_rows``.
    ``payload`` is the producer's blob handle either way, so a reader that only
    knows the handle keeps working when the storage moves.

    ``chart_spec`` is a plain object because it is the *output* of the
    allowlist walker (``alkera_core.schemas.objects.chart``): strict where it is
    written, permissive where it is read.
    """

    # 1.1.0: ``source_chat_id`` / ``source_event_id`` (additive).
    # 1.2.0: ``failure_reason`` (additive).
    # 1.3.0: ``payload`` is a versioned handle, so the spec emits two more keys
    # inside it (additive; a 1.2.0 handle with neither still loads).
    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    source_query_id: str | None = None
    #: The chat this result was promoted out of, and the transcript event (the
    #: tool result) it came from. The daemon that uploads the payload checks
    #: both before honouring a promote relay, so a relay cannot point it at a
    #: result that was not born in its own chat.
    source_chat_id: str | None = None
    source_event_id: str | None = None
    columns: list[ResultColumn] = Field(default_factory=list)
    receipt: Receipt = Field(default_factory=Receipt)
    chart_spec: dict[str, Any] | None = None
    payload: BlobHandle | None = None
    #: The whole envelope when it fit under the inline cap; null when spilled.
    inline: dict[str, Any] | None = None
    #: Rows in the full payload, so a reader knows the total before paging.
    total_rows: int = 0
    #: Why the payload is not coming, in the machine's words, when the object is
    #: ``failed``. A promote the machine cannot honour has to say so where the
    #: reader is looking — they are on the object's own page by then, not in the
    #: chat, and an object that only waits reads as "Saving…" for ever.
    failure_reason: str | None = None


class ReportConnection(VersionedModel):
    """One data source a report runs against, named the way a chat names one.

    A connection is named by ``plugin`` (``postgres``, ``snowflake``, …) and the
    ``handle`` the org knows it by — never by id and never by a credential. A
    fresh chat handed this folder resolves the pair against the connections it
    can actually see, so a report saved by one member is re-runnable by another
    without either of them being handed the other's secret.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    plugin: str = ""
    handle: str = ""


#: What a report step does. ``query`` reads from a connection, ``transform``
#: reshapes what earlier steps produced, ``render`` turns it into the document.
#: A closed set on purpose: an agent reading the folder has to know which steps
#: touch a data source and which do not, and a free-text kind would not say.
ReportStepKind = Literal["query", "transform", "render"]

#: How a report's document is produced. ``html`` means one self-contained page
#: (everything embedded); ``pdf`` the same document printed; ``markdown`` the
#: plain-text form. Closed, because the renderer has to switch on it.
ReportFormat = Literal["html", "pdf", "markdown"]


class ReportStep(VersionedModel):
    """One step of the report, in the order it is performed.

    ``text`` is the step's body in its own terms — the SQL for a ``query``, the
    reshaping for a ``transform``, the layout instruction for a ``render``. It
    carries ``{slot}`` parameters in the same spelling
    :class:`QuerySpec` uses, so the report's declared questions bind into a step
    through the one compiler and are never formatted into it.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: ReportStepKind = "query"
    text: str = Field(default="", max_length=MAX_SQL_LENGTH)
    #: Which declared connection this step runs against, by
    #: :attr:`ReportConnection.handle`. Empty means the report's only one — the
    #: common case — and is what a single-source report leaves unset.
    connection: str = ""


class ReportRendering(VersionedModel):
    """How the report's document is written out.

    ``template`` is the shape of the document, not the document: headings,
    what goes in which section, how a figure is captioned. It is prose the
    re-running agent follows, because a report's structure is the part a reader
    recognises across months and the part an agent otherwise reinvents.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    format: ReportFormat = "html"
    template: str = ""


class ReportSpec(VersionedModel):
    """A report as a REPLICATION CONTEXT: everything needed to produce it again.

    Not a snapshot. A promoted :class:`ResultSpec` is the frozen answer with the
    receipt that makes it credible; this is the opposite artefact — what a
    brand-new chat, holding none of the conversation that produced it, needs in
    order to run the same analysis over a different date range, region or
    subject and write it up the same way.

    ``questions`` is the part that makes that true. Every parameter is declared
    with a name, a type and — crucially — the ``prompt`` to ASK before running:
    a fresh agent reads them, asks the reader all of them, binds the answers,
    and only then re-runs. A value baked into a step's ``text`` rather than
    declared here is a question nobody will be asked and a number quietly
    carried over from last time.

    ``questions`` reuses :class:`QueryParam` rather than inventing a second
    parameter grammar, so a report's question compiles into a step's statement
    through exactly the compiler a saved query's slot does.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    title: str = ""
    #: Asked before anything runs, in this order.
    questions: list[QueryParam] = Field(default_factory=list)
    #: Every source the steps read from.
    connections: list[ReportConnection] = Field(default_factory=list)
    #: What to do, in order.
    steps: list[ReportStep] = Field(default_factory=list)
    #: What the report SAYS — the argument it makes and the framing a reader
    #: expects — as distinct from ``rendering.template``, which is where each
    #: part goes.
    narrative: str = ""
    rendering: ReportRendering = Field(default_factory=ReportRendering)
    #: The chat this report was saved out of, for provenance. A re-run is a new
    #: chat's work, not this one's.
    source_chat_id: str | None = None


__all__ = [
    "MAX_PARAM_NAME_LENGTH",
    "MAX_SQL_LENGTH",
    "PARAM_NAME_PATTERN",
    "RESULT_IMMUTABLE_FIELDS",
    "RESULT_SERVER_OWNED_FIELDS",
    "ChatSpec",
    "MachineStatus",
    "MirrorState",
    "ParamType",
    "QueryEngine",
    "QueryParam",
    "QuerySpec",
    "Receipt",
    "ReportConnection",
    "ReportFormat",
    "ReportRendering",
    "ReportSpec",
    "ReportStep",
    "ReportStepKind",
    "ResultColumn",
    "ResultSpec",
]
