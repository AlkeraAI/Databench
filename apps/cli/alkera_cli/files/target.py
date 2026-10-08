"""The POSIX materialization target behind ``alkera files pull``.

Every byte a pull writes goes through here, and every path a pull is asked to
write is contained first: the relative path is refused lexically (absolute, a
``..`` segment, an empty segment, a NUL) **before any I/O**, then proven to
land beneath the root — by ``openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS)`` on Linux, and by a canonical-path-plus-separator check
with a per-component symlink refusal everywhere else. A stored tree cannot
write outside the directory the user named, and cannot use a symlink it wrote
a moment ago as a door out.

``alkera_core.files.store._beneath`` does the same job for the object store and
is not reused: its keys are validated as portable store keys (lowercase ASCII,
``str``), while a materialized path is arbitrary bytes — ``README.md`` alone
would be refused, and a Latin-1 name cannot be spelled as UTF-8 at all.

Symlinks are written **last**. They are queued by :meth:`write_symlink` and
flushed on exit, so a link never resolves to a file the pull has not written
yet and a link that points at a later sibling is not a dangling window for
another process to see. A link is addressed by :meth:`resolve_leaf`, which
proves containment on the *parent* and leaves the last component alone: a link
already on disk is exactly a last component that is a symlink, and refusing it
would make a link whose stored target changed impossible to rewrite.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import stat as stat_module
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Final, Literal

from alkera_core.files.links import LinkKind, materialize_link, strip_extended_prefix
from alkera_core.files.sync.atomic import AtomicWriter

from alkera_cli.files.chat_fs import ChatTree, ChatTreeError
from alkera_cli.files.xattrs import write_xattrs

__all__ = ["ContainmentError", "MaterializationTarget"]

_RESOLVE_NO_MAGICLINKS: Final = 0x02
_RESOLVE_NO_SYMLINKS: Final = 0x04
_RESOLVE_BENEATH: Final = 0x08
_SYS_OPENAT2: Final = 437
_O_PATH: Final = 0o10000000
_ENOENT: Final = 2

_USING_OPENAT2: Final = sys.platform == "linux"
_WINDOWS: Final = os.name == "nt"
"""Which spelling of a path this machine produces. Read once, so a test can
drive the other platform's branch without a platform to run it on."""


class ContainmentError(ValueError):
    """A relative path would leave the materialization root."""


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


