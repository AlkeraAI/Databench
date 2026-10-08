"""What a host gets for a node: the ``MaterializationTarget`` seam.

``pull``, ``alkera files mount``, a box agent's stage-in and a CI runner's
checkout all answer the same question — *where do these bytes, this pointer and
this symlink go?* — and differ only in the answer. This protocol is that
question, so the materializer above it walks the tree once and never learns
which host it is writing to.

Four writes, not one with a mode flag. A node is materialized as bytes, as a
pointer file standing in for row-backed content, or as one of the three symlink
kinds; each is a separate method because a target that cannot make symlinks (a
Windows host without the privilege, a CI runner that flattens them) must be
able to refuse *that* case without a boolean parameter selecting between two
behaviours of the same call. ``set_attrs`` is separate for the same reason: a
target that restores POSIX mode and mtime and one that discards them differ in
one method, not in a flag threaded through every write.

``root`` is what makes a target addressable. Every ``path`` is relative to it,
so the seam carries no absolute paths and a recording target needs no
filesystem at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol, runtime_checkable

__all__ = ["SYMLINK_KINDS", "MaterializationTarget", "SymlinkKind", "TargetAttrs"]

#: The three symlink kinds the node model carries verbatim: one pointing inside
#: the drive, one pointing outside it, and one whose target does not resolve.
SymlinkKind = Literal["internal", "external", "dangling"]

#: Spelled once so a target can enumerate what it must handle.
SYMLINK_KINDS: tuple[SymlinkKind, ...] = ("internal", "external", "dangling")


@runtime_checkable
class TargetAttrs(Protocol):
    """The POSIX facts a host may restore. Stored verbatim on the node, so a
    target that cannot apply one drops it rather than translating it."""

    mode: int | None
    mtime_ns: int | None


@runtime_checkable
class MaterializationTarget(Protocol):
    """Where a materialized tree lands."""

    #: The path every other method's ``path`` is relative to. A string, not a
    #: ``Path``: a RunPod box and a CI runner address a prefix, not a directory
    #: this process can open.
    root: str

    async def write_bytes(
        self, path: str, stream: AsyncIterator[bytes], attrs: TargetAttrs
    ) -> None:
        """Place a file's content at ``path``, atomically from the reader's view."""

    async def write_pointer(self, path: str, pointer: bytes) -> None:
        """Place the pointer file that stands in for row-backed content."""

    async def write_symlink(self, path: str, kind: SymlinkKind, target: str) -> None:
        """Place a symlink, or refuse this ``kind`` — never silently write a copy."""

    async def set_attrs(self, path: str, attrs: TargetAttrs) -> None:
        """Apply mode and mtime after the content lands, or drop what it cannot."""
