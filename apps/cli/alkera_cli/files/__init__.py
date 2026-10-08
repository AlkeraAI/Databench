"""Local filesystem half of ``alkera files``: the walk out and the write back.

These modules import only the pure library primitives in
``alkera_core.files`` (names, links, hashing, ``sync.atomic``) — never the
object store, the API or a session — so a push and a pull can be exercised
against a directory with no service running.
"""

from alkera_cli.files.target import ContainmentError, MaterializationTarget
from alkera_cli.files.walk import EXCLUDE_PRESETS, Entry, EntryKind, SkipReason, walk
from alkera_cli.files.xattrs import XATTRS_SUPPORTED, read_xattrs, write_xattrs

__all__ = [
    "EXCLUDE_PRESETS",
    "XATTRS_SUPPORTED",
    "ContainmentError",
    "Entry",
    "EntryKind",
    "MaterializationTarget",
    "SkipReason",
    "read_xattrs",
    "walk",
    "write_xattrs",
]
