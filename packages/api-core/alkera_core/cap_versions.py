"""The version tag a cap slot carries, so a write can say which reading it was built on.

Every allowance and every member cap is read as a figure plus a ``version``;
a conditional write sends that version back in ``If-Match`` and is refused
with a 409 when the slot has moved since — two tabs editing the same cap
cannot silently overwrite each other. The tag is derived from what the reader
saw (the figure and when it was last written), so it needs no column of its
own and is the same whichever surface read the slot; an empty slot has a tag
too, so two tabs that both create the first cap race to a conflict as well.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

#: The tag of a slot that holds no cap. A write that expects it is a create.
UNSET_VERSION = "unset"


def cap_version(limit: int | None, updated_at: datetime | None) -> str:
    """The opaque tag for one cap slot: :data:`UNSET_VERSION` when ``limit`` is
    ``None``, else a short digest of the figure and its last write. Opaque on
    purpose — a client echoes it, never reads it."""
    if limit is None:
        return UNSET_VERSION
    stamp = "" if updated_at is None else f"{updated_at.timestamp():.6f}"
    return hashlib.blake2b(f"{limit}:{stamp}".encode(), digest_size=8).hexdigest()


def version_matches(expected: str | None, current: str) -> bool:
    """Whether an ``If-Match`` value admits the slot's ``current`` tag. No
    header (``None``) and the wildcard ``*`` are unconditional; a quoted or
    weak (``W/"…"``) entity tag is read as the tag inside it."""
    if expected is None:
        return True
    tag = expected.strip()
    if tag == "*":
        return True
    if tag.startswith("W/"):
        tag = tag[2:]
    return tag.strip('"') == current


__all__ = ["UNSET_VERSION", "cap_version", "version_matches"]