class MaterializationTarget:
    """A directory a stored tree is written into, safely.

    ``local_root`` is the org tree's mount point on this machine and defaults
    to the target root itself: it is what a canonical symlink target is
    rewritten against, the exact inverse of the classification the walk did.

    ``windows`` is the path flavour the rewrite uses, taken from the machine
    this runs on: a canonical target is re-anchored with the separators and
    the drive its own kernel reads.

    ``trusted`` decides whether setuid/setgid survive. It is ``False`` by
    default — a shared box must not gain a setuid binary because someone
    pushed one — and only a caller that owns the whole tree turns it on.

    ``tree`` is set when the directory is a chat's, written by the box daemon
    as root while the chat's agent controls every name in it: every write,
    move, delete and read then goes through :class:`ChatTree` (no link
    followed at any component, no special file opened, files owned by the
    sandbox with its modes), and no fifo is ever made there.
    """

    def __init__(
        self,
        root: Path,
        *,
        local_root: bytes | None = None,
        trusted: bool = False,
        tree: ChatTree | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tree = tree
        self._root_real = Path(os.path.realpath(self.root))
        # A Windows root can arrive extended-length prefixed, and the link text
        # written from it has to read like every other path on the machine.
        self.local_root = strip_extended_prefix(
            local_root if local_root is not None else os.fsencode(self.root), windows=_WINDOWS
        )
        self.windows = _WINDOWS
        self.trusted = trusted
        self._pending_links: list[tuple[bytes, LinkKind, bytes]] = []
        self.skipped_links: list[tuple[bytes, str]] = []
        """Links a chat tree refused to make at the last flush (a target that
        is absolute or climbs above the root), each with the tree's reason."""

    def __enter__(self) -> MaterializationTarget:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exc_type is None:
            self.flush_links()
        return False

    # -- containment ---------------------------------------------------

    def resolve(self, relative: bytes) -> Path:
        """The absolute path ``relative`` names beneath the root, or raise.

        The result is lexical and safe to create: every component that exists
        has been proven to be a real directory beneath the root.
        """
        self._refuse_lexically(relative)
        candidate = Path(
            os.path.normpath(os.fsdecode(os.fsencode(self._root_real) + b"/" + relative))
        )
        if candidate == self._root_real or self._root_real not in candidate.parents:
            raise ContainmentError(f"{relative!r} escapes the materialization root")
        segments = relative.split(b"/")
        if _USING_OPENAT2:  # pragma: no cover - exercised on Linux only
            self._walk_openat2(segments, relative)
        else:
            self._walk_lstat(segments, relative)
        return candidate

    @staticmethod
    def _refuse_lexically(relative: bytes) -> None:
        if not relative:
            raise ContainmentError("empty path")
        if b"\x00" in relative:
            raise ContainmentError("path contains NUL")
        if relative.startswith(b"/"):
            raise ContainmentError(f"{relative!r} is absolute")
        for segment in relative.split(b"/"):
            if not segment:
                raise ContainmentError(f"{relative!r} contains an empty segment")
            if segment in (b".", b".."):
                raise ContainmentError(f"{relative!r} contains a {segment!r} segment")

    def resolve_leaf(self, relative: bytes) -> Path:
        """The path ``relative`` names, with containment proven on its parent.

        :meth:`resolve` refuses any path whose last component is a symlink —
        that is what stops a link the pull just wrote from becoming a door out
        of the root. A symlink's *own* path is that case by construction, so
        rewriting a link already on disk has to ask a narrower question: every
        component **above** the leaf is still proven a real directory beneath
        the root, and the leaf itself is appended without being walked through.
        Nothing is ever opened or followed through the leaf, so a link on disk
        cannot redirect the write.
        """
        self._refuse_lexically(relative)
        parent, _, leaf = relative.rpartition(b"/")
        base = os.fsencode(self.resolve(parent)) if parent else os.fsencode(self._root_real)
        return Path(os.fsdecode(base + b"/" + leaf))

    def _walk_lstat(self, segments: list[bytes], relative: bytes) -> None:
        cursor = os.fsencode(self._root_real)
        for segment in segments:
            cursor = cursor + b"/" + segment
            try:
                info = os.lstat(cursor)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ContainmentError(f"{relative!r} is not resolvable beneath the root") from exc
            if stat_module.S_ISLNK(info.st_mode):
                raise ContainmentError(f"{relative!r} traverses the symlink {segment!r}")

    def _walk_openat2(  # pragma: no cover - Linux only
        self, segments: list[bytes], relative: bytes
    ) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        how = _OpenHow(
            flags=ctypes.c_uint64(_O_PATH),
            mode=ctypes.c_uint64(0),
            resolve=ctypes.c_uint64(
                _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_MAGICLINKS
            ),
        )
        root_fd = os.open(self._root_real, os.O_RDONLY | os.O_DIRECTORY)
        try:
            cursor = b""
            for segment in segments:
                cursor = cursor + b"/" + segment if cursor else segment
                fd = libc.syscall(
                    ctypes.c_long(_SYS_OPENAT2),
                    ctypes.c_int(root_fd),
                    ctypes.c_char_p(cursor),
                    ctypes.byref(how),
                    ctypes.c_size_t(ctypes.sizeof(how)),
                )
                if fd < 0:
                    if ctypes.get_errno() == _ENOENT:
                        return
                    raise ContainmentError(f"{relative!r} is not resolvable beneath the root")
                os.close(fd)
        finally:
            os.close(root_fd)

    # -- writes --------------------------------------------------------

    def mkdir(self, relative: bytes) -> Path:
        """Create the directory ``relative`` (and nothing above the root)."""
        where = self.resolve(relative)
        if self.tree is not None:
            self.tree.mkdirs(relative)
            return where
        where.mkdir(parents=True, exist_ok=True)
        return where

    def write_bytes(self, relative: bytes, data: bytes) -> Path:
        """Write ``data`` atomically: readers see the old bytes or the new."""
        where = self.resolve(relative)
        if self.tree is not None:
            self.tree.write(relative, data)
            return where
        where.parent.mkdir(parents=True, exist_ok=True)
        with AtomicWriter(where) as handle:
            handle.write(data)
        return where

    # -- file primitives -------------------------------------------------
    #
    # What a pull does to a file beyond writing it whole. Each goes through
    # the chat tree when there is one, and otherwise through the contained
    # path, exactly as a pull on a person's own machine always has.

    def lstat(self, relative: bytes) -> os.stat_result | None:
        """``relative``'s own status (a link's, not what it names), or None."""
        if self.tree is not None:
            return self.tree.stat(relative)
        try:
            return os.lstat(self.resolve_leaf(relative))
        except (OSError, ContainmentError):
            return None

    def is_regular(self, relative: bytes) -> bool:
        info = self.lstat(relative)
        return info is not None and stat_module.S_ISREG(info.st_mode)

    def open_read(self, relative: bytes) -> BinaryIO:
        """``relative`` opened for reading; a link at it is refused."""
        if self.tree is not None:
            return self.tree.open_read(relative)
        return self.resolve(relative).open("rb")

    @contextlib.contextmanager
    def open_write(self, relative: bytes, *, append: bool = False) -> Iterator[BinaryIO]:
        """``relative`` opened for writing in place (a partial download)."""
        if self.tree is not None:
            with self.tree.open_write(relative, append=append) as handle:
                yield handle
            return
        where = self.resolve(relative)
        where.parent.mkdir(parents=True, exist_ok=True)
        with where.open("ab" if append else "wb") as handle:
            yield handle

    def replace(self, source: bytes, destination: bytes) -> None:
        """Move the file at ``source`` over ``destination``."""
        if self.tree is not None:
            self.tree.rename(source, destination)
            return
        here = self.resolve(source)
        there = self.resolve(destination)
        there.parent.mkdir(parents=True, exist_ok=True)
        os.replace(here, there)

    def unlink(self, relative: bytes, *, missing_ok: bool = True) -> None:
        """Remove the file (or link) at ``relative``, never what a link names."""
        if self.tree is not None:
            self.tree.unlink(relative, missing_ok=missing_ok)
            return
        self.resolve_leaf(relative).unlink(missing_ok=missing_ok)

    def rmdir(self, relative: bytes) -> None:
        """Remove the empty directory at ``relative``."""
        if self.tree is not None:
            self.tree.rmdir(relative)
            return
        self.resolve(relative).rmdir()

    def copy(self, source: bytes, destination: bytes) -> None:
        """Fill ``destination`` with a copy of the file at ``source``."""
        if self.tree is not None:
            with self.tree.open_read(source) as handle:
                self.tree.write(destination, handle)
            return
        here = self.resolve(source)
        there = self.resolve(destination)
        there.parent.mkdir(parents=True, exist_ok=True)
        if there.exists():
            there.unlink()
        there.write_bytes(here.read_bytes())

    def readlink(self, relative: bytes) -> bytes | None:
        """The text of the link at ``relative``, never followed; None when
        it is not one."""
        if self.tree is not None:
            return self.tree.readlink(relative)
        try:
            return os.readlink(os.fsencode(self.resolve_leaf(relative)))
        except (OSError, ContainmentError):
            return None

    def make_fifo(self, relative: bytes) -> None:
        """Recreate a stored fifo. Never in a chat tree: the daemon makes no
        special file where the agent and the daemon both look."""
        if self.tree is not None:
            raise ChatTreeError(errno.EPERM, "a chat's folder holds no special files")
        where = self.resolve(relative)
        where.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(where)

    def write_pointer(self, relative: bytes, payload: Mapping[str, Any]) -> Path:
        """Write a ``.alkera-<kind>`` pointer file for a row-backed object."""
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return self.write_bytes(relative, encoded)

    def write_symlink(self, relative: bytes, kind: LinkKind, stored_target: bytes) -> None:
        """Queue a symlink; it is created by :meth:`flush_links`, never before.

        The queue is what makes "symlinks last" true for the whole tree rather
        than per directory.
        """
        self.resolve_leaf(relative)
        self._pending_links.append((relative, kind, stored_target))

    def flush_links(self) -> list[Path]:
        """Create every queued symlink, rewriting canonical targets locally.

        In a chat tree a link whose target is outside the tree (absolute, or
        climbing above the root) is not made: the tree refuses it, and the
        name and reason land in :attr:`skipped_links` for the pull to report.
        The rest of the queue is still flushed."""
        written: list[Path] = []
        self.skipped_links = []
        for relative, kind, stored_target in self._pending_links:
            where = self.resolve_leaf(relative)
            local_target = materialize_link(
                kind, stored_target, self.local_root, windows=self.windows
            )
            if self.tree is not None:
                try:
                    self.tree.symlink(relative, local_target)
                except ChatTreeError as refused:
                    self.skipped_links.append((relative, refused.strerror or str(refused)))
                    continue
                written.append(where)
                continue
            where.parent.mkdir(parents=True, exist_ok=True)
            if where.is_symlink():
                where.unlink()
            os.symlink(local_target, os.fsencode(where))
            written.append(where)
        self._pending_links.clear()
        return written

    def set_attrs(
        self,
        relative: bytes,
        *,
        mode: int | None = None,
        mtime_ns: int | None = None,
        xattrs: Mapping[bytes, bytes] | None = None,
    ) -> None:
        """Restore POSIX metadata on an already-written path.

        setuid and setgid are stripped unless the target is ``trusted``; the
        mtime is restored in nanoseconds so git's index does not decide every
        file is racy; ``user.*`` xattrs are set on the path itself.

        In a chat tree the mode is the tree's own (the stored one decides only
        whether the file is executable) and all of it is applied through a
        descriptor on the file.
        """
        where = self.resolve(relative)
        if self.tree is not None:
            self.tree.restore(relative, mode=mode, mtime_ns=mtime_ns, xattrs=xattrs)
            return
        if mode is not None:
            effective = (
                mode if self.trusted else mode & ~(stat_module.S_ISUID | stat_module.S_ISGID)
            )
            os.chmod(where, effective)
        if xattrs:
            write_xattrs(where, xattrs)
        if mtime_ns is not None:
            _stamp_mtime(where, mtime_ns)


def _stamp_mtime(where: Path, mtime_ns: int) -> None:
    """Restore the path's own mtime, never the mtime of what it points at.

    A symlink is the reason this is not a bare ``os.utime``: stamping a link
    has to be done on the link itself, and the only way to say so is
    ``follow_symlinks=False``. Windows' ``os.utime`` does not accept that flag
    (``os.utime not in os.supports_follow_symlinks``) and raises
    ``NotImplementedError`` -- so there a link keeps no mtime of its own, which
    is the honest answer: following it would rewrite the mtime of a file this
    entry does not own. Regular files and directories are stamped everywhere.
    """
    if not where.is_symlink():
        os.utime(where, ns=(mtime_ns, mtime_ns))
        return
    if os.utime in os.supports_follow_symlinks:
        os.utime(where, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
