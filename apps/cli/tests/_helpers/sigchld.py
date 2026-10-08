"""Put ``SIGCHLD`` back exactly as the kernel held it before a test changed it.

``signal.getsignal`` answers from Python's own table, which knows nothing of a
handler installed in C. The Temporal SDK's Rust runtime installs one (tokio
watches for its children through ``SIGCHLD``), so once a session's dev server
is up, the table still says ``SIG_DFL`` while the kernel holds tokio's handler.
A test that saves the table's answer and "restores" it writes ``SIG_DFL`` over
that handler: tokio never hears its dev server exit, the server stays a zombie,
and the session's last teardown waits on it until the suite's timeout ends the
worker's final test. So the disposition is saved and restored with
``sigaction`` itself, as an opaque record, alongside Python's table entry.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager

#: Larger than ``struct sigaction`` on every libc this runs on (16 bytes on
#: macOS, 152 on glibc); the call reads and writes only its own size of it.
_SIGACTION_BYTES = 512


def _sigaction(signum: int, new: ctypes.Array[ctypes.c_char] | None) -> ctypes.Array[ctypes.c_char]:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sigaction.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    libc.sigaction.restype = ctypes.c_int
    old = ctypes.create_string_buffer(_SIGACTION_BYTES)
    if libc.sigaction(signum, new, old) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return old


def kernel_handler(signum: int) -> int:
    """The handler address the kernel holds for ``signum`` (``SIG_DFL`` is 0,
    ``SIG_IGN`` 1). It is the record's first field on glibc and on macOS; the
    rest of the record is not compared, glibc leaves part of the mask unset."""
    raw = _sigaction(signum, None).raw
    return int.from_bytes(raw[: ctypes.sizeof(ctypes.c_void_p)], sys.byteorder)


@contextmanager
def sigchld_preserved() -> Iterator[None]:
    """Whatever the block does to ``SIGCHLD``, the process leaves it with the
    kernel's disposition and Python's table entry it had on the way in."""
    if sys.platform == "win32":
        yield
        return
    saved = _sigaction(signal.SIGCHLD, None)
    table = signal.getsignal(signal.SIGCHLD)
    try:
        yield
    finally:
        # The table first: setting it writes the kernel too, and the raw record
        # then puts back a handler the table could not have named.
        if table is not None:
            signal.signal(signal.SIGCHLD, table)
        _sigaction(signal.SIGCHLD, saved)
