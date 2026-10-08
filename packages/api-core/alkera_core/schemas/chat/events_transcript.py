"""The streamed transcript: session, message, part, chunk, turn, and LLM-call
events. `events.py` folds these into the `Event` union and is the import path;
it also documents the storage filter these events are subject to."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field

from alkera_core.credit_refusal import CreditRefusal
from alkera_core.schemas.chat.base import VersionedChatModel
from alkera_core.schemas.chat.parts import Part


class EventBase(VersionedChatModel):
    """Shared envelope for every event.

    `event_id` SHOULD be a ULID (lexicographically sortable, time-prefixed)
    so the on-disk order matches event ordering. The store doesn't
    *enforce* ULID -- any unique string works -- but every writer in the
    repo is expected to use one.
    """

    __abstract__: ClassVar[bool] = True

    event_id: str
    time: datetime
    session_id: str


# ===========================================================================
# Session + manifest lifecycle
# ===========================================================================


class SessionCreated(EventBase):
    """First event in every chat. Mirrors initial manifest fields."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["session.created"] = "session.created"
    parent_session_id: str | None = None
    title: str | None = None
    cwd: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    harness: dict[str, Any] = Field(default_factory=dict)


class SessionUpdated(EventBase):
    """Mutates manifest-level fields after creation (rename, model
    switch). Fields not present in the event are unchanged on the
    manifest."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["session.updated"] = "session.updated"
    title: str | None = None
    model: dict[str, Any] | None = None
    agent: str | None = None


SessionStatus = Literal["idle", "running", "error", "completed", "aborted"]
"""Top-level session state. Maps to the spinner-or-not decision in
the UI."""

SessionPhase = Literal[
    "idle",
    "submitting",
    "awaiting_llm",
    "streaming_response",
    "running_tool",
    "awaiting_permission",
    "awaiting_question",
    "compacting",
    "reverting",
    "error",
]
"""Fine-grained phase WITHIN `running` so the UI can show the right
indicator (`"calling claude-opus-4-7"`, `"running bash"`, etc.).
Mapped per-adapter from native event sequences -- see
`HARNESS_WRAP_RESEARCH.md` section 5.8."""


class SessionStatusChanged(EventBase):
    """Top-level + fine-grained status transitions. Drives spinners,
    phase indicators, and error banners in the UI.

    Emitted on:
    - Turn start (status=`running`, phase=`submitting`).
    - First byte from LLM (phase=`streaming_response`).
    - Tool execution (phase=`running_tool`).
    - Permission request (phase=`awaiting_permission`).
    - Turn end (status=`idle`).
    - Adapter / harness error (status=`error`).
    """

    SCHEMA_VERSION: ClassVar[str] = "1.3.0"

    event_type: Literal["session.status_changed"] = "session.status_changed"
    status: SessionStatus
    phase: SessionPhase | None = None
    detail: str | None = None
    overflow: bool = False
    """True only on a `status="error"` whose cause is a context-window overflow
    (the gateway stamped a structured `context_length_exceeded` code, classified by
    the translator -- not brittle free-text). The runtime's recovery pump keys off
    this to auto-compact + retry instead of surfacing the error. Additive in 1.1.0;
    older readers ignore it."""
    turn_id: str | None = None
    """The transport attempt that produced this status, stamped by the adapter
    from its own transport's grammar (`PromptInput.turn_id`). `None` when no
    attempt owns it (a runtime notice, an adapter with nothing in flight). The
    runtime treats a terminal stamped with a DIFFERENT attempt as inert; one
    carrying its own attempt or none settles the turn. Additive in 1.2.0."""
    refusal: CreditRefusal | None = None
    """Set only on a `status="error"` the model gateway ended for lack of credit:
    the stable code naming which allowance ran out, the pool's team when one did,
    the cycle's reset when one applies. The reader's surface keys off the code,
    never off the sentence in `detail`. Additive in 1.3.0."""


# ===========================================================================
# Message + part lifecycle
# ===========================================================================


MessageRole = Literal["user", "assistant", "system", "tool"]


class MessageCreated(EventBase):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["message.created"] = "message.created"
    message_id: str
    role: MessageRole
    parent_message_id: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
    mode: str | None = None
    """E.g. "plan", "edit", "ask" -- harness-specific."""


class MessageCompleted(EventBase):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["message.completed"] = "message.completed"
    message_id: str
    finish_reason: str = ""
    tokens: dict[str, int] = Field(default_factory=dict)
    cost: float | None = None
    error: dict[str, Any] | None = None


PartType = Literal["text", "reasoning", "tool_call", "file"]
"""Mirror of part variants on the wire -- used by `PartStarted`."""


class PartStarted(EventBase):
    """A new part begins streaming. UI reserves a rendering slot
    (empty bubble, typing cursor).

    Followed by zero or more `AgentMessageChunk` / `AgentThoughtChunk`
    events (for text/reasoning parts), or zero or more `ToolCallUpdate`
    events (for tool calls), and finally exactly ONE matching
    `PartCreated` event (the finalized part) -- unless the turn ends in
    cancel/error, in which case the adapter synthesizes a closing
    `PartCreated` with whatever partial state exists.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["part.started"] = "part.started"
    message_id: str
    part_id: str
    part_type: PartType
    initial: dict[str, Any] = Field(default_factory=dict)
    """Initial input/metadata. Empty for text/reasoning; for tool_call
    parts, carries `{tool_name, input}` so UI can show the call card
    immediately even before status/output arrive."""


