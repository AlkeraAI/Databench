"""How many org workers a box runs at once, from its memory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: The memory one org's worker is budgeted at, and the share of the box's
#: memory workers may take together; the rest is the chats'.
DEFAULT_WORKER_MEMORY_MB: Final = 400
WORKER_MEMORY_SHARE: Final = 4


def worker_capacity(env: Mapping[str, str], *, memory_total_bytes: int) -> int:
    """How many orgs this box runs a worker for at once: what the deployment
    names (``ALKERA_ORG_WORKER_BUDGET``), else as many workers as a quarter of
    the box's memory holds at ``ALKERA_ORG_WORKER_MEMORY_MB`` each. Never
    below one."""
    raw = env.get("ALKERA_ORG_WORKER_BUDGET", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    mb = env.get("ALKERA_ORG_WORKER_MEMORY_MB", "").strip()
    per_worker = (int(mb) if mb.isdigit() and int(mb) > 0 else DEFAULT_WORKER_MEMORY_MB) << 20
    return max(1, memory_total_bytes // (WORKER_MEMORY_SHARE * per_worker))


#: What the box keeps for itself (the supervisor, the system) before orgs share
#: the rest, and the least an org is ever given however many share the box.
HOST_MEMORY_RESERVE_BYTES: Final = 1 << 30
ORG_MEMORY_FLOOR_BYTES: Final = 1 << 30


def org_memory_share(env: Mapping[str, str], *, memory_total_bytes: int, orgs: int) -> int:
    """The memory one org may use on this box, its worker and its chats
    together, set as its cgroup's ``memory.max`` (which the worker's own
    admission reads). The box's memory less its reserve, split evenly over
    the ``orgs`` it serves now, never under the floor; a deployment that
    names ``ALKERA_ORG_WORKER_BUDGET`` splits it over that many instead.
    ``0`` (no limit) when the box's memory is unknown."""
    if memory_total_bytes <= 0:
        return 0
    raw = env.get("ALKERA_ORG_WORKER_BUDGET", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return memory_total_bytes // int(raw)
    usable = max(memory_total_bytes - HOST_MEMORY_RESERVE_BYTES, ORG_MEMORY_FLOOR_BYTES)
    return max(ORG_MEMORY_FLOOR_BYTES, usable // max(orgs, 1))


__all__ = [
    "DEFAULT_WORKER_MEMORY_MB",
    "HOST_MEMORY_RESERVE_BYTES",
    "ORG_MEMORY_FLOOR_BYTES",
    "WORKER_MEMORY_SHARE",
    "org_memory_share",
    "worker_capacity",
]
