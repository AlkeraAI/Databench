"""Restart a turn that a daemon restart or a lost machine interrupted.

:mod:`alkera_cli.harness.resume_reconcile` closes an interrupted turn when the
chat is opened again, so a reader is not left watching "working…" for ever.
Closing is not answering: the person's message sits in the transcript with
nothing after it. This module decides what the next open does about that —
restart the turn from the same message, or, once it has been restarted
:data:`TURN_RESTART_LIMIT` times and interrupted again, stop and say so.

It is pure (events in, a decision out) apart from :class:`RestartLedger`, the
small file beside the chat that counts how often THIS message has been
restarted. The count cannot be read off the log alone: a restart sends the
same words as a new message, and a person may send the same words again on
purpose.

Not restarted:

- a turn the person cancelled (the log records the cancel, and reconcile finds
  the turn already closed);
- a turn whose answer completed before the process went away (the assistant's
  message finished with a final reason; only the idle status was lost);
- a turn restarted three times already, which is closed as failed with
  :data:`TURN_RESTART_FAILED` in the transcript.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from alkera_core.atomic_io import write_json_atomic
from alkera_core.schemas.chat import (
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PromptCancelled,
    TextPart,
)

#: The line the model reads (on the per-turn hidden channel, never in the
#: person's bubble) when a turn is restarted.
TURN_RESTART_NOTE = (
    "The workspace restarted; continuing this turn. Anything the interrupted attempt "
    "wrote to the folder is still there."
)

#: How many times one message's turn is restarted before the harness stops.
TURN_RESTART_LIMIT = 3

#: What the transcript says when that limit is spent.
TURN_RESTART_FAILED = (
    "This turn was interrupted by a restart four times, so it was stopped. "
    "Send the message again to retry it."
)

#: Finish reasons that mean the assistant's answer was complete, as against a
#: step that stopped to call a tool.
_FINAL_REASONS: frozenset[str] = frozenset({"stop", "end_turn", "length", "max_tokens"})

#: The ledger's file name inside the chat directory.
LEDGER_FILE = "turn_restart.json"


@dataclass(frozen=True, slots=True)
class TurnRestart:
    """What the next open does about an interrupted turn.

    ``attempt`` is which restart this would be (1 for the first); ``give_up``
    says the limit is spent and the turn is closed as failed instead."""

    text: str
    attempt: int
    give_up: bool = False


def last_user_text(events: Sequence[Event]) -> tuple[int, str] | None:
    """The index and text of the last message the person sent, or ``None``.

    The text is the message's non-synthetic text parts, joined: exactly the
    words the harness echoed back for it."""
    user_ids: set[str] = set()
    last_index = -1
    last_id = ""
    for index, event in enumerate(events):
        if isinstance(event, MessageCreated) and event.role == "user":
            user_ids.add(event.message_id)
            last_index, last_id = index, event.message_id
    if last_index < 0:
        return None
    parts = [
        event.part.text
        for event in events
        if isinstance(event, PartCreated)
        and isinstance(event.part, TextPart)
        and event.part.message_id == last_id
        and not event.part.synthetic
        and event.part.text
    ]
    text = "\n".join(parts).strip()
    return (last_index, text) if text else None


def _answered_or_cancelled(events: Sequence[Event], after: int) -> bool:
    """Whether, after the person's last message, the turn was cancelled or an
    assistant message completed with a final reason."""
    assistants: set[str] = set()
    for event in events[after:]:
        if isinstance(event, PromptCancelled):
            return True
        if isinstance(event, MessageCreated) and event.role == "assistant":
            assistants.add(event.message_id)
        elif (
            isinstance(event, MessageCompleted)
            and event.message_id in assistants
            and event.finish_reason in _FINAL_REASONS
            and not event.error
        ):
            return True
    return False


def plan_turn_restart(
    events: Iterable[Event], *, interrupted: bool, restarted: int = 0, restarted_text: str = ""
) -> TurnRestart | None:
    """Decide what to do about the log's last turn.

    ``interrupted`` is whether reconcile found the turn open. ``restarted`` is
    how many times the ledger says the message ``restarted_text`` has already
    been restarted; a different message starts the count again."""
    if not interrupted:
        return None
    log = list(events)
    found = last_user_text(log)
    if found is None:
        return None
    index, text = found
    if _answered_or_cancelled(log, index):
        return None
    done = restarted if restarted_text == text else 0
    if done >= TURN_RESTART_LIMIT:
        return TurnRestart(text=text, attempt=done + 1, give_up=True)
    return TurnRestart(text=text, attempt=done + 1)


class RestartLedger:
    """How many times the chat's current message has been restarted, kept in
    a file beside the chat so it survives the process it counts."""

    def __init__(self, chat_dir: Path) -> None:
        self._path = chat_dir / LEDGER_FILE

    def read(self) -> tuple[int, str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0, ""
        if not isinstance(raw, dict):
            return 0, ""
        attempts = raw.get("attempts")
        text = raw.get("text")
        if not isinstance(attempts, int) or not isinstance(text, str):
            return 0, ""
        return attempts, text

    def record(self, restart: TurnRestart) -> None:
        write_json_atomic(self._path, {"attempts": restart.attempt, "text": restart.text})

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


__all__ = [
    "LEDGER_FILE",
    "TURN_RESTART_FAILED",
    "TURN_RESTART_LIMIT",
    "TURN_RESTART_NOTE",
    "RestartLedger",
    "TurnRestart",
    "last_user_text",
    "plan_turn_restart",
]
