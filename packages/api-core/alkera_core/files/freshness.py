"""Whether the drive's bytes are the bytes the holder has, decided from metadata.

A folder held by a machine is listed from rows the holder reports ahead of its
bytes, so a file on the drive is in one of four states against the disk it
mirrors: the store holds exactly what the holder has (``on_drive``), the store
holds an older version (``behind``), the store holds nothing yet
(``unlanded``), or the store holds nothing and the holder is gone, so the file
is left on the machine (``unsynced``). ``none`` is every row the question does
not apply to: a file under no holder's report, and every folder. A holder
going away never turns a file with bytes on the drive into ``unsynced``: those
bytes are saved whatever happens to the machine.

The answer is derived per row on every read and never stored, because it is a
statement about two facts that move independently -- the head version and the
holder's last report -- and storing it would mean a row to rewrite whenever
either moved. And it is decided from those facts alone, never by asking the
holder: a read of a current file costs the machine nothing.

The cheap pair ``(size, mtime_ns)`` decides unless both sides know the BLAKE3 of
the bytes, in which case the hash does -- a same-size rewrite that keeps its
modified time is real (``cp -p``, a checkout), and only the hash sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, get_args

#: What a row's bytes are, against the disk of the machine holding its folder.
ContentState = Literal["on_drive", "behind", "unlanded", "unsynced", "none"]
#: The same five words as a tuple, for a caller that iterates them.
CONTENT_STATES: Final[tuple[str, ...]] = get_args(ContentState)
#: The two states that mean bytes are still on their way to the drive -- what a
#: folder's "N landing" counts.
LANDING_STATES: Final[frozenset[str]] = frozenset({"behind", "unlanded"})


@dataclass(frozen=True, slots=True)
class HeadFacts:
    """What the drive holds for a file: its head version's size, the row's own
    modified time, and the version's BLAKE3 when it is known."""

    size: int
    mtime_ns: int
    content_hash: bytes | None = None


@dataclass(frozen=True, slots=True)
class HolderFacet:
    """What the holder last reported for the same file."""

    size: int
    mtime_ns: int
    hash: bytes | None = None


def head_digest(content_hash: str | bytes | None) -> bytes | None:
    """A version's hex ``content_hash`` (or raw digest) as the digest bytes.

    Empty and malformed values are ``None`` -- an unknown hash, which makes the
    pair decide -- rather than an error: the value comes off a stored row, and a
    row that cannot be compared is a row that is not known to be current.
    """
    if content_hash is None:
        return None
    if isinstance(content_hash, bytes):
        return content_hash or None
    if not content_hash:
        return None
    try:
        return bytes.fromhex(content_hash)
    except ValueError:
        return None


def current(head: HeadFacts | None, facet: HolderFacet | None) -> bool:
    """Whether the drive's head is the holder's file, byte for byte."""
    if head is None or facet is None:
        return False
    if head.content_hash is not None and facet.hash is not None:
        return head.content_hash == facet.hash
    return head.size == facet.size and head.mtime_ns == facet.mtime_ns


def landed_state(head: HeadFacts | None, facet: HolderFacet | None) -> ContentState:
    """The row's state from the head and the report alone, before the lease is
    consulted: ``none``, ``unlanded``, ``on_drive`` or ``behind``."""
    if facet is None:
        return "none"
    if head is None:
        return "unlanded"
    return "on_drive" if current(head, facet) else "behind"


def under_lease(landed: ContentState, *, lease_live: bool) -> ContentState:
    """``landed`` once the lease is known; ``lease_live`` is whether the holder's
    lease is live now.

    The holder going away (a release, a box restart, a lapse) changes the
    meaning of exactly one state: a file the holder reported and the drive has
    no bytes for is left on the machine, ``unsynced``. A file whose bytes are
    on the drive stays what it was -- saved when it is the holder's file, the
    older copy when the holder had moved past it -- because the bytes the drive
    holds did not go anywhere when the machine did. The one place this rule is
    spelled: a surface that learns the lease after rendering the row folds it in
    here, never with its own comparison.
    """
    if landed == "unlanded" and not lease_live:
        return "unsynced"
    return landed


def content_state(
    head: HeadFacts | None, facet: HolderFacet | None, *, lease_live: bool
) -> ContentState:
    """The row's state; ``lease_live`` is whether the holder's lease is live now."""
    return under_lease(landed_state(head, facet), lease_live=lease_live)


def current_sql(node: str = "n", head: str = "hv") -> str:
    """:func:`current` as a SQL predicate over a ``file_nodes`` row and its head.

    ``node`` is the alias of the row, ``head`` the alias of its head version
    LEFT JOINed on ``head_version_id``. The same rule, in the same order: no
    head is never current; both hashes known decides by hash; otherwise the
    size and the row's own modified time decide. Kept here beside the Python
    form so the two can only change together, and a test holds them to the
    same answers.
    """
    return (
        f"({node}.head_version_id IS NOT NULL AND {node}.holder_size IS NOT NULL AND CASE "
        f"WHEN {node}.holder_hash IS NOT NULL AND {head}.content_hash <> '' "
        f"THEN {head}.content_hash = encode({node}.holder_hash, 'hex') "
        f"ELSE {head}.size_bytes = {node}.holder_size "
        f"AND {node}.mtime_ns = {node}.holder_mtime_ns END)"
    )


__all__ = [
    "CONTENT_STATES",
    "LANDING_STATES",
    "ContentState",
    "HeadFacts",
    "HolderFacet",
    "content_state",
    "current",
    "current_sql",
    "head_digest",
    "landed_state",
    "under_lease",
]
