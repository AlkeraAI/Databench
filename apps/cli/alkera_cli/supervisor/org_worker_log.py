"""Each org worker's output (its log lines, and a traceback when it dies
before its logging is set up), copied from its pipe into a rotating,
owner-only ``<log dir>/org-<org id>.log`` of its own."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Final

ORG_LOG_MAX_BYTES: Final = 10 * 1024 * 1024
ORG_LOG_BACKUPS: Final = 3
#: The longest line kept whole; a longer one is cut there.
MAX_LINE_BYTES: Final = 64 * 1024


def org_log_path(log_dir: Path, org_id: str) -> Path:
    return log_dir / f"org-{org_id}.log"


class OrgLog:
    """An append-only file rolled at ``max_bytes`` into ``.1`` … ``.<backups>``."""

    def __init__(
        self, path: Path, *, max_bytes: int = ORG_LOG_MAX_BYTES, backups: int = ORG_LOG_BACKUPS
    ) -> None:
        self.path, self._max, self._backups = path, max_bytes, backups
        self._fd: int | None = None
        self._size = 0

    def write(self, line: bytes) -> None:
        line = line if line.endswith(b"\n") else line + b"\n"
        if self._size and self._size + len(line) > self._max:
            self.close()
            for index in range(self._backups, 0, -1):
                newer = self.path.with_name(f"{self.path.name}.{index - 1}") if index > 1 else None
                with contextlib.suppress(FileNotFoundError):
                    os.replace(newer or self.path, f"{self.path}.{index}")
            self.path.unlink(missing_ok=True)
        if self._fd is None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(self._fd, 0o600)
            self._size = os.fstat(self._fd).st_size
        os.write(self._fd, line)
        self._size += len(line)

    def note(self, text: str) -> None:
        """The supervisor's own line about this worker, stamped."""
        self.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} supervisor: {text}".encode())

    def close(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None


async def copy_output(stream: asyncio.StreamReader, log: OrgLog) -> None:
    """Copy ``stream`` into ``log`` until it ends. A log that cannot be
    written loses lines, never the reader: the worker would block writing."""
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as end:
            line = end.partial
        except asyncio.LimitOverrunError as over:
            line = await stream.readexactly(over.consumed)
        if not line:
            return
        with contextlib.suppress(OSError):
            log.write(line[:MAX_LINE_BYTES])
