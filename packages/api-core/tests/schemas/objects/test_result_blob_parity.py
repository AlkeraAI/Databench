"""The cloud's result-blob shapes and the daemon's must stay the same shape.

The backend cannot import ``alkera_cli`` (CLAUDE.md's layering rule), so the
envelope the daemon writes is spelled twice — once in the producer, once in
``alkera_core.schemas.objects.result_blob`` where the cloud store reads it.
Two spellings of one wire format is exactly the kind of thing that drifts
silently and truncates an upload months later, so this test is the pin: a field
added on either side fails HERE, at the boundary, rather than in production.

Importing the CLI package from an api-core test is deliberate and is the only
thing this module does with it: it reads the class, it does not call it.
"""

from __future__ import annotations

from typing import Any

from alkera_cli.plugins.plugin_base.result_blob import (
    BlobHandle as DaemonBlobHandle,
)
from alkera_cli.plugins.plugin_base.result_blob import (
    ResultBlobEnvelope as DaemonEnvelope,
)
from alkera_core.schemas.objects import BlobHandle, ResultBlobEnvelope


def _fields(model: type) -> dict[str, Any]:
    return {name: str(field.annotation) for name, field in model.model_fields.items()}


def test_blob_handle_fields_match() -> None:
    assert _fields(BlobHandle) == _fields(DaemonBlobHandle)


def test_envelope_fields_match() -> None:
    assert _fields(ResultBlobEnvelope) == _fields(DaemonEnvelope)


def test_envelope_schema_versions_match() -> None:
    """A cloud reading a version the daemon does not write (or the other way
    round) would need a migration nobody wrote."""
    assert ResultBlobEnvelope.SCHEMA_VERSION == DaemonEnvelope.SCHEMA_VERSION


def test_a_daemon_envelope_round_trips_through_the_cloud_reader() -> None:
    """The pin that matters most: real bytes from the producer, read by the
    store, dumped again, and read back by the producer."""
    produced = DaemonEnvelope(
        kind="rows", columns=["day", "orders"], rows=[["2026-09-01", 3]], total=1
    ).model_dump(mode="json")
    stored = ResultBlobEnvelope.model_validate(produced)
    assert stored.columns == ["day", "orders"]
    assert stored.rows == [["2026-09-01", 3]]
    assert DaemonEnvelope.model_validate(stored.model_dump(mode="json")).total == 1
