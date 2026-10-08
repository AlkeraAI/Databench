"""A chat folder copied whole opens as its own chat, over the transcript it brought.

The cloud copies a chat's folder byte for byte and files it under a NEW chat
id; the box then pulls it to ``.alkera/chats/<new id>/`` and opens it there.
What travels is the source's manifest — with the source's ``session_id`` in it
— its event log and the harness's own session storage. The store is the
loader: it must hand back a chat whose identity is the directory it was opened
under, with the copied events intact and the source untouched.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from alkera_core.project.chats.store import ChatStore
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import MessageCompleted, SessionCreated


def _store(tmp_path: Path) -> ChatStore:
    return ProjectDirectory(tmp_path / ".alkera").chats()


def _seed(store: ChatStore, session_id: str) -> list[str]:
    """A closed chat with a two-event transcript and harness storage beside it."""
    chat = store.create(
        session_id=session_id, title="Warehouse spike", harness={"pinned": "sess-1"}
    )
    try:
        chat.append_event(
            MessageCompleted(
                event_id="evt-2",
                time=datetime.now(UTC),
                session_id=session_id,
                message_id="m1",
                tokens={"input": 3, "output": 5},
                cost=0.25,
            )
        )
    finally:
        chat.close()
    runtime = chat.path / ".runtime" / "agent"
    runtime.mkdir(parents=True)
    (runtime / "agent.db").write_bytes(b"opaque harness session bytes")
    return [event.event_id for event in store.read_events(session_id)]


def _copy_folder(store: ChatStore, source: str, target: str) -> Path:
    """What a pull of the copied folder leaves on disk: everything but the
    per-process lock, under the new id, with the source's manifest as it was."""
    made = shutil.copytree(
        store.path / source, store.path / target, ignore=shutil.ignore_patterns(".lock*")
    )
    assert json.loads((made / "manifest.json").read_text())["session_id"] == source
    return made


def test_a_copied_folder_opens_under_its_new_id_with_the_transcript(tmp_path: Path) -> None:
    store = _store(tmp_path)
    events = _seed(store, "chat-source")
    copied = _copy_folder(store, "chat-source", "chat-copy")

    chat = store.open("chat-copy")
    try:
        assert chat.session_id == "chat-copy"
        assert chat.manifest.session_id == "chat-copy"
        assert chat.manifest.title == "Warehouse spike"
        # The harness's pinned session survives: it is what the adapter re-attaches to.
        assert chat.manifest.harness == {"pinned": "sess-1"}
        assert chat.manifest.tokens_total.output == 5 and chat.manifest.cost_total == 0.25
        assert [event.event_id for event in chat.events()] == events
        assert isinstance(next(iter(chat.events())), SessionCreated)
        # Every event names the chat it now belongs to: nothing downstream that
        # files an event by its session may hand the copy's turns to the source.
        assert {event.session_id for event in chat.events()} == {"chat-copy"}
    finally:
        chat.close()
    assert (
        copied / ".runtime" / "agent" / "agent.db"
    ).read_bytes() == b"opaque harness session bytes"
    # The adoption is durable: the next open reads the new id straight off disk.
    assert json.loads((copied / "manifest.json").read_text())["session_id"] == "chat-copy"


def test_adopting_the_copy_leaves_the_source_as_it_was(tmp_path: Path) -> None:
    store = _store(tmp_path)
    events = _seed(store, "chat-source")
    before = (store.path / "chat-source" / "manifest.json").read_text()
    _copy_folder(store, "chat-source", "chat-copy")

    store.open("chat-copy").close()

    assert (store.path / "chat-source" / "manifest.json").read_text() == before
    source = store.open("chat-source")
    try:
        assert source.session_id == "chat-source"
        assert [event.event_id for event in source.events()] == events
        assert {event.session_id for event in source.events()} == {"chat-source"}
    finally:
        source.close()
    assert sorted(store.list_session_ids()) == ["chat-copy", "chat-source"]


def test_a_manifest_that_already_names_its_directory_is_not_rewritten(tmp_path: Path) -> None:
    """The negative twin: an ordinary open is not a rewrite. A chat whose
    manifest agrees with its directory keeps the bytes it had."""
    store = _store(tmp_path)
    _seed(store, "chat-source")
    manifest = store.path / "chat-source" / "manifest.json"
    before = manifest.read_bytes()

    log_before = (store.path / "chat-source" / "chat.jsonl").read_bytes()

    store.open("chat-source").close()

    assert manifest.read_bytes() == before
    assert (store.path / "chat-source" / "chat.jsonl").read_bytes() == log_before


def test_the_copy_and_the_source_are_two_chats_that_diverge(tmp_path: Path) -> None:
    """Resuming from the duplicate means writing to it: an event appended to the
    copy lands in the copy's log only, and the copy's manifest tail moves while
    the source's stays."""
    store = _store(tmp_path)
    events = _seed(store, "chat-source")
    _copy_folder(store, "chat-source", "chat-copy")

    copy = store.open("chat-copy")
    try:
        copy.append_event(
            MessageCompleted(
                event_id="evt-3", time=datetime.now(UTC), session_id="chat-copy", message_id="m2"
            )
        )
    finally:
        copy.close()

    assert [e.event_id for e in store.read_events("chat-copy")] == [*events, "evt-3"]
    assert [e.event_id for e in store.read_events("chat-source")] == events
    assert json.loads((store.path / "chat-copy" / "manifest.json").read_text())[
        "last_event_id"
    ] == ("evt-3")
    assert (
        json.loads((store.path / "chat-source" / "manifest.json").read_text())["last_event_id"]
        == events[-1]
    )
