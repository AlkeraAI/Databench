"""Auto-discovering lineage tests for the realtime envelope, payloads and frames.

Walks every fixture under ``packages/api-core/tests/fixtures/realtime/v*/`` and asserts the
**current** reader loads it. The envelope is persisted (outbox ``doc.op``
rows, ``realtime_docs.state``) and the frames are the contract two clients pin
themselves against, so a reader must keep loading whatever a past writer
emitted. If a fixture stops loading, a backwards-compat break slipped in; the
fix is a ``MIGRATIONS`` entry, never an edit to the fixture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alkera_core.schemas.realtime import (
    CLIENT_FRAME_TAGS,
    ENVELOPE_KINDS,
    SERVER_FRAME_TAGS,
    AckPayload,
    CrdtAckPayload,
    CrdtChunk,
    CrdtEphemeralPayload,
    CrdtGonePayload,
    CrdtHelloPayload,
    CrdtLimits,
    CrdtSavingPayload,
    CrdtSyncPayload,
    CrdtUpdatePayload,
    DocEnvelope,
    ErrorPayload,
    FieldWrite,
    HelloPayload,
    OpPayload,
    PresenceCursor,
    PresencePayload,
    PresencePeer,
    RawFrame,
    ReloadPayload,
    SnapshotPayload,
    parse_client_frame,
    parse_server_frame,
)
from alkera_core.versioning import VersionedModel

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "realtime"

#: Which reader loads which payload fixture (by fixture stem prefix).
PAYLOAD_READERS: dict[str, type[VersionedModel]] = {
    "hello": HelloPayload,
    "snapshot": SnapshotPayload,
    "field_write": FieldWrite,
    "op": OpPayload,
    "ack": AckPayload,
    "reload": ReloadPayload,
    "error": ErrorPayload,
    "presence_peer": PresencePeer,
    "presence_cursor": PresenceCursor,
    "presence": PresencePayload,
    "crdt_hello": CrdtHelloPayload,
    "crdt_limits": CrdtLimits,
    "crdt_sync": CrdtSyncPayload,
    "crdt_chunk": CrdtChunk,
    "crdt_update": CrdtUpdatePayload,
    "crdt_ephemeral": CrdtEphemeralPayload,
    "crdt_ack": CrdtAckPayload,
    "crdt_gone": CrdtGonePayload,
    "crdt_saving": CrdtSavingPayload,
}


def _discover(subdir: str) -> list[pytest.param]:
    out: list[pytest.param] = []
    if not FIXTURE_ROOT.is_dir():
        return out
    for version_dir in sorted(FIXTURE_ROOT.iterdir()):
        if not version_dir.is_dir() or not version_dir.name.startswith("v"):
            continue
        category = version_dir / subdir
        if not category.is_dir():
            continue
        for fixture in sorted(category.glob("*.json")):
            out.append(pytest.param(fixture, id=f"{version_dir.name}/{subdir}/{fixture.stem}"))
    return out


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "realtime_fixture_generate", FIXTURE_ROOT / "generate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload_reader(stem: str) -> type[VersionedModel]:
    # Longest prefix wins so ``presence_peer`` is not read as ``presence``.
    for prefix in sorted(PAYLOAD_READERS, key=len, reverse=True):
        if stem == prefix or stem.startswith(prefix + "_"):
            return PAYLOAD_READERS[prefix]
    raise AssertionError(f"no reader registered for payload fixture {stem!r}")


@pytest.mark.parametrize("fixture_path", _discover("envelope"))
def test_envelope_fixture_loads_and_re_dumps(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    envelope = DocEnvelope.model_validate(payload)
    assert envelope.schema_version == DocEnvelope.SCHEMA_VERSION
    assert envelope.kind in ENVELOPE_KINDS
    assert envelope.channel == f"doc:{envelope.doc_type}:{envelope.doc_id}"
    dumped = envelope.model_dump(mode="json")
    assert dumped["schema_version"] == DocEnvelope.SCHEMA_VERSION
    assert DocEnvelope.model_validate(dumped) == envelope


@pytest.mark.parametrize("fixture_path", _discover("envelope"))
def test_unknown_envelope_fields_from_a_newer_writer_survive(fixture_path: Path) -> None:
    payload: dict[str, Any] = json.loads(fixture_path.read_text())
    payload["future_field"] = {"x": 1}
    payload["payload"]["future_payload_key"] = "y"
    dumped = DocEnvelope.model_validate(payload).model_dump(mode="json")
    assert dumped["future_field"] == {"x": 1}
    assert dumped["payload"]["future_payload_key"] == "y"


@pytest.mark.parametrize("fixture_path", _discover("payloads"))
def test_payload_fixture_loads_with_its_reader(fixture_path: Path) -> None:
    reader = _payload_reader(fixture_path.stem)
    model = reader.model_validate(json.loads(fixture_path.read_text()))
    assert model.schema_version == reader.SCHEMA_VERSION
    assert reader.model_validate(model.model_dump(mode="json")) == model


@pytest.mark.parametrize("fixture_path", _discover("frames/client"))
def test_client_frame_fixture_loads_as_a_known_frame(fixture_path: Path) -> None:
    frame = parse_client_frame(fixture_path.read_text())
    assert not isinstance(frame, RawFrame), "a recorded client frame must stay a known tag"
    assert frame.t in CLIENT_FRAME_TAGS
    assert frame.schema_version == type(frame).SCHEMA_VERSION


@pytest.mark.parametrize("fixture_path", _discover("frames/server"))
def test_server_frame_fixture_loads_as_a_known_frame(fixture_path: Path) -> None:
    frame = parse_server_frame(fixture_path.read_text())
    assert not isinstance(frame, RawFrame), "a recorded server frame must stay a known tag"
    assert frame.t in SERVER_FRAME_TAGS
    assert frame.schema_version == type(frame).SCHEMA_VERSION


def test_corpus_is_non_empty() -> None:
    """An empty corpus would make every lineage test pass vacuously."""
    for subdir in ("envelope", "payloads", "frames/client", "frames/server"):
        assert _discover(subdir), (
            f"no fixtures under packages/api-core/tests/fixtures/realtime/*/{subdir}"
        )


def test_corpus_covers_every_kind_and_every_tag() -> None:
    """The current version directory holds an envelope per kind and a frame
    per tag, so a new kind or tag cannot ship without its lineage evidence."""
    generate = _load_generator()
    # The corpus is stamped with the newest version among every model in it
    # (the generator's rule), so a payload bump writes a fresh directory too.
    current = FIXTURE_ROOT / f"v{generate._corpus_version().replace('.', '_')}"
    assert current.is_dir(), f"run generate.py: {current.name} is missing"
    kinds_on_disk = {
        json.loads(p.read_text())["kind"] for p in (current / "envelope").glob("*.json")
    }
    assert kinds_on_disk == set(ENVELOPE_KINDS)
    client_tags = {
        json.loads(p.read_text())["t"] for p in (current / "frames/client").glob("*.json")
    }
    server_tags = {
        json.loads(p.read_text())["t"] for p in (current / "frames/server").glob("*.json")
    }
    assert client_tags == set(CLIENT_FRAME_TAGS)
    assert server_tags == set(SERVER_FRAME_TAGS)
    assert {p.stem for p in (current / "payloads").glob("*.json")} == set(generate.payloads())


def test_current_writer_output_is_captured(tmp_path: Path) -> None:
    """The committed fixtures for the current version are byte-identical to
    what the writer emits today. A shape change that forgets to bump the
    version and regenerate fails here instead of silently rewriting history."""
    generate = _load_generator()
    fresh_dir = generate.regenerate(tmp_path)
    committed_dir = FIXTURE_ROOT / fresh_dir.name
    assert committed_dir.is_dir(), f"run generate.py: {committed_dir.name} is missing"
    fresh = {p.relative_to(fresh_dir): p.read_bytes() for p in fresh_dir.rglob("*.json")}
    committed = {
        p.relative_to(committed_dir): p.read_bytes() for p in committed_dir.rglob("*.json")
    }
    assert fresh == committed
