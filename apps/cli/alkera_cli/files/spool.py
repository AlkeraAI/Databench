"""Where an inbound download streams before it is put in a chat's tree.

The daemon's own directory, outside the tree: the bytes arrive where the agent
cannot swap a link in under them, and the tree only ever sees the finished
file, put in place through :class:`~alkera_cli.files.chat_fs.ChatTree`. How
many bytes of a file have arrived so far is still readable
(:meth:`InboundSpool.landing_bytes`), which is what keeps a turn waiting on a
slow hand-over instead of giving up on it.

A download is removed when it fails, but a daemon killed mid-transfer leaves
its partial file behind. The first use of a spool sweeps every partial file
nobody has written to for :data:`STALE_AFTER_S`: a live transfer writes as
bytes arrive, and one that stalls longer is cut by its own ceiling first. Left
alone, those files would fill the box's disk and count as bytes still
arriving for the file they were meant for.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Final

__all__ = ["SPOOL_SUFFIX", "STALE_AFTER_S", "InboundSpool", "stream_into"]

logger = logging.getLogger(__name__)

SPOOL_SUFFIX: Final = ".alkera-inbound"
#: How long a partial download may go unwritten before a sweep removes it.
STALE_AFTER_S: Final = 6 * 3600.0


class InboundSpool:
    """A directory of the daemon's own for inbound downloads. ``None`` makes a
    private temporary one on first use; a named one is created ``0700``."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        stale_after_s: float = STALE_AFTER_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._directory = directory
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._swept = False

    def sweep(self) -> int:
        """Remove every partial download nobody has written to for the stale
        window; how many went. Run once, on the spool's first use."""
        self._swept = True
        if self._directory is None:
            return 0
        now = self._clock() if self._clock is not None else time.time()
        cutoff = now - self._stale_after_s
        removed = 0
        try:
            entries = list(os.scandir(self._directory))
        except OSError:
            return 0
        for entry in entries:
            if not entry.name.endswith(SPOOL_SUFFIX):
                continue
            with contextlib.suppress(OSError):
                info = entry.stat(follow_symlinks=False)
                if info.st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
        if removed:
            logger.info(
                "removed %d partial inbound download(s) left in %s", removed, self._directory
            )
        return removed

    def _sweep_once(self) -> None:
        if not self._swept:
            self.sweep()

    @property
    def directory(self) -> Path | None:
        return self._directory

    @staticmethod
    def _key(relative: str) -> str:
        return hashlib.sha256(relative.encode("utf-8", "surrogateescape")).hexdigest()[:24]

    def new_file(self, relative: str) -> Path:
        """A fresh name for one download of ``relative`` (under the tree's
        root). Unique per attempt: two fetches of one node must never stream
        into one file, or what is put in place is neither."""
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="alkera-inbound-"))
        else:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._sweep_once()
        return self._directory / f"{self._key(relative)}.{uuid.uuid4().hex[:8]}{SPOOL_SUFFIX}"

    def landing_bytes(self, relative: str) -> int:
        """How many bytes of a download for ``relative`` have arrived so far."""
        if self._directory is None:
            return 0
        self._sweep_once()
        prefix = f"{self._key(relative)}."
        total = 0
        try:
            entries = list(os.scandir(self._directory))
        except OSError:
            return 0
        for entry in entries:
            if entry.name.startswith(prefix):
                with contextlib.suppress(OSError):
                    total += entry.stat(follow_symlinks=False).st_size
        return total


def stream_into(
    into: Path,
    chunks: Iterable[bytes],
    clock: Callable[[], float],
    *,
    since: float,
    ceiling: float,
    what: str,
) -> None:
    """Write ``chunks`` to the spool file ``into`` as they arrive. A transfer
    still arriving ``ceiling`` seconds after ``since`` (on ``clock``) is cut,
    and left owed by the caller; one that finishes under it lands however
    slow the link was."""
    with into.open("wb") as handle:
        for chunk in chunks:
            if clock() - since > ceiling:
                raise TimeoutError(
                    f"inbound download of {what} is still arriving after {ceiling:.0f}s; left owed"
                )
            handle.write(chunk)
