"""The blocking interaction surface: tool calls, permissions, and questions.
`events.py` folds these into the `Event` union and is the import path."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from alkera_core.schemas.chat.base import VersionedChatModel
from alkera_core.schemas.chat.events_transcript import EventBase

# ===========================================================================
# Tool call lifecycle (typed; complement to PartUpdated)
# ===========================================================================


ToolCallStatus = Literal["pending", "running", "completed", "error"]
"""Matches `ToolCallPart.state` (parts.py)."""


class ToolCall(EventBase):
    """Tool invocation announced. Equivalent to `PartStarted(part_type=
    "tool_call")` but typed so adapters and consumers can rely on the
    field shape without unpacking `initial`.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    event_type: Literal["tool.call"] = "tool.call"
    tool_call_id: str
    """The transcript's key for this call: every ``ToolCallUpdate`` and
    ``ToolCallPart`` for it carries the same id."""
    provider_call_id: str | None = None
    """The id the model provider gave the call, when the harness keys its own
    part differently (opencode announces a part ``prt_…`` for the provider's
    ``call_…``) -- a ``PermissionRequest`` raised for the call may carry either
    one, and a reader matching an ask to its call must accept both. ``None``
    when the two are one id. Added in 1.1.0."""
    message_id: str
    tool_name: str = ""
    tool_kind: str | None = None
    """E.g. "read" | "edit" | "shell" | "task" -- harness-specific."""
    input: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = "pending"


class ToolCallUpdate(EventBase):
    """State transition or progressive content for a tool call. Fold
    rule: last-write-wins, scoped to `tool_call_id`.

    For long-running tools (shell, browser), `content_deltas` carries
    interim output the UI can stream.

    `input` is optional and carries the tool's arguments once the
    harness has them -- opencode (and similar harnesses) emit a
    `pending` state first with input still absent, then a `running`
    state with input now populated. UIs that buffered the initial
    `ToolCall` with `input={}` can patch their card from this field.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["tool.call_update"] = "tool.call_update"
    tool_call_id: str
    status: ToolCallStatus | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | str | None = None
    error_text: str | None = None
    content_deltas: list[dict[str, Any]] = Field(default_factory=list)


# ===========================================================================
# Permissions
# ===========================================================================


PermissionOptionId = Literal[
    "allow_once",
    "allow_always",
    "reject_once",
    "reject_always",
    "cancelled",
]

#: The option ids that record a STANDING grant: an answer meant to outlive the
#: one ask it was given on. Everything that offers, withholds, downgrades or
#: refuses one names them from here.
STANDING_OPTIONS: frozenset[str] = frozenset({"allow_always", "reject_always"})


class PermissionOption(VersionedChatModel):
    """A single choice the user can pick. The set is presented as
    inline buttons in the chat UI / numeric prompts in the CLI."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    option_id: PermissionOptionId
    name: str
    """Human-readable label. Adapters localize at translation time."""


CanonicalPermissionKind = Literal[
    "edit",
    "shell",
    "network",
    "task",
    "external",
    "other",
]
"""Adapter-agnostic permission categories. Adapters MUST map their
native permission key (opencode's ``"bash"``, ``"webfetch"``) onto one
of these on the way out, so permission policy stays portable across
harnesses. Edit is file mutation, shell runs commands, network is
outbound HTTP, task spawns a subagent, external reaches outside the
project, and other covers anything else that still asks.
"""


