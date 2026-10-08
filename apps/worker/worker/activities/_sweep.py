"""The shape every periodic sweep activity shares.

A sweep reads its clock (pinned by the run, or the wall clock), runs its core
under the job's advisory lock while heartbeating, and treats a lock another run
holds as "nothing to do this tick". The activity keeps only what differs: its
name, its lock, its core and how it reports the result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from alkera_core.logging import get_logger
from alkera_core.schemas.temporal import DrainInput, SweepInput

from worker.tasks._hardening import run_locked
from worker.temporal.heartbeat import heartbeating

T = TypeVar("T")

log = get_logger(__name__)


def clock(input: SweepInput | DrainInput | None) -> datetime:
    """The pinned clock when the run carries one, else the wall clock."""
    if input is not None and input.now is not None:
        return input.now
    return datetime.now(UTC)


async def locked_sweep(
    lock: str,
    body: Callable[[], Awaitable[T]],
    *,
    skipped_event: str | None = None,
    budget: timedelta | None = None,
) -> T | None:
    """Run ``body`` under the advisory lock ``lock`` while heartbeating.

    Returns the body's result, or ``None`` when another run holds the lock (the
    body never ran). ``skipped_event``, when given, is logged on that skip.
    ``budget`` bounds the run (:func:`~worker.tasks._hardening.run_locked`).
    """
    async with heartbeating():
        result = await run_locked(lock, body, budget=budget)
    if result is None and skipped_event is not None:
        log.info(skipped_event)
    return result


__all__ = ["clock", "locked_sweep"]
