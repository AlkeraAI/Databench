"""Cross-platform asyncio plumbing for byte pipes (the daemon's stdio wire).

POSIX wires a pipe fd into the event loop with ``connect_read_pipe`` /
``connect_write_pipe``. The Windows ProactorEventLoop cannot: its pipe
transports require overlapped (named-pipe) handles, while ``os.pipe()`` and
console/redirected stdio are plain anonymous handles — ``connect_read_pipe``
either fails outright or, worse, never delivers a byte, so a frame read parks
in the proactor forever. On Windows we bridge with daemon threads instead: a
reader thread feeds an ``asyncio.StreamReader`` via ``call_soon_threadsafe``,
and writes run the blocking ``write+flush`` in the default executor.

Both ``alkera serve`` (real stdio) and the daemon tests (``os.pipe()`` pairs)
go through these helpers, so each platform's CI exercises the exact transport
production uses there.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from typing import BinaryIO

_READ_CHUNK = 64 * 1024


class PipeWriter(Protocol):
    """The writer surface the daemon's framing layer needs.

    ``asyncio.StreamWriter`` satisfies it structurally (the POSIX path);
    ``_ThreadPipeWriter`` is the Windows thread bridge."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class _ThreadPipeWriter:
    """``write()``/``drain()`` over a blocking binary file.

    ``write`` buffers; ``drain`` hands the buffer to the default executor for
    the blocking ``write+flush`` so the event loop never blocks on a pipe with
    a slow/full reader. An internal lock keeps concurrent ``drain``s ordered.
    """

    def __init__(self, file: BinaryIO) -> None:
        self._file = file
        self._buffer = bytearray()
        self._drain_lock = asyncio.Lock()

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)

    async def drain(self) -> None:
        async with self._drain_lock:
            if not self._buffer:
                return
            data = bytes(self._buffer)
            self._buffer.clear()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._blocking_write, data)

    def _blocking_write(self, data: bytes) -> None:
        self._file.write(data)
        self._file.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._file.close()


def _feed_reader_from_thread(file: BinaryIO, reader: asyncio.StreamReader) -> None:
    """Pump blocking reads into ``reader`` from a daemon thread.

    The thread may stay parked in ``read()`` at shutdown — it's a daemon
    thread, abandoned at process exit (same tradeoff as the login paste
    reader's helper thread)."""
    loop = asyncio.get_running_loop()

    def _pump() -> None:
        try:
            while True:
                chunk = file.read(_READ_CHUNK)
                if not chunk:
                    break
                loop.call_soon_threadsafe(reader.feed_data, chunk)
        except (OSError, ValueError):
            pass  # pipe closed under us — treat as EOF
        finally:
            with contextlib.suppress(RuntimeError):  # loop already closed
                loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=_pump, daemon=True, name="alkera-pipe-reader").start()


async def connect_pipe_reader(file: BinaryIO) -> asyncio.StreamReader:
    """An ``asyncio.StreamReader`` over a binary readable (pipe / stdio)."""
    reader = asyncio.StreamReader()
    if sys.platform == "win32":
        _feed_reader_from_thread(file, reader)
        return reader
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), file)
    return reader


async def connect_pipe_writer(file: BinaryIO) -> PipeWriter:
    """A ``PipeWriter`` over a binary writable (pipe / stdio)."""
    if sys.platform == "win32":
        return _ThreadPipeWriter(file)
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, file)
    return asyncio.StreamWriter(transport, protocol, None, loop)


__all__ = ["PipeWriter", "connect_pipe_reader", "connect_pipe_writer"]
