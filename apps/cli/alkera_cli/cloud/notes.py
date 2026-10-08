"""The events of a note the box writes into a chat's transcript itself."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from alkera_core.schemas.chat import MessageCompleted, MessageCreated, PartCreated, TextPart
from alkera_core.schemas.objects import aside_note_id


def note_events(
    chat_id: str, message: str, *, ok: bool = False, answers_nothing: bool = False
) -> tuple[MessageCreated, PartCreated, MessageCompleted]:
    """A system message of its own, with its own event ids, that no turn is
    settled or counted by. ``answers_nothing`` marks a note the box writes
    about ITSELF (spelled into the id, so a later box and the server read it
    the same way): the catch-up keeps looking past it for a question still
    waiting. Any other note answers the message before it."""
    token = secrets.token_hex(8)
    note_id = aside_note_id(token) if answers_nothing else f"note-{token}"
    now = datetime.now(UTC)
    return (
        MessageCreated(
            event_id=f"{note_id}-created",
            time=now,
            session_id=chat_id,
            message_id=note_id,
            role="system",
        ),
        PartCreated(
            event_id=f"{note_id}-text",
            time=now,
            session_id=chat_id,
            part=TextPart(
                part_id=f"{note_id}-part", message_id=note_id, text=message, synthetic=True
            ),
        ),
        MessageCompleted(
            event_id=f"{note_id}-done",
            time=now,
            session_id=chat_id,
            message_id=note_id,
            finish_reason="stop" if ok else "error",
        ),
    )


__all__ = ["note_events"]
