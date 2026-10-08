"""The closed vocabularies of the Files tree, each with a tolerant reader.

A kind, a state or a trust value is written to Postgres today and read back by
whatever binary happens to be running during a rolling deploy — including one
older than the writer. So the enums here are closed for *writing* (a value that
is not in the ladder never leaves this process) and open for *reading*:
:func:`parse_kind` hands back a :class:`RawKind` for a stored value this reader
has never heard of instead of raising, which is what keeps an old backend able
to list a folder a newer one wrote into.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from alkera_core.versioning import VersionedModel


class NodeKind(StrEnum):
    """What a node *is*. Day one writes folder | file | object | symlink | special."""

    FOLDER = "folder"
    FILE = "file"
    SYMLINK = "symlink"
    SHORTCUT = "shortcut"
    OBJECT = "object"
    SPECIAL = "special"
    DOCUMENT = "document"
    REMOTE = "remote"


class SymlinkKind(StrEnum):
    """How a symlink target is stored, rewritten on materialize, and exported."""

    RELATIVE = "relative"
    CANONICAL = "canonical"
    HOST = "host"


class NodeState(StrEnum):
    """The fence a node is under. ``live`` is the only state that accepts every write."""

    LIVE = "live"
    LOCKED = "locked"
    MOVING = "moving"
    ACL_REWRITING = "acl_rewriting"


class Trust(StrEnum):
    """Where the bytes came from — what decides whether attributes materialize."""

    OWN = "own"
    SHARED_IN = "shared_in"
    IMPORTED = "imported"
    LINK_UPLOADED = "link_uploaded"


class MimeClass(StrEnum):
    """The sniffed class denormalized onto the node so the filter chip is a predicate."""

    IMAGE = "image"
    TABULAR = "tabular"
    CODE = "code"
    ARCHIVE = "archive"
    DOCUMENT = "document"
    TEXT = "text"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class RawKind:
    """A stored kind this reader does not know.

    Frozen and hashable so it can sit in a set or a dict key beside a
    :class:`NodeKind`, and it carries the value verbatim so a reader that only
    passes it through (a listing, a history pane) writes back exactly what the
    newer writer stored.
    """

    value: str


def parse_kind(value: str) -> NodeKind | RawKind:
    """Read a stored node kind without ever raising on an unknown one."""
    try:
        return NodeKind(value)
    except ValueError:
        return RawKind(value)


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = []
"""No persisted model lives here — the vocabularies ride on the models that use them."""