class PermissionRequest(EventBase):
    """Adapter is blocked until a `PermissionResolved` arrives with a
    matching `request_id`. Brokered by the orchestrator's
    `PermissionBroker` (CLI prompts on stdin; daemon issues
    server->client `harness.permission_required` request).

    Timeout: brokers MAY auto-resolve to `reject_once` after a
    configurable interval (CLI default 5min; webview default no-timeout).
    """

    SCHEMA_VERSION: ClassVar[str] = "1.5.0"

    event_type: Literal["permission.request"] = "permission.request"
    request_id: str
    tool_call_id: str | None = None
    """The call this ask gates, by the transcript's key (the ``ToolCall``'s
    ``tool_call_id``) whenever the adapter has seen the call; an adapter that
    only knows the provider's id at ask time carries that instead."""
    provider_call_id: str | None = None
    """The id the model provider gave the gated call, when the harness keys its
    part differently (opencode: the ask names ``call_…`` while the transcript's
    rows carry ``prt_…``). Recorded alongside ``tool_call_id`` so a reader
    re-running or closing the call after a restart finds it under either id.
    Added in 1.5.0."""
    permission_kind: str
    """Adapter-native kind tag, preserved verbatim -- e.g. opencode's
    ``"bash"`` | ``"edit"`` | ``"webfetch"``. Adapter-specific; surfaced
    to UIs for human-facing detail. Policy code MUST switch on
    ``canonical_kind`` instead."""
    canonical_kind: CanonicalPermissionKind = "other"
    """Harness-agnostic categorization the orchestrator's policy code
    (permission modes, allow rules) reasons over. Defaults to
    ``"other"`` so pre-1.1 persisted events load gracefully."""
    patterns: list[str] = Field(default_factory=list)
    """Optional patterns the adapter wants to match against an allow
    rule (e.g. ["git status*"]). Used for future policy engine."""
    subject: dict[str, Any] | None = None
    """The typed ``ActionDescriptor`` (capability/effect/targets/cost),
    serialized, that this request is gating (added in 1.2.0). A
    dict -- not the typed model -- because api-core can't depend on the
    plugin layer that defines ``ActionDescriptor``; the plugin/permission
    layer round-trips it via ``ActionDescriptor.model_validate(subject)``.
    Lets the resolver + audit log reason over effect/target/prod/cost
    instead of a lossy transcript. ``None`` for non-classified requests."""
    options: list[PermissionOption] = Field(default_factory=list)
    insertions: int | None = None
    """Added line count for the edit this request gates, when the adapter
    captured the tool's diff at ask-time (write/edit tools only). Added in
    1.3.0."""
    deletions: int | None = None
    """Removed line count, paired with ``insertions``."""
    preview: dict[str, Any] | None = None
    """Renderable edit preview -- ``{kind: "diff", content: <unified diff>,
    title: <basename>}`` -- captured at ask-time so a UI can show the diff in
    the permission card BEFORE the user decides. The ``FileEdited`` that also
    carries it only lands AFTER the write, so the request itself must hold it
    for the pre-approval view. ``None`` for non-edit asks."""
    impact: dict[str, Any] | None = None
    """What the lineage graph says this action reaches, serialized: the affected
    columns with their edge kinds and severities, the knowledge filed against
    them, and the owning teams. A dict rather than the typed model because
    api-core can't depend on the plugin layer that defines ``ImpactAssessment``;
    the permission layer round-trips it. Attached by the resolver at ask-time so
    a card can show the blast radius before the user decides, and ``None`` when
    no graph was consulted. Added in 1.4.0."""


class PermissionResolved(EventBase):
    """Response to a `PermissionRequest`. Persisted so the chat record
    captures the user's decision history."""

    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    event_type: Literal["permission.resolved"] = "permission.resolved"
    request_id: str
    option_id: PermissionOptionId
    decided_by: Literal["user", "policy", "timeout"] = "user"
    decided_by_user_id: str | None = None
    """Which person decided, when ``decided_by="user"`` and the decision was
    recorded somewhere that knows the roster. A shared chat has many readers
    and the option alone does not say whose call it was. Only the server can
    fill it -- the box sees an ask and an answer, never a member list -- so it
    stays ``None`` for a decision the harness settled on the machine, and for
    a policy or timeout, which are nobody's.

    This is the AUDIT key, and the reason the pair is not just a name: "who
    approved this write" is a question asked months later, of a person who has
    since been renamed, married, or removed, and possibly of two colleagues who
    share a display name. An id answers it and a rendered string cannot. It is
    deliberately not what a surface prints -- see ``decided_by_name``. Added
    in 1.1.0."""
    decided_by_name: str | None = None
    """The decider's display name AS IT STOOD when the decision was recorded.

    A snapshot, not a lookup, and it is never refreshed: the transcript is a
    record of what was true at the time, it still has to name somebody who has
    since left the org, and a reader must not have to resolve an id against a
    roster it may not be allowed to read. A later rename moves the roster and
    leaves this alone; ``decided_by_user_id`` is what follows the person."""
    decided_via: Literal["web", "slack"] | None = None
    """Where the person was when they decided: the web app or a Slack thread.
    Written by the server beside the decider, and like the decider stripped
    from anything a machine publishes -- a box never sees which surface a
    person answered from. ``None`` for a decision the harness settled, a
    policy's and a timeout's. Added in 1.2.0."""


