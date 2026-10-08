"""Surface encoders: one injective, invertible spelling of a name per client façade.

A name is bytes. Every façade that cannot carry arbitrary bytes gets an encoder here,
paired with its exact inverse; `decode_from_surface(encode_for_surface(n, s), s) == n`
for every implemented surface and every byte string `n`. `posix` and `s3` are
implemented now; `windows`, `macos` and `webdav` land with their façades and refuse
loudly until then, so nothing silently ships a lossy spelling.

A surface names a client façade, never the host this process happens to run on, so
the codecs are spelled out rather than taken from `os.fsdecode`: that follows
`sys.getfilesystemencodeerrors()`, which is surrogateescape on POSIX but
surrogatepass on Windows — and surrogatepass raises on the undecodable bytes the
posix surface exists to carry.
"""

from __future__ import annotations

from typing import Final, Literal
from urllib.parse import unquote_to_bytes

__all__ = ["Surface", "UnsupportedSurface", "decode_from_surface", "encode_for_surface"]

Surface = Literal["posix", "s3", "windows", "macos", "webdav"]

_IMPLEMENTED: Final = frozenset({"posix", "s3"})
_PLANNED: Final = frozenset({"windows", "macos", "webdav"})

# RFC 3986 unreserved minus `~`, plus `!*'()`. `~` is percent-encoded so the output
# stays inside the charset the S3 key contract pins; `%` is never safe, which is what
# makes the encoding injective (`%41` and `A` cannot collide).
_S3_SAFE: Final = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!-_.*'()"
)
_S3_TABLE: Final = tuple(chr(byte) if byte in _S3_SAFE else f"%{byte:02X}" for byte in range(256))


class UnsupportedSurface(ValueError):  # noqa: N818 - the name the Files spec pins
    """The surface has no encoder yet, or is not a surface at all."""


def _check(surface: str) -> None:
    if surface in _IMPLEMENTED:
        return
    if surface in _PLANNED:
        msg = f"the {surface!r} surface encoder lands with its façade"
        raise UnsupportedSurface(msg)
    msg = f"unknown surface {surface!r}"
    raise UnsupportedSurface(msg)


def encode_for_surface(name: bytes, surface: Surface) -> str:
    """Spell `name` for `surface`. Injective: distinct names give distinct spellings."""
    _check(surface)
    if surface == "posix":
        return name.decode("utf-8", "surrogateescape")
    return "".join(_S3_TABLE[byte] for byte in name)


def decode_from_surface(encoded: str, surface: Surface) -> bytes:
    """Recover the name from its `surface` spelling. Exact inverse of the encoder."""
    _check(surface)
    if surface == "posix":
        return encoded.encode("utf-8", "surrogateescape")
    return unquote_to_bytes(encoded)
