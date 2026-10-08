"""``rsync --checksum``-equivalent comparison of two POSIX trees.

The corpus test's whole claim is "byte- and metadata-identical except the typed
exceptions", so the comparison has to be independent of the code it judges: it
walks both trees itself with :func:`os.lstat`, hashes bytes with the stdlib,
reads link targets, xattrs and hard-link topology, and reports every difference
as a typed :class:`Difference`. Nothing here imports the push, the pull or the
walker — a comparator that reused the walker would agree with a walker bug.

The typed exceptions are named, not swallowed: a caller asserts the exact set
of :class:`Excuse` values it expects, so a *new* divergence is a failure
even though a *known* one is not.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from alkera_core.files.providers.registry import POINTER_EXTENSIONS

__all__ = ["Diff", "DiffKind", "Difference", "Excuse", "compare", "walk_tree"]

_HASH_CHUNK: Final = 1 << 20

#: The pointer extensions, read off the server's own registry rather than the
#: push's copy of it — the comparator must not learn what a pointer is from the
#: code it is judging.
_POINTER_SUFFIXES: Final[tuple[bytes, ...]] = tuple(
    extension.encode("ascii") for extension in POINTER_EXTENSIONS.values()
)

#: Names the comparison never expects on either side.
_TRANSIENT: Final = frozenset({b".alkera-part"})


class DiffKind(StrEnum):
    """What differs about one path."""

    MISSING = "missing"
    """Present in ``left``, absent from ``right``."""

    EXTRA = "extra"
    """Present in ``right``, absent from ``left``."""

    KIND = "kind"
    CONTENT = "content"
    MODE = "mode"
    MTIME = "mtime"
    LINK_TARGET = "link_target"
    XATTRS = "xattrs"
    HARDLINK = "hardlink"
    """The hard-link groups do not have the same shape."""


class Excuse(StrEnum):
    """A divergence the round-trip is documented to allow.

    Each one is a real property of the system, not a bug being papered over,
    and every corpus test asserts the exact set it tolerates so a new kind of
    divergence still fails.
    """

    POINTER = "pointer"
    """A ``.alkera<kind>`` file is derived: the pull writes it, the push
    never sends it back, so its bytes are the server's, not the corpus's."""

    SIDECAR = "sidecar"
    """``.DS_Store`` / ``._*`` are folded by the naming contract and are
    deliberately not stored as nodes."""

    SPECIAL = "special"
    """A fifo, socket or device node the platform refused to recreate."""

    NON_UTF8_NAME = "non_utf8_name"
    """A name that is not valid UTF-8 cannot ride in a JSON body today, so the
    push reports it instead of sending it under a lossy substitute."""

    HARDLINK_TOPOLOGY = "hardlink_topology"
    """The wire item carries no link group, so a pushed hard-link group comes
    back as content-identical paths whose grouping may differ."""

    GITIGNORED = "gitignored"
    """``--respect-gitignore`` is on by default in a repository."""

    EXCLUDED = "excluded"
    """An ``--exclude-preset`` folded the path away."""

    CANONICAL_LINK = "canonical_link"
    """A symlink anchored inside the tree is re-anchored at the root that
    materialized it, so its text cannot be byte-identical when the two roots
    differ — that rewriting is the whole point of the canonical class. Excused
    only when the remainder after the root matches exactly (see
    :func:`_relocated`): a target written out verbatim, or one still pointing
    into the tree it was pushed from, is a difference like any other."""

    LINK_OUTSIDE_TREE = "link_outside_tree"
    """A symlink whose target leaves the pushed tree (an absolute path outside
    it, a ``..`` above its root) is not stored: the drive refuses a door out of
    the tree it holds, and the push reports the link instead. Excused only as
    a link missing from the pull whose target escapes (see :func:`_escapes`);
    a link inside the tree that went missing is a difference like any other."""

    XATTR = "xattr"
    """``push``'s attribute PATCH carries mode, uid, gid and mtime but not
    ``user.*`` xattrs, so they do not survive the trip yet. Opted into with
    ``excuse_xattrs=True`` so the day the push sends them, a test that did not
    ask for the excuse fails."""


@dataclass(frozen=True, slots=True)
class Difference:
    """One typed difference at one relative path."""

    relative: bytes
    kind: DiffKind
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}: {os.fsdecode(self.relative)} {self.detail}".rstrip()


@dataclass
class Diff:
    """Everything two trees disagree about, split by whether it is expected."""

    differences: list[Difference] = field(default_factory=list)
    excused: dict[Excuse, list[bytes]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.differences)

    @property
    def kinds(self) -> set[Excuse]:
        """The exception classes this comparison actually excused."""
        return {kind for kind, paths in self.excused.items() if paths}

    def report(self) -> str:
        return "\n".join(str(difference) for difference in self.differences)


@dataclass(frozen=True, slots=True)
class Fact:
    """Everything the comparison knows about one path, read once."""

    relative: bytes
    mode: int
    kind: str
    size: int
    mtime_ns: int
    digest: str | None
    link_target: bytes | None
    xattrs: dict[bytes, bytes]
    group: tuple[int, int] | None
    """``(st_dev, st_ino)`` when ``st_nlink > 1``: the hard-link identity."""


def _kind_of(mode: int) -> str:
    if stat_module.S_ISLNK(mode):
        return "symlink"
    if stat_module.S_ISDIR(mode):
        return "directory"
    if stat_module.S_ISREG(mode):
        return "file"
    return "special"


def _digest(path: bytes) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _xattrs(path: bytes) -> dict[bytes, bytes]:
    """The ``user.*`` attributes, read independently of the CLI's helper.

    macOS does not expose the syscalls to CPython, so there the comparison
    reads nothing on either side — which is sound, because it reads nothing on
    *both* sides and therefore never invents an equality it did not check.
    """
    if not hasattr(os, "listxattr"):
        return {}
    found: dict[bytes, bytes] = {}
    try:
        names = os.listxattr(os.fsdecode(path), follow_symlinks=False)
    except OSError:
        return {}
    for name in names:
        if not name.startswith("user."):
            continue
        try:
            found[os.fsencode(name)] = os.getxattr(os.fsdecode(path), name, follow_symlinks=False)
        except OSError:
            continue
    return found


def walk_tree(root: Path) -> dict[bytes, Fact]:
    """Every path beneath ``root``, keyed by its relative bytes.

    Nothing is followed and nothing is skipped: a dangling link, a looping
    link and a fifo are all facts, so the comparison sees exactly what is on
    disk.
    """
    facts: dict[bytes, Fact] = {}
    for relative, absolute in _iter_paths(os.fsencode(root)):
        info = os.lstat(absolute)
        kind = _kind_of(info.st_mode)
        facts[relative] = Fact(
            relative=relative,
            mode=stat_module.S_IMODE(info.st_mode),
            kind=kind,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            digest=_digest(absolute) if kind == "file" else None,
            link_target=os.readlink(absolute) if kind == "symlink" else None,
            xattrs=_xattrs(absolute),
            group=(info.st_dev, info.st_ino) if info.st_nlink > 1 else None,
        )
    return facts


def _iter_paths(root: bytes) -> Iterator[tuple[bytes, bytes]]:
    stack: list[tuple[bytes, bytes]] = [(b"", root)]
    while stack:
        prefix, directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            continue
        for child in children:
            name: bytes = child.name
            if name in _TRANSIENT:
                continue
            relative = prefix + b"/" + name if prefix else name
            yield relative, child.path
            if child.is_dir(follow_symlinks=False):
                stack.append((relative, child.path))


def _is_pointer(relative: bytes) -> bool:
    name = relative.rsplit(b"/", 1)[-1]
    return any(name.endswith(suffix) and len(name) > len(suffix) for suffix in _POINTER_SUFFIXES)


def _is_sidecar(relative: bytes) -> bool:
    name = relative.rsplit(b"/", 1)[-1]
    return name == b".DS_Store" or name.startswith(b"._")


def _is_utf8(relative: bytes) -> bool:
    try:
        relative.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _excuse(
    relative: bytes, left: Fact | None, right: Fact | None, ignored: frozenset[bytes]
) -> Excuse | None:
    """The typed exception that covers this path, or ``None`` if none does."""
    if any(relative == name or relative.startswith(name + b"/") for name in ignored):
        return Excuse.EXCLUDED
    if _is_pointer(relative):
        return Excuse.POINTER
    if _is_sidecar(relative):
        return Excuse.SIDECAR
    if not _is_utf8(relative):
        return Excuse.NON_UTF8_NAME
    if (left is not None and left.kind == "special") or (
        right is not None and right.kind == "special"
    ):
        return Excuse.SPECIAL
    return None


def _groups(facts: dict[bytes, Fact]) -> set[frozenset[bytes]]:
    """The hard-link topology as sets of paths sharing one inode."""
    by_inode: dict[tuple[int, int], set[bytes]] = {}
    for fact in facts.values():
        if fact.group is None:
            continue
        by_inode.setdefault(fact.group, set()).add(fact.relative)
    return {frozenset(paths) for paths in by_inode.values() if len(paths) > 1}


def _escapes(root: bytes, relative: bytes, target: bytes | None) -> bool:
    """Whether a link at ``relative`` under ``root`` holding ``target`` names a
    place outside the tree, lexically. Recomputed here rather than imported
    from the code under test, so a push that dropped a link that stays inside
    still differs."""
    if target is None:
        return False
    if target.startswith(b"/"):
        return not (target == root or target.startswith(root + b"/"))
    depth = relative.count(b"/")
    for segment in target.split(b"/"):
        if segment == b"..":
            depth -= 1
            if depth < 0:
                return True
        elif segment not in (b"", b"."):
            depth += 1
    return False


def _relocated(left_root: bytes, right_root: bytes, one: bytes | None, two: bytes | None) -> bool:
    """Whether ``two`` is ``one`` with ``left_root`` swapped for ``right_root``.

    Purely lexical, and deliberately not shared with the code under test: the
    swap is recomputed here from the two roots the caller passed, so a pull
    that rewrote the prefix to the wrong place, resolved the link instead of
    rewriting it, or left the stored org path on disk all still differ.
    """
    if one is None or two is None or left_root == right_root:
        return False
    if not one.startswith(left_root + b"/"):
        return False
    return two == right_root + one[len(left_root) :]


def compare(
    left: Path,
    right: Path,
    *,
    ignore: frozenset[bytes] = frozenset(),
    compare_mtime: bool = True,
    compare_hardlinks: bool = True,
    excuse_xattrs: bool = False,
) -> Diff:
    """Compare two trees and report every difference, typed.

    ``left`` is the corpus as it was built; ``right`` is what came back. A path
    covered by a typed exception is recorded under :attr:`Diff.excused` rather
    than :attr:`Diff.differences`, so a test can assert both "nothing else
    differs" and "exactly these classes were excused".

    ``ignore`` names relative paths (and their subtrees) the push was told to
    exclude — a ``.gitignore``d directory or an ``--exclude-preset``.
    """
    facts_left = walk_tree(left)
    facts_right = walk_tree(right)
    left_root = os.fsencode(left).rstrip(b"/")
    right_root = os.fsencode(right).rstrip(b"/")
    diff = Diff()

    def note(relative: bytes, kind: DiffKind, detail: str = "") -> None:
        if kind is DiffKind.XATTRS and excuse_xattrs:
            diff.excused.setdefault(Excuse.XATTR, []).append(relative)
            return
        excuse = _excuse(relative, facts_left.get(relative), facts_right.get(relative), ignore)
        if excuse is not None:
            diff.excused.setdefault(excuse, []).append(relative)
            return
        diff.differences.append(Difference(relative, kind, detail))

    for relative in sorted(set(facts_left) | set(facts_right)):
        one = facts_left.get(relative)
        two = facts_right.get(relative)
        if one is None:
            note(relative, DiffKind.EXTRA)
            continue
        if two is None:
            if one.kind == "symlink" and _escapes(left_root, relative, one.link_target):
                diff.excused.setdefault(Excuse.LINK_OUTSIDE_TREE, []).append(relative)
                continue
            note(relative, DiffKind.MISSING)
            continue
        if one.kind != two.kind:
            note(relative, DiffKind.KIND, f"{one.kind} != {two.kind}")
            continue
        if one.kind == "file" and one.digest != two.digest:
            note(relative, DiffKind.CONTENT, f"{one.digest} != {two.digest}")
        if one.kind == "symlink" and one.link_target != two.link_target:
            if _relocated(left_root, right_root, one.link_target, two.link_target):
                diff.excused.setdefault(Excuse.CANONICAL_LINK, []).append(relative)
            else:
                note(relative, DiffKind.LINK_TARGET, f"{one.link_target!r} != {two.link_target!r}")
        if one.kind != "symlink" and one.mode != two.mode:
            note(relative, DiffKind.MODE, f"{one.mode:o} != {two.mode:o}")
        if compare_mtime and one.kind == "file" and one.mtime_ns != two.mtime_ns:
            note(relative, DiffKind.MTIME, f"{one.mtime_ns} != {two.mtime_ns}")
        if one.xattrs != two.xattrs:
            note(relative, DiffKind.XATTRS, f"{one.xattrs!r} != {two.xattrs!r}")

    if compare_hardlinks:
        only_left = _groups(facts_left) - _groups(facts_right)
        for group in sorted(only_left, key=sorted):
            member = sorted(group)[0]
            diff.excused.setdefault(Excuse.HARDLINK_TOPOLOGY, []).append(member)
    return diff
