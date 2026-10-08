"""Operation state and the inverse an undo replays.

`file_ops.state` and `file_ops.inverse` are JSONB read by whichever backend
version polls the operation next, so both are `VersionedModel`s. The inverse is
a tagged union with a `RawInverse` catch-all: an operation kind added by a newer
writer must still round-trip through an older reader instead of raising.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Union

from pydantic import Discriminator, Field, Tag

from alkera_core.versioning import VersionedModel, make_unknown_tag_discriminator


class OperationKind(StrEnum):
    MOVE = "move"
    RENAME = "rename"
    COPY = "copy"
    TRASH = "trash"
    RESTORE = "restore"
    DELETE = "delete"
    ATTRS = "attrs"
    UPLOAD = "upload"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationError(VersionedModel):
    """A failure inside a batched operation — ids and a code, never a name."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    code: str = ""
    node_id: str | None = None


class OperationConflict(VersionedModel):
    """A node the operation could not apply cleanly, and what became of it."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    node_id: str = ""
    code: str = ""
    conflict_node_id: str | None = None


class OperationState(VersionedModel):
    """The progress record an `Operation` resource is rendered from."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: OperationKind = OperationKind.MOVE
    state: OperationStatus = OperationStatus.PENDING
    done: int = 0
    total: int = 0
    bytes: int = 0
    skipped: int = 0
    conflicts: list[OperationConflict] = Field(default_factory=list)
    errors: list[OperationError] = Field(default_factory=list)
    result_url: str | None = None
    result_url_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cursor: str | None = None


class _Inverse(VersionedModel):
    """Base for every inverse: the tag lives on `kind`."""

    __abstract__: ClassVar[bool] = True


class MoveInverse(_Inverse):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal[OperationKind.MOVE] = OperationKind.MOVE
    node_id: str = ""
    previous_parent_id: str = ""


class RenameInverse(_Inverse):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal[OperationKind.RENAME] = OperationKind.RENAME
    node_id: str = ""
    previous_name: str = ""


class TrashInverse(_Inverse):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal[OperationKind.TRASH] = OperationKind.TRASH
    trash_op_id: str = ""
    node_ids: list[str] = Field(default_factory=list)


class RestoreInverse(_Inverse):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal[OperationKind.RESTORE] = OperationKind.RESTORE
    trash_op_id: str = ""
    node_ids: list[str] = Field(default_factory=list)


class AttrsInverse(_Inverse):
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: Literal[OperationKind.ATTRS] = OperationKind.ATTRS
    node_id: str = ""
    before: dict[str, Any] = Field(default_factory=dict)


class RawInverse(_Inverse):
    """The catch-all a newer writer's kind routes to, so the payload survives."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    kind: str = "__unknown__"
    payload: dict[str, Any] = Field(default_factory=dict)


INVERSE_KINDS: frozenset[str] = frozenset(
    {
        OperationKind.MOVE.value,
        OperationKind.RENAME.value,
        OperationKind.TRASH.value,
        OperationKind.RESTORE.value,
        OperationKind.ATTRS.value,
    }
)

_inverse_tag = make_unknown_tag_discriminator(set(INVERSE_KINDS), field="kind")

OperationInverse = Annotated[
    Union[  # noqa: UP007 - Pydantic needs the explicit Union for the tagged members
        Annotated[MoveInverse, Tag(OperationKind.MOVE.value)],
        Annotated[RenameInverse, Tag(OperationKind.RENAME.value)],
        Annotated[TrashInverse, Tag(OperationKind.TRASH.value)],
        Annotated[RestoreInverse, Tag(OperationKind.RESTORE.value)],
        Annotated[AttrsInverse, Tag(OperationKind.ATTRS.value)],
        Annotated[RawInverse, Tag("__unknown__")],
    ],
    Discriminator(_inverse_tag),
]


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    (
        "operation_state",
        lambda: OperationState(
            kind=OperationKind.MOVE,
            state=OperationStatus.RUNNING,
            done=17,
            total=100,
            bytes=65_536,
            skipped=1,
            conflicts=[OperationConflict(node_id="n-1", code="files.name_taken")],
            errors=[OperationError(code="files.quota_exceeded", node_id="n-2")],
            heartbeat_at=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
            cursor="cursor-17",
        ),
    ),
    ("operation_inverse_move", lambda: MoveInverse(node_id="n-1", previous_parent_id="p-0")),
    ("operation_inverse_rename", lambda: RenameInverse(node_id="n-1", previous_name="report.pdf")),
    ("operation_inverse_trash", lambda: TrashInverse(trash_op_id="t-1", node_ids=["n-1", "n-2"])),
    ("operation_inverse_restore", lambda: RestoreInverse(trash_op_id="t-1", node_ids=["n-1"])),
    ("operation_inverse_attrs", lambda: AttrsInverse(node_id="n-1", before={"mode": 33188})),
]
