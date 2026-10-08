"""What a node looked like before and after one mutation.

``file_history.before`` / ``after`` hold this shape. History is compliance
evidence that must stay readable for years, which sets two rules the fields
follow: every field is optional (a ``rename`` row snapshots the name and nothing
else — a row of defaults would be a lie about the other attributes), and the
``kind`` and ``state`` are read back through the tolerant readers in
:mod:`alkera_core.schemas.files.kinds` rather than as enums, so a snapshot
written by a newer backend still loads on an older one.

The name is bytes, exactly as it arrived from the filesystem, and travels as
base64 — a filename is not text and re-encoding it would lose the very bytes
history exists to preserve.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from alkera_core.schemas.files.attrs import Base64Bytes, NodeAttrs
from alkera_core.schemas.files.kinds import NodeKind, NodeState, RawKind, parse_kind
from alkera_core.versioning import VersionedModel


class HistoryKind(StrEnum):
    """The mutations that get a history row. One row per mutation, in its own txn."""

    CREATE = "create"
    RENAME = "rename"
    MOVE = "move"
    ATTRS = "attrs"
    TRASH = "trash"
    RESTORE = "restore"
    LOCK = "lock"
    HOLD = "hold"
    LABEL = "label"
    ACL = "acl"


class HistorySnapshot(VersionedModel):
    """One side (``before`` or ``after``) of a recorded mutation."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    #: The filename verbatim; base64 on the wire because it is not text.
    name: Base64Bytes | None = None
    parent_id: str | None = None
    #: Stored as a string and read through :meth:`node_kind`, never as an enum,
    #: so a kind a newer writer introduced does not break this reader.
    kind: str | None = None
    attrs: NodeAttrs | None = None
    acl_id: str | None = None
    state: str | None = None
    trashed_at: datetime | None = None

    def node_kind(self) -> NodeKind | RawKind | None:
        """The kind as a value, tolerating one this reader has never seen."""
        return None if self.kind is None else parse_kind(self.kind)


def _snapshot_example() -> HistorySnapshot:
    return HistorySnapshot(
        name=b"quarterly-report.json",
        parent_id="1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
        kind=NodeKind.FILE.value,
        attrs=NodeAttrs(mode=0o100644, uid=501, gid=20, mtime_ns=1_767_268_800_123_456_789),
        acl_id="b1c2d3e4-f506-4718-89ab-cdef01234567",
        state=NodeState.LIVE.value,
        trashed_at=None,
        metadata={"history_kind": HistoryKind.RENAME.value},
    )


def _trashed_snapshot_example() -> HistorySnapshot:
    return HistorySnapshot(
        name=b"draft.csv",
        parent_id="1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
        kind=NodeKind.FILE.value,
        state=NodeState.LIVE.value,
        trashed_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        metadata={"history_kind": HistoryKind.TRASH.value},
    )


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    ("history_snapshot", _snapshot_example),
    ("history_snapshot_trashed", _trashed_snapshot_example),
]
