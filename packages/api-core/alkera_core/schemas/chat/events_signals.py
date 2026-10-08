"""Sub-agent pointers, live signals, fold-time cursors, and the unknown-tag
fallback. `events.py` folds these into the `Event` union and is the import path."""

from __future__ import annotations

from typing import Any, ClassVar, Final, Literal

from pydantic import Field

from alkera_core.schemas.chat.events_transcript import EventBase

# ===========================================================================
# Sub-agents
# ===========================================================================


class SubagentStarted(EventBase):
    """A sub-agent (child chat session) was spawned by this chat.

    The CHILD chat lives in its own folder `<.alkera>/chats/<child_sid>/`
    linked via `ChatManifest.parent_session_id`. This event is a
    POINTER from the parent chat to its child; navigate to the child
    chat folder to see the child's full event log.

    Multi-turn parent<->subagent communication is supported: each
    parent prompt that involves the sub-agent emits a fresh
    `SubagentStarted` / `SubagentCompleted` pair on the parent's
    chat.jsonl, and the child's chat.jsonl captures the sub-agent's
    internal turn.

    DATA-ONLY: the UI does NOT render a separate card for this event -- it
    binds `child_session_id` onto the originating `spawn_agent` tool card
    (matched by `agent_name` + `prompt`, since the MCP transport drops the
    chat tool-call id) so that card can stream the running child's tool calls
    and drill into its live transcript.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    event_type: Literal["subagent.started"] = "subagent.started"
    child_session_id: str
    parent_message_id: str = ""
    """The parent's message that triggered the spawn (best-effort; "" when the
    session doesn't track a current message id)."""
    agent_name: str
    description: str | None = None
    prompt: str = ""
    """The spawn brief -- the correlation key the UI uses to bind this start to
    its `spawn_agent` tool card (the tool-call id isn't available parent-side)."""


class SubagentCompleted(EventBase):
    """Sub-agent has finished its work. The summary is the brief
    handed back to the parent (rendered inline in the parent's UI).
    Full sub-agent transcript is in the child chat folder.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["subagent.completed"] = "subagent.completed"
    child_session_id: str
    summary: str | None = None
    error: str | None = None


# ===========================================================================
# Misc live signals
# ===========================================================================


class AvailableCommandsUpdate(EventBase):
    """Slash commands the harness currently advertises. Editor uses
    this to populate autocomplete in the prompt input. Replaced
    wholesale on each emission (not patched)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["available_commands_update"] = "available_commands_update"
    commands: list[dict[str, Any]] = Field(default_factory=list)


class PlanUpdated(EventBase):
    """Multi-step plan emitted by the harness (todo-style task list).
    Renders as a checklist in the UI; entries flip status as work
    progresses. Mirrors ACP's `plan` update."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["plan.updated"] = "plan.updated"
    entries: list[dict[str, Any]] = Field(default_factory=list)


class Heartbeat(EventBase):
    """Keep-alive emitted ~10s while a turn is active and no other
    event has fired. Distinguishes "model thinking" from "adapter
    dead" in the UI. NOT PERSISTED."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["heartbeat"] = "heartbeat"
    last_activity_ms: int


class FileEdited(EventBase):
    """A file was modified during this turn (typically by a tool call).

    Generic IR event -- every coding-agent harness emits something
    equivalent. Adapters translate their harness's per-edit signal
    (opencode's `file.edited`, Claude Code's edit-tool result, etc.)
    into this shape so the UI doesn't need to know which harness is
    underneath.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    event_type: Literal["file.edited"] = "file.edited"
    path: str
    insertions: int | None = None
    """Added line count for the edit, when the adapter could derive it."""
    deletions: int | None = None
    """Removed line count for the edit, when the adapter could derive it."""
    preview: dict[str, Any] | None = None
    """Renderable edit preview -- `{kind: "diff", content: <unified diff>,
    title: <basename>}` when the adapter captured the tool's diff. The UI
    folds this into the write card's resource preview."""


class CommandExecuted(EventBase):
    """A harness-side slash command was executed (e.g. `/help`,
    `/clear`, custom user commands). Distinct from tool calls -- these
    are UX shortcuts the harness exposes to the user; tool calls are
    the model invoking capabilities.

    Generic IR event -- every harness with slash-command support emits
    something equivalent.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["command.executed"] = "command.executed"
    name: str
    arguments: str = ""
    message_id: str | None = None


class CommandResult(EventBase):
    """The structured result of an editor-dispatched slash command
    (`chat_slash.dispatch_ui`), persisted so its result card is part of
    the transcript and survives reopen -- the live RPC return is volatile.
    Carries the raw outcome (not pre-rendered text) so the UI derives the
    card and copy can improve retroactively. Commands whose result already
    has a dedicated persisted representation (`/clear` -> `ConversationCleared`,
    `/compact` -> `CompactionApplied`) do NOT emit this."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["command.result"] = "command.result"
    command: str | None = None
    outcome_kind: str = "ok"
    """`ok` | `bad_usage` | `cli_only` | `unknown` -- mirrors `UiCommandOutcome.kind`."""
    payload: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None


