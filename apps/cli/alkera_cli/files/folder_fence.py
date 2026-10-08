"""Nothing on a box writes a folder whose lease is in doubt.

A box that stops hearing from the server stops its own live sync within two
beats (``files.mount.SelfFence``), but its lease runs for its whole TTL and
the box cannot be told when another box takes it. A notebook kernel writing
the folder in that window writes what the next holder's sync then undoes.

So the folder custody's verdict (``FolderCustody.fenced``) is the one answer
both planes read: the live sync sends nothing while it is fenced, and every
holder registered here (the notebook kernels) is frozen and refuses new runs
until the fence opens again, or until the folder is put away for good.
:func:`run` asks every :data:`POLL_SECONDS`, so a kernel is frozen within that
of the sync stopping.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

logger = logging.getLogger(__name__)

#: How often the verdict is read; cheap (a clock read per held folder).
POLL_SECONDS: Final = 2.0

Fenced = Callable[[str], bool]
#: A holder brings what it holds into line with the verdict for each key.
Holder = Callable[[Fenced], Awaitable[None]]

_HOLDERS: list[Holder] = []


def register_fence_holder(holder: Holder) -> None:
    """Add a holder of writers into folders (the notebook kernels)."""
    if holder not in _HOLDERS:
        _HOLDERS.append(holder)


async def apply(fenced: Fenced) -> None:
    """Every holder, told the verdict once. One that fails is logged and
    asked again on the next pass; it never keeps the others from theirs."""
    for holder in list(_HOLDERS):
        try:
            await holder(fenced)
        except Exception:
            logger.exception("a folder fence holder could not apply the verdict")


async def run(
    fenced: Fenced,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval: float = POLL_SECONDS,
) -> None:
    """Apply the verdict every ``interval`` seconds, for the life of the box."""
    while True:
        await apply(fenced)
        await sleep(interval)


__all__ = ["POLL_SECONDS", "Fenced", "Holder", "apply", "register_fence_holder", "run"]
