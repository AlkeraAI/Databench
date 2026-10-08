"""Reading and writing a tree other people can also write, never through a link.

A workspace tree is written by the notebook's people and agents (from any
cell, as the kernel's uid) as well as by the engine, which may run as a
more privileged uid than the kernel. A link placed in the tree must never
make the engine read, write, mode or delete anything outside it.

:class:`Tree` is the one way the engine touches such a tree. It is anchored
at a trusted root (chosen by the host, never by the tree's writers) and
reaches every entry below it one name at a time, each directory opened
relative to its parent with ``O_NOFOLLOW | O_DIRECTORY``: no component, the
last one included, is ever resolved through a link. A link, or an entry of
the wrong kind (a directory where a file goes, a fifo where a file is read),
raises :class:`LinkRefusedError` and nothing is changed. Files are written
to a fresh temporary name (``O_CREAT | O_EXCL | O_NOFOLLOW``) and renamed
into place relative to the same directory, so an existing file (or a hard
link to someone else's) is replaced, never written through.

Where the platform has no ``dir_fd`` support (Windows, where the tree is a
laptop's and one uid's), each component is checked with ``lstat`` instead.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from pathlib import Path, PurePath, PurePosixPath
from typing import BinaryIO

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

#: Whether every directory can be walked by descriptor. Where it cannot, each
#: component is checked by ``lstat`` before it is used.
USES_DIR_FD = bool(
    _O_NOFOLLOW
    and _O_DIRECTORY
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
)

# Errors a no-follow open gives for a link (ELOOP; EMLINK on the BSDs) or for
# a non-directory opened as one (ENOTDIR).
_REFUSED_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})


class LinkRefusedError(OSError):
    """A path in the tree is a link, or not the kind of entry asked for."""

    def __init__(self, path: PurePath | str, what: str = "is a link") -> None:
        super().__init__(errno.ELOOP, f"{path} {what}; it is not followed", str(path))


@dataclasses.dataclass(frozen=True)
class TreeModes:
    """The modes the registry gives what it writes for a build to read.

    Where builds run as a uid other than the registry's (a sandboxed build uid
    that shares the tree's group and nothing else), a spec directory the
    registry made under its own umask (owner-only) is one the build cannot
    even enter. With modes set, every directory the registry makes or holds
    on a build's path gets ``directory`` (a setgid ``2770`` keeps what the build makes inside the
    group's) and every file it writes there ``file``. ``None`` (a laptop, one
    uid) leaves both to the umask."""

    directory: int
    file: int


def chmod_owned(fd: int, mode: int) -> None:
    """Only what this uid owns (another uid's entry is its own to mode,
    and a chmod of it would be refused anyway) and only when it differs."""
    info = os.fstat(fd)
    if info.st_uid == os.geteuid() and (info.st_mode & 0o7777) != mode:
        os.fchmod(fd, mode)


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


class _Dir:
    """One open directory of the tree: a descriptor, or (without ``dir_fd``
    support) a path whose every component was checked."""

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self.fd = fd

    def name(self, name: str) -> str:
        return name if self.fd is not None else str(self.path / name)

    def kw(self) -> dict[str, int]:
        return {} if self.fd is None else {"dir_fd": self.fd}

    def lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(self.name(name), follow_symlinks=False, **self.kw())
        except FileNotFoundError:
            return None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Tree:
    """Everything below ``root``, reached without following a link.

    ``root`` itself is trusted: the host names it. Paths given to a method are
    relative to it (a ``str`` in POSIX form, or any path; an absolute one must
    lie under ``root`` as written). ``..``, ``.`` and empty names are refused.
    With ``modes``, every directory walked or made gets ``modes.directory``
    and every file written ``modes.file`` (see :class:`TreeModes`)."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        modes: TreeModes | None = None,
        new_dir_mode: int = 0o777,
    ) -> None:
        self.root = Path(root)
        self.modes = modes
        #: The mode a directory this tree makes is created with (under the umask).
        self.new_dir_mode = new_dir_mode

    def __repr__(self) -> str:
        return f"Tree({str(self.root)!r})"

    # -- paths -------------------------------------------------------------------

    def parts(self, rel: str | os.PathLike[str]) -> tuple[str, ...]:
        """``rel``'s names below the root, each one checked."""
        path = PurePosixPath(rel) if isinstance(rel, str) else PurePath(rel)
        if path.is_absolute():
            try:
                path = PurePath(path).relative_to(self.root)
            except ValueError:
                raise ValueError(f"{rel} is outside {self.root}") from None
        names = tuple(path.parts)
        for name in names:
            if name in ("", ".", "..") or "/" in name or "\0" in name or (os.sep in name):
                raise ValueError(f"{rel} is not a path inside the tree")
        return names

    def path(self, rel: str | os.PathLike[str]) -> Path:
        """Where ``rel`` is (for messages and for handing to another process
        that runs as the tree's own uid); never opened by this process."""
        return self.root.joinpath(*self.parts(rel))

    # -- walking ------------------------------------------------------------------

    def _open_root(self, *, create: bool) -> _Dir:
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if not USES_DIR_FD:
            if not self.root.is_dir():
                raise FileNotFoundError(errno.ENOENT, "no such directory", str(self.root))
            return _Dir(self.root, None)
        return _Dir(self.root, os.open(self.root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC))

    def _child(self, parent: _Dir, name: str, *, create: bool) -> _Dir:
        where = parent.path / name
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(parent.name(name), self.new_dir_mode, **parent.kw())
        if parent.fd is None:
            info = parent.lstat(name)
            if info is None:
                raise FileNotFoundError(errno.ENOENT, "no such directory", str(where))
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise LinkRefusedError(where, "is a link or not a directory")
            return _Dir(where, None)
        try:
            fd = os.open(
                name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent.fd
            )
        except OSError as exc:
            if exc.errno in _REFUSED_ERRNOS:
                raise LinkRefusedError(where, "is a link or not a directory") from None
            raise
        if create and self.modes is not None:
            try:
                chmod_owned(fd, self.modes.directory)
            except BaseException:
                os.close(fd)
                raise
        return _Dir(where, fd)

    @contextlib.contextmanager
    def _walk(self, names: tuple[str, ...], *, create: bool) -> Iterator[_Dir]:
        """The directory ``names`` names, opened one name at a time."""
        current = self._open_root(create=create)
        try:
            for name in names:
                child = self._child(current, name, create=create)
                current.close()
                current = child
            yield current
        finally:
            current.close()

    def _split(self, rel: str | os.PathLike[str]) -> tuple[tuple[str, ...], str]:
        names = self.parts(rel)
        if not names:
            raise ValueError("the tree's root is not a file")
        return names[:-1], names[-1]

    # -- reading ----------------------------------------------------------------

    def lstat(self, rel: str | os.PathLike[str]) -> os.stat_result | None:
        """``rel``'s own entry (a link is reported, not followed), or None when
        it or a directory on its way is missing."""
        names = self.parts(rel)
        if not names:
            return os.stat(self.root)
        try:
            with self._walk(names[:-1], create=False) as d:
                return d.lstat(names[-1])
        except FileNotFoundError:
            return None

    def is_file(self, rel: str | os.PathLike[str]) -> bool:
        info = self.lstat(rel)
        return info is not None and stat.S_ISREG(info.st_mode) and not _is_link(info)

    def is_dir(self, rel: str | os.PathLike[str]) -> bool:
        info = self.lstat(rel)
        return info is not None and stat.S_ISDIR(info.st_mode) and not _is_link(info)

    def exists(self, rel: str | os.PathLike[str]) -> bool:
        return self.lstat(rel) is not None

    def read_bytes(self, rel: str | os.PathLike[str]) -> bytes:
        """A regular file's bytes. A link or any other kind of entry is
        refused; a missing one raises ``FileNotFoundError``."""
        parents, leaf = self._split(rel)
        with self._walk(parents, create=False) as d:
            if d.fd is None:
                info = d.lstat(leaf)
                if info is not None and (_is_link(info) or not stat.S_ISREG(info.st_mode)):
                    raise LinkRefusedError(d.path / leaf, "is a link or not a file")
            flags = (
                os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                fd = os.open(d.name(leaf), flags, **d.kw())
            except OSError as exc:
                if exc.errno in _REFUSED_ERRNOS:
                    raise LinkRefusedError(d.path / leaf, "is a link or not a file") from None
                raise
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise LinkRefusedError(d.path / leaf, "is not a file")
                return fh.read()

    def read_text(self, rel: str | os.PathLike[str], *, encoding: str = "utf-8") -> str:
        return self.read_bytes(rel).decode(encoding)

    def files(self, rel: str | os.PathLike[str] = "") -> list[str]:
        """The names of the regular files directly in ``rel`` (links and
        directories left out); empty when it is missing."""
        return self._entries(rel, stat.S_ISREG)

    def dirs(self, rel: str | os.PathLike[str] = "") -> list[str]:
        """The names of the directories directly in ``rel`` (links left out);
        empty when it is missing."""
        return self._entries(rel, stat.S_ISDIR)

    def _entries(self, rel: str | os.PathLike[str], kind: Callable[[int], bool]) -> list[str]:
        try:
            with self._walk(self.parts(rel), create=False) as d:
                out: list[str] = []
                with os.scandir(d.fd if d.fd is not None else d.path) as it:
                    for entry in it:
                        info = entry.stat(follow_symlinks=False)
                        if not _is_link(info) and kind(info.st_mode):
                            out.append(entry.name)
                return sorted(out)
        except FileNotFoundError:
            return []

    # -- writing ----------------------------------------------------------------

    def make_dirs(self, rel: str | os.PathLike[str]) -> None:
        """``rel`` and every directory on its way, made when missing."""
        with self._walk(self.parts(rel), create=True):
            pass

    def write_atomic(
        self, rel: str | os.PathLike[str], data: bytes, *, mode: int | None = None
    ) -> None:
        """``data`` at ``rel`` in one rename: a reader sees the old file or
        the new one. Missing directories on the way are made. ``mode`` is the
        new file's (under the umask; ``modes.file`` when the tree has modes,
        else ``0o666``)."""
        parents, leaf = self._split(rel)
        if mode is None:
            mode = self.modes.file if self.modes is not None else 0o666
        with self._walk(parents, create=True) as d:
            tmp = f".{leaf}.{secrets.token_hex(6)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
            fd = os.open(d.name(tmp), flags, mode, **d.kw())
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    if self.modes is not None:
                        chmod_owned(fh.fileno(), self.modes.file)
                    os.fsync(fh.fileno())
                self._replace_in(d, tmp, d, leaf)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(d.name(tmp), **d.kw())
                raise

    def write_text(
        self, rel: str | os.PathLike[str], text: str, *, mode: int | None = None
    ) -> None:
        """:meth:`write_atomic` of ``text`` in UTF-8, line endings as given."""
        self.write_atomic(rel, text.encode("utf-8"), mode=mode)

    def create(self, rel: str | os.PathLike[str], *, mode: int = 0o666) -> BinaryIO:
        """A new file at ``rel`` opened for writing; an entry already there
        (a link included) raises ``FileExistsError``. Its directory must
        exist."""
        parents, leaf = self._split(rel)
        with self._walk(parents, create=False) as d:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
            fd = os.open(d.name(leaf), flags, mode, **d.kw())
        return os.fdopen(fd, "wb")

    def share_file(self, rel: str | os.PathLike[str]) -> None:
        """An existing regular file given ``modes.file``; a link, another kind
        of entry or a missing file is left as it is."""
        if self.modes is None:
            return
        parents, leaf = self._split(rel)
        with self._walk(parents, create=False) as d:
            if d.fd is None:
                return
            try:
                fd = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=d.fd)
            except OSError:
                return
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    chmod_owned(fd, self.modes.file)
            finally:
                os.close(fd)

    def symlink(self, target: str, rel: str | os.PathLike[str]) -> None:
        """A link at ``rel`` that reads ``target`` (made, never followed)."""
        parents, leaf = self._split(rel)
        with self._walk(parents, create=True) as d:
            if d.fd is None:
                os.symlink(target, d.name(leaf))
            else:
                os.symlink(target, leaf, dir_fd=d.fd)

    @staticmethod
    def _replace_in(src: _Dir, src_name: str, dst: _Dir, dst_name: str) -> None:
        if src.fd is not None and dst.fd is not None:
            os.replace(src_name, dst_name, src_dir_fd=src.fd, dst_dir_fd=dst.fd)
            return
        if dst.fd is None:
            info = dst.lstat(dst_name)
            if info is not None and stat.S_ISDIR(info.st_mode) and not _is_link(info):
                raise IsADirectoryError(errno.EISDIR, "is a directory", str(dst.path / dst_name))
        os.replace(src.name(src_name), dst.name(dst_name))

    def replace(self, src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        """Rename ``src`` to ``dst`` (whatever ``dst`` was, a link included,
        is replaced, not followed). Both directories must exist."""
        src_parents, src_leaf = self._split(src)
        dst_parents, dst_leaf = self._split(dst)
        with (
            self._walk(src_parents, create=False) as s,
            self._walk(dst_parents, create=False) as d,
        ):
            self._replace_in(s, src_leaf, d, dst_leaf)

    # -- removing ---------------------------------------------------------------

    def unlink(self, rel: str | os.PathLike[str], *, missing_ok: bool = True) -> None:
        """Remove the entry ``rel`` (a link is removed, not what it names)."""
        parents, leaf = self._split(rel)
        try:
            with self._walk(parents, create=False) as d:
                os.unlink(d.name(leaf), **d.kw())
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self, rel: str | os.PathLike[str]) -> None:
        """Remove the empty directory ``rel``."""
        parents, leaf = self._split(rel)
        with self._walk(parents, create=False) as d:
            os.rmdir(d.name(leaf), **d.kw())

    def rmtree(self, rel: str | os.PathLike[str], *, missing_ok: bool = True) -> None:
        """Remove ``rel`` and everything in it. A link inside is removed as a
        link; nothing it names is touched. A link at ``rel`` itself is
        removed the same way."""
        parents, leaf = self._split(rel)
        try:
            with self._walk(parents, create=False) as d:
                info = d.lstat(leaf)
                if info is None:
                    raise FileNotFoundError(errno.ENOENT, "no such entry", str(d.path / leaf))
                if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                    os.unlink(d.name(leaf), **d.kw())
                    return
                self._empty(d, leaf)
                os.rmdir(d.name(leaf), **d.kw())
        except FileNotFoundError:
            if not missing_ok:
                raise

    def _empty(self, parent: _Dir, name: str) -> None:
        d = self._child(parent, name, create=False)
        try:
            with os.scandir(d.fd if d.fd is not None else d.path) as it:
                entries = [(e.name, e.stat(follow_symlinks=False)) for e in it]
            for child, info in entries:
                if stat.S_ISDIR(info.st_mode) and not _is_link(info):
                    self._empty(d, child)
                    os.rmdir(d.name(child), **d.kw())
                else:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(d.name(child), **d.kw())
        finally:
            d.close()


def tree_of(path: str | os.PathLike[str]) -> tuple[Tree, str]:
    """A tree rooted at ``path``'s own directory and ``path``'s name in it:
    for a caller holding one file and no wider root (a laptop's CLI)."""
    p = Path(path)
    return Tree(p.parent), p.name


__all__ = [
    "USES_DIR_FD",
    "LinkRefusedError",
    "Tree",
    "TreeModes",
    "chmod_owned",
    "tree_of",
]
