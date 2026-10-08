"""The folder lease and its epoch arithmetic.

An epoch is `(restore_generation << 32) + seq`: the low half is the per-node
high-water mark, the high half is a platform counter the restore runbook bumps
before the database is opened for writes. That way an epoch issued after a
restore is above every epoch the restore lost, so a client that was fenced
before the restore cannot come back and write at a "current" epoch.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import ClassVar, Literal, get_args

from alkera_core.files.ids import NodeId
from alkera_core.versioning import VersionedModel

from .sharing import Principal

LeasePurpose = Literal["mount", "box", "share", "chat", "workspace"]

#: What a node under a live lease is doing right now, while the drive still
#: holds the previous copy: the first four are the holder pushing bytes out,
#: the last three a change the holder has yet to apply locally. The column's
#: CHECK is built from the same seven words (``alkera_core.models.files``).
LiveEntryState = Literal[
    "writing",
    "uploading",
    "on_box",
    "deferred",
    "inbound",
    "inbound_delete",
    "inbound_rename",
]
#: The same vocabulary as a tuple, so a caller can iterate it without
#: reaching into ``typing`` internals.
LIVE_ENTRY_STATE_VALUES: tuple[str, ...] = get_args(LiveEntryState)

SEQ_BITS = 32
SEQ_MAX = (1 << SEQ_BITS) - 1


class Lease(VersionedModel):
    """One holder's exclusive write claim on a folder's subtree."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    node_id: NodeId | None = None
    epoch: int = 0
    holder: Principal = Principal()
    instance_id: str = ""
    machine_id: str = ""
    purpose: LeasePurpose = "mount"
    acquired_at: datetime | None = None
    heartbeat_at: datetime | None = None
    expires_at: datetime | None = None
    last_sync_at: datetime | None = None
    stale: bool = False
    grantable_after: datetime | None = None


def epoch_for(restore_generation: int, seq: int) -> int:
    """Compose an epoch from the platform generation and the per-node sequence."""
    if restore_generation < 0:
        raise ValueError("restore_generation must not be negative")
    if not 0 <= seq <= SEQ_MAX:
        raise ValueError(f"seq must fit in {SEQ_BITS} bits")
    return (restore_generation << SEQ_BITS) + seq


def split_epoch(epoch: int) -> tuple[int, int]:
    """The inverse of `epoch_for`: `(restore_generation, seq)`."""
    if epoch < 0:
        raise ValueError("epoch must not be negative")
    return epoch >> SEQ_BITS, epoch & SEQ_MAX


FIXTURE_EXAMPLES: list[tuple[str, Callable[[], VersionedModel]]] = [
    (
        "lease",
        lambda: Lease(
            epoch=epoch_for(2, 17),
            holder=Principal(kind="user", id="6b1f0b1e-0000-4000-8000-000000000002"),
            instance_id="instance-a",
            machine_id="machine-a",
            purpose="mount",
            acquired_at=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
            heartbeat_at=datetime.fromisoformat("2026-01-01T12:00:30+00:00"),
            expires_at=datetime.fromisoformat("2026-01-01T12:01:30+00:00"),
            last_sync_at=datetime.fromisoformat("2026-01-01T12:00:18+00:00"),
            grantable_after=datetime.fromisoformat("2026-01-01T12:02:30+00:00"),
        ),
    ),
]
