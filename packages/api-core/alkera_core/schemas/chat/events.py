"""Chat events — the append-only log entries in `chat.jsonl`.

Every chat is a stream of events: mutators grow the materialized state, cursors mark earlier
events inactive at fold time, and live signals drive the live UI. The event
classes live in `events_transcript` (sessions, messages, parts, chunks, turns,
LLM calls), `events_interaction` (tool calls, permissions, questions), and
`events_signals` (sub-agents, live signals, cursors); this module folds them
into the `Event` union and stays the import path. Unknown event_type tags fall
through to `RawEvent` for forward-compat.

`Chat.append_event` consults `NON_PERSISTED_EVENT_TYPES` (defined below): the
per-token chunk deltas and `heartbeat` flow over the live firehose but never
land in `chat.jsonl`. Everything else IS persisted -- finalized parts plus all
semantically meaningful state changes, sufficient to reconstruct a chat without
ever seeing the per-token deltas.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Discriminator, Tag

from alkera_core.schemas.chat.events_interaction import (
    STANDING_OPTIONS,
    CanonicalPermissionKind,
    PermissionOption,
    PermissionOptionId,
    PermissionRequest,
    PermissionResolved,
    QuestionAnswered,
    QuestionOption,
    QuestionPrompt,
    QuestionRejected,
    QuestionRequest,
    ToolCall,
    ToolCallStatus,
    ToolCallUpdate,
)
from alkera_core.schemas.chat.events_signals import (
    PROMPT_CANCELLED_FAILED,
    PROMPT_CANCELLED_NO_SLOT,
    PROMPT_CANCELLED_NOT_TAKEN,
    PROMPT_CANCELLED_STOPPED,
    AvailableCommandsUpdate,
    CommandExecuted,
    CommandResult,
    CompactionApplied,
    ConversationCleared,
    FileEdited,
    Heartbeat,
    PlanUpdated,
    PromptCancelled,
    RateLimited,
    RawEvent,
    Retrying,
    RevertApplied,
    SubagentCompleted,
    SubagentStarted,
    TombstoneApplied,
)
from alkera_core.schemas.chat.events_transcript import (
    AgentMessageChunk,
    AgentThoughtChunk,
    EventBase,
    LlmCallFinished,
    LlmCallStarted,
    MessageCompleted,
    MessageCreated,
    MessageRole,
    PartCreated,
    PartStarted,
    PartType,
    PartUpdated,
    SessionCreated,
    SessionPhase,
    SessionStatus,
    SessionStatusChanged,
    SessionUpdated,
    TurnFinished,
    TurnStarted,
    TurnStopReason,
)
from alkera_core.versioning import make_unknown_tag_discriminator

# ===========================================================================
# Discriminated union + storage filter
# ===========================================================================


_KNOWN_EVENT_TAGS: set[str] = {
    # Session lifecycle
    "session.created",
    "session.updated",
    "session.status_changed",
    # Message + part lifecycle
    "message.created",
    "message.completed",
    "part.started",
    "part.created",
    "part.updated",
    # Streaming token deltas (NOT persisted)
    "agent.message_chunk",
    "agent.thought_chunk",
    # Turn + LLM-call lifecycle
    "turn.started",
    "turn.finished",
    "llm.call_started",
    "llm.call_finished",
    # Tool call lifecycle
    "tool.call",
    "tool.call_update",
    # Permissions
    "permission.request",
    "permission.resolved",
    # Questions
    "question.request",
    "question.answered",
    "question.rejected",
    # Sub-agents
    "subagent.started",
    "subagent.completed",
    # Misc live signals
    "available_commands_update",
    "plan.updated",
    "heartbeat",
    "file.edited",
    "command.executed",
    "command.result",
    "rate_limited",
    "retrying",
    "prompt.cancelled",
    # Cursors / filters
    "revert.applied",
    "compaction.applied",
    "conversation.cleared",
    "tombstone.applied",
}


NON_PERSISTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # Per-token deltas — UX-only, derived from finalized state
        "agent.message_chunk",
        "agent.thought_chunk",
        # Keep-alive — UX-only
        "heartbeat",
    }
)
"""Event types that flow over the live firehose but are NEVER persisted
to `chat.jsonl`. The `Chat.append_event` writer drops these on the
floor. Restore from JSONL replays only finalized state."""


_event_tag = make_unknown_tag_discriminator(_KNOWN_EVENT_TAGS, field="event_type")

Event = Annotated[
    (
        # Session lifecycle
        Annotated[SessionCreated, Tag("session.created")]
        | Annotated[SessionUpdated, Tag("session.updated")]
        | Annotated[SessionStatusChanged, Tag("session.status_changed")]
        # Message + part lifecycle
        | Annotated[MessageCreated, Tag("message.created")]
        | Annotated[MessageCompleted, Tag("message.completed")]
        | Annotated[PartStarted, Tag("part.started")]
        | Annotated[PartCreated, Tag("part.created")]
        | Annotated[PartUpdated, Tag("part.updated")]
        # Streaming token deltas
        | Annotated[AgentMessageChunk, Tag("agent.message_chunk")]
        | Annotated[AgentThoughtChunk, Tag("agent.thought_chunk")]
        # Turn + LLM
        | Annotated[TurnStarted, Tag("turn.started")]
        | Annotated[TurnFinished, Tag("turn.finished")]
        | Annotated[LlmCallStarted, Tag("llm.call_started")]
        | Annotated[LlmCallFinished, Tag("llm.call_finished")]
        # Tool calls
        | Annotated[ToolCall, Tag("tool.call")]
        | Annotated[ToolCallUpdate, Tag("tool.call_update")]
        # Permissions
        | Annotated[PermissionRequest, Tag("permission.request")]
        | Annotated[PermissionResolved, Tag("permission.resolved")]
        # Questions
        | Annotated[QuestionRequest, Tag("question.request")]
        | Annotated[QuestionAnswered, Tag("question.answered")]
        | Annotated[QuestionRejected, Tag("question.rejected")]
        # Sub-agents
        | Annotated[SubagentStarted, Tag("subagent.started")]
        | Annotated[SubagentCompleted, Tag("subagent.completed")]
        # Misc
        | Annotated[AvailableCommandsUpdate, Tag("available_commands_update")]
        | Annotated[PlanUpdated, Tag("plan.updated")]
        | Annotated[Heartbeat, Tag("heartbeat")]
        | Annotated[FileEdited, Tag("file.edited")]
        | Annotated[CommandExecuted, Tag("command.executed")]
        | Annotated[CommandResult, Tag("command.result")]
        | Annotated[RateLimited, Tag("rate_limited")]
        | Annotated[Retrying, Tag("retrying")]
        | Annotated[PromptCancelled, Tag("prompt.cancelled")]
        # Cursors / filters
        | Annotated[RevertApplied, Tag("revert.applied")]
        | Annotated[CompactionApplied, Tag("compaction.applied")]
        | Annotated[ConversationCleared, Tag("conversation.cleared")]
        | Annotated[TombstoneApplied, Tag("tombstone.applied")]
        # Unknown
        | Annotated[RawEvent, Tag("__unknown__")]
    ),
    Discriminator(_event_tag),
]
"""Discriminated union over every known Event variant + an unknown-tag
fallback. Use with `TypeAdapter(Event).validate_python(d)`."""


__all__ = [
    # Storage filter
    "NON_PERSISTED_EVENT_TYPES",
    # Why a message was never run
    "PROMPT_CANCELLED_FAILED",
    "PROMPT_CANCELLED_NOT_TAKEN",
    "PROMPT_CANCELLED_NO_SLOT",
    "PROMPT_CANCELLED_STOPPED",
    "STANDING_OPTIONS",
    # Streaming chunks
    "AgentMessageChunk",
    "AgentThoughtChunk",
    # Misc
    "AvailableCommandsUpdate",
    # Permissions (canonical kind sorted alphabetically into this slot)
    "CanonicalPermissionKind",
    "CommandExecuted",
    "CommandResult",
    "CompactionApplied",
    "ConversationCleared",
    # Discriminated union
    "Event",
    "EventBase",
    # File edits
    "FileEdited",
    # Heartbeat / errors
    "Heartbeat",
    # LLM lifecycle
    "LlmCallFinished",
    "LlmCallStarted",
    # Message lifecycle
    "MessageCompleted",
    "MessageCreated",
    "MessageRole",
    "PartCreated",
    "PartStarted",
    "PartType",
    "PartUpdated",
    # Permissions
    "PermissionOption",
    "PermissionOptionId",
    "PermissionRequest",
    "PermissionResolved",
    # Plan
    "PlanUpdated",
    # Prompts the box never ran
    "PromptCancelled",
    # Questions
    "QuestionAnswered",
    "QuestionOption",
    "QuestionPrompt",
    "QuestionRejected",
    "QuestionRequest",
    "RateLimited",
    # Unknown
    "RawEvent",
    "Retrying",
    "RevertApplied",
    # Sessions
    "SessionCreated",
    "SessionPhase",
    "SessionStatus",
    "SessionStatusChanged",
    "SessionUpdated",
    # Sub-agents
    "SubagentCompleted",
    "SubagentStarted",
    "TombstoneApplied",
    # Tools
    "ToolCall",
    "ToolCallStatus",
    "ToolCallUpdate",
    # Turns
    "TurnFinished",
    "TurnStarted",
    "TurnStopReason",
]
