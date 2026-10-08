"""A model or effort change as the transcript stores it, read by today's reader.

Every historical fixture under ``fixtures/model_change`` is loaded; the day one
stops loading, a model-change row written by an older build would fail to load
wherever the transcript is read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alkera_core.schemas.objects.transcript import (
    MODEL_CHANGED_KIND,
    SERVER_ONLY_KINDS,
    ModelChangeRecord,
    answers_a_waiting_message,
    aside_note_id,
    model_changed_entry,
)

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "model_change").glob("*.json"))


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.stem for f in FIXTURES])
def test_every_stored_shape_still_loads(fixture: Path) -> None:
    raw = json.loads(fixture.read_text())
    record = ModelChangeRecord.model_validate(raw)
    assert (record.model_id, record.effort) == (raw["model_id"], raw["effort"])
    assert record.model_dump(mode="json")["schema_version"] == ModelChangeRecord.SCHEMA_VERSION


def test_a_field_from_a_newer_writer_survives_a_round_trip() -> None:
    record = ModelChangeRecord.model_validate({"event_id": "e", "model_id": "m", "later": 1})
    assert record.model_dump(mode="json")["later"] == 1


def test_a_surface_this_build_does_not_know_still_loads() -> None:
    """A newer surface (an editor, the CLI) writes a ``decided_via`` an older
    reader has never heard of; the row must still load."""
    record = ModelChangeRecord.model_validate(
        {"event_id": "e", "model_id": "m", "decided_via": "vscode"}
    )
    assert record.decided_via == "vscode"


def test_the_row_answers_no_waiting_message_and_only_the_server_writes_it() -> None:
    entry = model_changed_entry(ModelChangeRecord(event_id=aside_note_id("model-x"), model_id="m"))
    assert entry["kind"] == MODEL_CHANGED_KIND == "model.changed"
    assert entry["role"] == "system"
    assert not answers_a_waiting_message(role=entry["role"], event_id=entry["event_id"])
    assert MODEL_CHANGED_KIND in SERVER_ONLY_KINDS
