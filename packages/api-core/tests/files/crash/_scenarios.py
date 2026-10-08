"""Crash scenarios executed inside the child process of the crash harness.

Each scenario is a plain function taking the working directory as a string.
The parent launches this file with ``runpy.run_path`` (see ``_harness.py``), so
nothing here may rely on being imported as part of a package.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.sync.atomic import write_bytes_atomic
from blake3 import blake3

OLD_BYTES = b"A" * (1024 * 1024)
NEW_BYTES = b"B" * (1024 * 1024)

CRASH_AT_ENV = "FILES_CRASH_AT"
CRASH_DIR_ENV = "FILES_CRASH_DIR"

TARGET_NAME = "target.bin"
BEFORE_MARKER = "before.marker"
AFTER_MARKER = "after.marker"


def kill_hook(name: str) -> None:
    """SIGKILL this process the moment the requested checkpoint is reached.

    No ``os.sync()`` and no extra fsync: an added durability barrier here would
    mask a missing one in the writer, which is exactly what the harness exists
    to detect.
    """
    if name == os.environ.get(CRASH_AT_ENV):
        os.kill(os.getpid(), signal.SIGKILL)


def write_new_file(workdir: str) -> None:
    """Atomically create a target that does not exist yet."""
    directory = Path(workdir)
    (directory / BEFORE_MARKER).write_bytes(b"before")
    write_bytes_atomic(directory / TARGET_NAME, NEW_BYTES, on_checkpoint=kill_hook)
    (directory / AFTER_MARKER).write_bytes(b"after")


def overwrite_existing(workdir: str) -> None:
    """Atomically replace a target that already holds ``OLD_BYTES``."""
    directory = Path(workdir)
    target = directory / TARGET_NAME
    write_bytes_atomic(target, OLD_BYTES)
    (directory / BEFORE_MARKER).write_bytes(b"before")
    write_bytes_atomic(target, NEW_BYTES, on_checkpoint=kill_hook)
    (directory / AFTER_MARKER).write_bytes(b"after")


OBJECT_KEY = "blobs/ab/cd/abcdef.bin"
OBJECT_BYTES = bytes(range(256)) * (8 * 1024)  # 2 MiB, non-uniform so a truncation shows
STORE_ROOT = "root"
STREAM_CHUNK = 256 * 1024


class KillingCheckpoints:
    """A `Checkpoints` seam that SIGKILLs the process at the requested name.

    The library's `PausingCheckpoints` raises `CheckpointKilled` instead, which
    unwinds the stack and would let the writer's cleanup run — exactly the code
    a crash must not get to execute. This one dies where a power loss would.
    """

    async def reach(self, name: str) -> None:
        kill_hook(name)

    def reach_sync(self, name: str) -> None:
        kill_hook(name)

    def as_hook(self) -> Callable[[str], None]:
        return self.reach_sync


def _epoch() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), STREAM_CHUNK):
        yield payload[offset : offset + STREAM_CHUNK]


def put_object(workdir: str) -> None:
    """Put a 2 MiB object into a filesystem store, killable at every checkpoint."""
    directory = Path(workdir)
    store = FilesystemStore(
        directory / STORE_ROOT,
        clock=_epoch,
        checkpoints=KillingCheckpoints(),
    )
    (directory / BEFORE_MARKER).write_bytes(b"before")
    asyncio.run(
        store.put(
            OBJECT_KEY,
            _stream(OBJECT_BYTES),
            size=len(OBJECT_BYTES),
            checksum=blake3(OBJECT_BYTES).digest(),
        )
    )
    (directory / AFTER_MARKER).write_bytes(b"after")
