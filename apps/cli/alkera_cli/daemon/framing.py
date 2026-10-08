"""LSP-style Content-Length framing for JSON-RPC over stdio.

The wire format is:

    Content-Length: <N>\\r\\n
    Content-Type: application/vscode-jsonrpc; charset=utf-8\\r\\n   (optional)
    \\r\\n
    <N bytes of JSON, UTF-8>

This is the framing the Language Server Protocol uses. By matching it, our
daemon talks the same wire as every LSP client library (`vscode-jsonrpc`,
`LSP4J jsonrpc`) without any custom code on the editor side.

Both sync (``read_frame``/``write_frame`` over ``BinaryIO``) and async
(``read_frame_async``/``write_frame_async`` over ``asyncio.StreamReader``/
``StreamWriter``) variants are provided. The async path is what the daemon
loop uses; the sync path is what unit tests exercise.

Robustness rules:
- Header lines are CRLF-terminated. We accept a bare LF as a courtesy
  because some test fixtures write it that way; production clients are
  strict CRLF.
- ``Content-Length`` is the only required header. Other headers
  (``Content-Type``, etc.) are read and ignored.
- A missing ``Content-Length`` raises ``FramingError`` — the caller should
  log + drop the frame and continue reading from the stream.
- EOF before completing a header block raises ``EOFError`` so the caller
  can distinguish "peer closed cleanly" from "wire was malformed".
"""

from __future__ import annotations

import asyncio
from typing import BinaryIO

from alkera_cli.daemon.pipes import PipeWriter


class FramingError(ValueError):
    """The framing layer saw something it can't make sense of.

    Raised on missing/invalid `Content-Length`, or on a header line that
    isn't `Key: Value`. The caller decides whether to log + skip or treat
    the connection as broken.
    """


_DEFAULT_HEADERS = b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"


def _parse_header_line(line: bytes) -> tuple[str, str]:
    """Split ``Key: Value`` (case-insensitive on key)."""
    key, _, value = line.decode("ascii").partition(":")
    if not _:
        raise FramingError(f"malformed header line: {line!r}")
    return key.strip().lower(), value.strip()


def _parse_content_length(headers: dict[str, str]) -> int:
    raw = headers.get("content-length")
    if raw is None:
        raise FramingError("missing Content-Length header")
    try:
        n = int(raw)
    except ValueError as exc:
        raise FramingError(f"invalid Content-Length: {raw!r}") from exc
    if n < 0:
        raise FramingError(f"negative Content-Length: {n}")
    return n


def _format_frame(body: bytes) -> bytes:
    return f"Content-Length: {len(body)}\r\n".encode("ascii") + _DEFAULT_HEADERS + b"\r\n" + body


# ---------------- sync API (tests use this) ----------------


def read_frame(stream: BinaryIO) -> bytes:
    """Read one framed message from ``stream``. Returns the JSON body
    (still bytes; the caller decodes UTF-8).

    Raises ``EOFError`` if the stream closes before a complete frame.
    Raises ``FramingError`` on malformed headers.
    """
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            if headers:
                raise FramingError(f"stream closed mid-header block: {headers!r}")
            raise EOFError("stream closed before any frame")
        # Strip trailing newline (CRLF or bare LF — see docstring).
        line = line.rstrip(b"\r\n")
        if line == b"":
            break
        key, value = _parse_header_line(line)
        headers[key] = value

    length = _parse_content_length(headers)
    body = stream.read(length)
    if len(body) != length:
        raise EOFError(f"stream closed mid-body: wanted {length} bytes, got {len(body)}")
    return body


def write_frame(stream: BinaryIO, body: bytes) -> None:
    """Write ``body`` as a framed message to ``stream`` and flush."""
    stream.write(_format_frame(body))
    stream.flush()


# ---------------- async API (server uses this) ----------------


async def read_frame_async(reader: asyncio.StreamReader) -> bytes:
    """Async counterpart to ``read_frame``. Same error semantics."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            if headers:
                raise FramingError(f"stream closed mid-header block: {headers!r}")
            raise EOFError("stream closed before any frame")
        line = line.rstrip(b"\r\n")
        if line == b"":
            break
        key, value = _parse_header_line(line)
        headers[key] = value

    length = _parse_content_length(headers)
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise EOFError(
            f"stream closed mid-body: wanted {length} bytes, got {len(exc.partial)}"
        ) from exc
    return body


async def write_frame_async(writer: PipeWriter, body: bytes) -> None:
    """Async counterpart to ``write_frame``. ``drain``s after writing.

    ``writer`` is anything with the ``PipeWriter`` surface — a real
    ``asyncio.StreamWriter`` on POSIX, the thread-bridged writer on Windows
    (see ``daemon.pipes``)."""
    writer.write(_format_frame(body))
    await writer.drain()


__all__ = [
    "FramingError",
    "read_frame",
    "read_frame_async",
    "write_frame",
    "write_frame_async",
]