# ===========================================================================
# Questions (mid-turn clarification asks from the harness)
# ===========================================================================
#
# Distinct from permissions: a permission asks "may I do X?" with a
# fixed yes/no/once/always option set. A question asks "what do you
# want?" with adapter-supplied options and (optionally) a free-form
# custom answer. opencode's `question` tool, ACP's similar clarifier,
# and future harness-driven clarification surfaces all map here.


class QuestionOption(VersionedChatModel):
    """One selectable answer to a question."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    label: str
    """Short answer text (1-5 words). Used as the chosen-value when
    the user picks this option."""
    description: str | None = None
    """Longer explanation shown alongside the label."""


class QuestionPrompt(VersionedChatModel):
    """One question in a `QuestionRequest`. A request may carry
    multiple prompts the user answers in order."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    question: str
    """Full question text."""
    header: str | None = None
    """Optional short label / category."""
    options: list[QuestionOption] = Field(default_factory=list)
    multiple: bool = False
    """If true, the user may pick more than one option."""
    custom: bool = True
    """If true, the user may type a free-form custom answer instead of
    (or in addition to) picking from `options`."""


class QuestionRequest(EventBase):
    """Adapter is blocked until a `QuestionAnswered` or
    `QuestionRejected` arrives with a matching `request_id`. Brokered
    by the orchestrator's `QuestionBroker` (CLI prompts on stdin;
    daemon issues a server->client `harness.question_required`
    request).

    Carries one or more `QuestionPrompt`s. Each answer is a list of
    strings -- the labels the user picked (1 element for single-select,
    N for multi-select; may be a single free-form string when
    `custom=true`).
    """

    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    event_type: Literal["question.request"] = "question.request"
    request_id: str
    tool_call_id: str | None = None
    """When the question came from a tool call (opencode's `question`
    tool), the originating tool-call id. Lets the UI nest the prompt
    under the right card."""
    questions: list[QuestionPrompt] = Field(default_factory=list)
    kind: Literal["question", "plan_approval"] = "question"
    plan_markdown: str = ""
    """For a `plan_approval`: the plan's Markdown, read from the model's plan file
    by the adapter and surfaced to the editor / TUI here -- so the UI renders the
    full plan WITHOUT it being re-fed into the model's context (the model's tool
    result is only a short approve/reject outcome). Empty for a normal question."""
    """Structured discriminator for what the user is being asked. The
    adapter sets `plan_approval` when the question is the plan-approval
    surface (e.g. opencode's `plan_present` tool); the CLI / webview
    branch on this to render the approval UI + map the chosen accept
    option to a permission-mode switch. Adapter-internal mechanisms
    (e.g. the `alkera:plan-approval` sentinel header) stay private to
    the adapter -- consumers MUST switch on `kind`, not on header
    strings."""


class QuestionAnswered(EventBase):
    """Response to a `QuestionRequest`. The `answers` list is
    parallel to `QuestionRequest.questions` (entry N is the user's
    chosen labels for question N). Persisted so the chat captures
    the user's decision history."""

    SCHEMA_VERSION: ClassVar[str] = "1.2.0"

    event_type: Literal["question.answered"] = "question.answered"
    request_id: str
    answers: list[list[str]] = Field(default_factory=list)
    decided_by: Literal["user", "policy", "timeout"] = "user"
    decided_by_user_id: str | None = None
    """Which person answered -- the same contract as
    :attr:`PermissionResolved.decided_by_user_id`: filled only where the
    roster is known, ``None`` for an answer the harness settled itself and for
    a policy or timeout. Added in 1.1.0."""
    decided_by_name: str | None = None
    """The answerer's display name as it stood when the answer was recorded."""
    note: str | None = None
    """What the answerer wrote for the model alongside the answers -- on a plan,
    the guidance the run starts under or why it was sent back. Added in 1.2.0."""


class QuestionRejected(EventBase):
    """User declined to answer (Ctrl-C in the REPL, dismissed the UI
    card, etc.). The harness sees this as a hard rejection -- its tool
    call errors out."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["question.rejected"] = "question.rejected"
    request_id: str
    reason: str | None = None
