"""Round-trip tests for every Event variant + the unknown-tag fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_core.project.directory import ProjectDirectory
from alkera_core.schemas.chat import (
    CompactionApplied,
    Event,
    FileEdited,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartUpdated,
    RawEvent,
    RevertApplied,
    SessionCreated,
    SessionStatusChanged,
    SessionUpdated,
    TextPart,
    TombstoneApplied,
)
from pydantic import TypeAdapter

EventAdapter = TypeAdapter(Event)
_T = datetime(2026, 5, 26, tzinfo=UTC)


@pytest.mark.parametrize(
    "build",
    [
        lambda: SessionCreated(
            event_id="ev1",
            time=_T,
            session_id="s1",
            title="Investigate slow query",
            cwd="/Users/x/proj",
            model={"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
            agent="general",
        ),
        lambda: SessionUpdated(event_id="ev2", time=_T, session_id="s1", title="Renamed"),
        lambda: MessageCreated(
            event_id="ev3",
            time=_T,
            session_id="s1",
            message_id="m1",
            role="user",
            mode="ask",
        ),
        lambda: MessageCompleted(
            event_id="ev4",
            time=_T,
            session_id="s1",
            message_id="m1",
            finish_reason="stop",
            tokens={"input": 100, "output": 50},
            cost=0.01,
        ),
        lambda: PartCreated(
            event_id="ev5",
            time=_T,
            session_id="s1",
            part=TextPart(part_id="p1", message_id="m1", time=_T, text="hi"),
        ),
        lambda: PartUpdated(
            event_id="ev6",
            time=_T,
            session_id="s1",
            part_id="p1",
            patch={"state": "completed", "output": {"ok": True}},
        ),
        lambda: RevertApplied(event_id="ev7", time=_T, session_id="s1", to_message_id="m1"),
        lambda: CompactionApplied(
            event_id="ev8",
            time=_T,
            session_id="s1",
            summarised_message_ids=["m1", "m2"],
            summary_text="user asked about X",
        ),
        lambda: TombstoneApplied(
            event_id="ev9",
            time=_T,
            session_id="s1",
            event_ids=["ev3", "ev5"],
            reason="pii redaction",
        ),
        # FileEdited 1.1.0 — the optional diff fields must survive round-trip.
        lambda: FileEdited(
            event_id="ev10",
            time=_T,
            session_id="s1",
            path="src/new.py",
            insertions=2,
            deletions=0,
            preview={
                "kind": "diff",
                "content": "--- src/new.py\n+++ src/new.py\n@@ -0,0 +1,2 @@\n+a\n+b\n",
                "title": "new.py",
            },
        ),
        # FileEdited with the diff fields omitted — defaults to None.
        lambda: FileEdited(event_id="ev11", time=_T, session_id="s1", path="src/x.py"),
        # SessionStatusChanged 1.2.0: the attempt stamp survives round-trip.
        lambda: SessionStatusChanged(
            event_id="ev12", time=_T, session_id="s1", status="idle", turn_id="t1"
        ),
        # ... and an event no attempt owns (a runtime notice) keeps its None.
        lambda: SessionStatusChanged(event_id="ev13", time=_T, session_id="s1", status="error"),
    ],
)
def test_event_round_trip(build) -> None:
    original = build()
    dumped = original.model_dump(mode="json")
    reloaded = EventAdapter.validate_python(dumped)
    assert type(reloaded) is type(original)
    assert reloaded.model_dump(mode="json") == dumped


def test_an_unstamped_status_persists_the_null(tmp_path: Path) -> None:
    """An unstamped status writes `turn_id: null` to disk rather than dropping the
    key."""
    chat = ProjectDirectory(tmp_path / ".alkera").chats().create(title="c")
    try:
        chat.append_event(
            SessionStatusChanged(event_id="ev", time=_T, session_id=chat.session_id, status="idle")
        )
        written = [json.loads(line) for line in chat.chat_jsonl_path.read_text().splitlines()]
    finally:
        chat.close()

    line = next(e for e in written if e["event_type"] == "session.status_changed")
    assert "turn_id" in line
    assert line["turn_id"] is None


def test_unknown_event_type_falls_back_to_raw_event() -> None:
    payload: dict[str, Any] = {
        "event_type": "future.event_type",
        "event_id": "ev-x",
        "time": _T.isoformat(),
        "session_id": "s1",
        "schema_version": "1.0.0",
        "custom_payload": {"key": "value"},
    }
    reloaded = EventAdapter.validate_python(payload)
    assert isinstance(reloaded, RawEvent)
    assert reloaded.event_type == "future.event_type"
    assert reloaded.model_dump(mode="json").get("custom_payload") == {"key": "value"}


def test_part_created_holds_unknown_part_variant() -> None:
    """`PartCreated.part` is a `Part` union — should accept a future
    part type via the RawPart fallback."""
    payload: dict[str, Any] = {
        "event_type": "part.created",
        "event_id": "ev1",
        "time": _T.isoformat(),
        "session_id": "s1",
        "schema_version": "1.0.0",
        "part": {
            "type": "future_part_type",
            "part_id": "p1",
            "message_id": "m1",
            "schema_version": "1.0.0",
        },
    }
    reloaded = EventAdapter.validate_python(payload)
    assert isinstance(reloaded, PartCreated)
    # And the part inside is a RawPart (not a crash).
    assert reloaded.part.type == "future_part_type"  # type: ignore[union-attr]
