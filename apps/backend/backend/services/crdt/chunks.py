"""Splitting a Loro blob into frames, and putting one back together.

A blob larger than :data:`CRDT_CHUNK_BYTES` travels as ordered pieces that
share a transfer id and each carry the whole transfer's size and SHA-256. The
assembler is strict on purpose: pieces arrive in order or the transfer is
dropped, every piece must agree with the first about the transfer, the whole
must add up and hash right, a socket holds at most a few transfers at once,
and a transfer that stalls is dropped. Every refusal is a :class:`ChunkError`
the gateway answers in band; the client resends the update whole (updates are
idempotent), so nothing is lost by refusing.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from alkera_core.schemas.realtime import (
    CRDT_CHUNK_BYTES,
    CrdtChunk,
    decode_b64,
    encode_b64,
)

#: How many transfers one socket may have open at once.
MAX_OPEN_TRANSFERS: Final = 2
#: How long the next piece may take, and the whole transfer.
GAP_SECONDS: Final = 15.0
TOTAL_SECONDS: Final = 60.0


class ChunkError(ValueError):
    """``code`` is ``chunk_invalid``, ``chunk_timeout`` or ``chunk_limit``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def split(data: bytes, xfer_id: str) -> list[CrdtChunk]:
    """``data`` as ordered pieces. Only for a blob over one piece: a smaller
    one travels inline."""
    if len(data) <= CRDT_CHUNK_BYTES:
        raise ValueError("a blob that fits one frame is sent inline")
    digest = encode_b64(hashlib.sha256(data).digest())
    count = -(-len(data) // CRDT_CHUNK_BYTES)
    return [
        CrdtChunk(
            xfer_id=xfer_id,
            index=index,
            count=count,
            total_bytes=len(data),
            sha256_b64=digest,
            data_b64=encode_b64(data[index * CRDT_CHUNK_BYTES : (index + 1) * CRDT_CHUNK_BYTES]),
        )
        for index in range(count)
    ]


@dataclass(slots=True)
class _Transfer:
    count: int
    total_bytes: int
    sha256_b64: str
    started: float
    last: float
    parts: list[bytes] = field(default_factory=list)


@dataclass
class ChunkAssembler:
    """One socket's open transfers. ``max_total_bytes`` is the largest whole
    blob it accepts (the document type's update cap)."""

    max_total_bytes: int
    clock: Callable[[], float] = time.monotonic
    max_open: int = MAX_OPEN_TRANSFERS
    gap_seconds: float = GAP_SECONDS
    total_seconds: float = TOTAL_SECONDS
    _open: dict[str, _Transfer] = field(default_factory=dict)

    def _expire(self, now: float) -> list[str]:
        gone = [
            xfer
            for xfer, transfer in self._open.items()
            if now - transfer.last > self.gap_seconds or now - transfer.started > self.total_seconds
        ]
        for xfer in gone:
            del self._open[xfer]
        return gone

    def add(self, chunk: CrdtChunk) -> bytes | None:
        """The whole blob once its last piece arrives; ``None`` before."""
        now = self.clock()
        expired = self._expire(now)
        transfer = self._open.get(chunk.xfer_id)
        if transfer is None:
            if chunk.xfer_id in expired:
                raise ChunkError("chunk_timeout", "the transfer stalled and was dropped")
            if chunk.index != 0:
                raise ChunkError("chunk_invalid", "a transfer starts at its first piece")
            if chunk.total_bytes > self.max_total_bytes:
                raise ChunkError(
                    "chunk_limit", f"a transfer is at most {self.max_total_bytes} bytes"
                )
            if len(self._open) >= self.max_open:
                raise ChunkError("chunk_limit", f"at most {self.max_open} transfers at once")
            transfer = _Transfer(
                count=chunk.count,
                total_bytes=chunk.total_bytes,
                sha256_b64=chunk.sha256_b64,
                started=now,
                last=now,
            )
            self._open[chunk.xfer_id] = transfer
        elif (
            chunk.index != len(transfer.parts)
            or chunk.count != transfer.count
            or chunk.total_bytes != transfer.total_bytes
            or chunk.sha256_b64 != transfer.sha256_b64
        ):
            del self._open[chunk.xfer_id]
            raise ChunkError("chunk_invalid", "a piece disagrees with its transfer")
        piece = decode_b64(chunk.data_b64)
        last = chunk.index == chunk.count - 1
        if not last and len(piece) != CRDT_CHUNK_BYTES:
            del self._open[chunk.xfer_id]
            raise ChunkError("chunk_invalid", "every piece but the last is a whole piece")
        transfer.parts.append(piece)
        transfer.last = now
        if not last:
            return None
        del self._open[chunk.xfer_id]
        whole = b"".join(transfer.parts)
        if len(whole) != transfer.total_bytes:
            raise ChunkError("chunk_invalid", "the pieces do not add up to the transfer")
        if encode_b64(hashlib.sha256(whole).digest()) != transfer.sha256_b64:
            raise ChunkError("chunk_invalid", "the pieces do not hash to the transfer")
        return whole

    def drop(self, xfer_id: str) -> None:
        self._open.pop(xfer_id, None)

    def __len__(self) -> int:
        return len(self._open)


__all__ = [
    "GAP_SECONDS",
    "MAX_OPEN_TRANSFERS",
    "TOTAL_SECONDS",
    "ChunkAssembler",
    "ChunkError",
    "split",
]
