"""One pass over a stream must produce the digests a second, independent pass would.

Every expected value here is computed with `hashlib` / `blake3` directly over the whole
byte string — never with `StreamHasher` — so the assertions cannot agree with the code
by construction. The block boundary is the interesting place: a stream is chunked by
whatever the network hands the upload path, and the digests may not depend on that.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence

import pytest
from alkera_core.files.hashing import (
    Digests,
    StreamHasher,
    hash_bytes,
    hash_stream,
    verify_block,
)
from blake3 import blake3
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

MIB4 = 4 * 1024 * 1024


def payload(size: int) -> bytes:
    """Deterministic, non-repeating bytes: a repeat would hide a block ordering bug."""
    out = bytearray()
    seed = 0x9E3779B9
    while len(out) < size:
        seed = (seed * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        out += seed.to_bytes(8, "big")
    return bytes(out[:size])


def expected_blocks(data: bytes) -> list[bytes]:
    """A second implementation: slice the whole buffer, hash each slice on its own."""
    return [hashlib.sha256(data[i : i + MIB4]).digest() for i in range(0, len(data), MIB4)]


SIZES = [
    pytest.param(0, id="empty"),
    pytest.param(1, id="one-byte"),
    pytest.param(MIB4 - 1, id="one-below-a-block"),
    pytest.param(MIB4, id="exactly-one-block"),
    pytest.param(MIB4 + 1, id="one-above-a-block"),
    pytest.param(3 * MIB4 + 7, id="three-blocks-plus-seven"),
]


@pytest.mark.parametrize("size", SIZES)
def test_digests_match_an_independent_computation(size: int) -> None:
    data = payload(size)

    digests = hash_bytes(data)

    blocks = expected_blocks(data)
    assert digests.size == size
    assert digests.content_hash == blake3(data).digest()
    assert digests.block_hashes == tuple(blocks)
    assert digests.block_hash == hashlib.sha256(b"".join(blocks)).digest()


def test_an_empty_stream_has_no_blocks_and_hashes_the_empty_string() -> None:
    digests = hash_bytes(b"")

    assert digests.block_hashes == ()
    assert digests.block_hash == hashlib.sha256(b"").digest()


@pytest.mark.parametrize("size", SIZES)
def test_block_count_follows_the_boundary(size: int) -> None:
    digests = hash_bytes(payload(size))

    assert len(digests.block_hashes) == -(-size // MIB4)


def feed(data: bytes, chunk_sizes: Sequence[int]) -> Digests:
    hasher = StreamHasher()
    offset = 0
    for chunk in chunk_sizes:
        hasher.update(data[offset : offset + chunk])
        offset += chunk
    hasher.update(data[offset:])
    return hasher.finalize()


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "chunk",
    [pytest.param(1, id="byte-at-a-time"), pytest.param(1000, id="thousand-byte-chunks")],
)
def test_chunking_does_not_change_the_digests(size: int, chunk: int) -> None:
    data = payload(size)

    whole = hash_bytes(data)
    chunked = feed(data, [chunk] * (len(data) // chunk + 1))

    assert chunked == whole


def test_empty_chunks_are_ignored() -> None:
    data = payload(1024)
    hasher = StreamHasher()
    for byte in data:
        hasher.update(b"")
        hasher.update(bytes([byte]))
    hasher.update(b"")

    assert hasher.finalize() == hash_bytes(data)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    data=st.binary(min_size=0, max_size=4096),
    splits=st.lists(st.integers(min_value=0, max_value=4096), max_size=12),
)
def test_any_split_of_the_same_bytes_hashes_the_same(data: bytes, splits: list[int]) -> None:
    assert feed(data, splits) == hash_bytes(data)


def test_finalize_is_repeatable_and_the_hasher_keeps_accepting_bytes() -> None:
    hasher = StreamHasher()
    hasher.update(b"alkera")

    first = hasher.finalize()
    assert hasher.finalize() == first

    hasher.update(b"-files")
    assert hasher.finalize() == hash_bytes(b"alkera-files")


@pytest.mark.parametrize("size", SIZES)
async def test_hash_stream_equals_hash_bytes(size: int) -> None:
    data = payload(size)

    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(data), 7919):
            yield data[offset : offset + 7919]

    assert await hash_stream(chunks()) == hash_bytes(data)


def test_verify_block_accepts_the_block_it_was_promised() -> None:
    data = payload(MIB4 + 32)
    digests = hash_bytes(data)

    assert verify_block(0, data[:MIB4], digests.block_hashes[0]) is True
    assert verify_block(1, data[MIB4:], digests.block_hashes[1]) is True


def test_verify_block_rejects_a_single_flipped_byte() -> None:
    data = payload(MIB4 + 32)
    digests = hash_bytes(data)
    corrupt = bytearray(data[:MIB4])
    corrupt[123456] ^= 0x01

    assert verify_block(0, bytes(corrupt), digests.block_hashes[0]) is False


def test_verify_block_rejects_a_truncated_block() -> None:
    data = payload(MIB4)
    digests = hash_bytes(data)

    assert verify_block(0, data[:-1], digests.block_hashes[0]) is False


def test_verify_block_rejects_the_wrong_blocks_bytes() -> None:
    data = payload(2 * MIB4)
    digests = hash_bytes(data)

    assert verify_block(1, data[:MIB4], digests.block_hashes[1]) is False


def test_a_negative_block_index_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        verify_block(-1, b"x", hashlib.sha256(b"x").digest())
