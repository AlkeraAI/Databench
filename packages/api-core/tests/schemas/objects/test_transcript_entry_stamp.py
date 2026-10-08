"""The sequence a transcript entry is told to a reader under.

``ChatTranscriptEntry.seq`` is how a reader NAMES a row: the chat document's
state carries it, and so does the rebroadcast of the append that recorded it.
A reader whose loaded window is full may only let go of rows it can ask the
durable record for again, so an entry with no sequence is one it must keep —
which is why the field degrades to "no sequence" rather than refusing a body,
and why a spelling the reader cannot trust as an ordinal is not believed.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.schemas.objects import ChatTranscriptEntry


def _entry(**overrides: Any) -> dict[str, Any]:
    return {
        "event_id": "e1",
        "role": "assistant",
        "kind": "message.completed",
        "payload": {"event_type": "message.completed"},
        **overrides,
    }


def test_a_stamped_entry_round_trips_its_sequence() -> None:
    dumped = ChatTranscriptEntry.model_validate(_entry(seq=17)).model_dump(mode="json")
    assert dumped["seq"] == 17
    assert ChatTranscriptEntry.model_validate(dumped).seq == 17


def test_an_entry_from_a_writer_that_never_stamped_one_loads_with_no_sequence() -> None:
    """Every row written before the stamp existed, and every frame a publisher
    sends: the entry is the machine's, the sequence is the server's."""
    entry = ChatTranscriptEntry.model_validate(_entry())
    assert entry.seq is None
    assert entry.event_id == "e1" and entry.kind == "message.completed"


@pytest.mark.parametrize(
    "seq",
    [
        pytest.param("17", id="numeric-string"),
        pytest.param(17.5, id="float"),
        pytest.param(True, id="bool"),
        pytest.param([17], id="list"),
        pytest.param({"seq": 17}, id="object"),
        pytest.param("banana", id="nonsense"),
    ],
)
def test_a_sequence_that_is_not_an_ordinal_degrades_to_none(seq: Any) -> None:
    """A transcript entry is parsed on a live socket, so a body that is partial
    or hostile has to become a record the existing checks refuse rather than a
    validation error mid-frame. Unbelieved means unstamped, and an unstamped
    row is KEPT — the fail-safe direction: a reader over its budget is a lesser
    fault than a reader that dropped transcript it cannot ask for again."""
    assert ChatTranscriptEntry.model_validate(_entry(seq=seq)).seq is None


def test_the_schema_version_travels_with_the_stamp() -> None:
    """The field is additive, so a reader on the older version still loads a
    newer writer's entry — it simply does not know the key means anything."""
    dumped = ChatTranscriptEntry.model_validate(_entry(seq=3)).model_dump(mode="json")
    assert dumped["schema_version"] == ChatTranscriptEntry.SCHEMA_VERSION
    assert ChatTranscriptEntry.SCHEMA_VERSION.startswith("1."), "additive, never a break"


def test_an_unknown_field_from_a_newer_writer_still_survives_the_round_trip() -> None:
    dumped = ChatTranscriptEntry.model_validate(
        _entry(seq=4, something_a_newer_server_sends="kept")
    ).model_dump(mode="json")
    assert dumped["something_a_newer_server_sends"] == "kept"
    assert dumped["seq"] == 4
