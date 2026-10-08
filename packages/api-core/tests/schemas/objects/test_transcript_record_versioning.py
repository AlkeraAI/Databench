"""Versioned persistence at the chat transcript boundary: every shape that is persisted in
``chat_messages.payload`` or relayed over the chat document must be a
``VersionedModel`` — it carries ``schema_version`` and survives an unknown
field from a newer writer.

Three producers write the shapes a reader in another process consumes:

* the machine's transcript entry envelope, ``alkera_cli.cloud.publish.append_entry``
  (``{event_id, role, kind, payload}``), persisted verbatim by
  ``chat_service.persist_published_events`` and read back by every browser and
  by the mirror's catch-up;
* the person's prompt record, ``chat_service.append_user_message``
  (``{kind, text, client_id, user_id}``), persisted and re-read as a relay by
  ``alkera_cli.cloud.mirror._relay_from_transcript``;
* the relay bodies a route mints (``prompt`` / ``run_query`` / ``promote`` /
  an interrupt answer), parsed field by field in ``mirror._handle_relay``.

Importing the CLI package from an api-core test is deliberate and follows
``test_receipt_seam.py``: the backend may not import ``alkera_cli``, so the
boundary is pinned here. These tests are RED on the reviewed tree — that is the
finding: none of the three shapes is a ``VersionedModel``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_cli.cloud.mirror import _relay_from_transcript
from alkera_cli.cloud.publish import RoleIndex, append_entry
from alkera_core.schemas.chat import MessageCreated


def _machine_entry() -> dict[str, Any]:
    event = MessageCreated(
        event_id="e1",
        session_id="sess-1",
        time=datetime(2026, 9, 7, tzinfo=UTC),
        message_id="m1",
        role="assistant",
    )
    return append_entry(event, RoleIndex())


def test_the_machine_published_entry_carries_a_schema_version() -> None:
    """The envelope the machine appends is what ``chat_messages.payload`` stores
    for every assistant/tool row; only the INNER harness event is versioned."""
    entry = _machine_entry()
    assert entry["payload"]["schema_version"], "the inner harness event is versioned"
    assert "schema_version" in entry, "the persisted envelope itself is not"


def test_a_stored_prompt_read_back_as_a_relay_carries_a_schema_version() -> None:
    """A person's prompt row (written by ``chat_service.append_user_message`` as
    a dict literal) is read back by the mirror as the relay it was — the same
    dict literal, spelled a second time on the other side of the process."""
    row = {
        "id": "8c4e3d6f-5031-4d5e-a08f-a4d6f2c3e444",
        "seq": 1,
        "role": "user",
        "kind": "prompt",
        "payload": {"kind": "prompt", "text": "hi", "client_id": "c1", "user_id": "u1"},
    }
    relay = _relay_from_transcript(row)
    assert relay["kind"] == "prompt" and relay["text"] == "hi"
    assert "schema_version" in row["payload"], "the stored prompt record is unversioned"
    assert "schema_version" in relay, "the relay rebuilt from it is unversioned"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("ChatTranscriptEntry", id="machine-entry-envelope"),
        pytest.param("ChatPromptRecord", id="person-prompt-record"),
        pytest.param("ChatRelay", id="relay-body"),
    ],
)
def test_the_transcript_shapes_have_a_shared_versioned_model(name: str) -> None:
    """One model per shape, in api-core, that BOTH the backend and the daemon
    import — the fix for the hand-transcribed envelope in
    ``apps/backend/tests/test_chat_transcript.py::_published_entry`` and the
    four dict-literal producers in ``routes/chats.py``, ``routes/objects.py``
    and ``realtime/docsync.py``."""
    import alkera_core.schemas.objects as objects

    assert hasattr(objects, name), f"alkera_core.schemas.objects.{name} does not exist"
