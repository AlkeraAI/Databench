"""The one atomic local-disk write primitive for Files.

Every materialized file — the ``filesystem`` store driver, the sync journal and
the CLI's ``files pull`` — writes through :class:`AtomicWriter`, so a reader of
a target always sees either the old bytes or the new bytes and never a mix, and
a target is never modified in place.

The durability sequence is fixed: write into a same-directory temp file,
``fsync`` the file, publish it onto the target, ``fsync`` the parent
directory. Each step is an injectable seam and each boundary between two steps
is a named checkpoint, which is what lets the crash harness SIGKILL a process
at exactly one of them and assert the old-or-new invariant afterwards.

Publishing has two modes. ``replace`` renames over whatever is there, which is
what the journal and the client want. ``link`` publishes with ``os.link``, so a
target that already exists makes the write fail with ``FileExistsError``
instead of clobbering bytes — that is how the content-addressed store keeps its
create-exclusive (``if_absent``) semantics without a check-then-act race.
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal

if sys.platform != "win32":
    import fcntl

__all__ = [
    "CHECKPOINTS",
    "LINK_CHECKPOINTS",
    "AtomicWriter",
    "PublishMode",
    "checkpoints_for",
    "default_fsync_dir",
    "write_bytes_atomic",
]

PublishMode = Literal["replace", "link"]

#: Every point a crash harness may kill a ``replace`` write at, in order.
CHECKPOINTS: tuple[str, ...] = (
    "atomic.after_write",
    "atomic.after_fsync_file",
    "atomic.after_rename",
    "atomic.after_fsync_dir",
)

#: The same points for a ``link`` write; only the publish step differs.
LINK_CHECKPOINTS: tuple[str, ...] = (
    "atomic.after_write",
    "atomic.after_fsync_file",
    "atomic.after_link",
    "atomic.after_fsync_dir",
)


def checkpoints_for(mode: PublishMode) -> tuple[str, ...]:
    """The kill points a writer in ``mode`` reaches, in the order it reaches them."""
    return LINK_CHECKPOINTS if mode == "link" else CHECKPOINTS


FsyncFile = Callable[[int], None]
Replace = Callable[[Path, Path], None]
Link = Callable[[Path, Path], None]
FsyncDir = Callable[[Path], None]
OnCheckpoint = Callable[[str], None]


def default_fsync_dir(directory: Path) -> None:
    """Flush the directory entry so the rename itself survives a power loss."""
    if sys.platform == "win32":  # pragma: no cover - POSIX-only directory fsync
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(source: Path, target: Path) -> None:
    os.replace(source, target)


def _link(source: Path, target: Path) -> None:
    os.link(source, target)


class AtomicWriter:
    """A context manager yielding a binary handle whose bytes land atomically.

    On a clean exit the temp file is flushed, fsynced, published onto
    ``target`` (renamed, or hard-linked when ``mode="link"``) and the parent
    directory is fsynced. On any exception — including the ``FileExistsError``
    a ``link`` publish raises on a taken target — the temp file is removed and
    ``target`` is left exactly as it was.
    """

    def __init__(
        self,
        target: Path,
        *,
        mode: PublishMode = "replace",
        fsync_file: FsyncFile = os.fsync,
        replace: Replace = _replace,
        link: Link = _link,
        fsync_dir: FsyncDir = default_fsync_dir,
        on_checkpoint: OnCheckpoint | None = None,
    ) -> None:
        self.target = Path(target)
        self.mode: PublishMode = mode
        self._fsync_file = fsync_file
        self._replace = replace
        self._link = link
        self._fsync_dir = fsync_dir
        self._on_checkpoint = on_checkpoint
        self._temp_path: Path | None = None
        self._handle: IO[bytes] | None = None

    @property
    def temp_path(self) -> Path | None:
        """The same-directory temp file, once the block has been entered."""
        return self._temp_path

    def _checkpoint(self, name: str) -> None:
        if self._on_checkpoint is not None:
            self._on_checkpoint(name)

    def __enter__(self) -> IO[bytes]:
        parent = self.target.parent
        temp = parent / f".{self.target.name}.{secrets.token_hex(8)}.tmp"
        # "x" so a colliding temp name is an error rather than a silent clobber
        # of another writer's in-flight bytes.
        self._handle = temp.open("xb")
        self._temp_path = temp
        return self._handle

    def _full_fsync(self, fd: int) -> None:
        """Issue the macOS barrier that actually reaches the platter."""
        if sys.platform != "darwin":
            return
        command = getattr(fcntl, "F_FULLFSYNC", None)
        if command is None:  # pragma: no cover - every supported macOS has it
            return
        fcntl.fcntl(fd, command)

    def _discard(self) -> None:
        if self._temp_path is not None:
            self._temp_path.unlink(missing_ok=True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        handle = self._handle
        temp = self._temp_path
        if handle is None or temp is None:  # pragma: no cover - __enter__ always sets both
            return False
        if exc_type is not None:
            handle.close()
            self._discard()
            return False
        try:
            handle.flush()
            self._checkpoint("atomic.after_write")
            fd = handle.fileno()
            self._full_fsync(fd)
            self._fsync_file(fd)
            self._checkpoint("atomic.after_fsync_file")
            handle.close()
            if self.mode == "link":
                self._link(temp, self.target)
                self._checkpoint("atomic.after_link")
            else:
                self._replace(temp, self.target)
                self._checkpoint("atomic.after_rename")
            self._fsync_dir(self.target.parent)
            self._checkpoint("atomic.after_fsync_dir")
            # A linked publish leaves the temp as a second name for the same
            # inode; dropping it is bookkeeping, not part of the commit.
            self._discard()
        except BaseException:
            handle.close()
            self._discard()
            raise
        return False


def write_bytes_atomic(target: Path, data: bytes, **seams: Any) -> None:
    """Write ``data`` to ``target`` atomically. ``seams`` go to AtomicWriter."""
    with AtomicWriter(target, **seams) as handle:
        handle.write(data)
