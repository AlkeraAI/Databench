"""The one way the box daemon touches a file inside a chat's tree.

The daemon runs as root and the chat's agent as the chat's own uid, and the
agent controls every name under the tree: it can put a symlink, a fifo or a
directory where the daemon expects a file, or swap one in between the
daemon's check and its write. So nothing the daemon does inside a chat tree
goes through a path string. Every operation here starts from a descriptor on
the tree's root and walks down one component at a time with ``O_NOFOLLOW``:

- a symlink anywhere on the way, at the leaf included, is refused, never
  followed, so no write and no read ever lands outside the root;
- a fifo, a device or a directory where a file is expected is refused, and the
  open that finds one is non-blocking, so a fifo cannot stall the daemon;
- a write lands whole: a temporary file in the same directory, fsynced, then
  renamed over the name, so a reader sees the old bytes or the new;
- what the daemon writes belongs to the sandbox's identity (:func:`identity_for`)
  with modes anyone inside the sandbox can read and write, whoever wrote it.
  ``other`` stays closed because on a shared box ``other`` is the next chat.

:meth:`ChatTree.repair` brings a tree an older daemon wrote back to that state
before the agent starts, without following a link.

On a platform with no ``dir_fd`` support (Windows, which never hosts a
sandbox) the same operations run on paths with a per-component link check;
there is no second uid to defend against there.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import logging
import os
import stat
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

from alkera_core.files.links import link_stays_inside
from alkera_core.project.local_state import register_local_state

from alkera_cli.files.hashing import stream_hash

__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "ChatTree",
    "ChatTreeError",
    "RepairReport",
    "TreeIdentity",
    "file_mode",
    "has_identity_resolver",
    "identity_for",
    "link_stays_inside",
    "set_identity_resolver",
]

logger = logging.getLogger(__name__)

FILE_MODE: Final = 0o660
"""A file's mode in a chat tree: read and write for the sandbox's user and
group. An executable file also gets ``x`` for both (:func:`file_mode`)."""

DIR_MODE: Final = 0o2770
"""A directory's mode: everything for the sandbox's user and group, and
setgid so what a second uid of the group creates inside stays the group's."""

_TEMP_SUFFIX: Final = ".alkera-tmp"
# A write in progress (or one a crash left half done) is this machine's alone:
# the live plane and the checkpoint push never send it.
register_local_state(f".*{_TEMP_SUFFIX}", f"*/.*{_TEMP_SUFFIX}")
_CHUNK: Final = 1 << 20
_MAX_DEPTH: Final = 512

_O_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_BINARY: Final = getattr(os, "O_BINARY", 0)

_DIR_FDS: Final = (
    os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and bool(_O_NOFOLLOW)
    and bool(_O_DIRECTORY)
)
"""Whether this platform walks by descriptor. Read once, so a test can drive
the path fallback on a platform that has descriptors."""


class ChatTreeError(OSError):
    """An operation inside a chat tree was refused: a link, a special file, a
    directory where a file was expected, or a path that leaves the root."""


@dataclass(frozen=True, slots=True)
class TreeIdentity:
    """The uid and gid every file the daemon writes into a chat tree gets."""

    uid: int
    gid: int


def _wears(info: os.stat_result | None, stamp: tuple[int, int]) -> bool:
    """Whether ``info`` is a file still stamped ``(size, mtime_ns)``."""
    return info is not None and (info.st_size, info.st_mtime_ns) == stamp


