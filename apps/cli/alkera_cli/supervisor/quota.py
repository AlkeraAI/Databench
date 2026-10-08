"""Each org's share of the box's disk: a filesystem project quota on its root.

The data volume is formatted with ext4's project quotas and mounted
``prjquota`` (the node bootstrap). Each slot's root carries project id
:data:`PROJECT_BASE` + slot with the inherit flag, so everything the org's
worker and its chats write beneath it is charged to that project, and the
project's hard limit stops one org from filling the volume for every other.
The supervisor sets both on the root it made; it never walks beneath it.
How big the share is, is ``alkera_core.compute.disk.org_slot_bytes``'s to say,
the same arithmetic the server checks a purchase with.

A filesystem without project quotas (a developer box, a volume formatted
before this) refuses the steps; the worker still starts, and the supervisor
says the org shares the volume uncapped. Disk is availability, not disclosure:
the org's data stays its own either way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from alkera_cli.supervisor.slots import Slot

#: Where the slots' project ids start: clear of 0 (unassigned) and of the
#: small ids an operator might have used by hand.
PROJECT_BASE: Final = 100_000


def project_id(slot: Slot) -> int:
    return PROJECT_BASE + slot.index


def mountpoint_of(path: Path) -> Path:
    """The mount the path lives on: the setquota target."""
    current = path.resolve()
    while not os.path.ismount(current) and current != current.parent:
        current = current.parent
    return current


def quota_steps(
    slot: Slot, root: Path, *, limit_bytes: int, mountpoint: Path
) -> tuple[tuple[str, ...], ...]:
    """Charge everything beneath ``root`` to the slot's project, and cap the
    project (a hard block limit in KiB, no soft limit, inodes uncapped)."""
    project = str(project_id(slot))
    return (
        ("chattr", "+P", "-p", project, str(root)),
        ("setquota", "-P", project, "0", str(limit_bytes // 1024), "0", "0", str(mountpoint)),
    )


__all__ = [
    "PROJECT_BASE",
    "mountpoint_of",
    "project_id",
    "quota_steps",
]
