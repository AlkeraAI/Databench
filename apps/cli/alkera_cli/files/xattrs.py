"""Extended attributes for the CLI's push/pull, read and written without following links.

Only the ``user.*`` namespace travels: it is the one namespace an unprivileged
process may write back on Linux, so anything else would be read on one machine
and silently refused on the next.

Three platforms, one contract. Linux has :func:`os.listxattr` and friends with a
``follow_symlinks`` keyword. macOS ships the same syscalls but CPython does not
expose them, so they are called through ``ctypes`` with ``XATTR_NOFOLLOW``.
Windows has no equivalent, so :data:`XATTRS_SUPPORTED` is ``False`` and both
calls degrade to "no attributes" rather than raising — a push from Windows
carries none and a pull onto Windows drops them.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

__all__ = ["USER_PREFIX", "XATTRS_SUPPORTED", "read_xattrs", "write_xattrs"]

USER_PREFIX: Final = b"user."

_XATTR_NOFOLLOW: Final = 0x0001

XATTRS_SUPPORTED: Final = sys.platform in ("linux", "darwin")


def _libc() -> ctypes.CDLL:  # pragma: no cover - darwin only
    libc = ctypes.CDLL(None, use_errno=True)
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.restype = ctypes.c_int
    return libc


def _darwin_list(path: bytes) -> list[bytes]:  # pragma: no cover - darwin only
    libc = _libc()
    size = libc.listxattr(path, None, ctypes.c_size_t(0), _XATTR_NOFOLLOW)
    if size <= 0:
        return []
    buffer = ctypes.create_string_buffer(size)
    written = libc.listxattr(path, buffer, ctypes.c_size_t(size), _XATTR_NOFOLLOW)
    if written <= 0:
        return []
    return [name for name in buffer.raw[:written].split(b"\x00") if name]


def _darwin_get(path: bytes, name: bytes) -> bytes | None:  # pragma: no cover - darwin only
    libc = _libc()
    size = libc.getxattr(path, name, None, ctypes.c_size_t(0), 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    buffer = ctypes.create_string_buffer(size or 1)
    written = libc.getxattr(path, name, buffer, ctypes.c_size_t(size), 0, _XATTR_NOFOLLOW)
    if written < 0:
        return None
    return bytes(buffer.raw[:written])


def _darwin_set(path: bytes, name: bytes, value: bytes) -> None:  # pragma: no cover - darwin
    libc = _libc()
    result = libc.setxattr(path, name, value, ctypes.c_size_t(len(value)), 0, _XATTR_NOFOLLOW)
    if result != 0:
        raise OSError(ctypes.get_errno(), "setxattr failed", os.fsdecode(path))


def read_xattrs(path: Path) -> dict[bytes, bytes]:
    """The ``user.*`` attributes on ``path`` itself, never on a symlink's target.

    An attribute that disappears between the listing and the read is dropped
    rather than raised: a concurrent remover must not fail a whole walk.
    """
    if not XATTRS_SUPPORTED:
        return {}
    found: dict[bytes, bytes] = {}
    if sys.platform == "darwin":  # pragma: no cover - one branch per platform
        encoded = os.fsencode(path)
        for name in _darwin_list(encoded):
            if not name.startswith(USER_PREFIX):
                continue
            value = _darwin_get(encoded, name)
            if value is not None:
                found[name] = value
        return found
    for name_str in os.listxattr(path, follow_symlinks=False):  # pragma: no cover - linux
        name = os.fsencode(name_str)
        if not name.startswith(USER_PREFIX):
            continue
        try:
            found[name] = os.getxattr(path, name_str, follow_symlinks=False)
        except OSError:
            continue
    return found


def write_xattrs(path: Path, values: Mapping[bytes, bytes]) -> None:
    """Set every ``user.*`` attribute in ``values`` on ``path`` itself.

    A no-op where the platform has no extended attributes, and a skip for a
    non-``user.`` name: a stored tree must never be able to make a pull write
    into a privileged namespace.
    """
    if not XATTRS_SUPPORTED or not values:
        return
    for name, value in values.items():
        if not name.startswith(USER_PREFIX):
            continue
        if sys.platform == "darwin":  # pragma: no cover - one branch per platform
            _darwin_set(os.fsencode(path), name, value)
        else:  # pragma: no cover - linux
            os.setxattr(path, os.fsdecode(name), value, follow_symlinks=False)