#: The flag that makes the platform's two-name rename swap the names instead
#: of replacing one: ``RENAME_EXCHANGE`` for ``renameat2`` (Linux 3.15+, glibc
#: 2.28+), ``RENAME_SWAP`` for ``renameatx_np`` (macOS 10.12+).
_SWAP_FLAG: Final = 2
#: What an exchange answers when the filesystem (or an old kernel) cannot
#: swap: the write then replaces the name as it did before swaps.
_NO_SWAP: Final = frozenset(
    {errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", errno.EINVAL), errno.EOPNOTSUPP}
)

Exchange = Callable[[int, str, str], bool]


def _find_exchange() -> Exchange | None:
    """The platform's atomic swap of two names in one directory, or ``None``.

    The callable answers ``True`` once the two names have swapped and
    ``False`` when this filesystem cannot swap (nothing moved); any other
    failure raises :class:`OSError`."""
    if sys.platform.startswith("linux"):
        name = "renameat2"
    elif sys.platform == "darwin":
        name = "renameatx_np"
    else:
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        call = getattr(libc, name)
    except (OSError, AttributeError):
        return None
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int

    def exchange(parent: int, first: str, second: str) -> bool:
        if call(parent, os.fsencode(first), parent, os.fsencode(second), _SWAP_FLAG) == 0:
            return True
        failure = ctypes.get_errno()
        if failure in _NO_SWAP:
            return False
        raise OSError(failure, os.strerror(failure), second)

    return exchange


_EXCHANGE: Exchange | None = _find_exchange()
"""Read once; a test drives the fallback by setting it to ``None``."""


def file_mode(previous: int | None = None) -> int:
    """The mode a file in a chat tree is left with.

    Read and write for the sandbox, whatever the bytes arrived with (a 0444
    upload is still a file the agent can edit), with the execute bit kept for
    user and group when any execute bit was set before."""
    if previous is not None and previous & 0o111:
        return FILE_MODE | 0o110
    return FILE_MODE


def _default_identity(chat_id: str) -> TreeIdentity | None:
    """No identity: files stay the writing process's. The box daemon installs
    the sandbox's resolver at start (``alkera_cli.cloud.chat_fs``), which is
    where the chat's uid is known."""
    del chat_id
    return None


_resolver: Callable[[str], TreeIdentity | None] = _default_identity


def identity_for(chat_id: str) -> TreeIdentity | None:
    """Who owns what the daemon writes into ``chat_id``'s tree. The one place
    that answers it: a sandbox that changes which uid runs the agent changes
    it here (:func:`set_identity_resolver`), and every write follows."""
    return _resolver(chat_id)


def has_identity_resolver() -> bool:
    """Whether a resolver other than the default one is installed."""
    return _resolver is not _default_identity


def set_identity_resolver(
    resolver: Callable[[str], TreeIdentity | None] | None,
) -> Callable[[str], TreeIdentity | None]:
    """Install ``resolver`` (``None`` restores the default); answers the one
    it replaced."""
    global _resolver
    previous = _resolver
    _resolver = resolver if resolver is not None else _default_identity
    return previous


@dataclass(slots=True)
class RepairReport:
    """What :meth:`ChatTree.repair` changed."""

    owners: int = 0
    modes: int = 0
    skipped: int = 0


def _split(relative: str | bytes | PurePosixPath) -> list[str]:
    spelled = os.fsdecode(relative) if isinstance(relative, bytes) else str(relative)
    spelled = spelled.replace("\\", "/") if os.sep == "\\" else spelled
    if not spelled or "\x00" in spelled or spelled.startswith("/"):
        raise ChatTreeError(errno.EINVAL, f"{spelled!r} is not a path inside the tree")
    parts = spelled.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ChatTreeError(errno.EINVAL, f"{spelled!r} is not a path inside the tree")
    return parts


class ChatTree:
    """A chat's tree as the daemon may touch it.

    Paths are relative to ``root`` (``str``, ``bytes`` or a path under the
    root, which is measured lexically and never resolved). ``identity`` is who
    the written files belong to; ``None`` leaves them the daemon's own, with
    the same permissive modes."""

    def __init__(self, root: Path, identity: TreeIdentity | None = None) -> None:
        self.root = Path(root)
        self.identity = identity

    @classmethod
    def for_chat(cls, root: Path, chat_id: str | None) -> ChatTree:
        return cls(root, identity_for(chat_id) if chat_id else None)

    # -- addressing ----------------------------------------------------

    def relative(self, path: str | bytes | Path) -> list[str]:
        """``path``'s components under the root; an absolute path must sit
        lexically beneath it."""
        if isinstance(path, Path) and path.is_absolute():
            try:
                inside = path.relative_to(self.root)
            except ValueError as outside:
                raise ChatTreeError(errno.EXDEV, f"{path} is not under {self.root}") from outside
            return _split(inside.as_posix())
        return _split(path if not isinstance(path, Path) else path.as_posix())

    def _open_dir(self, parent: int, name: str, *, create: bool) -> int:
        try:
            return os.open(
                name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent
            )
        except FileNotFoundError:
            if not create:
                raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent)
        try:
            self._own(fd, DIR_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @contextlib.contextmanager
    def _directory(self, names: list[str], *, create: bool) -> Iterator[int]:
        """A descriptor on the directory ``names`` spells under the root, each
        component opened without following a link (and made when ``create``)."""
        current = os.open(self.root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        try:
            for name in names:
                try:
                    below = self._open_dir(current, name, create=create)
                except OSError as failure:
                    if failure.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                        raise ChatTreeError(
                            failure.errno, f"{'/'.join(names)}: {name} is not a directory"
                        ) from failure
                    raise
                os.close(current)
                current = below
            yield current
        finally:
            os.close(current)

    def _parent(self, parts: list[str], *, create: bool) -> contextlib.AbstractContextManager[int]:
        """The directory holding ``parts[-1]``."""
        return self._directory(parts[:-1], create=create)

    def _own(self, fd: int, mode: int) -> None:
        if self.identity is not None:
            os.fchown(fd, self.identity.uid, self.identity.gid)
        os.fchmod(fd, mode)

    @staticmethod
    def _lstat(parent: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _refuse_unless_file(info: os.stat_result | None, spelled: str) -> None:
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise ChatTreeError(errno.EPERM, f"{spelled} is not a regular file")

    # -- reads ---------------------------------------------------------

    def stat(self, path: str | bytes | Path) -> os.stat_result | None:
        """``path``'s own status, a link's included; ``None`` when nothing
        is there or a component above it is not a directory."""
        parts = self.relative(path)
        if not _DIR_FDS:
            return self._path_lstat(parts)
        try:
            with self._parent(parts, create=False) as parent:
                return self._lstat(parent, parts[-1])
        except (FileNotFoundError, ChatTreeError):
            return None

    def is_file(self, path: str | bytes | Path) -> bool:
        info = self.stat(path)
        return info is not None and stat.S_ISREG(info.st_mode)

    def open_read(self, path: str | bytes | Path) -> BinaryIO:
        """``path`` opened for reading, refused unless it is a regular file."""
        parts = self.relative(path)
        spelled = "/".join(parts)
        if not _DIR_FDS:
            return self._path_open_read(parts)
        with self._parent(parts, create=False) as parent:
            try:
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC,
                    dir_fd=parent,
                )
            except OSError as failure:
                if failure.errno in (errno.ELOOP, errno.EMLINK):
                    raise ChatTreeError(failure.errno, f"{spelled} is a link") from failure
                raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ChatTreeError(errno.EPERM, f"{spelled} is not a regular file")
            os.set_blocking(fd, True)
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def digest(self, path: str | bytes | Path) -> tuple[str, int]:
        """The BLAKE3 and size of the regular file at ``path``, read through
        :meth:`open_read` (the hash a drive version's ``contentHash`` is)."""
        with self.open_read(path) as handle:
            return stream_hash(handle)

    def read_bytes(self, path: str | bytes | Path) -> bytes:
        with self.open_read(path) as handle:
            return handle.read()

    # -- writes --------------------------------------------------------

    def write(
        self,
        path: str | bytes | Path,
        source: bytes | BinaryIO,
        *,
        executable: bool | None = None,
        over: tuple[int, int] | None = None,
    ) -> bool:
        """Put ``source`` at ``path`` whole, creating its directories.

        A file already there keeps its execute bit unless ``executable`` says
        otherwise; anything there that is not a regular file is refused.

        ``over`` writes only over a file still stamped ``(size, mtime_ns)``,
        looked at once the bytes are on disk and just before the rename, so
        a write somebody else makes meanwhile (the agent saving) is never
        replaced unseen. False, with nothing written, when it moved."""
        parts = self.relative(path)
        spelled = "/".join(parts)
        if not _DIR_FDS:
            return self._path_write(parts, source, executable=executable, over=over)
        with self._parent(parts, create=True) as parent:
            existing = self._lstat(parent, parts[-1])
            self._refuse_unless_file(existing, spelled)
            if executable is None:
                previous = existing.st_mode if existing is not None else None
            else:
                previous = 0o100 if executable else 0
            temporary = f".{parts[-1]}.{uuid.uuid4().hex[:8]}{_TEMP_SUFFIX}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    _copy(source, handle)
                    handle.flush()
                os.fsync(fd)
                self._own(fd, file_mode(previous))
            except BaseException:
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=parent)
                raise
            ours = os.fstat(fd).st_ino
            os.close(fd)
            try:
                if over is not None and not _wears(self._lstat(parent, parts[-1]), over):
                    os.unlink(temporary, dir_fd=parent)
                    return False
                if over is not None:
                    swapped = self._swap_over(parent, temporary, parts[-1], over, ours)
                    if swapped is not None:
                        return swapped
                # No swap here (another platform, an old kernel, a filesystem
                # that cannot): the stamp was looked at just above, the
                # narrowest window a plain rename leaves.
                os.rename(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=parent)
                raise
            with contextlib.suppress(OSError):
                os.fsync(parent)
        return True

    def _swap_over(
        self, parent: int, temporary: str, name: str, over: tuple[int, int], ours: int
    ) -> bool | None:
        """Put ``temporary`` at ``name`` only over the file stamped ``over``,
        with no window between the look and the replace: the two names swap
        atomically, and the file that came out is looked at after. One that
        no longer wears the stamp is a save of the agent's that landed after
        the look; it is swapped back in and the write is refused, as a file
        that moved before the look is. ``None`` when this platform or
        filesystem cannot swap: the caller replaces as before."""
        exchange = _EXCHANGE
        if exchange is None:
            return None
        try:
            if not exchange(parent, temporary, name):
                return None
        except FileNotFoundError:
            # The file went away after the look: nothing to write over.
            os.unlink(temporary, dir_fd=parent)
            return False
        if _wears(self._lstat(parent, temporary), over):
            os.unlink(temporary, dir_fd=parent)
            with contextlib.suppress(OSError):
                os.fsync(parent)
            return True
        # The agent saved in between: its file is back under the name, ours
        # under the temporary one. Should it have saved yet again since the
        # swap, the swap back brings that newer save out instead of ours: it
        # is swapped in once more, and the older save it replaced dropped,
        # as the agent's own write would have dropped it.
        exchange(parent, temporary, name)
        came_out = self._lstat(parent, temporary)
        if came_out is not None and came_out.st_ino != ours:
            exchange(parent, temporary, name)
        os.unlink(temporary, dir_fd=parent)
        return False

    def install(
        self, path: str | bytes | Path, source: Path, *, executable: bool | None = None
    ) -> None:
        """Put the file at ``source`` (a daemon-private path outside the tree)
        at ``path``; see :meth:`write`."""
        with source.open("rb") as handle:
            self.write(path, handle, executable=executable)

    @contextlib.contextmanager
    def open_write(self, path: str | bytes | Path, *, append: bool = False) -> Iterator[BinaryIO]:
        """``path`` opened for writing in place (a partial download that is
        resumed by appending), created owned and refused unless a regular
        file. The caller renames it into its final name with :meth:`rename`."""
        parts = self.relative(path)
        spelled = "/".join(parts)
        if not _DIR_FDS:
            with self._path_open_write(parts, append=append) as handle:
                yield handle
            return
        # Opened without truncating: what is there is judged first, and only a
        # plain file with this one name is then cut to nothing. A truncating
        # open would empty a host file the name hard-links before any check.
        flags = os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
        if append:
            flags |= os.O_APPEND
        with self._parent(parts, create=True) as parent:
            try:
                fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
            except OSError as failure:
                if failure.errno in (errno.ELOOP, errno.EMLINK, errno.ENXIO, errno.EISDIR):
                    raise ChatTreeError(
                        failure.errno, f"{spelled} is not a regular file"
                    ) from failure
                raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ChatTreeError(errno.EPERM, f"{spelled} is not a regular file")
            if info.st_nlink > 1:
                # A second name for the same bytes may be a host file's (a hard
                # link made where the kernel allows one to a file one cannot
                # write); writing in place or handing it over would reach it.
                raise ChatTreeError(errno.EMLINK, f"{spelled} is a hard link")
            os.set_blocking(fd, True)
            if not append:
                os.ftruncate(fd, 0)
            self._own(fd, file_mode(info.st_mode))
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "wb") as handle:
            yield handle

    def mkdirs(self, path: str | bytes | Path) -> None:
        """Create the directory ``path`` and those above it."""
        parts = self.relative(path)
        if not _DIR_FDS:
            self._path_mkdirs(parts)
            return
        with self._directory(parts, create=True):
            pass

    def unlink(self, path: str | bytes | Path, *, missing_ok: bool = True) -> None:
        """Remove the file (or link) at ``path``; never a directory."""
        parts = self.relative(path)
        if not _DIR_FDS:
            self._path_unlink(parts, missing_ok=missing_ok)
            return
        try:
            with self._parent(parts, create=False) as parent:
                info = self._lstat(parent, parts[-1])
                if info is not None and stat.S_ISDIR(info.st_mode):
                    raise ChatTreeError(errno.EISDIR, f"{'/'.join(parts)} is a directory")
                os.unlink(parts[-1], dir_fd=parent)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rename(self, source: str | bytes | Path, destination: str | bytes | Path) -> None:
        """Move the regular file at ``source`` to ``destination`` (replacing a
        file there), creating the destination's directories."""
        here = self.relative(source)
        there = self.relative(destination)
        if not _DIR_FDS:
            self._path_rename(here, there)
            return
        with self._parent(here, create=False) as src, self._parent(there, create=True) as dst:
            moving = self._lstat(src, here[-1])
            if moving is None:
                raise FileNotFoundError(errno.ENOENT, "/".join(here))
            self._refuse_unless_file(moving, "/".join(here))
            target = self._lstat(dst, there[-1])
            if target is not None and stat.S_ISDIR(target.st_mode):
                raise ChatTreeError(errno.EISDIR, f"{'/'.join(there)} is a directory")
            os.rename(here[-1], there[-1], src_dir_fd=src, dst_dir_fd=dst)

    def link(self, source: str | bytes | Path, destination: str | bytes | Path) -> None:
        """A second name for the regular file at ``source``."""
        here = self.relative(source)
        there = self.relative(destination)
        if not _DIR_FDS or os.link not in os.supports_dir_fd:
            self._path_link(here, there)
            return
        with self._parent(here, create=False) as src, self._parent(there, create=True) as dst:
            self._refuse_unless_file(self._lstat(src, here[-1]), "/".join(here))
            os.link(here[-1], there[-1], src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)

    def rmdir(self, path: str | bytes | Path) -> None:
        """Remove the empty directory at ``path`` (a link there is refused)."""
        parts = self.relative(path)
        if not _DIR_FDS:
            self._path_check(parts)
            self._path(parts).rmdir()
            return
        with self._parent(parts, create=False) as parent:
            info = self._lstat(parent, parts[-1])
            if info is None:
                raise FileNotFoundError(errno.ENOENT, "/".join(parts))
            if not stat.S_ISDIR(info.st_mode):
                raise ChatTreeError(errno.ENOTDIR, f"{'/'.join(parts)} is not a directory")
            os.rmdir(parts[-1], dir_fd=parent)

    def link_text(self, path: str | bytes | Path, points_at: bytes) -> bytes:
        """What :meth:`symlink` writes for a link at ``path`` meant to hold
        ``points_at``: the target itself when it is relative and stays inside
        the tree; the relative spelling when it is an absolute path under the
        root, so the link resolves wherever the tree is mounted (a sandbox
        sees the root at its own path, not the host's); a
        :class:`ChatTreeError` for a target outside the tree, absolute or
        climbing above the root. The daemon writes as root into a tree the
        sandbox reads, and a link it made to a host path would stand there as
        a door whether or not the sandbox can open it today."""
        parts = self.relative(path)
        text = os.fsdecode(points_at)
        root = self.root.as_posix()
        if text == root or text.startswith(root.rstrip("/") + "/"):
            below = text[len(root) :].strip("/")
            here = "/".join(parts[:-1])
            text = os.path.relpath(below or ".", start=here or ".").replace(os.sep, "/")
            points_at = text.encode()
        if not link_stays_inside(parts, points_at):
            raise ChatTreeError(
                errno.EXDEV, f"{'/'.join(parts)} would point outside the tree: {points_at!r}"
            )
        return points_at

    def symlink(self, path: str | bytes | Path, points_at: bytes) -> None:
        """Make ``path`` a link holding :meth:`link_text` of ``points_at``,
        replacing a link (never anything else) already there. The link is
        written, never followed."""
        parts = self.relative(path)
        points_at = self.link_text(path, points_at)
        if not _DIR_FDS:
            self._path_check(parts[:-1])
            where = self._path(parts)
            where.parent.mkdir(parents=True, exist_ok=True)
            if where.is_symlink():
                where.unlink()
            os.symlink(points_at, os.fsencode(where))
            return
        with self._parent(parts, create=True) as parent:
            existing = self._lstat(parent, parts[-1])
            if existing is not None:
                if not stat.S_ISLNK(existing.st_mode):
                    raise ChatTreeError(errno.EEXIST, f"{'/'.join(parts)} is not a link")
                os.unlink(parts[-1], dir_fd=parent)
            os.symlink(points_at, parts[-1], dir_fd=parent)
            if self.identity is not None:
                os.chown(
                    parts[-1],
                    self.identity.uid,
                    self.identity.gid,
                    dir_fd=parent,
                    follow_symlinks=False,
                )

    def readlink(self, path: str | bytes | Path) -> bytes | None:
        """The text of the link at ``path``; ``None`` when it is not a link."""
        parts = self.relative(path)
        try:
            if not _DIR_FDS:
                self._path_check(parts[:-1])
                return os.readlink(os.fsencode(self._path(parts)))
            with self._parent(parts, create=False) as parent:
                return os.readlink(os.fsencode(parts[-1]), dir_fd=parent)
        except (OSError, ChatTreeError):
            return None

    def restore(
        self,
        path: str | bytes | Path,
        *,
        mode: int | None = None,
        mtime_ns: int | None = None,
        xattrs: Mapping[bytes, bytes] | None = None,
    ) -> None:
        """Put a regular file's stored metadata back, through a descriptor on
        it. The mode is the tree's (:func:`file_mode`): the stored one only
        says whether the file is executable, never who may write it."""
        if not _DIR_FDS:
            where = self._path(self.relative(path))
            if mode is not None:
                os.chmod(where, file_mode(mode))
            if mtime_ns is not None:
                os.utime(where, ns=(mtime_ns, mtime_ns))
            return
        with self.open_read(path) as handle:
            fd = handle.fileno()
            if mode is not None:
                self._own(fd, file_mode(mode))
            if xattrs and hasattr(os, "setxattr"):
                for name, value in xattrs.items():
                    if name.startswith(b"user."):
                        os.setxattr(fd, os.fsdecode(name), value)
            if mtime_ns is not None:
                os.utime(fd, ns=(mtime_ns, mtime_ns))

    def samefile(self, first: str | bytes | Path, second: str | bytes | Path) -> bool:
        one = self.stat(first)
        two = self.stat(second)
        return (
            one is not None
            and two is not None
            and (one.st_dev, one.st_ino) == (two.st_dev, two.st_ino)
        )

    # -- repair --------------------------------------------------------

    def repair(self) -> RepairReport:
        """Give every file and directory under the root (the root included)
        to the identity with the tree's modes, links left alone and never
        followed, nothing on another filesystem entered. Specials are
        counted and left."""
        report = RepairReport()
        if not _DIR_FDS:
            return report
        try:
            fd = os.open(self.root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        except FileNotFoundError:
            return report
        try:
            device = os.fstat(fd).st_dev
            self._repair_dir(fd, device, report, depth=0)
        finally:
            os.close(fd)
        return report

    def hand_over(self, *, files: bool = True, when_wrong: bool = False) -> RepairReport:
        """:meth:`repair` for a sandbox launch: root, giving the tree to its
        identity while the agent and the workspace's other processes may be
        renaming inside it. The walk never follows a name out of the tree, and
        the root itself is opened refusing a link (an :class:`OSError`, as is a
        missing root). ``when_wrong`` walks only when the root is not already
        the identity's with :data:`DIR_MODE`: once it is, what is made below
        inherits the group and the setgid bit. ``files`` False repairs the
        directories alone."""
        report = RepairReport()
        if not _DIR_FDS:
            return report
        fd = os.open(self.root, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
        try:
            info = os.fstat(fd)
            mine = self.identity
            right = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            if when_wrong and mine is not None and right == (mine.uid, mine.gid, DIR_MODE):
                return report
            self._repair_dir(fd, info.st_dev, report, depth=0, files=files)
        finally:
            os.close(fd)
        return report

    def _fix(self, fd: int, info: os.stat_result, mode: int, report: RepairReport) -> None:
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            # The same bytes under another name, possibly a host file's; left
            # as they are rather than handed over or re-moded through the link.
            report.skipped += 1
            return
        if self.identity is not None and (info.st_uid, info.st_gid) != (
            self.identity.uid,
            self.identity.gid,
        ):
            os.fchown(fd, self.identity.uid, self.identity.gid)
            report.owners += 1
        if stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(fd, mode)
            report.modes += 1

    def _repair_dir(
        self, fd: int, device: int, report: RepairReport, *, depth: int, files: bool = True
    ) -> None:
        info = os.fstat(fd)
        self._fix(fd, info, DIR_MODE, report)
        if depth >= _MAX_DEPTH:
            report.skipped += 1
            return
        for name in os.listdir(fd):
            try:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if entry.st_dev != device:
                report.skipped += 1
                continue
            if stat.S_ISLNK(entry.st_mode):
                if self.identity is not None and entry.st_uid != self.identity.uid:
                    with contextlib.suppress(OSError):
                        os.chown(
                            name,
                            self.identity.uid,
                            self.identity.gid,
                            dir_fd=fd,
                            follow_symlinks=False,
                        )
                        report.owners += 1
                continue
            if stat.S_ISDIR(entry.st_mode):
                try:
                    below = os.open(
                        name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd
                    )
                except OSError:
                    report.skipped += 1
                    continue
                try:
                    self._repair_dir(below, device, report, depth=depth + 1, files=files)
                finally:
                    os.close(below)
                continue
            if not files:
                continue
            if not stat.S_ISREG(entry.st_mode):
                report.skipped += 1
                continue
            try:
                handle = os.open(
                    name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=fd
                )
            except OSError:
                report.skipped += 1
                continue
            try:
                now = os.fstat(handle)
                if stat.S_ISREG(now.st_mode):
                    self._fix(handle, now, file_mode(now.st_mode), report)
                else:
                    report.skipped += 1
            finally:
                os.close(handle)

    # -- the path fallback (no dir_fd: Windows) ---------------------------

    def _path(self, parts: list[str]) -> Path:
        return self.root.joinpath(*parts)

    def _path_check(self, parts: list[str]) -> None:
        cursor = self.root
        for name in parts:
            cursor = cursor / name
            try:
                info = os.lstat(cursor)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode):
                raise ChatTreeError(errno.ELOOP, f"{'/'.join(parts)} crosses a link at {name}")

    def _path_lstat(self, parts: list[str]) -> os.stat_result | None:
        try:
            self._path_check(parts[:-1])
            return os.lstat(self._path(parts))
        except (FileNotFoundError, NotADirectoryError, ChatTreeError):
            return None

    def _path_open_read(self, parts: list[str]) -> BinaryIO:
        self._path_check(parts)
        where = self._path(parts)
        if not stat.S_ISREG(os.lstat(where).st_mode):
            raise ChatTreeError(errno.EPERM, f"{'/'.join(parts)} is not a regular file")
        return where.open("rb")

    def _path_write(
        self,
        parts: list[str],
        source: bytes | BinaryIO,
        *,
        executable: bool | None,
        over: tuple[int, int] | None = None,
    ) -> bool:
        self._path_check(parts)
        where = self._path(parts)
        where.parent.mkdir(parents=True, exist_ok=True)
        existing = self._path_lstat(parts)
        self._refuse_unless_file(existing, "/".join(parts))
        if executable is None:
            previous = existing.st_mode if existing is not None else None
        else:
            previous = 0o100 if executable else 0
        temporary = where.parent / f".{where.name}.{uuid.uuid4().hex[:8]}{_TEMP_SUFFIX}"
        try:
            with temporary.open("xb") as handle:
                _copy(source, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, file_mode(previous))
            # No atomic swap on the path walk (Windows): the stamp is looked
            # at right before the replace, the narrowest window it leaves.
            if over is not None and not _wears(self._path_lstat(parts), over):
                temporary.unlink()
                return False
            os.replace(temporary, where)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        return True

    @contextlib.contextmanager
    def _path_open_write(self, parts: list[str], *, append: bool) -> Iterator[BinaryIO]:
        self._path_check(parts)
        where = self._path(parts)
        where.parent.mkdir(parents=True, exist_ok=True)
        existing = self._path_lstat(parts)
        self._refuse_unless_file(existing, "/".join(parts))
        if existing is not None and existing.st_nlink > 1:
            raise ChatTreeError(errno.EMLINK, f"{'/'.join(parts)} is a hard link")
        with where.open("ab" if append else "wb") as handle:
            yield handle

    def _path_mkdirs(self, parts: list[str]) -> None:
        self._path_check(parts)
        self._path(parts).mkdir(parents=True, exist_ok=True)

    def _path_unlink(self, parts: list[str], *, missing_ok: bool) -> None:
        self._path_check(parts[:-1])
        where = self._path(parts)
        info = self._path_lstat(parts)
        if info is not None and stat.S_ISDIR(info.st_mode):
            raise ChatTreeError(errno.EISDIR, f"{'/'.join(parts)} is a directory")
        where.unlink(missing_ok=missing_ok)

    def _path_rename(self, here: list[str], there: list[str]) -> None:
        self._path_check(here)
        self._path_check(there[:-1])
        info = self._path_lstat(here)
        if info is None:
            raise FileNotFoundError(errno.ENOENT, "/".join(here))
        self._refuse_unless_file(info, "/".join(here))
        target = self._path_lstat(there)
        if target is not None and stat.S_ISDIR(target.st_mode):
            raise ChatTreeError(errno.EISDIR, f"{'/'.join(there)} is a directory")
        self._path(there).parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._path(here), self._path(there))

    def _path_link(self, here: list[str], there: list[str]) -> None:
        self._path_check(here)
        self._path_check(there[:-1])
        self._refuse_unless_file(self._path_lstat(here), "/".join(here))
        self._path(there).parent.mkdir(parents=True, exist_ok=True)
        os.link(self._path(here), self._path(there))


def _copy(source: bytes | BinaryIO, handle: BinaryIO) -> None:
    if isinstance(source, bytes | bytearray | memoryview):
        handle.write(source)
        return
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            return
        handle.write(chunk)
