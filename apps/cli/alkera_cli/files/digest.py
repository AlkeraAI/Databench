"""The per-directory digest a holder and the drive compare to find what moved.

A walk of a held folder is cheap only if it can skip what nothing changed, and
the only way to know a directory is unchanged without listing it on the drive
is a fingerprint both sides compute from the same facts. Per directory, it is
the number of direct children and an order-independent fold over them: each
child contributes the first eight bytes of
``blake3(kind || name_bytes || size || mtime_ns)`` read as an unsigned 64-bit
integer, and the fold is XOR — so the order a directory is listed in never
matters, and adding then removing a child leaves the digest where it was.

A directory child contributes with a size and modified time of zero: its own
contents are its own digest's business, so a change three levels down marks
only the directory it happened in.

The encoding of one child is exact because the drive computes the same bytes
(``alkera_core.files.digest``); the two are pinned by one shared vector file.

* ``kind`` is the one byte ``f`` (a file) or ``d`` (a folder);
* ``name_bytes`` is the child's name as the filesystem spells it (UTF-8, with
  undecodable bytes kept as they are);
* ``size`` is eight bytes, big-endian, unsigned;
* ``mtime_ns`` is eight bytes, big-endian, signed.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["EMPTY", "DirDigest", "child_value", "digest_of", "from_hex"]

Kind = Literal["file", "dir"]

_KINDS: Final[dict[str, bytes]] = {"file": b"f", "dir": b"d"}


@dataclass(frozen=True, slots=True)
class DirDigest:
    """One directory's fingerprint: how many direct children, and their fold."""

    count: int = 0
    xor: int = 0

    @property
    def hex(self) -> str:
        """The fold as the wire spells it: sixteen lowercase hex digits."""
        return f"{self.xor:016x}"

    def with_child(self, value: int) -> DirDigest:
        return DirDigest(count=self.count + 1, xor=self.xor ^ value)


#: What a directory with nothing in it — or one the drive does not list — reads as.
EMPTY: Final = DirDigest()


def child_value(kind: Kind, name: bytes, size: int = 0, mtime_ns: int = 0) -> int:
    """One child's contribution to its parent's fold."""
    from blake3 import blake3

    if kind == "dir":
        size, mtime_ns = 0, 0
    payload = _KINDS[kind] + name + struct.pack(">Qq", size, mtime_ns)
    return int.from_bytes(blake3(payload).digest()[:8], "big", signed=False)


def digest_of(children: Iterable[tuple[Kind, bytes, int, int]]) -> DirDigest:
    """The digest of a directory whose direct children are ``children``."""
    digest = EMPTY
    for kind, name, size, mtime_ns in children:
        digest = digest.with_child(child_value(kind, name, size, mtime_ns))
    return digest


def from_hex(count: int, xor: str) -> DirDigest:
    """A digest as the drive answers it."""
    return DirDigest(count=count, xor=int(xor, 16))
