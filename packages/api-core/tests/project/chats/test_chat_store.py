"""End-to-end tests for `ChatStore` lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alkera_core.project.chats.chat import ChatNotFoundError
from alkera_core.project.directory import ProjectDirectory
from alkera_core.project.locking import LockHeldError
from alkera_core.schemas.chat import (
    MessageCompleted,
    SessionCreated,
    TextPart,
)


def _store(tmp_path: Path):
    pd = ProjectDirectory(tmp_path / ".alkera")
    return pd.chats()


def test_create_returns_locked_chat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(title="hello")
    try:
        assert chat.session_id != ""
        assert chat.manifest.title == "hello"
        # Lock file present
        assert (chat.path / ".lock").exists()
        # Seeded session.created event in jsonl
        events = list(chat.events())
        assert len(events) == 1
        assert isinstance(events[0], SessionCreated)
        assert events[0].title == "hello"
    finally:
        chat.close()


def test_create_uses_provided_session_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(session_id="my-session")
    try:
        assert chat.session_id == "my-session"
        assert (store.path / "my-session").is_dir()
    finally:
        chat.close()


def test_create_duplicate_session_id_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    c1 = store.create(session_id="dup")
    c1.close()
    with pytest.raises(FileExistsError):
        store.create(session_id="dup")


def test_close_releases_lock_and_writes_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(title="t")
    chat.close()
    # Lock file should be gone.
    assert not (chat.path / ".lock").exists()
    # Manifest persisted with the title.
    import json

    manifest = json.loads((chat.path / "manifest.json").read_text())
    assert manifest["title"] == "t"


def test_open_existing_chat(tmp_path: Path) -> None:
    """Smoke: create + list summaries works while the chat handle is
    held. See test_create_then_close_then_reopen for the full open
    lifecycle."""
    store = _store(tmp_path)
    chat = store.create(title="first")
    try:
        summaries = list(store.list_summaries())
        assert len(summaries) == 1
        assert summaries[0].title == "first"
    finally:
        chat.close()


def test_create_then_close_then_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(title="first")
    sid = chat.session_id
    chat.append_event(
        MessageCompleted(
            event_id="ev1",
            time=datetime.now(UTC),
            session_id=sid,
            message_id="m1",
            finish_reason="stop",
            tokens={"input": 100, "output": 50},
        )
    )
    chat.close()

    # Reopen.
    chat2 = store.open(sid)
    try:
        events = list(chat2.events())
        # session.created + message.completed
        assert len(events) == 2
        # Manifest aggregated tokens.
        assert chat2.manifest.tokens_total.input == 100
        assert chat2.manifest.tokens_total.output == 50
    finally:
        chat2.close()


def test_open_missing_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ChatNotFoundError):
        store.open("nope")


def test_open_locked_chat_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(session_id="locked")
    try:
        # Second open from the same process - should refuse because
        # the lock is already held by us.
        with pytest.raises(LockHeldError):
            store.open("locked")
    finally:
        chat.close()


def test_delete_removes_chat_dir(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(session_id="ephemeral")
    chat.close()
    assert (store.path / "ephemeral").is_dir()
    store.delete("ephemeral")
    assert not (store.path / "ephemeral").exists()


def test_delete_missing_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ChatNotFoundError):
        store.delete("never-existed")


def test_list_summaries_picks_up_chats(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat1 = store.create(title="first")
    chat1.close()
    chat2 = store.create(title="second")
    chat2.close()

    summaries = store.list_summaries()
    titles = sorted(s.title for s in summaries if s.title)
    assert titles == ["first", "second"]


def test_list_summaries_falls_back_to_jsonl_when_manifest_corrupt(
    tmp_path: Path,
) -> None:
    """If manifest.json is unparseable, we still produce a usable
    summary by folding the JSONL."""
    store = _store(tmp_path)
    chat = store.create(session_id="rec", title="reconstructable")
    chat.close()
    # Corrupt the manifest.
    (chat.path / "manifest.json").write_text("garbage{not-json")

    summaries = store.list_summaries()
    assert len(summaries) == 1
    assert summaries[0].title == "reconstructable"
    assert summaries[0].session_id == "rec"


def test_attach_blob_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(session_id="with-blob")
    try:
        file_part = chat.add_blob(b"contents", filename="x.txt", mime="text/plain")
        assert file_part.size == 8
        # The blob is now in the project's global blob store.
        assert store.blobs.exists(file_part.sha256)
        assert store.blobs.read(file_part.sha256) == b"contents"
    finally:
        chat.close()


def test_gc_after_chat_delete_sweeps_blobs(tmp_path: Path) -> None:
    """Delete a chat that referenced a blob, then run GC with grace=0
    — the blob should disappear."""
    import os
    import time as _t

    store = _store(tmp_path)
    chat = store.create(session_id="will-delete")
    file_part = chat.add_blob(b"to-be-swept", filename="x.bin")
    sha = file_part.sha256
    # We didn't ACTUALLY reference the blob from an event yet — that's
    # how a real consumer would link them. Append a PartCreated so the
    # GC walker sees the reference and we can verify "still referenced"
    # behavior in the dedicated test below.
    from alkera_core.schemas.chat import PartCreated as _Pc

    chat.append_event(
        _Pc(
            event_id="ev-blob",
            time=datetime.now(UTC),
            session_id="will-delete",
            part=file_part.model_copy(update={"message_id": "m1"}),
        )
    )
    chat.close()

    # Sanity: blob is referenced now, gc should keep it.
    report = store.gc(grace_period_seconds=0)
    assert store.blobs.exists(sha)
    assert report.deleted == 0

    # Delete the chat → blob is now orphaned.
    store.delete("will-delete")
    # Backdate the blob so it's past the grace period.
    blob_path = store.blobs.path(sha)
    past = _t.time() - 3600
    os.utime(blob_path, (past, past))
    report = store.gc(grace_period_seconds=60)
    assert report.deleted == 1
    assert not store.blobs.exists(sha)


def test_text_part_event_round_trip_via_chat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chat = store.create(session_id="tp")
    sid = chat.session_id
    from alkera_core.schemas.chat import PartCreated as _Pc

    chat.append_event(
        _Pc(
            event_id="ev1",
            time=datetime.now(UTC),
            session_id=sid,
            part=TextPart(part_id="p1", message_id="m1", text="hello"),
        )
    )
    chat.close()

    chat2 = store.open(sid)
    try:
        events = list(chat2.events())
        # session.created + part.created
        assert len(events) == 2
        pc = events[1]
        assert isinstance(pc, _Pc)
        assert pc.part.type == "text"  # type: ignore[union-attr]
    finally:
        chat2.close()
