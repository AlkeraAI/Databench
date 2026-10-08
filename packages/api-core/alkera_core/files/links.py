"""Symlink classification and relocation.

A symlink's target is classified at export and rewritten at materialization. The two
functions here are exact inverses and are **purely lexical**: they never touch the
filesystem, never resolve a symlinked component, and never collapse `.`, `..` or a
repeated separator. Only a separator-bounded prefix is rewritten, so a target that
escapes the root through `..` stays canonical with its `..` intact and is resolved by
the kernel on the machine that materialized it, exactly as it was on the machine that
exported it.

Both take the path flavour from the caller rather than reading the running platform:
the server classifies POSIX targets whichever host it runs on, and only a client on
Windows asks for the Windows spellings. That matters for correctness, not just for
tests — a POSIX name may legitimately contain a backslash, a colon, or the bytes of an
extended-length prefix, and none of them may change what a POSIX target classifies as.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from enum import StrEnum

__all__ = [
    "LinkKind",
    "classify_link",
    "link_stays_inside",
    "materialize_link",
    "stored_link_stays_inside",
    "strip_extended_prefix",
    "target_stays_inside",
]

_EXTENDED_PREFIX = b"\\\\?\\"
_EXTENDED_UNC_PREFIX = b"\\\\?\\UNC\\"
_WINDOWS_SEPARATORS = b"\\/"


class LinkKind(StrEnum):
    """How a symlink target travels between machines."""

    RELATIVE = "relative"
    """Not anchored at `/`; stored and written back verbatim."""

    CANONICAL = "canonical"
    """Anchored inside the local root; stored as the remainder after the root.

    The remainder keeps every byte the root did not cover, so the root itself is
    stored as `b""` and the root named with a trailing separator as `b"/"`. That
    is what keeps the pair injective: collapsing both to `b"/"` would make
    `/workspace/` come back as `/workspace`.
    """

    HOST = "host"
    """Absolute but outside the local root; stored and written back verbatim."""


def _normalise_root(local_root: bytes) -> bytes:
    """Drop trailing separators so `/workspace` and `/workspace/` behave alike.

    The filesystem root normalises to `b""`, which makes the prefix test below
    (`root + b"/"`) come out as `b"/"` — every absolute target is canonical under it.
    """
    return local_root.rstrip(b"/")


def strip_extended_prefix(path: bytes, *, windows: bool = False) -> bytes:
    """Drop Windows' extended-length prefix, leaving the path it names.

    `os.readlink` on Windows reports a symlink's substitute name, which the
    kernel stores prefixed (`\\\\?\\C:\\...`, `\\\\?\\UNC\\server\\share\\...`).
    The same path without the prefix names the same file, and is the spelling
    every other path a client holds is written in, so the two can be compared.

    Off Windows the bytes are returned untouched: they are an ordinary POSIX
    name there, not a prefix.
    """
    if not windows:
        return path
    if path[: len(_EXTENDED_UNC_PREFIX)].upper() == _EXTENDED_UNC_PREFIX:
        return b"\\\\" + path[len(_EXTENDED_UNC_PREFIX) :]
    if path.startswith(_EXTENDED_PREFIX):
        return path[len(_EXTENDED_PREFIX) :]
    return path


def _is_windows_absolute(path: bytes) -> bool:
    """Whether `path` names a volume: a drive letter, or a UNC share.

    A path rooted with no drive (`\\data\\x`) is relative to the current drive
    and means nothing on the machine that pulls it, so it is not absolute here
    — the same judgement `ntpath.isabs` makes.
    """
    if path[:1] in (b"\\", b"/") and path[1:2] in (b"\\", b"/"):
        return True
    return path[:1].isalpha() and path[1:2] == b":" and path[2:3] in (b"\\", b"/")


def _windows_key(path: bytes) -> bytes:
    """A comparison form for a Windows path, byte length preserved.

    Both separators name the same thing there, and its filesystems match names
    without regard to case. Only ASCII case is folded, so a root spelled with a
    different non-ASCII case classifies as `HOST` — verbatim, never re-anchored
    at the wrong place.
    """
    return path.replace(b"/", b"\\").lower()


def _classify_windows(target: bytes, local_root: bytes) -> tuple[LinkKind, bytes]:
    if not _is_windows_absolute(target):
        return LinkKind.RELATIVE, target

    root = local_root.rstrip(_WINDOWS_SEPARATORS)
    root_key = _windows_key(root)
    target_key = _windows_key(target)
    if target_key == root_key:
        return LinkKind.CANONICAL, b""
    if target_key.startswith(root_key + b"\\"):
        # The remainder is stored as an org path — `/`-separated, the one
        # spelling a machine on any platform can re-anchor. Nothing is lost:
        # Windows forbids both separators inside a name.
        return LinkKind.CANONICAL, target[len(root) :].replace(b"\\", b"/")
    return LinkKind.HOST, target


def classify_link(
    target: bytes, local_root: bytes, *, windows: bool = False
) -> tuple[LinkKind, bytes]:
    """Classify `target` against `local_root` and return the form to store.

    `windows` reads both paths the way that platform spells them: absolute
    means a drive letter or a UNC share, the two separators are the same
    separator, the root matches without regard to ASCII case, and a canonical
    remainder is stored `/`-separated so the machine that pulls it can be of
    any kind. Pass the extended-length prefix through
    :func:`strip_extended_prefix` first; this pair is lexical and does not
    rewrite one.
    """
    if windows:
        return _classify_windows(target, local_root)

    if not target.startswith(b"/"):
        return LinkKind.RELATIVE, target

    root = _normalise_root(local_root)
    if target == root:
        return LinkKind.CANONICAL, b""
    if target.startswith(root + b"/"):
        return LinkKind.CANONICAL, target[len(root) :]
    return LinkKind.HOST, target


def materialize_link(
    kind: LinkKind, target: bytes, local_root: bytes, *, windows: bool = False
) -> bytes:
    """Rewrite a stored target for `local_root`. Exact inverse of `classify_link`.

    Under `windows` the inverse holds up to the spelling of the separators: a
    stored org path is written back with the native one, so a target exported
    as `C:/root/a` comes back as `C:\\root\\a`. Both name the same file, and no
    name can be changed by the rewrite because a Windows name may contain
    neither separator.
    """
    if kind is not LinkKind.CANONICAL:
        return target

    if target and not target.startswith(b"/"):
        msg = f"a canonical link target must be an org path starting with '/': {target!r}"
        raise ValueError(msg)

    if windows:
        root = local_root.rstrip(_WINDOWS_SEPARATORS)
        if not target:
            return root or b"\\"
        return root + target.replace(b"/", b"\\")

    root = _normalise_root(local_root)
    if not target:
        # The root itself. `root + b"/"` would name the same directory with
        # different bytes, and a link target must round-trip byte for byte.
        return root or b"/"
    return root + target


_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def target_stays_inside(below: int, points_at: bytes) -> bool:
    """Whether a link ``below`` folders under a tree's root (``0`` for a link
    in the root itself) holding ``points_at`` names a place inside the tree,
    lexically: an absolute target does not, nor does a relative one whose
    ``..`` climb above the root from the link's own folder.

    The one definition of a link that stays inside, used where a link is
    stored (the drive) and where one is made on disk (a box's tree, a push).
    What stays inside may still dangle or name something refused; that is
    judged when the name is opened, never followed from here."""
    text = os.fsdecode(points_at)
    if os.sep == "\\":
        text = text.replace("\\", "/")
    if not text or "\x00" in text or text.startswith("/") or _DRIVE_LETTER.match(text):
        return False
    depth = below
    for segment in text.split("/"):
        if segment == "..":
            depth -= 1
            if depth < 0:
                return False
        elif segment not in ("", "."):
            depth += 1
    return True


def link_stays_inside(parts: Sequence[str], points_at: bytes) -> bool:
    """:func:`target_stays_inside` for a link at ``parts``, its path under the
    root (the link's own name last)."""
    return target_stays_inside(len(parts) - 1, points_at)


def stored_link_stays_inside(kind: str, below: int, target: bytes) -> bool:
    """Whether a link the drive stores as ``kind`` with ``target`` stays inside
    the tree it is stored in, ``below`` folders under its root.

    A relative target is read from the link's folder. A canonical one is an
    org path read from the root a puller anchors it at, so only a ``..``
    above that root leaves it. A host target is a path on some machine,
    outside every tree by definition."""
    if kind == LinkKind.RELATIVE:
        return target_stays_inside(below, target)
    if kind == LinkKind.CANONICAL:
        remainder = target.lstrip(b"/")
        return not remainder or target_stays_inside(0, remainder)
    return False
