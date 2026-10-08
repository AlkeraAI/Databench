"""The filesystem walker behind ``alkera files push``.

The walk is the export half of the round-trip corpus, so it is deliberately
faithful rather than convenient:

* names are **bytes** (``os.fsencode``), because a Latin-1 archive name is not
  decodable and must still push and pull back byte-identical;
* nothing is followed — every ``stat`` is ``follow_symlinks=False``, so a
  looping or dangling link is an entry, not a hang or an error;
* the walk never leaves its root while the tree changes under it: on POSIX it
  descends through directory descriptors opened ``O_NOFOLLOW`` relative to
  their parent and checked against the ``stat`` that classified them, and it
  opens a file only ``O_NOFOLLOW | O_NONBLOCK`` and only while it is still the
  regular file it was, so a directory swapped for a link to ``/`` or a file
  swapped for a fifo is left out of the walk rather than followed or waited on;
* hard links are grouped by ``(st_dev, st_ino)`` whenever ``st_nlink > 1``, so
  a pnpm-style store pushes its bytes once;
* a symlink target is classified against the local root by
  :func:`alkera_core.files.links.classify_link`, so an absolute link into the
  org tree survives the trip to a machine that mounts it elsewhere;
* sidecars and excluded paths are **yielded with a typed reason** rather than
  dropped silently, so a caller can report what it folded.

Everything here is read-only and pure apart from the ``os`` calls; the walker
imports the library's name, link and xattr primitives and nothing from the
store or the API.
"""

from __future__ import annotations

import errno
import fnmatch
import functools
import os
import stat as stat_module
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from alkera_core.files.links import LinkKind, classify_link, strip_extended_prefix
from alkera_core.project import is_local_state

from alkera_cli.files.xattrs import read_xattrs

__all__ = [
    "EXCLUDE_PRESETS",
    "Entry",
    "EntryKind",
    "ExportRules",
    "SkipReason",
    "walk",
]


class EntryKind(StrEnum):
    """What the entry is on disk, decided without following a link."""

    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    SPECIAL = "special"
    """A fifo, socket, or device node: recorded, never uploaded."""


class SkipReason(StrEnum):
    """Why an entry was visited but is not part of the push."""

    SIDECAR = "sidecar"
    """``.DS_Store`` / ``._*``: platform junk, folded per the naming contract."""

    GITIGNORED = "gitignored"
    EXCLUDE_PRESET = "exclude_preset"
    LOCAL_STATE = "local_state"
    """A lock or other per-process artefact that belongs to this machine only —
    see :mod:`alkera_core.project.local_state`. Skipped when the caller asked
    for it, which a folder that travels between machines does."""


#: Directory basenames each ``--exclude-preset`` folds away.
EXCLUDE_PRESETS: Final[dict[str, frozenset[bytes]]] = {
    "venv": frozenset({b".venv", b"venv", b"__pycache__", b".mypy_cache", b".pytest_cache"}),
    "node_modules": frozenset({b"node_modules"}),
    # The tool caches a working directory accumulates when it doubles as the
    # agent's home — a chat's scratch folder does — none of which is the work:
    # an interpreter cache, a package index, a language server's scratch. A held
    # folder folds these by default; a person's push names the preset.
    "caches": frozenset(
        {
            b".uvcache",
            b".uvpython",
            b".cache",
            b".local",
            b".npm",
            b".yarn",
            b".pnpm-store",
            b".ruff_cache",
            b".mypy_cache",
            b".pytest_cache",
            b"__pycache__",
            b".venv",
            b"venv",
            b"node_modules",
            b".tox",
            b".nox",
            b".ipynb_checkpoints",
        }
    ),
}

#: The presets a held folder's live sync applies when its caller names none.
LIVE_SYNC_DEFAULT_PRESETS: Final[tuple[str, ...]] = ("caches",)

