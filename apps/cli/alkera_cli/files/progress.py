"""One line of progress for a transfer, and the seam the library reports through.

A push or a pull of a real tree is minutes of silence otherwise — the customer
finding was 150 MB and thirteen seconds with nothing on the terminal at all. The
library takes a plain callable so it stays terminal-free: it says which entry it
is about to move and how big that entry is, and the *caller* decides whether
that becomes a redrawn line, a log record, or nothing.

:class:`TransferProgress` is the terminal's answer. It rewrites one line in
place while stdout is a TTY and prints nothing at all when it is not, so a
script that captures the output gets the summary and only the summary.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

__all__ = ["Progress", "TransferProgress"]

#: What the library calls as it starts each entry: the entry's relative path
#: and, when it is known, the bytes about to move.
Progress = Callable[[bytes], None]

#: Beyond this the name is elided in the middle, so the counter never wraps.
_NAME_WIDTH = 48


@dataclass
class TransferProgress:
    """A one-line counter, redrawn in place, only when someone is watching."""

    total: int = 0
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    done: int = 0
    written: bool = field(default=False, init=False)

    @property
    def enabled(self) -> bool:
        """Whether anything is printed at all: a TTY, and nothing else."""
        try:
            return bool(self.stream.isatty())
        except ValueError:  # a stream closed under us is not a terminal
            return False

    def __call__(self, relative: bytes) -> None:
        self.done += 1
        if not self.enabled:
            return
        name = relative.decode("utf-8", "replace")
        if len(name) > _NAME_WIDTH:
            name = name[: _NAME_WIDTH // 2 - 1] + "…" + name[-_NAME_WIDTH // 2 :]
        counted = f"{self.done}/{self.total}" if self.total else str(self.done)
        self.stream.write(f"\r\033[K{counted}  {name}")
        self.stream.flush()
        self.written = True

    def finish(self) -> None:
        """Clear the counter so the summary starts on a clean line."""
        if self.enabled and self.written:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self.written = False
