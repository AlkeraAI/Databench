"""A folder's digest: the cheap answer to "has anything in here moved?".

A machine holding a folder walks its disk now and then to find what its watcher
missed, and asks the drive for the same folders' digests. Where the two agree
nothing is sent; where they differ, only that folder's entries are. The digest
is therefore computed on both sides from the same facts by the same rule, and
the rule is spelled once here -- the holder's copy in the CLI is held to it by
one shared vector file
(``packages/api-core/tests/files/fixtures/digest_vectors.json``).

Per folder, over its DIRECT children only:

* ``count`` is how many there are;
* ``xor`` is the XOR of one 64-bit value per child: the first eight bytes of
  ``blake3(kind || name || size || mtime_ns)`` read as a big-endian unsigned
  integer, where ``kind`` is the one byte ``f`` (a file) or ``d`` (a folder),
  ``name`` the name's raw bytes, ``size`` eight bytes big-endian unsigned and
  ``mtime_ns`` eight bytes big-endian signed. A folder's size and mtime are 0.

XOR makes the order the children are visited in irrelevant, and the fixed-width
tail after the name keeps the encoding unambiguous without a separator. The
wire form of ``xor`` is sixteen lowercase hex digits.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from blake3 import blake3

DigestKind = Literal["file", "dir"]

#: The byte each kind is hashed as.
KIND_BYTES: Final[dict[str, bytes]] = {"file": b"f", "dir": b"d"}
_U64: Final = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class DigestChild:
    """One direct child of a folder as the digest sees it."""

    kind: DigestKind
    name: bytes
    size: int = 0
    mtime_ns: int = 0


@dataclass(frozen=True, slots=True)
class Digest:
    """A folder's digest: how many direct children, and their XOR."""

    count: int
    xor: int

    @property
    def hex(self) -> str:
        return f"{self.xor:016x}"

    def wire(self) -> dict[str, int | str]:
        return {"count": self.count, "xor": self.hex}


EMPTY: Final = Digest(count=0, xor=0)


def child_hash(kind: DigestKind, name: bytes, size: int = 0, mtime_ns: int = 0) -> int:
    """One child's 64-bit contribution. A folder's size and mtime are forced to 0,
    so a folder never differs by facts the rule says it does not have."""
    try:
        tag = KIND_BYTES[kind]
    except KeyError:
        raise ValueError(f"unknown digest kind {kind!r}") from None
    if kind == "dir":
        size, mtime_ns = 0, 0
    if size < 0:
        raise ValueError("a size is never negative")
    encoded = (
        tag
        + name
        + size.to_bytes(8, "big", signed=False)
        + mtime_ns.to_bytes(8, "big", signed=True)
    )
    return int.from_bytes(blake3(encoded).digest()[:8], "big") & _U64


def directory_digest(children: Iterable[DigestChild]) -> Digest:
    """The digest of a folder whose direct children are ``children``."""
    count = 0
    xor = 0
    for child in children:
        count += 1
        xor ^= child_hash(child.kind, child.name, child.size, child.mtime_ns)
    return Digest(count=count, xor=xor)


__all__ = [
    "EMPTY",
    "KIND_BYTES",
    "Digest",
    "DigestChild",
    "DigestKind",
    "child_hash",
    "directory_digest",
]
