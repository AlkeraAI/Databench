"""The stored spelling of POSIX extended attributes, and the limits on them.

The library writes the ``xattrs`` JSONB column (``ops.py`` on a patch,
``namespace.py`` on an import), so the encoding and the limits have to live
here: `alkera_core.files` may never import its own API shapes, and a helper the
library calls is not an API shape. ``alkera_core.schemas.files.attrs`` re-exports
every name below, so the surfaces that already read them from the schemas layer
keep working — the same direction ``PointerFile`` and the delta token take.

Extended attribute values are arbitrary bytes, so they are stored as base64 text
and the limits are counted in bytes, never characters.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Final

#: An xattr name is stored as bytes by the kernel; the limit is on the bytes.
MAX_XATTR_NAME_BYTES: Final = 255
#: Per value, and per node summed over every value.
MAX_XATTR_VALUE_BYTES: Final = 64 * 1024
MAX_XATTR_TOTAL_BYTES: Final = 64 * 1024

#: Only the `user.` namespace round-trips. `system.`, `security.` and
#: `trusted.` are kernel-owned: a box could not restore them without privilege,
#: so storing one would promise a materialization that cannot happen.
USER_XATTR_PREFIX: Final = "user."


def stored_xattrs(xattrs: Mapping[str, bytes | str]) -> dict[str, str]:
    """The JSONB spelling of an xattr set: base64 text, one value per name.

    The column holds exactly what the wire renders, so a value written through
    a create and a value written through a PATCH read back byte-identical and
    no reader has to know which path produced it. ``str`` values are already
    that base64 (an inverse replays what it read); ``bytes`` are encoded here.
    """
    return {
        name: value if isinstance(value, str) else base64.b64encode(value).decode("ascii")
        for name, value in xattrs.items()
    }


def validate_xattrs(xattrs: dict[str, bytes]) -> dict[str, bytes]:
    """Refuse an xattr set no filesystem downstream could store.

    Raising here rather than truncating is deliberate: a silently clipped value
    would materialize on a box as bytes the user never wrote.
    """
    total = 0
    for name, value in xattrs.items():
        name_bytes = len(name.encode("utf-8"))
        if name_bytes > MAX_XATTR_NAME_BYTES:
            raise ValueError(
                f"xattr name is {name_bytes} bytes, over the {MAX_XATTR_NAME_BYTES}-byte limit"
            )
        if len(value) > MAX_XATTR_VALUE_BYTES:
            raise ValueError(
                f"xattr {name!r} is {len(value)} bytes, over the "
                f"{MAX_XATTR_VALUE_BYTES}-byte per-value limit"
            )
        total += len(value)
    if total > MAX_XATTR_TOTAL_BYTES:
        raise ValueError(
            f"xattrs total {total} bytes, over the {MAX_XATTR_TOTAL_BYTES}-byte per-node limit"
        )
    return xattrs


__all__ = [
    "MAX_XATTR_NAME_BYTES",
    "MAX_XATTR_TOTAL_BYTES",
    "MAX_XATTR_VALUE_BYTES",
    "USER_XATTR_PREFIX",
    "stored_xattrs",
    "validate_xattrs",
]
