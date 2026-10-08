"""The frames between the backend and a Loro sandbox worker.

A frame is a JSON header and zero or more binary blobs, so Loro bytes never
pass through base64 or JSON on the way in or out::

    u32 header_length | header (UTF-8 JSON object) | u32 blob_count
    | (u32 blob_length | blob) * blob_count

All integers are big-endian. Both sides refuse a frame over its caps before
reading the body, so a confused or hostile peer cannot make the other buffer
an unbounded amount. This module imports nothing from Loro and nothing from the
backend: the worker and the pool both use it, and it is tested on its own.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field
from typing import Any, Final

#: The largest JSON header either side accepts.
MAX_HEADER_BYTES: Final = 256 * 1024
#: The most blobs one frame carries (a snapshot plus a log of updates).
MAX_BLOBS: Final = 4096
#: The most blob bytes one frame carries in total.
MAX_BLOB_BYTES: Final = 64 * 1024 * 1024
#: The most Loro peers one write may name as its writer's.
MAX_PEERS: Final = 4096

_U32 = struct.Struct(">I")


class FrameError(ValueError):
    """A frame that breaks the format or its caps."""


@dataclass(frozen=True, slots=True)
class Frame:
    header: dict[str, Any]
    blobs: tuple[bytes, ...] = field(default_factory=tuple)


def encode_frame(frame: Frame) -> bytes:
    header = json.dumps(frame.header, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(header) > MAX_HEADER_BYTES:
        raise FrameError(f"header is {len(header)} bytes; the cap is {MAX_HEADER_BYTES}")
    if len(frame.blobs) > MAX_BLOBS:
        raise FrameError(f"{len(frame.blobs)} blobs; the cap is {MAX_BLOBS}")
    total = sum(len(blob) for blob in frame.blobs)
    if total > MAX_BLOB_BYTES:
        raise FrameError(f"blobs add up to {total} bytes; the cap is {MAX_BLOB_BYTES}")
    parts = [_U32.pack(len(header)), header, _U32.pack(len(frame.blobs))]
    for blob in frame.blobs:
        parts.append(_U32.pack(len(blob)))
        parts.append(bytes(blob))
    return b"".join(parts)


def _header(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("header is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise FrameError("header is not a JSON object")
    return value


def decode_frame(data: bytes) -> Frame:
    """The one frame ``data`` holds exactly; raises :class:`FrameError` on
    anything else, trailing bytes included."""
    view = memoryview(data)
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(view):
            raise FrameError("frame is truncated")
        chunk = bytes(view[pos : pos + n])
        pos += n
        return chunk

    (header_length,) = _U32.unpack(take(4))
    if header_length > MAX_HEADER_BYTES:
        raise FrameError(f"header is {header_length} bytes; the cap is {MAX_HEADER_BYTES}")
    header = _header(take(header_length))
    (count,) = _U32.unpack(take(4))
    if count > MAX_BLOBS:
        raise FrameError(f"{count} blobs; the cap is {MAX_BLOBS}")
    blobs: list[bytes] = []
    total = 0
    for _ in range(count):
        (length,) = _U32.unpack(take(4))
        total += length
        if total > MAX_BLOB_BYTES:
            raise FrameError(f"blobs add up to more than {MAX_BLOB_BYTES} bytes")
        blobs.append(take(length))
    if pos != len(view):
        raise FrameError("trailing bytes after the frame")
    return Frame(header=header, blobs=tuple(blobs))


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """The next frame on ``reader``. Raises ``asyncio.IncompleteReadError`` when
    the stream ends (cleanly between frames, or mid-frame) and
    :class:`FrameError` on a frame that breaks the format."""
    (header_length,) = _U32.unpack(await reader.readexactly(4))
    if header_length > MAX_HEADER_BYTES:
        raise FrameError(f"header is {header_length} bytes; the cap is {MAX_HEADER_BYTES}")
    header = _header(await reader.readexactly(header_length))
    (count,) = _U32.unpack(await reader.readexactly(4))
    if count > MAX_BLOBS:
        raise FrameError(f"{count} blobs; the cap is {MAX_BLOBS}")
    blobs: list[bytes] = []
    total = 0
    for _ in range(count):
        (length,) = _U32.unpack(await reader.readexactly(4))
        total += length
        if total > MAX_BLOB_BYTES:
            raise FrameError(f"blobs add up to more than {MAX_BLOB_BYTES} bytes")
        blobs.append(await reader.readexactly(length))
    return Frame(header=header, blobs=tuple(blobs))


def encode_projection(value: dict[str, Any]) -> bytes:
    """A projection as the blob it travels in: a projection never rides the
    header, whose cap is far below a large document's size."""
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_projection(blob: bytes) -> dict[str, Any]:
    """A projection from its blob; ``{}`` for anything that is not a JSON
    object."""
    try:
        value = json.loads(blob.decode("utf-8")) if blob else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "MAX_BLOBS",
    "MAX_BLOB_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_PEERS",
    "Frame",
    "FrameError",
    "decode_frame",
    "decode_projection",
    "encode_frame",
    "encode_projection",
    "read_frame",
]