class RateLimited(EventBase):
    """Provider rate-limit hit; adapter is backing off."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["rate_limited"] = "rate_limited"
    retry_in_ms: int | None = None
    provider: str | None = None
    message: str | None = None


class Retrying(EventBase):
    """Transient error retry (network blip, 5xx, etc.). Distinct from
    `RateLimited` (which is provider-imposed)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["retrying"] = "retrying"
    attempt: int
    reason: str | None = None
    next_attempt_in_ms: int | None = None


#: Why a person's message was never run: the reader ended the turn it was
#: queued behind. A reader that meets a word it does not know says the message
#: was not sent without saying why, so a later reason needs no coordinated
#: release — which is what lets :data:`PROMPT_CANCELLED_FAILED` ship on its own.
PROMPT_CANCELLED_STOPPED: Final[str] = "stopped"

#: The other reason: the box could not get the message as far as the agent —
#: the files it names could not be fetched, the chat's own store would not
#: answer. Nobody ended anything, so it must not be reported as stopped; the
#: point of reporting it at all is that a message the box drops in silence is
#: one the reader watches sit unanswered for as long as they are willing to.
PROMPT_CANCELLED_FAILED: Final[str] = "failed"

#: The third: a machine that answers never took the chat up, and the server
#: gave up waiting for it. The message is kept on the transcript for the person
#: to send again; it is never run behind their back once they have been told.
PROMPT_CANCELLED_NOT_TAKEN: Final[str] = "not_taken"

#: The fourth: the machine had the message queued for a slot that never freed.
PROMPT_CANCELLED_NO_SLOT: Final[str] = "no_slot"


class PromptCancelled(EventBase):
    """A person's message the agent was never handed.

    A message relayed while a turn is running waits behind it. Ending that
    turn empties the queue, so everything still in it is dropped rather than
    started — and the person, whose message is on screen the moment the server
    takes it, is told that here instead of watching it sit unanswered.

    `message_id` is the message's own transcript id, the one the reader's copy
    already carries, so a reader matches this to what it is showing rather than
    to a second telling of the same words. `client_id` is the id the sender
    minted for it, which a reader that has not yet seen the recorded row still
    has.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["prompt.cancelled"] = "prompt.cancelled"
    message_id: str = ""
    client_id: str = ""
    reason: str = PROMPT_CANCELLED_STOPPED


# ===========================================================================
# Cursors / filters (apply at fold time, never edit history in place)
# ===========================================================================


class RevertApplied(EventBase):
    """Non-destructive revert cursor. Everything *after* `to_message_id`
    (and `to_part_id` if set) is treated as inactive at fold time.

    See `CHAT_CONSIDERATIONS.md` section 8 -- revert is a cursor, not deletion.
    Destructive cleanup is a separate `TombstoneApplied` event.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["revert.applied"] = "revert.applied"
    to_message_id: str
    to_part_id: str | None = None


class CompactionApplied(EventBase):
    """Marks the listed message IDs as elided. The summary text
    replaces them at fold time for downstream consumers (UI, LLM
    context)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["compaction.applied"] = "compaction.applied"
    summarised_message_ids: list[str] = Field(default_factory=list)
    summary_text: str = ""


class ConversationCleared(EventBase):
    """The conversation context was reset to empty -- the next turn starts
    fresh, seeing none of the prior turns. Earlier events remain in
    `chat.jsonl` for audit; this marks the boundary (rendered as a
    `-- cleared --` divider on replay)."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["conversation.cleared"] = "conversation.cleared"
    cleared_message_ids: list[str] = Field(default_factory=list)


class TombstoneApplied(EventBase):
    """Marks specific events as ignored at fold time. Used for PII
    redaction (target a text part), or destructive cleanup after a
    revert. Never deletes the underlying lines -- they remain in jsonl
    for audit."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: Literal["tombstone.applied"] = "tombstone.applied"
    event_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


# ===========================================================================
# Catch-all fallback for UNKNOWN event_type tags
# ===========================================================================


class RawEvent(EventBase):
    """Fallback for unknown event_type tags. Carries the unknown tag
    verbatim so the line round-trips. Extras ride on
    `__pydantic_extra__`."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    event_type: str = "unknown"
