"""The actions a Files policy can decide about.

Deliberately its own enum rather than a widening of
:class:`alkera_core.authz.enums.Action`: the ladder in :mod:`.ladder` is a table
over *these* names, and the platform RBAC engine that replaces the ladder later
maps its own vocabulary onto them. The platform ``Action`` stays the coarse
verb an ``enforce()`` call carries; this enum is the fine-grained thing the
ladder and ``allowed_actions`` speak.

``EXPORT`` is "get the bytes out" — download, reconstruct, materialize onto a
box. ``READ`` is metadata, listing and preview, which is why a ``no_download``
node still reads: the flag drops ``EXPORT`` and leaves ``READ`` standing.
"""

from __future__ import annotations

from enum import StrEnum


class FilesAction(StrEnum):
    """What a caller is trying to do to a node."""

    READ = "read"
    WRITE = "write"
    SHARE = "share"
    DELETE = "delete"
    RESTORE = "restore"
    LEASE = "lease"
    LEASE_REQUEST = "lease_request"
    LEASE_FORCE = "lease_force"
    SNAPSHOT = "snapshot"
    LOCK = "lock"
    HOLD = "hold"
    EXPORT = "export"
    COMMENT = "comment"
    #: Make a new node (a subtree, a chat, a saved object) from this one,
    #: somewhere the caller may write. Reading is all it takes from the
    #: source: the bytes are shared, never moved, and what the copy becomes
    #: is decided at its destination.
    COPY = "copy"


#: The actions that survive a read-only node: nothing here changes a row or a
#: byte, so a frozen drive keeps exactly this much.
READ_ONLY_ACTIONS: frozenset[FilesAction] = frozenset(
    {FilesAction.READ, FilesAction.EXPORT, FilesAction.LEASE_REQUEST, FilesAction.COPY}
)

__all__ = ["READ_ONLY_ACTIONS", "FilesAction"]
