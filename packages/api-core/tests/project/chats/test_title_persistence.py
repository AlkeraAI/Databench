"""A chat title (set via `/title`) must be durable in the alkera format.

The title lives on `ChatManifest.title`. A `SessionUpdated` event with a
`title` mirrors into the manifest (`Chat.append_event`) and is written to
`chat.jsonl`; on close the manifest is flushed to `manifest.json`, which
`store.open` reads back on resume. This test pins that whole loop at the
storage layer (no harness / no opencode).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import SessionUpdated


def _store(tmp_path: Path):
    return ProjectDirectory(tmp_path / ".alkera").chats()


def test_session_updated_title_mirrors_into_manifest_and_resumes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    chat = store.create()  # no title → untitled
    sid = chat.session_id
    try:
        assert chat.manifest.title is None
        chat.append_event(
            SessionUpdated(
                event_id="u1",
                time=datetime.now(UTC),
                session_id=sid,
                title="Investigate slow query",
            )
        )
        # Mirrored into the in-memory manifest immediately.
        assert chat.manifest.title == "Investigate slow query"
    finally:
        chat.close()  # flushes manifest.json

    # Resume reads the title back from manifest.json.
    chat2 = store.open(sid)
    try:
        assert chat2.manifest.title == "Investigate slow query"
    finally:
        chat2.close()


def test_title_only_changes_when_event_carries_one(tmp_path: Path) -> None:
    """A `SessionUpdated` WITHOUT a title (e.g. a model-only update, or
    opencode's title-less RawEvent on resume-fold) must not wipe an
    existing title."""
    store = _store(tmp_path)
    chat = store.create(title="Original")
    sid = chat.session_id
    try:
        chat.append_event(
            SessionUpdated(
                event_id="u1",
                time=datetime.now(UTC),
                session_id=sid,
                model={"provider_id": "anthropic", "model_id": "x"},
            )
        )
        assert chat.manifest.title == "Original"
    finally:
        chat.close()
