"""Conversation clears must be durable in the alkera chat format.

`/clear` resets the model context, but the prior turns stay in `chat.jsonl`
for audit. The `ConversationCleared` event is the boundary marker — it MUST
persist (so resume/replay can show a `— cleared —` divider) and survive a
close+reopen round-trip. Losing it would erase the record that a clear ever
happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import ConversationCleared
from alkera_core.schemas.chat.events import NON_PERSISTED_EVENT_TYPES


def _store(tmp_path: Path):
    return ProjectDirectory(tmp_path / ".alkera").chats()


def test_conversation_cleared_is_a_persisted_event_type() -> None:
    """Guard: conversation.cleared must never be dropped by the persist
    pump (it's not a per-token chunk or heartbeat)."""
    assert "conversation.cleared" not in NON_PERSISTED_EVENT_TYPES


def test_conversation_cleared_round_trips_through_chat_jsonl(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    chat = store.create(title="t")
    sid = chat.session_id
    try:
        chat.append_event(
            ConversationCleared(
                event_id="x1",
                time=datetime.now(UTC),
                session_id=sid,
                cleared_message_ids=["m1", "m2"],
            )
        )
    finally:
        chat.close()

    # Re-open the same chat (resume) and fold the durable log.
    chat2 = store.open(sid)
    try:
        events = list(chat2.events())
    finally:
        chat2.close()

    clears = [e for e in events if isinstance(e, ConversationCleared)]
    assert len(clears) == 1
    assert clears[0].cleared_message_ids == ["m1", "m2"]
