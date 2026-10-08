"""Compaction must be durable in the alkera chat format.

`CompactionApplied` carries the summary + elided ids; it MUST persist to
`chat.jsonl` (so resume/replay can show it) and survive a close+reopen
round-trip. The summary message opencode streams is suppressed by the
translator, so the ONLY record of a compaction in our save format is this
event — losing it would silently drop the summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import CompactionApplied
from alkera_core.schemas.chat.events import NON_PERSISTED_EVENT_TYPES


def _store(tmp_path: Path):
    return ProjectDirectory(tmp_path / ".alkera").chats()


def test_compaction_applied_is_a_persisted_event_type() -> None:
    """Guard: compaction.applied must never be dropped by the persist
    pump (it's not a per-token chunk or heartbeat)."""
    assert "compaction.applied" not in NON_PERSISTED_EVENT_TYPES


def test_compaction_applied_round_trips_through_chat_jsonl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(title="t")
    sid = chat.session_id
    try:
        chat.append_event(
            CompactionApplied(
                event_id="c1",
                time=datetime.now(UTC),
                session_id=sid,
                summary_text="## Goal\n- summarized the work",
                summarised_message_ids=["m1", "m2"],
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

    compactions = [e for e in events if isinstance(e, CompactionApplied)]
    assert len(compactions) == 1
    assert compactions[0].summary_text == "## Goal\n- summarized the work"
    assert compactions[0].summarised_message_ids == ["m1", "m2"]
