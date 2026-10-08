"""A turn that ends with nothing on screen says why.

A model can finish a message with no text and no tool call at all — a content
filter tripped, the length limit hit before the first visible token, or a stop
the provider did not explain. The turn then went idle and the transcript showed
nothing, which reads as a hang. The watch below sees every event a session
publishes and, when an assistant message completes on a final finish reason
with nothing visible since the person's message, hands back one text part for
that message naming why. The session publishes it just before the completion,
so the transcript, a cloud mirror and the terminal all show it like the
model's own words, and it is persisted with the rest of the turn.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from alkera_core.schemas.chat import (
    AgentMessageChunk,
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    TextPart,
    ToolCall,
    ToolCallPart,
    TurnStarted,
)

#: Finish reasons that mean the model is about to run a tool and keep going.
_CONTINUES = frozenset({"tool-calls", "tool_calls", "tool_use"})

#: Finish reasons that mean the provider declined to answer.
_DECLINED = frozenset({"content-filter", "content_filter", "refusal"})

#: Finish reasons that mean the answer hit the model's output limit.
_CUT_OFF = frozenset({"length", "max_tokens"})

DECLINED = "The model declined to answer this message."
CUT_OFF = "The answer was cut off at the model's length limit."


def empty_answer_sentence(finish_reason: str) -> str:
    """The one sentence for a turn that ended on ``finish_reason`` with nothing
    shown."""
    reason = finish_reason.strip().lower()
    if reason in _DECLINED:
        return DECLINED
    if reason in _CUT_OFF:
        return CUT_OFF
    return f"The model returned no answer ({reason})."


class EmptyAnswerWatch:
    """Every published event in; the text part an empty answer needs out."""

    def __init__(self) -> None:
        self._assistant: set[str] = set()
        self._shown = False

    def precede(self, event: Event) -> Sequence[Event]:
        """The events to publish before ``event`` — empty for every event but
        the completion of an assistant message that left the turn blank."""
        if isinstance(event, TurnStarted):
            self._shown = False
        elif isinstance(event, MessageCreated):
            if event.role == "user":
                self._shown = False
            elif event.role == "assistant":
                self._assistant.add(event.message_id)
        elif isinstance(event, AgentMessageChunk):
            if event.message_id in self._assistant and event.text.strip():
                self._shown = True
        elif isinstance(event, ToolCall):
            self._shown = True
        elif isinstance(event, PartCreated):
            self._note_part(event)
        elif isinstance(event, MessageCompleted):
            return self._on_completed(event)
        return ()

    def _note_part(self, event: PartCreated) -> None:
        part = event.part
        if part.message_id not in self._assistant:
            return
        if isinstance(part, ToolCallPart):
            self._shown = True
        elif isinstance(part, TextPart) and part.text.strip() and not part.ignored:
            self._shown = True

    def _on_completed(self, event: MessageCompleted) -> Sequence[Event]:
        if event.message_id not in self._assistant:
            return ()
        self._assistant.discard(event.message_id)
        reason = (event.finish_reason or "").strip().lower()
        if not reason or reason in _CONTINUES or reason == "error" or event.error:
            # A continuation is not the end of the turn, and an error ends it
            # with its own notice.
            return ()
        if self._shown:
            return ()
        self._shown = True
        part = TextPart(
            part_id=f"{event.message_id}-empty-answer",
            message_id=event.message_id,
            text=empty_answer_sentence(reason),
            metadata={"alkera_notice": "empty_answer"},
        )
        return (
            PartCreated(
                event_id=f"{event.event_id}-empty-answer",
                time=datetime.now(UTC),
                session_id=event.session_id,
                part=part,
            ),
        )


__all__ = ["CUT_OFF", "DECLINED", "EmptyAnswerWatch", "empty_answer_sentence"]
