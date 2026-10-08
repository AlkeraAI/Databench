"""The Loro lane's payloads: what is admitted and what is refused before a byte
reaches the sandbox. Pure — no database, no Loro."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_MAX_EPHEMERAL_BYTES,
    CRDT_MAX_INLINE_BYTES,
    CRDT_MAX_PEER,
    CRDT_MAX_TRANSFER_BYTES,
    CRDT_PROTOCOL,
    CRDT_SERVER_PEER_MAX,
    CrdtAckPayload,
    CrdtChunk,
    CrdtEphemeralPayload,
    CrdtGonePayload,
    CrdtHelloPayload,
    CrdtLimits,
    CrdtSaving,
    CrdtSavingPayload,
    CrdtSyncPayload,
    CrdtUpdatePayload,
    DocEnvelope,
    ErrorPayload,
    RawCrdtPayload,
    ReloadPayload,
    SocketLimits,
    WelcomeFrame,
    decode_b64,
    encode_b64,
    parse_crdt_payload,
)
from pydantic import ValidationError

PEER = CRDT_SERVER_PEER_MAX + 1
VV = encode_b64(b"\x01\x02\x03")


def _b64(size: int) -> str:
    return encode_b64(b"x" * size)


def _limits() -> CrdtLimits:
    return CrdtLimits(chunk_bytes=CRDT_CHUNK_BYTES, max_update_bytes=1, max_doc_bytes=1)


def _chunk(**overrides: Any) -> dict[str, Any]:
    total = 2 * CRDT_CHUNK_BYTES + 1
    base: dict[str, Any] = {
        "xfer_id": "x-1",
        "index": 0,
        "count": 3,
        "total_bytes": total,
        "sha256_b64": encode_b64(hashlib.sha256(b"whole").digest()),
        "data_b64": _b64(CRDT_CHUNK_BYTES),
    }
    base.update(overrides)
    return base


def _sync(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "updates",
        "vv_b64": VV,
        "loro_peer": PEER,
        "doc_schema": 1,
        "limits": _limits().model_dump(mode="json"),
        "data_b64": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# base64
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("AAE", id="missing-padding"),
        pytest.param("AA E=", id="whitespace"),
        pytest.param("AA\nE=", id="newline"),
        pytest.param("-_8=", id="url-alphabet"),
        pytest.param("AAE=AAE=", id="padding-mid-string"),
        pytest.param("AAÉ=", id="non-ascii"),
    ],
)
def test_only_strict_standard_base64_decodes(value: str) -> None:
    with pytest.raises(ValueError, match="not standard base64"):
        decode_b64(value)


@pytest.mark.parametrize("raw", [b"", b"\x00", b"\xff\xfe\xfd", bytes(range(256))])
def test_base64_round_trips(raw: bytes) -> None:
    assert decode_b64(encode_b64(raw)) == raw


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def test_a_well_formed_chunk_round_trips() -> None:
    chunk = CrdtChunk.model_validate(_chunk(index=2, data_b64=_b64(1)))
    assert CrdtChunk.model_validate(chunk.model_dump(mode="json")) == chunk


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"index": 3}, id="index-at-count"),
        pytest.param({"index": -1}, id="negative-index"),
        pytest.param({"count": 4}, id="count-disagrees-with-size"),
        pytest.param({"count": 1, "total_bytes": CRDT_CHUNK_BYTES}, id="fits-one-frame"),
        pytest.param({"count": 2, "total_bytes": CRDT_CHUNK_BYTES}, id="total-at-one-chunk"),
        pytest.param(
            {
                "count": CRDT_MAX_TRANSFER_BYTES // CRDT_CHUNK_BYTES + 1,
                "total_bytes": CRDT_MAX_TRANSFER_BYTES + 1,
            },
            id="transfer-too-large",
        ),
        pytest.param({"data_b64": _b64(CRDT_CHUNK_BYTES + 1)}, id="piece-over-chunk-size"),
        pytest.param({"data_b64": ""}, id="empty-piece"),
        pytest.param({"data_b64": "not base64!"}, id="piece-not-base64"),
        pytest.param({"sha256_b64": encode_b64(b"x" * 31) + "A"}, id="digest-wrong-length"),
        pytest.param({"sha256_b64": encode_b64(b"x" * 33)}, id="digest-too-long"),
        pytest.param({"xfer_id": "has space"}, id="bad-xfer-id"),
    ],
)
def test_a_malformed_chunk_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CrdtChunk.model_validate(_chunk(**overrides))


def test_the_largest_transfer_is_a_whole_number_of_chunks() -> None:
    chunk = CrdtChunk.model_validate(
        _chunk(
            count=CRDT_MAX_TRANSFER_BYTES // CRDT_CHUNK_BYTES, total_bytes=CRDT_MAX_TRANSFER_BYTES
        )
    )
    assert chunk.count == 512


# ---------------------------------------------------------------------------
# hello
# ---------------------------------------------------------------------------


def test_a_first_hello_carries_no_state() -> None:
    hello = CrdtHelloPayload(proto=CRDT_PROTOCOL, loro="1.16.4", doc_schema=1)
    assert (hello.vv_b64, hello.loro_peer, hello.epoch_seen) == (None, None, None)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"proto": 0}, id="protocol-zero"),
        pytest.param({"doc_schema": 0}, id="schema-zero"),
        pytest.param({"loro": ""}, id="no-loro-version"),
        pytest.param({"vv_b64": VV}, id="vector-without-its-epoch"),
        pytest.param({"vv_b64": "%%", "epoch_seen": 1}, id="vector-not-base64"),
        pytest.param({"loro_peer": CRDT_SERVER_PEER_MAX}, id="peer-in-server-range"),
        pytest.param({"loro_peer": 0}, id="peer-zero"),
        pytest.param({"loro_peer": CRDT_MAX_PEER + 1}, id="peer-past-a-js-number"),
        pytest.param({"epoch_seen": 0}, id="epoch-zero"),
    ],
)
def test_a_malformed_hello_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CrdtHelloPayload.model_validate(
            {"proto": CRDT_PROTOCOL, "loro": "1.16.4", "doc_schema": 1, **overrides}
        )


def test_a_resuming_hello_names_its_vector_epoch_and_peer() -> None:
    hello = CrdtHelloPayload(
        proto=1, loro="1.16.4", doc_schema=1, vv_b64=VV, loro_peer=CRDT_MAX_PEER, epoch_seen=3
    )
    assert CrdtHelloPayload.model_validate(hello.model_dump(mode="json")) == hello


# ---------------------------------------------------------------------------
# sync / update: inline or chunked, never both, never neither
# ---------------------------------------------------------------------------


def test_an_empty_catch_up_is_a_valid_sync() -> None:
    assert CrdtSyncPayload.model_validate(_sync()).data_b64 == ""


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"data_b64": None}, id="neither-inline-nor-chunk"),
        pytest.param({"chunk": _chunk()}, id="both-inline-and-chunk"),
        pytest.param({"data_b64": _b64(CRDT_CHUNK_BYTES + 1)}, id="inline-over-one-frame"),
        pytest.param({"mode": "delta"}, id="unknown-mode"),
        pytest.param({"loro_peer": CRDT_SERVER_PEER_MAX}, id="hands-out-a-server-peer"),
        pytest.param({"vv_b64": "!"}, id="vector-not-base64"),
    ],
)
def test_a_malformed_sync_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CrdtSyncPayload.model_validate(_sync(**overrides))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(None, {"state": "ok", "reason": ""}, id="not-paused"),
        pytest.param("no_writer", {"state": "paused", "reason": "no_writer"}, id="paused"),
        pytest.param("", {"state": "paused", "reason": ""}, id="paused-with-no-reason"),
        pytest.param("x" * 80, {"state": "paused", "reason": "x" * 64}, id="reason-capped"),
    ],
)
def test_the_saving_state_is_the_rows_paused_reason(
    row: str | None, expected: dict[str, str]
) -> None:
    saving = CrdtSaving.of(row)
    assert {"state": saving.state, "reason": saving.reason} == expected


@pytest.mark.parametrize(
    "model",
    [pytest.param(CrdtSyncPayload, id="sync"), pytest.param(ReloadPayload, id="reload")],
)
def test_a_sync_or_reload_from_an_older_server_says_nothing_about_saving(model: type) -> None:
    raw = _sync() if model is CrdtSyncPayload else {"epoch": 2, "reason": "compacted"}
    assert model.model_validate(raw).saving is None


def test_a_sync_carries_the_saving_state_and_refuses_an_unknown_one() -> None:
    sync = CrdtSyncPayload.model_validate(_sync(saving={"state": "paused", "reason": "gone"}))
    assert sync.saving == CrdtSaving(state="paused", reason="gone")
    with pytest.raises(ValidationError):
        CrdtSyncPayload.model_validate(_sync(saving={"state": "stalled"}))


def test_a_chunked_sync_carries_its_header_on_the_piece() -> None:
    sync = CrdtSyncPayload.model_validate(_sync(data_b64=None, chunk=_chunk(), mode="snapshot"))
    assert sync.chunk is not None and sync.chunk.count == 3
    assert CrdtSyncPayload.model_validate(sync.model_dump(mode="json")) == sync


def test_a_stored_broadcast_may_carry_a_larger_delta_inline_than_a_frame() -> None:
    at_cap = CrdtUpdatePayload(update_id="u1", data_b64=_b64(CRDT_MAX_INLINE_BYTES))
    assert len(decode_b64(at_cap.data_b64 or "")) == CRDT_MAX_INLINE_BYTES
    with pytest.raises(ValidationError):
        CrdtUpdatePayload(update_id="u1", data_b64=_b64(CRDT_MAX_INLINE_BYTES + 1))


@pytest.mark.parametrize(
    "update_id",
    [
        pytest.param("", id="empty"),
        pytest.param("has space", id="space"),
        pytest.param("x" * 65, id="too-long"),
        pytest.param("u/1", id="slash"),
    ],
)
def test_an_update_id_is_a_short_token(update_id: str) -> None:
    with pytest.raises(ValidationError):
        CrdtUpdatePayload(update_id=update_id, data_b64="")


def test_a_by_reference_broadcast_carries_no_bytes_and_round_trips() -> None:
    ref = CrdtUpdatePayload.model_validate(
        {"t": "update", "update_id": "u1", "log_ref": 7, "loro_peer": PEER, "vv_b64": VV}
    )
    assert (ref.log_ref, ref.data_b64, ref.chunk) == (7, "", None)
    assert CrdtUpdatePayload.model_validate(ref.model_dump(mode="json")) == ref


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"data_b64": "AAAA"}, id="with-inline-bytes"),
        pytest.param({"chunk": _chunk()}, id="with-a-chunk"),
    ],
)
def test_a_by_reference_broadcast_with_bytes_is_refused(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="by-reference"):
        CrdtUpdatePayload.model_validate({"update_id": "u1", "log_ref": 7, **extra})


# ---------------------------------------------------------------------------
# ephemeral / ack / the crdt discriminator
# ---------------------------------------------------------------------------


def test_an_ephemeral_update_is_capped_to_fit_the_relay() -> None:
    assert CrdtEphemeralPayload(data_b64=_b64(CRDT_MAX_EPHEMERAL_BYTES)).t == "ephemeral"
    for bad in (_b64(CRDT_MAX_EPHEMERAL_BYTES + 1), ""):
        with pytest.raises(ValidationError):
            CrdtEphemeralPayload(data_b64=bad)


def test_an_ack_carries_the_servers_vector() -> None:
    ack = CrdtAckPayload(update_id="u1", changed=False, vv_b64=VV)
    assert CrdtAckPayload.model_validate(ack.model_dump(mode="json")) == ack
    with pytest.raises(ValidationError):
        CrdtAckPayload.model_validate({"update_id": "u1", "changed": True})


def test_the_crdt_payload_is_told_apart_by_its_tag() -> None:
    assert isinstance(
        parse_crdt_payload({"t": "update", "update_id": "u", "data_b64": ""}), CrdtUpdatePayload
    )
    assert isinstance(
        parse_crdt_payload({"t": "ephemeral", "data_b64": "AAAA"}), CrdtEphemeralPayload
    )
    future = parse_crdt_payload({"t": "awareness_v2", "blob": 1})
    assert isinstance(future, RawCrdtPayload)
    assert future.model_dump(mode="json")["blob"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"t": "update", "update_id": "u"}, id="known-tag-bad-body"),
        pytest.param({"update_id": "u", "data_b64": ""}, id="no-tag"),
        pytest.param({"t": "ephemeral", "data_b64": "!!!!"}, id="ephemeral-not-base64"),
    ],
)
def test_a_crdt_payload_that_is_not_one_raises(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_crdt_payload(payload)


def test_a_crdt_envelope_on_a_workspace_channel() -> None:
    envelope = DocEnvelope(
        doc_id="sess-1",
        doc_type="chat_draft",
        epoch=1,
        peer_id="p:1",
        seq=0,
        kind="crdt",
        payload=CrdtUpdatePayload(update_id="u1", data_b64="").model_dump(mode="json"),
    )
    assert envelope.channel == "doc:chat_draft:sess-1"
    assert isinstance(parse_crdt_payload(envelope.payload), CrdtUpdatePayload)


# ---------------------------------------------------------------------------
# Additive fields on older models
# ---------------------------------------------------------------------------


def test_an_error_names_the_update_it_refuses_and_when_to_retry() -> None:
    error = ErrorPayload(code="crdt_busy", update_id="u1", reason="queue_full", retry_after_ms=250)
    assert ErrorPayload.model_validate(error.model_dump(mode="json")) == error
    older = ErrorPayload.model_validate({"schema_version": "1.1.0", "code": "forbidden"})
    assert (older.update_id, older.reason, older.retry_after_ms) == (None, None, None)
    for bad in (-1, 600_001):
        with pytest.raises(ValidationError):
            ErrorPayload(code="crdt_busy", retry_after_ms=bad)


def test_the_welcome_tells_a_socket_its_budget() -> None:
    limits = SocketLimits(
        frames_per_window=200, bytes_per_window=1024, window_seconds=10.0, max_frame_bytes=64
    )
    welcome = WelcomeFrame.model_validate(
        {
            "peer_id": "p:1",
            "server_time": "2026-10-01T00:00:00Z",
            "instance": "i1",
            "limits": limits.model_dump(mode="json"),
        }
    )
    assert welcome.limits == limits
    older = WelcomeFrame.model_validate(
        {"peer_id": "p:1", "server_time": "2026-10-01T00:00:00Z", "instance": "i1"}
    )
    assert older.limits is None
    budget_only = SocketLimits.model_validate(
        {
            "schema_version": "1.0.0",
            "frames_per_window": 200,
            "bytes_per_window": 1024,
            "window_seconds": 10.0,
            "max_frame_bytes": 64,
        }
    )
    assert (budget_only.ephemeral_max_bytes, budget_only.doc_max_bytes) == (None, None)
    for bad in ({"ephemeral_max_bytes": 0}, {"doc_max_bytes": 0}):
        with pytest.raises(ValidationError):
            SocketLimits.model_validate({**limits.model_dump(mode="json"), **bad})
    with pytest.raises(ValidationError):
        SocketLimits(frames_per_window=0, bytes_per_window=1, window_seconds=1, max_frame_bytes=1)
    with pytest.raises(ValidationError):
        SocketLimits(frames_per_window=1, bytes_per_window=1, window_seconds=0, max_frame_bytes=1)


def test_a_departure_names_a_minted_peer_and_parses_as_its_own_kind() -> None:
    gone = parse_crdt_payload({"t": "gone", "loro_peer": CRDT_SERVER_PEER_MAX + 1})
    assert isinstance(gone, CrdtGonePayload)
    for reserved in (0, CRDT_SERVER_PEER_MAX):
        with pytest.raises(ValidationError):
            parse_crdt_payload({"t": "gone", "loro_peer": reserved})


@pytest.mark.parametrize(
    ("raw", "valid"),
    [
        pytest.param({"t": "saving", "state": "paused", "reason": "leased"}, True, id="paused"),
        pytest.param({"t": "saving", "state": "ok"}, True, id="ok-needs-no-reason"),
        pytest.param({"t": "saving", "state": "stuck"}, False, id="unknown-state"),
        pytest.param({"t": "saving"}, False, id="no-state"),
        pytest.param(
            {"t": "saving", "state": "paused", "reason": "x" * 65}, False, id="long-reason"
        ),
    ],
)
def test_a_saving_notice_parses_as_its_own_kind(raw: dict[str, object], valid: bool) -> None:
    if not valid:
        with pytest.raises(ValidationError):
            parse_crdt_payload(raw)
        return
    notice = parse_crdt_payload(raw)
    assert isinstance(notice, CrdtSavingPayload)
    assert notice.state == raw["state"]
