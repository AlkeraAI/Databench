"""Cross-platform pipe plumbing (``daemon.pipes``).

The Windows ProactorEventLoop has no transport for ``os.pipe()`` / console
handles, so ``daemon.pipes`` thread-bridges them there. The bridge classes
themselves are platform-neutral (blocking file + executor + feed_data), so
these tests drive the WINDOWS wiring directly on POSIX CI too — the platform
gate only picks which flavor production gets.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from alkera_cli.daemon.framing import read_frame_async, write_frame_async
from alkera_cli.daemon.pipes import (
    _feed_reader_from_thread,
    _ThreadPipeWriter,
    connect_pipe_reader,
    connect_pipe_writer,
)


@pytest.mark.asyncio
async def test_thread_bridge_round_trip():
    """The Windows-flavor wiring (thread reader + executor writer), driven
    explicitly: frames written through the bridge arrive intact."""
    r_fd, w_fd = os.pipe()
    reader = asyncio.StreamReader()
    _feed_reader_from_thread(os.fdopen(r_fd, "rb", buffering=0), reader)
    writer = _ThreadPipeWriter(os.fdopen(w_fd, "wb", buffering=0))

    try:
        await write_frame_async(writer, b'{"x":1}')
        await write_frame_async(writer, b'{"x":2}')
        assert await asyncio.wait_for(read_frame_async(reader), timeout=10) == b'{"x":1}'
        assert await asyncio.wait_for(read_frame_async(reader), timeout=10) == b'{"x":2}'
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_thread_bridge_eof():
    """Closing the write end surfaces as EOF through the bridge reader."""
    r_fd, w_fd = os.pipe()
    reader = asyncio.StreamReader()
    _feed_reader_from_thread(os.fdopen(r_fd, "rb", buffering=0), reader)
    os.close(w_fd)

    with pytest.raises(EOFError):
        await asyncio.wait_for(read_frame_async(reader), timeout=10)


@pytest.mark.asyncio
async def test_connect_helpers_pick_bridge_on_windows(monkeypatch):
    """With the platform forced to win32, the connect helpers return the
    thread-bridge flavors and a full frame round trip still works — pinning
    on POSIX CI the exact objects production uses on Windows."""
    monkeypatch.setattr(sys, "platform", "win32")

    r_fd, w_fd = os.pipe()
    reader = await connect_pipe_reader(os.fdopen(r_fd, "rb", buffering=0))
    writer = await connect_pipe_writer(os.fdopen(w_fd, "wb", buffering=0))
    assert isinstance(writer, _ThreadPipeWriter)

    try:
        await write_frame_async(writer, b'{"win":true}')
        assert await asyncio.wait_for(read_frame_async(reader), timeout=10) == b'{"win":true}'
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_connect_helpers_use_transports_on_posix():
    """On POSIX the helpers return a real StreamWriter over a pipe transport
    (the existing production wiring, unchanged)."""
    if sys.platform == "win32":
        pytest.skip("POSIX transport flavor")
    r_fd, w_fd = os.pipe()
    reader = await connect_pipe_reader(os.fdopen(r_fd, "rb", buffering=0))
    writer = await connect_pipe_writer(os.fdopen(w_fd, "wb", buffering=0))
    assert isinstance(writer, asyncio.StreamWriter)

    try:
        await write_frame_async(writer, b'{"posix":true}')
        assert await asyncio.wait_for(read_frame_async(reader), timeout=10) == b'{"posix":true}'
    finally:
        writer.close()
