"""Containment: a relative key may only ever name a path beneath the root.

Two enforcement paths, one contract. On Linux the kernel decides, via
``openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)``;
everywhere else a canonical-path-plus-separator check plus a per-component
symlink refusal does the same job in user space.

Both raise :class:`InvalidKey`. The lexical refusals (``..``, an absolute
key, an empty segment, NUL, a ``domains/`` prefix) happen before the walk
touches the filesystem at all, so a hostile key never reaches ``open``.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Final

from alkera_core.files.store.errors import InvalidKey
from alkera_core.files.store.keys import validate_relative_key

RESOLVE_NO_MAGICLINKS: Final = 0x02
RESOLVE_NO_SYMLINKS: Final = 0x04
RESOLVE_BENEATH: Final = 0x08

_SYS_OPENAT2: Final = 437
_O_PATH: Final = 0o10000000
"""Linux ``O_PATH``; spelled here because ``os`` does not define it off Linux."""
_OPEN_HOW_FLAGS: Final = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS

USING_OPENAT2: Final = sys.platform == "linux"


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


OpenBeneath = Callable[[int, str, _OpenHow], int]
"""Open one path component beneath an open root, or raise ``OSError``."""

OpenRoot = Callable[[Path], int]
"""Take a descriptor on the store root for the kernel to resolve beneath."""


def _directory_open_flags() -> int:
    """Read-only open of a directory, where the platform has such a thing.

    Windows' ``os`` defines no ``O_DIRECTORY``, so naming the flag directly is
    an ``AttributeError`` there before any open is attempted — and it has no
    directory descriptor to ask for either, which is why the walk this serves
    runs on Linux alone and is reachable elsewhere only through its seams.
    """
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


def _open_root(root_real: Path) -> int:
    """A descriptor on the store root, for ``openat2`` to resolve components beneath."""
    return os.open(root_real, _directory_open_flags())


def resolve_beneath(root: Path, relative: str) -> Path:
    """Return the path ``relative`` names beneath ``root``, or raise.

    The returned path is *lexical*: it is safe to create, because every
    existing component on the way to it has been proven to be a real
    directory beneath ``root`` and not a symlink.
    """
    validate_relative_key(relative)
    root_real = Path(os.path.realpath(root))
    candidate = Path(os.path.normpath(root_real / relative))
    if candidate == root_real or root_real not in candidate.parents:
        raise InvalidKey(f"key {relative!r} escapes the store root")
    if USING_OPENAT2:  # pragma: no cover - exercised on Linux only
        _walk_openat2(root_real, relative)
    else:
        _walk_lstat(root_real, relative)
    return candidate


def _walk_lstat(root_real: Path, relative: str) -> None:
    """Refuse a symlinked component anywhere on the path (portable path)."""
    cursor = root_real
    for segment in relative.split("/"):
        cursor = cursor / segment
        try:
            if cursor.is_symlink():
                raise InvalidKey(f"key {relative!r} traverses a symlink at {segment!r}")
        except OSError as exc:  # pragma: no cover - unreadable parent
            raise InvalidKey(f"key {relative!r} is not resolvable beneath the root") from exc
        if not cursor.exists():
            return


@lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    """The process's libc, loaded once: the walk opens it per path component."""
    return ctypes.CDLL(None, use_errno=True)


def _openat2(root_fd: int, path: str, how: _OpenHow) -> int:  # pragma: no cover - Linux only
    """``openat2(root_fd, path, how)``, raising ``OSError`` the way ``os.open`` does.

    The seam the walk is driven through: the syscall exists on Linux alone, so
    the error handling around it is only reachable elsewhere with this
    substituted.
    """
    fd = _libc().syscall(
        ctypes.c_long(_SYS_OPENAT2),
        ctypes.c_int(root_fd),
        ctypes.c_char_p(path.encode("utf-8")),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)
    return int(fd)


def _walk_openat2(
    root_real: Path,
    relative: str,
    *,
    open_beneath: OpenBeneath = _openat2,
    open_root: OpenRoot = _open_root,
) -> None:
    """Let the kernel decide containment for the deepest existing prefix.

    Every refusal reaches the caller as :class:`InvalidKey`, the root's own
    included: a store root that cannot be opened is "not resolvable beneath
    the root" here exactly as an unreadable component is on the portable path,
    rather than the raw ``OSError`` of whichever directory said no.

    Taking the root's descriptor is its own seam because a host without
    directory descriptors cannot take one at all: substituting it is what lets
    the refusals below be driven by the fake kernel rather than by the
    platform.
    """
    how = _OpenHow(
        flags=ctypes.c_uint64(_O_PATH),
        mode=ctypes.c_uint64(0),
        resolve=ctypes.c_uint64(_OPEN_HOW_FLAGS),
    )
    try:
        root_fd = open_root(root_real)
    except FileNotFoundError:
        # The root itself is not there yet, which is not a refusal: nothing
        # below a directory that does not exist can escape through it, and the
        # portable walk never opens the root at all — it stops at the first
        # missing component and lets the caller create the path. The two have
        # to agree, because the bucket-rooted handle resolves a key beneath the
        # DOMAIN's own directory, and that directory does not exist until the
        # first object is written into it. Refusing here made the ownership
        # marker, which is the first object any domain ever gets, impossible to
        # write or read on the one platform that takes this branch.
        return
    except OSError as exc:
        raise InvalidKey(f"key {relative!r} is not resolvable beneath the root") from exc
    try:
        cursor = ""
        for segment in relative.split("/"):
            cursor = f"{cursor}/{segment}" if cursor else segment
            try:
                fd = open_beneath(root_fd, cursor, how)
            except FileNotFoundError:
                # Nothing below exists, so nothing below can escape.
                return
            except OSError as exc:
                raise InvalidKey(
                    f"key {relative!r} is not resolvable beneath the root (errno {exc.errno})"
                ) from exc
            os.close(fd)
    finally:
        os.close(root_fd)
