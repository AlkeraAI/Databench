"""The memory ceiling on every sandboxed chat together.

Each chat's cgroup is capped on its own (2 GB on a pool box, 16 GB on a
dedicated one), but the parent ``alkera.slice`` they all hang under had no
ceiling, and the box's chat count is budgeted on what an IDLE agent holds. A
few chats running builds at once could push the whole machine into a global
OOM, where the kernel may pick the daemon — and every chat on the box dies with
it. With ``memory.max`` set on the parent to the box's memory less the daemon's
reserve, the kernel's OOM stays inside the slice: it kills a chat, never the
daemon.

Best-effort and idempotent: a box without the slice (no sandbox, macOS, a
container with a read-only cgroupfs) is left as it is.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: The parent slice's memory ceiling, the same path under systemd and under a
#: bare cgroup v2 hierarchy (``sandbox-prereqs.sh`` creates it either way).
CHAT_SLICE_MEMORY_MAX = Path("/sys/fs/cgroup/alkera.slice/memory.max")
#: Below this the ceiling would starve every chat; a box that small is left to
#: the per-chat caps rather than bounded to nothing.
MIN_CHAT_SLICE_BYTES = 512 * 1024 * 1024


def chat_slice_memory_bytes(limit_bytes: int | None, *, reserve_bytes: int) -> int | None:
    """What the chats together may hold: the box's memory less what the daemon
    keeps for itself, or ``None`` when the memory is unknown or too small to
    leave the chats a useful share."""
    if limit_bytes is None:
        return None
    share = limit_bytes - reserve_bytes
    if share < MIN_CHAT_SLICE_BYTES:
        return None
    return share


def bound_chat_slice(
    limit_bytes: int | None,
    *,
    reserve_bytes: int,
    path: Path = CHAT_SLICE_MEMORY_MAX,
) -> int | None:
    """Write the chats' shared ceiling onto ``path``. Returns the bytes
    written, or ``None`` when nothing was: no slice on this box, a memory that
    cannot be known, or a cgroupfs this process may not write."""
    share = chat_slice_memory_bytes(limit_bytes, reserve_bytes=reserve_bytes)
    if share is None or not path.exists():
        return None
    try:
        path.write_text(f"{share}\n", encoding="ascii")
    except OSError as exc:
        logger.warning(
            "the chats' shared memory ceiling was not set on %s (%s); only the per-chat "
            "limits bound them",
            path,
            exc,
        )
        return None
    logger.info(
        "the sandboxed chats together may hold %d MiB; the rest is the daemon's",
        share // (1024 * 1024),
    )
    return share


__all__ = [
    "CHAT_SLICE_MEMORY_MAX",
    "MIN_CHAT_SLICE_BYTES",
    "bound_chat_slice",
    "chat_slice_memory_bytes",
]
