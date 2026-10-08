"""One streaming pass over a file's bytes producing every digest Files needs.

A version's bytes are hashed twice by contract — BLAKE3 over the whole stream is the
content address used for dedup, and a SHA-256 per 4 MiB block lets a reader verify what
it has already received before it hands those bytes on. Both come out of a single pass
here so the upload path never re-reads the stream or buffers the whole object.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from hmac import compare_digest

from blake3 import blake3

BLOCK_BYTES = 4 * 1024 * 1024

__all__ = [
    "BLOCK_BYTES",
    "Digests",
    "StreamHasher",
    "hash_bytes",
    "hash_stream",
    "verify_block",
]


@dataclass(frozen=True, slots=True)
class Digests:
    """Everything one pass over a stream yields.

    ``content_hash`` is BLAKE3 over the whole stream; ``block_hashes`` holds one SHA-256
    per 4 MiB block (the last one short); ``block_hash`` is SHA-256 over those digests
    concatenated, so an empty stream — which has no blocks — hashes the empty string.
    """

    content_hash: bytes
    block_hash: bytes
    size: int
    block_hashes: tuple[bytes, ...]


class StreamHasher:
    """Feeds arbitrary chunks; the result depends only on the bytes, not the chunking."""

    __slots__ = ("_block", "_blocks", "_fill", "_size", "_whole")

    def __init__(self) -> None:
        self._whole = blake3()
        self._block = hashlib.sha256()
        self._fill = 0
        self._blocks: list[bytes] = []
        self._size = 0

    def update(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._whole.update(chunk)
        self._size += len(chunk)
        view = memoryview(chunk)
        offset = 0
        length = len(view)
        while offset < length:
            take = min(BLOCK_BYTES - self._fill, length - offset)
            self._block.update(view[offset : offset + take])
            self._fill += take
            offset += take
            if self._fill == BLOCK_BYTES:
                self._blocks.append(self._block.digest())
                self._block = hashlib.sha256()
                self._fill = 0

    def finalize(self) -> Digests:
        """Snapshot the digests; non-destructive, so more chunks may still follow."""
        blocks = list(self._blocks)
        if self._fill:
            blocks.append(self._block.copy().digest())
        return Digests(
            content_hash=self._whole.digest(),
            block_hash=hashlib.sha256(b"".join(blocks)).digest(),
            size=self._size,
            block_hashes=tuple(blocks),
        )


def hash_bytes(data: bytes) -> Digests:
    hasher = StreamHasher()
    hasher.update(data)
    return hasher.finalize()


async def hash_stream(data: AsyncIterator[bytes]) -> Digests:
    hasher = StreamHasher()
    async for chunk in data:
        hasher.update(chunk)
    return hasher.finalize()


def verify_block(index: int, block_bytes: bytes, expected: bytes) -> bool:
    """True when ``block_bytes`` is the block a reader was promised at ``index``.

    A reader checks each 4 MiB block before yielding it, so a store that flips or drops
    bytes is caught before the corrupt block reaches the caller.
    """
    if index < 0:
        raise ValueError("block index must not be negative")
    return compare_digest(hashlib.sha256(block_bytes).digest(), expected)
