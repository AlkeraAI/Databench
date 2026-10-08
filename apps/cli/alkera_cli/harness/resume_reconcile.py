"""Close a turn left interrupted by a crash or restart, on chat (re)open.

While its process is alive the adapter upholds the streaming-closure invariant
(every ``turn.started`` has exactly one ``turn.finished``; every open part, tool
call, subagent and request is closed) by synthesizing close events on a
mid-turn failure. A daemon crash or restart skips that, leaving ``chat.jsonl``
frozen mid-turn, and a UI replaying it would show "working…" forever.

:func:`reconcile_interrupted_turn` folds the persisted log, finds any open
state, and returns the synthetic close events that end the chat cleanly as
interrupted, or ``[]`` when the log is already closed (so re-running is
idempotent). It is pure (events in, events out) and sits above the adapter, so
it covers every backend.

A tool call may block for as long as its process lives; once the process is
gone, the next open fails the orphaned work instead of inheriting a phantom
running turn.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    PartCreated,
    PartStarted,
    PermissionRequest,
    PermissionResolved,
    QuestionAnswered,
    QuestionRejected,
    QuestionRequest,
    SessionStatusChanged,
    SubagentCompleted,
    SubagentStarted,
    ToolCall,
    ToolCallUpdate,
    TurnFinished,
    TurnStarted,
)

#: Model-facing reason stamped on every synthesized closure.
_INTERRUPTED = "interrupted — the session was restarted before this finished"

#: Tool-call / part states that mean "still in flight" (need closing).
_OPEN_TOOL_STATES: frozenset[str] = frozenset({"pending", "running"})


def reconcile_interrupted_turn(
    events: Iterable[Event],
    *,
    session_id: str,
    now: datetime,
    next_id: Callable[[], str],
) -> list[Event]:
    """Synthetic close events for an interrupted turn in ``events`` (the persisted
    log), or ``[]`` when the log is already cleanly closed.

    Pure: ``now`` stamps every closure and ``next_id`` mints their event ids, so the
    function is fully deterministic under test. Detects open turns, parts, tool
    calls, subagents, and permission/question requests, plus a left-``running``
    session status; closes each (tools→error, subagents→error, requests→cancelled,
    parts→finalized, turn→cancelled) and finally marks the session ``idle`` so the
    UI is ready for input again."""
    open_turns: dict[str, None] = {}
    open_parts: dict[str, PartStarted] = {}
    tool_status: dict[str, str] = {}
    open_subagents: dict[str, None] = {}
    open_permissions: dict[str, None] = {}
    open_questions: dict[str, None] = {}
    last_status: str | None = None

    for ev in events:
        if isinstance(ev, TurnStarted):
            open_turns[ev.turn_id] = None
        elif isinstance(ev, TurnFinished):
            open_turns.pop(ev.turn_id, None)
        elif isinstance(ev, PartStarted):
            open_parts[ev.part_id] = ev
        elif isinstance(ev, PartCreated):
            open_parts.pop(ev.part.part_id, None)
        elif isinstance(ev, MessageCompleted):
            # A message that completed has no part still streaming, whatever
            # the log holds for it: a text part the adapter never finalized
            # (nothing to finalize, its deltas are not persisted) is not an
            # interruption once its message is done.
            for part_id, started in list(open_parts.items()):
                if started.message_id == ev.message_id:
                    del open_parts[part_id]
        elif isinstance(ev, ToolCall):
            tool_status[ev.tool_call_id] = ev.status
        elif isinstance(ev, ToolCallUpdate):
            if ev.status is not None:
                tool_status[ev.tool_call_id] = ev.status
        elif isinstance(ev, SubagentStarted):
            open_subagents[ev.child_session_id] = None
        elif isinstance(ev, SubagentCompleted):
            open_subagents.pop(ev.child_session_id, None)
        elif isinstance(ev, PermissionRequest):
            open_permissions[ev.request_id] = None
        elif isinstance(ev, PermissionResolved):
            open_permissions.pop(ev.request_id, None)
        elif isinstance(ev, QuestionRequest):
            open_questions[ev.request_id] = None
        elif isinstance(ev, (QuestionAnswered, QuestionRejected)):
            open_questions.pop(ev.request_id, None)
        elif isinstance(ev, SessionStatusChanged):
            last_status = ev.status
            if ev.status != "running":
                # The turn ended. A part still open past that is one nothing
                # will finish — the closure below leaves exactly such parts
                # behind (dropped, not finalized), so counting them again would
                # have every later open find the same interruption and write
                # the same closure once more.
                open_parts.clear()

    open_tools = [tid for tid, st in tool_status.items() if st in _OPEN_TOOL_STATES]
    interrupted = bool(
        open_turns
        or open_parts
        or open_tools
        or open_subagents
        or open_permissions
        or open_questions
        or last_status == "running"
    )
    if not interrupted:
        return []

    out: list[Event] = []

    def _emit(event: Event) -> None:
        out.append(event)

    base: dict[str, Any] = {"session_id": session_id}
    # Dead requests can no longer be answered (their broker is gone).
    for rid in open_permissions:
        _emit(
            PermissionResolved(
                event_id=next_id(),
                time=now,
                option_id="cancelled",
                decided_by="timeout",
                request_id=rid,
                **base,
            )
        )
    for rid in open_questions:
        _emit(
            QuestionRejected(
                event_id=next_id(), time=now, request_id=rid, reason=_INTERRUPTED, **base
            )
        )
    # In-flight tool calls fail (the canonical tool representation across backends).
    for tid in open_tools:
        _emit(
            ToolCallUpdate(
                event_id=next_id(),
                time=now,
                tool_call_id=tid,
                status="error",
                error_text=_INTERRUPTED,
                output={"error": _INTERRUPTED},
                **base,
            )
        )
    # Open text/reasoning parts are DROPPED, not finalized: their token deltas aren't
    # persisted, so the partial text is unrecoverable, and persisting an EMPTY part
    # renders as a content-less guardrail line (a stray │) and a blank reply. This
    # matches the live adapter's _synthesize_close_events. (Their presence still trips
    # the open-turn detection above; the tool calls, turn, and session close below.)
    # Dangling subagents resolve as errored so the parent's spawn card stops spinning.
    for child in open_subagents:
        _emit(
            SubagentCompleted(
                event_id=next_id(), time=now, child_session_id=child, error=_INTERRUPTED, **base
            )
        )
    # Close the open turn(s) as cancelled.
    for turn_id in open_turns:
        _emit(
            TurnFinished(
                event_id=next_id(),
                time=now,
                turn_id=turn_id,
                stop_reason="cancelled",
                error_detail=_INTERRUPTED,
                **base,
            )
        )
    # Finally, the session is idle again — the UI shows it ready for input.
    _emit(
        SessionStatusChanged(
            event_id=next_id(), time=now, status="idle", phase="idle", detail=_INTERRUPTED, **base
        )
    )
    return out


__all__ = ["reconcile_interrupted_turn"]
