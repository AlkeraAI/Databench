"""Splitting a blob into frames and assembling it back, strictly."""

from __future__ import annotations

import hashlib
import secrets

import pytest
from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CRDT_MAX_TRANSFER_BYTES,
    CrdtChunk,
    encode_b64,
)
from backend.services.crdt.chunks import ChunkAssembler, ChunkError, split
from backend.services.crdt.registry import CrdtRegistry


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _blob(size: int) -> bytes:
    return secrets.token_bytes(size)


@pytest.mark.parametrize(
    "size",
    [CRDT_CHUNK_BYTES + 1, 2 * CRDT_CHUNK_BYTES, 2 * CRDT_CHUNK_BYTES + 7, 9 * CRDT_CHUNK_BYTES],
)
def test_a_split_blob_reassembles_exactly(size: int) -> None:
    data = _blob(size)
    pieces = split(data, "x1")
    assert [p.index for p in pieces] == list(range(len(pieces)))
    assembler = ChunkAssembler(max_total_bytes=size)
    out = [assembler.add(p) for p in pieces]
    assert out[:-1] == [None] * (len(pieces) - 1)
    assert out[-1] == data
    assert len(assembler) == 0


def test_a_sync_of_a_whole_file_past_four_mib_travels_as_one_transfer() -> None:
    """A sync carries a whole document in one transfer; a file's may hold
    8 MiB. Five MiB splits into pieces the wire schema admits, and they
    reassemble whole on the other end."""
    data = _blob(5 * 1024 * 1024)
    pieces = [CrdtChunk.model_validate(p.model_dump(mode="json")) for p in split(data, "sync-1")]
    assembler = ChunkAssembler(max_total_bytes=CRDT_MAX_TRANSFER_BYTES)
    out = [assembler.add(p) for p in pieces]
    assert out[-1] == data


def test_every_document_type_fits_one_transfer_with_room_to_spare() -> None:
    registry = CrdtRegistry()
    for name in registry.names():
        doc_type = registry.get(name)
        assert doc_type is not None
        assert 2 * doc_type.limits.max_doc_bytes <= CRDT_MAX_TRANSFER_BYTES, name


def test_a_blob_that_fits_one_frame_is_not_split() -> None:
    with pytest.raises(ValueError, match="inline"):
        split(_blob(CRDT_CHUNK_BYTES), "x")


def _assembled(pieces: list[CrdtChunk], assembler: ChunkAssembler) -> bytes | None:
    result = None
    for piece in pieces:
        result = assembler.add(piece)
    return result


@pytest.mark.parametrize(
    "case",
    [
        "starts-mid-transfer",
        "skips-a-piece",
        "repeats-a-piece",
        "count-changes",
        "digest-changes",
        "short-middle-piece",
    ],
)
def test_pieces_out_of_order_or_disagreeing_are_refused(case: str) -> None:
    data = _blob(3 * CRDT_CHUNK_BYTES)
    a, b, c = split(data, "x1")
    assembler = ChunkAssembler(max_total_bytes=len(data))
    if case == "starts-mid-transfer":
        sequence = [b]
    elif case == "skips-a-piece":
        sequence = [a, c]
    elif case == "repeats-a-piece":
        sequence = [a, a]
    elif case == "count-changes":
        sequence = [a, b.model_copy(update={"count": 4, "total_bytes": 4 * CRDT_CHUNK_BYTES})]
    elif case == "digest-changes":
        sequence = [
            a,
            b.model_copy(update={"sha256_b64": encode_b64(hashlib.sha256(b"x").digest())}),
        ]
    else:
        sequence = [a, b.model_copy(update={"data_b64": encode_b64(b"short")})]
    with pytest.raises(ChunkError) as excinfo:
        _assembled(sequence, assembler)
    assert excinfo.value.code == "chunk_invalid"
    assert len(assembler) == 0, "a refused transfer is dropped"


def test_pieces_that_hash_wrong_are_refused() -> None:
    data = _blob(2 * CRDT_CHUNK_BYTES + 1)
    forged = [
        p.model_copy(update={"sha256_b64": encode_b64(hashlib.sha256(b"other").digest())})
        for p in split(data, "x1")
    ]
    with pytest.raises(ChunkError, match="hash"):
        _assembled(forged, ChunkAssembler(max_total_bytes=len(data)))


def test_a_transfer_over_the_cap_is_refused_at_its_first_piece() -> None:
    data = _blob(3 * CRDT_CHUNK_BYTES)
    with pytest.raises(ChunkError) as excinfo:
        ChunkAssembler(max_total_bytes=len(data) - 1).add(split(data, "x1")[0])
    assert excinfo.value.code == "chunk_limit"


def test_only_a_few_transfers_are_open_at_once_and_a_finished_one_frees_its_slot() -> None:
    assembler = ChunkAssembler(max_total_bytes=10 * CRDT_CHUNK_BYTES, max_open=2)
    blobs = {name: _blob(2 * CRDT_CHUNK_BYTES + 1) for name in ("a", "b", "c")}
    pieces = {name: split(data, name) for name, data in blobs.items()}
    assembler.add(pieces["a"][0])
    assembler.add(pieces["b"][0])
    with pytest.raises(ChunkError) as excinfo:
        assembler.add(pieces["c"][0])
    assert excinfo.value.code == "chunk_limit"
    assert _assembled(pieces["a"][1:], assembler) == blobs["a"]
    assert _assembled(pieces["c"], assembler) == blobs["c"]


@pytest.mark.parametrize(
    ("steps", "code"),
    [
        pytest.param([0.0, 15.1], "chunk_timeout", id="gap"),
        pytest.param([0.0, 14.0, 14.0, 14.0, 14.0, 14.0], "chunk_timeout", id="total"),
    ],
)
def test_a_stalled_transfer_is_dropped(steps: list[float], code: str) -> None:
    clock = _Clock()
    data = _blob(6 * CRDT_CHUNK_BYTES)
    pieces = split(data, "x1")
    assembler = ChunkAssembler(max_total_bytes=len(data), clock=clock)
    with pytest.raises(ChunkError) as excinfo:
        for piece, wait in zip(pieces, steps, strict=False):
            clock.now += wait
            assembler.add(piece)
    assert excinfo.value.code == code
    assert len(assembler) == 0


def test_a_transfer_just_inside_its_gap_completes() -> None:
    clock = _Clock()
    data = _blob(2 * CRDT_CHUNK_BYTES + 1)
    pieces = split(data, "x1")
    assembler = ChunkAssembler(max_total_bytes=len(data), clock=clock)
    assert assembler.add(pieces[0]) is None
    clock.now += 15.0
    assert assembler.add(pieces[1]) is None
    clock.now += 15.0
    assert assembler.add(pieces[2]) == data
