"""`ChatStore.read_events` — lock-free replay (the observe-path substrate).

A reader must be able to replay a chat's events WITHOUT taking the per-chat write
lock, so an observer can follow a chat its owner is actively writing (e.g. a user
inspecting a running subagent the parent holds open). These pin that invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import SessionCreated, SessionStatusChanged


def _store(tmp_path: Path):
    return ProjectDirectory(tmp_path / ".alkera").chats()


def test_read_events_works_while_the_chat_is_open_and_locked(tmp_path: Path) -> None:
    # The whole point: a reader does NOT take the lock, so it can read a chat that
    # is OPEN (its writer holds the exclusive lock) — no LockHeldError.
    store = _store(tmp_path)
    chat = store.create(session_id="s1")  # holds the write lock
    try:
        chat.append_event(
            SessionStatusChanged(
                event_id="e-running",
                time=datetime.now(UTC),
                session_id="s1",
                status="running",
            )
        )
        events = list(store.read_events("s1"))  # lock-free; must NOT raise
        event_ids = [e.event_id for e in events]
        # SessionCreated (stamped on create) + the running status we appended.
        assert any(isinstance(e, SessionCreated) for e in events)
        assert "e-running" in event_ids
    finally:
        chat.close()


def test_read_events_after_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(session_id="s2").close()
    events = list(store.read_events("s2"))
    assert any(isinstance(e, SessionCreated) for e in events)


def test_read_events_unknown_session_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert list(store.read_events("does-not-exist")) == []
