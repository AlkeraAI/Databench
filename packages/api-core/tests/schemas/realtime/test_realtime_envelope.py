"""The doc-sync envelope and its kind-specific payloads: what is admitted,
what is refused, and the channel grammar. Pure — no database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.schemas.realtime import (
    CHANNEL_PATTERN,
    CRDT_DOC_TYPES,
    DOC_TYPES,
    ENVELOPE_KINDS,
    LEGACY_DOC_TYPE_ALIASES,
    MAX_DOC_ID_LENGTH,
    MAX_OP_ID_LENGTH,
    MAX_PEER_ID_LENGTH,
    OP_INTENTS,
    RESERVED_DOC_TYPES,
    RESERVED_KINDS,
    SERVER_PEER_ID,
    AckPayload,
    DocEnvelope,
    ErrorPayload,
    FieldWrite,
    HelloPayload,
    OpPayload,
    PresencePayload,
    PresencePeer,
    ReloadPayload,
    SnapshotPayload,
    canonical_channel,
    canonical_doc_type,
    is_valid_channel,
    legacy_channel,
    legacy_doc_type,
)
from pydantic import ValidationError


def _envelope(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doc_id": "sess-1",
        "doc_type": "chat",
        "epoch": 2,
        "peer_id": "p:abc",
        "seq": 5,
        "kind": "op",
        "payload": {"op_id": "o1", "intent": "append", "events": []},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DocEnvelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ENVELOPE_KINDS)
def test_every_kind_round_trips(kind: str) -> None:
    envelope = DocEnvelope.model_validate(_envelope(kind=kind, epoch=1, payload={}))
    assert envelope.kind == kind
    assert envelope.schema_version == DocEnvelope.SCHEMA_VERSION
    assert DocEnvelope.model_validate(envelope.model_dump(mode="json")) == envelope


def test_the_vocabulary_is_pinned() -> None:
    assert ENVELOPE_KINDS == (
        "hello",
        "snapshot",
        "op",
        "ack",
        "presence",
        "reload",
        "error",
        "crdt",
    )
    assert OP_INTENTS == ("append", "set_meta", "user_message", "chunk", "set_fields")
    assert RESERVED_KINDS == frozenset()
    assert DOC_TYPES == ("chat", "artifact", "chat_draft", "file", "notebook", "chat_workspace")
    assert CRDT_DOC_TYPES == {"chat_draft", "file", "notebook"}
    assert RESERVED_DOC_TYPES == frozenset()
    assert SERVER_PEER_ID == "srv:0"


@pytest.mark.parametrize("written_at", ["1.0.0", "1.1.0"])
def test_an_envelope_naming_the_old_draft_type_reads_as_chat_draft(written_at: str) -> None:
    old = _envelope(doc_type="chat_workspace", kind="crdt", schema_version=written_at)
    envelope = DocEnvelope.model_validate(old)
    assert envelope.doc_type == "chat_draft"
    assert envelope.channel == "doc:chat_draft:sess-1"
    assert envelope.model_dump(mode="json")["schema_version"] == "2.1.0"


@pytest.mark.parametrize("doc_type", ["chat", "artifact", "file"])
def test_the_rename_leaves_every_other_old_envelope_alone(doc_type: str) -> None:
    old = _envelope(doc_type=doc_type, epoch=1, kind="hello", schema_version="1.1.0")
    assert DocEnvelope.model_validate(old).doc_type == doc_type


def test_a_client_from_before_the_rename_may_still_say_the_old_name() -> None:
    # The wire keeps the alias for a deprecation window: an older web or VS
    # Code build sends unstamped envelopes under the old name, and the socket
    # (not the model) maps it to the current one.
    envelope = DocEnvelope.model_validate(_envelope(doc_type="chat_workspace", kind="crdt"))
    assert envelope.doc_type == "chat_workspace"
    assert canonical_doc_type(envelope.doc_type) == "chat_draft"


@pytest.mark.parametrize(
    ("spelled", "current", "was_legacy"),
    [
        pytest.param("doc:chat_workspace:sess-1", "doc:chat_draft:sess-1", True, id="legacy"),
        pytest.param("doc:chat_draft:sess-1", "doc:chat_draft:sess-1", False, id="current"),
        pytest.param("doc:chat:sess-1", "doc:chat:sess-1", False, id="another-type"),
        pytest.param("machine:m-1", "machine:m-1", False, id="not-a-document"),
        # A document id that happens to hold the old name is not respelled.
        pytest.param(
            "doc:chat:chat_workspace:1", "doc:chat:chat_workspace:1", False, id="the-name-in-an-id"
        ),
    ],
)
def test_canonical_channel_respells_only_the_legacy_type(
    spelled: str, current: str, was_legacy: bool
) -> None:
    assert canonical_channel(spelled) == (current, was_legacy)


@pytest.mark.parametrize(
    ("current", "spelled"),
    [
        pytest.param("doc:chat_draft:sess-1", "doc:chat_workspace:sess-1", id="draft"),
        pytest.param("doc:chat:sess-1", "doc:chat:sess-1", id="no-legacy-spelling"),
        pytest.param("machine:m-1", "machine:m-1", id="not-a-document"),
    ],
)
def test_legacy_channel_is_the_inverse_for_a_renamed_type(current: str, spelled: str) -> None:
    assert legacy_channel(current) == spelled
    assert canonical_channel(spelled)[0] == current


def test_every_legacy_alias_names_a_current_document_type() -> None:
    for legacy, current in LEGACY_DOC_TYPE_ALIASES.items():
        assert legacy in DOC_TYPES and current in DOC_TYPES
        assert legacy not in CRDT_DOC_TYPES and current in CRDT_DOC_TYPES
        assert legacy_doc_type(current) == legacy


def test_hello_may_carry_epoch_zero_and_any_later_epoch() -> None:
    assert DocEnvelope.model_validate(_envelope(kind="hello", epoch=0, payload={})).epoch == 0
    assert DocEnvelope.model_validate(_envelope(kind="hello", epoch=4, payload={})).epoch == 4


@pytest.mark.parametrize("kind", [k for k in ENVELOPE_KINDS if k != "hello"])
def test_epoch_zero_is_refused_on_every_kind_but_hello(kind: str) -> None:
    with pytest.raises(ValidationError, match="epoch 0 is only valid on a hello"):
        DocEnvelope.model_validate(_envelope(kind=kind, epoch=0, payload={}))


def test_epoch_zero_is_refused_on_assignment_too() -> None:
    envelope = DocEnvelope.model_validate(_envelope())
    with pytest.raises(ValidationError):
        envelope.epoch = 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"epoch": -1}, id="negative-epoch"),
        pytest.param({"seq": -1}, id="negative-seq"),
        pytest.param({"kind": "sync"}, id="unknown-kind"),
        pytest.param({"kind": ""}, id="empty-kind"),
        pytest.param({"doc_type": "graph"}, id="unknown-doc-type"),
        pytest.param({"doc_id": ""}, id="empty-doc-id"),
        pytest.param({"doc_id": "x" * (MAX_DOC_ID_LENGTH + 1)}, id="doc-id-too-long"),
        pytest.param({"peer_id": ""}, id="empty-peer-id"),
        pytest.param({"peer_id": "p" * (MAX_PEER_ID_LENGTH + 1)}, id="peer-id-too-long"),
        pytest.param({"payload": []}, id="payload-not-an-object"),
        pytest.param({"epoch": "two"}, id="epoch-not-an-int"),
    ],
)
def test_malformed_envelopes_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocEnvelope.model_validate(_envelope(**overrides))


def test_missing_required_fields_are_refused() -> None:
    for field in ("doc_id", "doc_type", "epoch", "peer_id", "seq", "kind"):
        body = _envelope()
        del body[field]
        with pytest.raises(ValidationError):
            DocEnvelope.model_validate(body)


def test_channel_is_derived_from_type_and_id() -> None:
    assert DocEnvelope.model_validate(_envelope()).channel == "doc:chat:sess-1"
    artifact = DocEnvelope.model_validate(_envelope(doc_type="artifact", doc_id="0f2b"))
    assert artifact.channel == "doc:artifact:0f2b"


def test_payload_defaults_to_empty_and_is_kept_verbatim() -> None:
    body = _envelope()
    del body["payload"]
    assert DocEnvelope.model_validate(body).payload == {}
    kept = DocEnvelope.model_validate(_envelope(payload={"anything": [1, {"nested": True}]}))
    assert kept.payload == {"anything": [1, {"nested": True}]}


def test_unknown_fields_survive_round_trip() -> None:
    dumped = DocEnvelope.model_validate(_envelope(future=1)).model_dump(mode="json")
    assert dumped["future"] == 1


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intent", OP_INTENTS)
def test_every_intent_is_accepted(intent: str) -> None:
    op = OpPayload(op_id="o1", intent=intent)  # type: ignore[arg-type]
    assert op.intent == intent
    assert op.events == [] and op.fields == {} and op.meta == {}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"op_id": "o1", "intent": "merge"}, id="unknown-intent"),
        pytest.param({"op_id": "", "intent": "append"}, id="empty-op-id"),
        pytest.param({"op_id": "o" * (MAX_OP_ID_LENGTH + 1), "intent": "append"}, id="op-id-long"),
        pytest.param({"intent": "append"}, id="missing-op-id"),
        pytest.param({"op_id": "o1"}, id="missing-intent"),
        pytest.param({"op_id": "o1", "intent": "append", "events": {}}, id="events-not-a-list"),
        pytest.param(
            {"op_id": "o1", "intent": "set_fields", "fields": {"title": {"value": "x"}}},
            id="field-write-without-ts",
        ),
        pytest.param(
            {"op_id": "o1", "intent": "set_fields", "fields": {"title": "plain"}},
            id="field-write-not-an-object",
        ),
    ],
)
def test_malformed_ops_are_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OpPayload.model_validate(body)


def test_set_fields_parses_each_field_write() -> None:
    op = OpPayload.model_validate(
        {
            "op_id": "o1",
            "intent": "set_fields",
            "fields": {"title": {"value": "T", "ts": 1.5}, "body": {"value": None, "ts": 2}},
        }
    )
    assert op.fields["title"] == FieldWrite(value="T", ts=1.5)
    assert op.fields["body"].value is None
    assert op.fields["body"].ts == 2.0


def test_hello_since_seq_is_optional_and_non_negative() -> None:
    assert HelloPayload().since_seq is None
    assert HelloPayload(since_seq=0).since_seq == 0
    with pytest.raises(ValidationError):
        HelloPayload(since_seq=-1)


def test_snapshot_defaults_and_bounds() -> None:
    assert SnapshotPayload().model_dump(mode="json")["state"] == {}
    assert SnapshotPayload(state={"a": 1}, seq=3).seq == 3
    with pytest.raises(ValidationError):
        SnapshotPayload(seq=-1)


def test_ack_reload_and_error_shapes() -> None:
    assert AckPayload(op_id="o1", seq=4, changed=False).changed is False
    with pytest.raises(ValidationError):
        AckPayload(op_id="o1", seq=4)  # type: ignore[call-arg]
    assert ReloadPayload(epoch=1, reason="stale_epoch").epoch == 1
    with pytest.raises(ValidationError):
        ReloadPayload(epoch=0, reason="stale_epoch")
    with pytest.raises(ValidationError):
        ReloadPayload(epoch=1, reason="")
    assert ErrorPayload(code="forbidden").message == ""
    with pytest.raises(ValidationError):
        ErrorPayload(code="")


def test_presence_roster_round_trips_with_timezone_aware_times() -> None:
    peer = PresencePeer(peer_id="p:1", user_id="u1", last_seen_at=datetime(2026, 9, 5, tzinfo=UTC))
    roster = PresencePayload(peers=[peer])
    reloaded = PresencePayload.model_validate(roster.model_dump(mode="json"))
    assert reloaded.peers[0].last_seen_at == peer.last_seen_at
    assert reloaded.peers[0].last_seen_at.tzinfo is not None
    with pytest.raises(ValidationError):
        PresencePeer(peer_id="", user_id="u1", last_seen_at=peer.last_seen_at)


# ---------------------------------------------------------------------------
# Channel grammar
# ---------------------------------------------------------------------------


def test_channel_pattern_is_pinned() -> None:
    assert CHANNEL_PATTERN == (
        r"^doc:(chat_workspace|chat_draft|artifact|notebook|chat|file):([A-Za-z0-9._:-]{1,255})$"
    )


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        pytest.param("doc:chat:sess-1", True, id="chat"),
        pytest.param("doc:artifact:0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f", True, id="artifact-uuid"),
        pytest.param("doc:chat:a.b_c:d", True, id="allowed-punctuation"),
        pytest.param("doc:chat_draft:sess-1", True, id="chat-draft"),
        pytest.param("doc:chat_workspace:sess-1", True, id="the-legacy-draft-spelling"),
        pytest.param("doc:file:f-1", True, id="file"),
        pytest.param("doc:chat_work:sess-1", False, id="a-prefix-of-a-type"),
        pytest.param("doc:chat_draftx:sess-1", False, id="a-type-with-a-suffix"),
        pytest.param("doc:chat:" + "x" * 255, True, id="max-length-id"),
        pytest.param("doc:chat:" + "x" * 256, False, id="id-too-long"),
        pytest.param("doc:chat:", False, id="empty-id"),
        pytest.param("doc:graph:g1", False, id="unknown-type"),
        pytest.param("chat:sess-1", False, id="missing-doc-prefix"),
        pytest.param("doc:chat:sess 1", False, id="space"),
        pytest.param("doc:chat:sess/1", False, id="slash"),
        pytest.param("doc:chat:sess-1\n", False, id="trailing-newline"),
        pytest.param("DOC:chat:sess-1", False, id="uppercase-prefix"),
        pytest.param("doc:Chat:sess-1", False, id="uppercase-type"),
        pytest.param("", False, id="empty"),
    ],
)
def test_is_valid_channel(value: str, valid: bool) -> None:
    assert is_valid_channel(value) is valid