#: The repository metadata directory, which a push carries as ordinary nodes.
GIT_DIR: Final = b".git"
_WINDOWS: Final = os.name == "nt"
"""Which spelling of a path this machine produces. Read once, so a test can
drive the other platform's branch without a platform to run it on."""
_SIDECAR_EXACT: Final = frozenset({b".DS_Store"})
_SIDECAR_PREFIX: Final = b"._"


@dataclass(frozen=True, slots=True)
class Entry:
    """One visited path, relative to the walk root.

    ``skipped`` is the typed note: a caller pushes the entries whose
    ``skipped`` is ``None`` and reports the rest.
    """

    relative: bytes
    kind: EntryKind
    mode: int
    """The permission bits (``st_mode & 0o7777``), setuid/setgid included."""
    uid: int
    gid: int
    size: int
    mtime_ns: int
    link_kind: LinkKind | None = None
    link_target: bytes | None = None
    """The *stored* target: canonical links already rewritten to an org path."""
    hardlink_group: str | None = None
    sparse: bool = False
    xattrs: dict[bytes, bytes] = field(default_factory=dict)
    skipped: SkipReason | None = None


def _is_sidecar(name: bytes) -> bool:
    return name in _SIDECAR_EXACT or name.startswith(_SIDECAR_PREFIX)


def _kind_of(mode: int) -> EntryKind:
    if stat_module.S_ISLNK(mode):
        return EntryKind.SYMLINK
    if stat_module.S_ISDIR(mode):
        return EntryKind.DIRECTORY
    if stat_module.S_ISREG(mode):
        return EntryKind.FILE
    return EntryKind.SPECIAL


def _is_sparse(path: bytes, info: os.stat_result, dir_fd: int | None = None) -> bool:
    """Whether the file has a hole.

    ``SEEK_HOLE`` is authoritative where the platform has it (Linux): a hole
    that starts before EOF means the file is sparse. macOS has the concept but
    not the constant, so there the allocated-block count stands in — a file
    holding fewer 512-byte blocks than its length needs has a hole.

    ``path`` is a name relative to ``dir_fd`` when one is given; then the file
    opened must still be the regular file ``info`` describes, or
    :class:`_ReplacedError` is raised.
    """
    size = info.st_size
    if size == 0:
        return False
    if hasattr(os, "SEEK_HOLE"):
        try:
            fd = _open_nofollow(path, dir_fd)
        except OSError as exc:
            if dir_fd is not None and _names_something_else(exc):
                raise _ReplacedError from exc
            return False
        try:
            if dir_fd is not None and not _still_the_file(os.fstat(fd), info):
                raise _ReplacedError
            return os.lseek(fd, 0, os.SEEK_HOLE) < size
        except OSError:
            return False
        finally:
            os.close(fd)
    blocks: int | None = getattr(info, "st_blocks", None)
    if blocks is None:
        return False
    return blocks * 512 < size


_NOFOLLOW_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
"""How the walk opens a file: never through a link, and never waiting for a
writer, so a fifo put at the name cannot hang the walk."""

_DIRECTORY_FLAGS: Final = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)

_FD_WALK: Final = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and os.readlink in os.supports_dir_fd
)
"""Whether this platform walks from directory descriptors (POSIX). Windows
cannot, and keeps the by-path walk: it has no fifos, and no shared box runs it."""


class _ReplacedError(Exception):
    """The name no longer holds what its ``stat`` described."""


#: What ``open`` answers when the name now holds something else: a link under
#: ``O_NOFOLLOW`` (``ELOOP``, ``EMLINK`` on the BSDs), a non-directory under
#: ``O_DIRECTORY``, a socket, or a name that is gone. Any other failure (a full
#: descriptor table, say) is not evidence of a swap and must not drop an entry.
_REPLACED_ERRNOS: Final = frozenset(
    {errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.ENXIO, errno.ENOENT}
)


def _names_something_else(exc: OSError) -> bool:
    return exc.errno in _REPLACED_ERRNOS


