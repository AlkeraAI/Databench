"""A mode change as the transcript stores it, read by today's reader.

Every historical fixture under ``fixtures/mode_change`` is loaded; the day one
stops loading, a mode-change row written by an older build would fail to render
on both the web and the Slack thread.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.schemas.objects.transcript import (
    ModeChangeRecord,
    answers_a_waiting_message,
    aside_note_id,
    mode_changed_entry,
)

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "mode_change").glob("*.json"))


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.stem for f in FIXTURES])
def test_every_stored_shape_still_loads(fixture: Path) -> None:
    raw = json.loads(fixture.read_text())
    record = ModeChangeRecord.model_validate(raw)
    assert record.mode == raw["mode"]
    assert record.model_dump(mode="json")["schema_version"] == ModeChangeRecord.SCHEMA_VERSION


def test_a_field_from_a_newer_writer_survives_a_round_trip() -> None:
    record = ModeChangeRecord.model_validate({"event_id": "e", "mode": "auto", "later": 1})
    assert record.model_dump(mode="json")["later"] == 1


def test_the_row_answers_no_waiting_message() -> None:
    """A mode change between a person's message and the agent's turn must not
    read as the machine having taken the message up."""
    entry = mode_changed_entry(ModeChangeRecord(event_id=aside_note_id("mode-x"), mode="plan"))
    assert entry["kind"] == "mode.changed"
    assert not answers_a_waiting_message(role=entry["role"], event_id=entry["event_id"])
