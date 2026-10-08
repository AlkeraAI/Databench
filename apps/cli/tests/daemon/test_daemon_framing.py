"""Wire-format tests for LSP-style Content-Length framing.

These cover the byte-exact round trip + the error edges the production
daemon must handle without crashing.
"""

from __future__ import annotations

import io

import pytest
from alkera_cli.daemon.framing import (
    FramingError,
    read_frame,
    read_frame_async,
    write_frame,
    write_frame_async,
)
from alkera_cli.daemon.pipes import connect_pipe_reader, connect_pipe_writer

# ---------------- sync round-trip ----------------


def test_round_trip_ascii_body():
    buf = io.BytesIO()
    write_frame(buf, b'{"hello":"world"}')
    buf.seek(0)
    assert read_frame(buf) == b'{"hello":"world"}'


def test_round_trip_unicode_body():
    body = '{"emoji":"🎯","greeting":"héllo"}'.encode()
    buf = io.BytesIO()
    write_frame(buf, body)
    buf.seek(0)
    assert read_frame(buf) == body


def test_round_trip_empty_body():
    buf = io.BytesIO()
    write_frame(buf, b"")
    buf.seek(0)
    assert read_frame(buf) == b""


def test_writer_includes_required_headers():
    """The producer must emit `Content-Length` so any LSP-compatible peer
    can parse it. (No assertion on Content-Type ordering — readers ignore it.)"""
    buf = io.BytesIO()
    write_frame(buf, b'{"a":1}')
    raw = buf.getvalue()
    assert raw.startswith(b"Content-Length: 7\r\n")
    assert b"\r\n\r\n" in raw  # header/body separator


# ---------------- consecutive frames on one stream ----------------


def test_read_two_frames_from_same_stream():
    buf = io.BytesIO()
    write_frame(buf, b'{"id":1}')
    write_frame(buf, b'{"id":2}')
    buf.seek(0)
    assert read_frame(buf) == b'{"id":1}'
    assert read_frame(buf) == b'{"id":2}'


# ---------------- error cases ----------------


def test_missing_content_length_raises():
    buf = io.BytesIO(b"Content-Type: text/plain\r\n\r\nbody")
    with pytest.raises(FramingError, match="missing Content-Length"):
        read_frame(buf)


def test_invalid_content_length_raises():
    buf = io.BytesIO(b"Content-Length: not-a-number\r\n\r\nbody")
    with pytest.raises(FramingError, match="invalid Content-Length"):
        read_frame(buf)


def test_negative_content_length_raises():
    buf = io.BytesIO(b"Content-Length: -5\r\n\r\n")
    with pytest.raises(FramingError, match="negative Content-Length"):
        read_frame(buf)


def test_malformed_header_raises():
    buf = io.BytesIO(b"this-has-no-colon\r\n\r\n")
    with pytest.raises(FramingError, match="malformed header"):
        read_frame(buf)


def test_eof_before_any_frame_raises_eoferror():
    buf = io.BytesIO(b"")
    with pytest.raises(EOFError, match="closed before any frame"):
        read_frame(buf)


def test_eof_mid_header_raises_framing_error():
    buf = io.BytesIO(b"Content-Length: 10\r\n")  # no \r\n\r\n separator
    with pytest.raises(FramingError, match="closed mid-header"):
        read_frame(buf)


def test_eof_mid_body_raises_eoferror():
    buf = io.BytesIO(b"Content-Length: 100\r\n\r\nshort")
    with pytest.raises(EOFError, match="closed mid-body"):
        read_frame(buf)


def test_bare_lf_in_headers_is_tolerated():
    """Some test fixtures (and `printf` ad-hoc invocations) emit bare LF.
    Production clients are strict CRLF; we accept LF as a courtesy.
    """
    buf = io.BytesIO(b"Content-Length: 4\n\nbody")
    assert read_frame(buf) == b"body"


def test_extra_headers_are_ignored():
    raw = (
        b"Content-Length: 4\r\n"
        b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
        b"X-Custom: anything\r\n"
        b"\r\n"
        b"body"
    )
    assert read_frame(io.BytesIO(raw)) == b"body"


# ---------------- async path ----------------


@pytest.mark.asyncio
async def test_async_round_trip_via_pipe():
    """Drive ``read_frame_async`` / ``write_frame_async`` over a real pipe
    through the daemon's OWN pipe wiring (``daemon.pipes``) — the exact wire
    each platform uses in production. (Raw ``connect_read_pipe`` over an
    ``os.pipe()`` fd never delivers on the Windows ProactorEventLoop; this
    test hung CI for 24 minutes when wired that way.)"""
    import os

    r_fd, w_fd = os.pipe()
    reader = await connect_pipe_reader(os.fdopen(r_fd, "rb", buffering=0))
    writer = await connect_pipe_writer(os.fdopen(w_fd, "wb", buffering=0))

    try:
        await write_frame_async(writer, b'{"x":1}')
        await write_frame_async(writer, b'{"x":2}')
        assert await read_frame_async(reader) == b'{"x":1}'
        assert await read_frame_async(reader) == b'{"x":2}'
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_async_eof_raises_eoferror():
    import os

    r_fd, w_fd = os.pipe()
    reader = await connect_pipe_reader(os.fdopen(r_fd, "rb", buffering=0))
    os.close(w_fd)  # write end closes -> immediate EOF on read

    with pytest.raises(EOFError):
        await read_frame_async(reader)