def _open_nofollow(name: bytes, dir_fd: int | None) -> int:
    """Open ``name`` read-only without following a link or blocking on a fifo."""
    if dir_fd is None:
        return os.open(name, _NOFOLLOW_FLAGS)
    return os.open(name, _NOFOLLOW_FLAGS, dir_fd=dir_fd)


def _still_the_file(opened: os.stat_result, described: os.stat_result) -> bool:
    return stat_module.S_ISREG(opened.st_mode) and (opened.st_dev, opened.st_ino) == (
        described.st_dev,
        described.st_ino,
    )


def _open_subdirectory(name: bytes, parent_fd: int, described: os.stat_result) -> int | None:
    """A descriptor for the directory ``described``, found at ``name`` in its parent.

    Raises :class:`_ReplacedError` when the name is now a link, a non-directory,
    another directory than the one classified, or gone. ``None`` when it cannot
    be opened for any other reason: its entry still stands and the walk does
    not descend, as a directory it cannot list never did.
    """
    try:
        fd = os.open(name, _DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if _names_something_else(exc):
            raise _ReplacedError from exc
        return None
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (described.st_dev, described.st_ino):
        os.close(fd)
        raise _ReplacedError
    return fd


def _read_regular_file(path: Path) -> bytes | None:
    """The bytes of ``path`` when it is a regular file reached without a link.

    ``None`` for a link, a fifo, a device, or a name that is not there: none
    of those is a file the walk reads, and none may make it wait.
    """
    try:
        fd = _open_nofollow(os.fsencode(path), None)
    except OSError:
        return None
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


class _GitIgnore:
    """A deliberately small ``.gitignore`` matcher — no new dependency.

    Supported: comments and blank lines, a leading ``/`` anchor, a trailing
    ``/`` directory-only pattern, ``!`` negation with last-match-wins, and
    ``fnmatch`` globbing against either the whole relative path or the
    basename.

    Not supported (documented limits, because the fallback is always "push
    it"): nested ``.gitignore`` files below the root, ``**`` as a distinct
    token (``fnmatch``'s ``*`` already crosses separators here, which makes
    this matcher slightly *more* eager than git for a pattern like ``a/*/b``),
    and character-class escapes. A path git would ignore but this matcher does
    not is pushed, never the reverse-critical direction of dropping data.
    """

    def __init__(self, patterns: Sequence[tuple[bytes, bool, bool]]) -> None:
        self._patterns = patterns

    @classmethod
    def load(cls, root: Path) -> _GitIgnore:
        parsed: list[tuple[bytes, bool, bool]] = []
        raw = _read_regular_file(root / ".gitignore")
        if raw is None:
            return cls(())
        for line in raw.split(b"\n"):
            pattern = line.strip()
            if not pattern or pattern.startswith(b"#"):
                continue
            negated = pattern.startswith(b"!")
            if negated:
                pattern = pattern[1:]
            directory_only = pattern.endswith(b"/")
            pattern = pattern.rstrip(b"/").lstrip(b"/")
            if pattern:
                parsed.append((pattern, negated, directory_only))
        return cls(parsed)

    def matches(self, relative: bytes, *, is_dir: bool) -> bool:
        text = os.fsdecode(relative)
        base = os.fsdecode(relative.rsplit(b"/", 1)[-1])
        ignored = False
        for raw_pattern, negated, directory_only in self._patterns:
            if directory_only and not is_dir:
                continue
            pattern = os.fsdecode(raw_pattern)
            if fnmatch.fnmatchcase(text, pattern) or fnmatch.fnmatchcase(base, pattern):
                ignored = not negated
        return ignored


def walk(
    root: Path,
    *,
    local_root: bytes | None = None,
    respect_gitignore: bool = True,
    exclude_presets: Sequence[str] = (),
    skip_local_state: bool = False,
    exclude_presets_within: Mapping[str, Sequence[str]] | None = None,
) -> Iterator[Entry]:
    """Yield every path beneath ``root``, parents before children.

    ``local_root`` is the org tree's mount point on this machine and defaults
    to ``root`` itself: it is what a symlink target is classified against, and
    it is the same value the pull re-anchors a canonical target onto. A walk
    of a subdirectory of a mounted tree passes the mount point, so a link into
    a sibling of that subdirectory still travels as an org path.

    ``respect_gitignore`` only bites when ``root`` is a git working tree (it
    has a ``.git`` entry); ``.git/`` itself is always walked, because a repo
    round-trips through Files as ordinary nodes.

    ``skip_local_state`` folds away the entries a machine keeps about its own
    hold on the tree — the per-chat write lock and its forensic rotations. Off
    by default: a person pushing a directory gets every byte in it. On for a
    folder that is copied between machines, where such a file is at best noise
    and at worst a lock no other host may ever reclaim.

    ``exclude_presets_within`` maps a root-relative directory to presets that
    fold only beneath it, at any depth: a held chat folder leaves the tool
    caches its working directory grows behind without touching the records
    beside it, whose own names are the local-state rules' business.

    Entries are yielded in byte order per directory, so two walks of the same
    tree produce the same sequence. A skipped directory is not descended into.
    """
    root = Path(root)
    directory = os.fsencode(root)
    link_root = directory if local_root is None else local_root
    rules = ExportRules.load(
        root,
        respect_gitignore=respect_gitignore,
        exclude_presets=exclude_presets,
        skip_local_state=skip_local_state,
        exclude_presets_within=exclude_presets_within,
    )
    walk_dir = functools.partial(
        _walk_dir,
        prefix=b"",
        local_root=link_root,
        excluded=rules.excluded,
        ignore=rules.ignore,
        skip_local_state=rules.skip_local_state,
        scoped=rules.scoped,
    )
    if not _FD_WALK:
        yield from walk_dir(directory, None)
        return
    try:
        # The root is the caller's choice and may be reached through a link;
        # nothing beneath it is.
        root_fd = os.open(directory, _DIRECTORY_FLAGS)
    except OSError:
        return
    try:
        yield from walk_dir(directory, root_fd)
    finally:
        os.close(root_fd)


@dataclass(frozen=True, slots=True)
class ExportRules:
    """What an export leaves out, asked of one path rather than of a walk.

    The walk decides a directory's fate once and never descends into one it
    skipped; a caller that meets paths one at a time — a watcher naming what
    changed — has no walk to lean on, so :meth:`skip_reason` asks the same
    question of every folder above the path and then of the path itself. It is
    the walk's own classification, so the two can never disagree about what
    an export carries.
    """

    excluded: frozenset[bytes] = frozenset()
    ignore: _GitIgnore = field(default_factory=lambda: _GitIgnore(()))
    skip_local_state: bool = False
    scoped: tuple[tuple[bytes, frozenset[bytes]], ...] = ()
    """Basenames folded only beneath a root-relative directory, at any depth."""

    @classmethod
    def load(
        cls,
        root: Path,
        *,
        respect_gitignore: bool = True,
        exclude_presets: Sequence[str] = (),
        skip_local_state: bool = False,
        exclude_presets_within: Mapping[str, Sequence[str]] | None = None,
    ) -> ExportRules:
        """The rules a :func:`walk` of ``root`` with these options applies."""
        excluded = _preset_names(exclude_presets)
        scoped = tuple(
            (os.fsencode(prefix.strip("/")), names)
            for prefix, presets in sorted((exclude_presets_within or {}).items())
            if prefix.strip("/") and (names := _preset_names(presets))
        )
        root = Path(root)
        ignore = (
            _GitIgnore.load(root)
            if respect_gitignore and (root / ".git").exists()
            else _GitIgnore(())
        )
        return cls(
            excluded=excluded, ignore=ignore, skip_local_state=skip_local_state, scoped=scoped
        )

    def skip_reason(self, relative: bytes, *, is_dir: bool) -> SkipReason | None:
        """Why an export leaves ``relative`` out, or ``None`` when it carries it.

        A path under a folder the walk would skip is skipped for that folder's
        reason, because the walk never reaches it.
        """
        parts = relative.strip(b"/").split(b"/")
        for depth in range(1, len(parts)):
            reason = _classify_skip(
                parts[depth - 1],
                b"/".join(parts[:depth]),
                EntryKind.DIRECTORY,
                self.excluded,
                self.ignore,
                self.skip_local_state,
                self.scoped,
            )
            if reason is not None:
                return reason
        return _classify_skip(
            parts[-1],
            b"/".join(parts),
            EntryKind.DIRECTORY if is_dir else EntryKind.FILE,
            self.excluded,
            self.ignore,
            self.skip_local_state,
            self.scoped,
        )


def _list_directory(
    directory: bytes, directory_fd: int | None
) -> list[tuple[bytes, os.DirEntry[bytes] | os.DirEntry[str]]]:
    """The directory's children with their byte names, in byte order.

    A listing by descriptor names its entries as ``str``, so the byte name is
    computed once here and the order is the same either way.
    """
    if directory_fd is None:
        with os.scandir(directory) as by_path:
            listed: list[tuple[bytes, os.DirEntry[bytes] | os.DirEntry[str]]] = [
                (item.name, item) for item in by_path
            ]
    else:
        with os.scandir(directory_fd) as by_fd:
            listed = [(os.fsencode(item.name), item) for item in by_fd]
    return sorted(listed, key=lambda pair: pair[0])


def _walk_dir(
    directory: bytes,
    directory_fd: int | None,
    *,
    prefix: bytes,
    local_root: bytes,
    excluded: frozenset[bytes],
    ignore: _GitIgnore,
    skip_local_state: bool,
    scoped: tuple[tuple[bytes, frozenset[bytes]], ...] = (),
) -> Iterator[Entry]:
    """Walk one directory, through ``directory_fd`` when one is given.

    ``directory`` is the path spelling, kept for what only a path can serve
    (the xattr calls off Linux, and the by-path walk on Windows). A child
    directory is opened, and checked to be the one its ``stat`` described,
    before its entry is yielded: a consumer that runs between an entry and
    its descent cannot redirect the descent either.
    """
    try:
        children = _list_directory(directory, directory_fd)
    except OSError:
        return
    for name, child in children:
        relative = prefix + b"/" + name if prefix else name
        path = os.path.join(directory, name)
        sub_fd: int | None = None
        descend = False
        try:
            info = child.stat(follow_symlinks=False)
            kind = _kind_of(info.st_mode)
            skipped = _classify_skip(
                name, relative, kind, excluded, ignore, skip_local_state, scoped
            )
            descend = kind is EntryKind.DIRECTORY and skipped is None
            if descend and directory_fd is not None:
                sub_fd = _open_subdirectory(name, directory_fd, info)
                descend = sub_fd is not None
            entry = _entry(path, relative, kind, info, local_root, skipped, directory_fd, name)
        except (FileNotFoundError, _ReplacedError):
            # The tree is live: an entry removed between the listing and the
            # last of the reads that describe it (its stat, its link target,
            # its xattrs) is simply not part of this walk; the next one will
            # not list it. One replaced since its stat (a directory by a link,
            # a file by a fifo) is left out the same way, never followed.
            if sub_fd is not None:
                os.close(sub_fd)
            continue
        except BaseException:
            if sub_fd is not None:
                os.close(sub_fd)
            raise
        if not descend:
            yield entry
            continue
        try:
            yield entry
            yield from _walk_dir(
                path,
                sub_fd,
                prefix=relative,
                local_root=local_root,
                excluded=excluded,
                ignore=ignore,
                skip_local_state=skip_local_state,
                scoped=scoped,
            )
        finally:
            if sub_fd is not None:
                os.close(sub_fd)


def _classify_skip(
    name: bytes,
    relative: bytes,
    kind: EntryKind,
    excluded: frozenset[bytes],
    ignore: _GitIgnore,
    skip_local_state: bool = False,
    scoped: tuple[tuple[bytes, frozenset[bytes]], ...] = (),
) -> SkipReason | None:
    if skip_local_state and is_local_state(relative):
        return SkipReason.LOCAL_STATE
    if _is_sidecar(name):
        return SkipReason.SIDECAR
    if name in excluded:
        return SkipReason.EXCLUDE_PRESET
    for prefix, names in scoped:
        if name in names and relative.startswith(prefix + b"/"):
            return SkipReason.EXCLUDE_PRESET
    if _inside_repository_metadata(relative):
        # git never consults .gitignore inside a repository's own metadata, so
        # neither may a push: ordinary root-level patterns (`logs/`, `info/`,
        # `tmp/`, `*.pack`) name real files under the metadata directory, and
        # pruning them ships a repository the far side cannot `fsck`.
        return None
    if ignore.matches(relative, is_dir=kind is EntryKind.DIRECTORY):
        return SkipReason.GITIGNORED
    return None


def _preset_names(presets: Sequence[str]) -> frozenset[bytes]:
    names: frozenset[bytes] = frozenset()
    for preset in presets:
        names |= EXCLUDE_PRESETS.get(preset, frozenset())
    return names


def _inside_repository_metadata(relative: bytes) -> bool:
    """Whether ``relative`` is a repository metadata directory, or under one.

    Any depth, because a walk root can hold nested repositories and git's rule
    is per-repository, not per-walk.
    """
    return GIT_DIR in relative.split(b"/")


def _xattr_path(path: bytes, dir_fd: int | None, name: bytes) -> Path:
    """The path the xattr calls read, anchored to ``dir_fd`` where it can be.

    The xattr calls take a path and refuse a link only at its last component.
    On Linux ``/proc/self/fd/<fd>`` is the open directory itself, so no
    ancestor swapped since the walk opened it can redirect the read.
    """
    if dir_fd is not None and sys.platform == "linux":
        proc = os.fsencode(f"/proc/self/fd/{dir_fd}")
        if os.path.isdir(proc):
            return Path(os.fsdecode(os.path.join(proc, name)))
    return Path(os.fsdecode(path))


def _entry(
    path: bytes,
    relative: bytes,
    kind: EntryKind,
    info: os.stat_result,
    local_root: bytes,
    skipped: SkipReason | None,
    dir_fd: int | None = None,
    name: bytes = b"",
) -> Entry:
    """Describe one child; ``name`` is relative to ``dir_fd`` when one is given."""
    link_kind: LinkKind | None = None
    link_target: bytes | None = None
    group: str | None = None
    sparse = False
    if kind is EntryKind.SYMLINK:
        # Windows reports a symlink's substitute name: extended-length
        # prefixed, and spelled with its own separators and drive. The
        # classification has to read the target and the root the way the
        # platform that produced them writes them, or an absolute link into
        # the tree degrades to `relative` and travels with this machine's
        # path in it.
        target = os.readlink(path) if dir_fd is None else os.readlink(name, dir_fd=dir_fd)
        link_kind, link_target = classify_link(
            strip_extended_prefix(target, windows=_WINDOWS),
            strip_extended_prefix(local_root, windows=_WINDOWS),
            windows=_WINDOWS,
        )
    elif kind is EntryKind.FILE and skipped is None:
        if info.st_nlink > 1:
            group = f"{info.st_dev}:{info.st_ino}"
        if dir_fd is None:
            sparse = _is_sparse(path, info)
        else:
            sparse = _is_sparse(name, info, dir_fd)
    return Entry(
        relative=relative,
        kind=kind,
        mode=stat_module.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        link_kind=link_kind,
        link_target=link_target,
        hardlink_group=group,
        sparse=sparse,
        xattrs={} if skipped is not None else read_xattrs(_xattr_path(path, dir_fd, name)),
        skipped=skipped,
    )