class PartCreated(EventBase):
    """The full final-state part. NAMED `part.created` for historical
    reasons; semantically this is the **finalized** part (research's
    `IRPartFinalized`).

    The matching `PartStarted` event with the same `part_id` opens the
    stream; this event closes it. For text/reasoning, `part.text` is
    the byte-exact concatenation of all `AgentMessageChunk.text`
    deltas for the part. For tool calls, `part` carries the full
    {input, output, status} after the tool ran.

    Streaming deltas (chunks) themselves are NOT persisted -- only this
    final-state event is. See storage filter at module docstring.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["part.created"] = "part.created"
    part: Part


class PartUpdated(EventBase):
    """Field-scoped update to an already-created part. The fold rule
    is "last-write-wins on field basis, scoped to `part_id`" -- see
    `CHAT_CONSIDERATIONS.md` section 6. The patch is a sparse dict; only
    present keys are applied.

    Mostly superseded by typed events (`ToolCallUpdate`) for known
    part kinds; reserved for genuinely generic patches.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["part.updated"] = "part.updated"
    part_id: str
    patch: dict[str, Any] = Field(default_factory=dict)


# ===========================================================================
# Streaming token deltas (NOT persisted to chat.jsonl)
# ===========================================================================


class AgentMessageChunk(EventBase):
    """A text delta to append to a specific text part. NOT PERSISTED.

    Concatenating all chunks for a given `(message_id, part_id)` in
    `sequence` order yields the part's final text byte-exactly. The
    matching `PartCreated` with the same `part_id` carries the
    canonical full text (which the UI can use to verify the
    concatenation, and which mid-stream joiners use to skip the
    chunk-replay buffer).
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["agent.message_chunk"] = "agent.message_chunk"
    message_id: str
    part_id: str
    sequence: int
    text: str
    is_final: bool = False
    """True iff this is the last chunk for the part -- set inline so a
    consumer doesn't need to wait for `PartCreated` to know."""


class AgentThoughtChunk(EventBase):
    """Reasoning (Anthropic extended thinking / OpenAI o1) token delta.
    NOT PERSISTED. Same contract as `AgentMessageChunk`."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["agent.thought_chunk"] = "agent.thought_chunk"
    message_id: str
    part_id: str
    sequence: int
    text: str
    is_final: bool = False
    signature: str | None = None
    """Anthropic redacted-thinking signature blob."""
    encrypted: bool = False


# ===========================================================================
# Turn + LLM-call lifecycle
# ===========================================================================


TurnStopReason = Literal[
    "end_turn",
    "max_tokens",
    "max_turn_requests",
    "refusal",
    "cancelled",
    "error",
]


class TurnStarted(EventBase):
    """User prompt accepted. Spinner ON. Always paired with exactly one
    `TurnFinished` per turn -- adapters synthesize the close event even
    on crash/error so the UI never sees an unmatched start.

    A single turn may issue multiple LLM round-trips (tool use ->
    intermediate responses -> more tool use -> final text). Each is
    bracketed by its own `LlmCallStarted` / `LlmCallFinished` pair.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["turn.started"] = "turn.started"
    turn_id: str
    user_message_id: str
    model: dict[str, str] | None = None


class TurnFinished(EventBase):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["turn.finished"] = "turn.finished"
    turn_id: str
    stop_reason: TurnStopReason
    cost_usd: float | None = None
    tokens: dict[str, int] | None = None
    error_detail: str | None = None
    """Populated when `stop_reason == "error"` -- caller-facing message."""


class LlmCallStarted(EventBase):
    """First byte from the LLM expected. Distinct from `turn.started`
    because one turn may have multiple LLM round-trips (tool use)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["llm.call_started"] = "llm.call_started"
    call_id: str
    turn_id: str
    model: dict[str, str] = Field(default_factory=dict)


class LlmCallFinished(EventBase):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["llm.call_finished"] = "llm.call_finished"
    call_id: str
    turn_id: str
    finish_reason: str | None = None
    """E.g. "stop" | "length" | "tool_use" | "refusal"."""
    tokens: dict[str, int] | None = None
    cost_usd: float | None = None
