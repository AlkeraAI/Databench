"""The whole-file BLAKE3 a drive version's ``contentHash`` is, computed here once.

Every comparison a push, a pull or a working copy makes against the drive
(is this the same file?) compares a local hash with ``contentHash``; one
spelling of how that hash is taken keeps them from drifting apart. The read
streams, so a 10 GB checkpoint never sits in memory.

``blake3`` is imported inside each function, as the rest of the Files code
always has, so importing the package does not load the native extension.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Final

#: How much of a file one read takes while hashing.
STREAM_CHUNK: Final = 1 << 20


def content_hash(data: bytes) -> str:
    """The BLAKE3 of ``data``, as lowercase hex."""
    from blake3 import blake3

    return str(blake3(data).hexdigest())


def stream_hash(handle: IO[bytes]) -> tuple[str, int]:
    """The BLAKE3 and length of everything ``handle`` reads from where it
    stands to its end."""
    from blake3 import blake3

    digest = blake3()
    size = 0
    while chunk := handle.read(STREAM_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return str(digest.hexdigest()), size


def file_hash(path: Path) -> tuple[str, int]:
    """The BLAKE3 and size of the file at ``path``."""
    with path.open("rb") as handle:
        return stream_hash(handle)


__all__ = ["STREAM_CHUNK", "content_hash", "file_hash", "stream_hash"]
