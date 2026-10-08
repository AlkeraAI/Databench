"""How long a supervised restart in place waits for the chats it keeps.

Nothing is handed back on a restart: the next process on the box takes every
chat straight back under the lease this one held. What a restart waits for is
the work the next process cannot pick up. A turn it can restart, so a turn
gets the short restart ceiling. A background job it cannot (the job lives in
this process), so while a job runs the restart waits up to its job ceiling
(minutes, never past the drain ceiling). The ceiling follows the jobs running
at each poll: once the last one ends, what is still working (the turn the
model runs on the job's result, or any other chat) gets the short ceiling
again, counted from that moment, and a job seen once never holds the restart
open after it ended. A job still running when its ceiling passes is reported
to its chat as interrupted by the next process (``harness/interrupted_jobs.py``).

The wait logs its bound when it starts and its outcome when it ends, so a box
journal says how long a restart held its chats and whether any were cut.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from alkera_core.compute.liveness import RESTART_DRAIN_CEILING_SECONDS

from alkera_cli.cloud.activity import ChatActivity
from alkera_cli.supervisor.org_resume import RESTART_JOB_CEILING_SECONDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestartWait:
    """What a restart's wait found: how many chats held it at the start, how
    long it waited, and how many (and of them on a job) it left working."""

    held: int
    waited: float
    still_working: int
    on_a_job: int


def job_ceiling(drain_ceiling: float) -> float:
    """How long since its start a restart waits while a background job runs."""
    return max(
        float(RESTART_DRAIN_CEILING_SECONDS),
        min(float(RESTART_JOB_CEILING_SECONDS), drain_ceiling),
    )


def _held(activities: Callable[[], Sequence[ChatActivity]]) -> list[ChatActivity]:
    return [a for a in activities() if a.holds_a_stop]


async def wait_for_in_flight(
    activities: Callable[[], Sequence[ChatActivity]],
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    drain_ceiling: float,
    poll: float,
) -> RestartWait:
    """Return once nothing ``activities`` reports holds a stop, or at the
    ceiling the work running now earns."""
    started = clock()
    #: When a job was last seen running; the short ceiling counts from it.
    settled_from = started
    held = _held(activities)
    if not held:
        return RestartWait(held=0, waited=0.0, still_working=0, on_a_job=0)
    first = len(held)
    bound = job_ceiling(drain_ceiling) if ChatActivity.RUNNING_JOB in held else None
    logger.info(
        "restart: waiting up to %.0f s for %d chat(s) still working to finish",
        bound if bound is not None else RESTART_DRAIN_CEILING_SECONDS,
        first,
    )
    while held:
        jobs = held.count(ChatActivity.RUNNING_JOB)
        now = clock()
        if jobs:
            settled_from = now
            deadline = started + job_ceiling(drain_ceiling)
        else:
            deadline = settled_from + RESTART_DRAIN_CEILING_SECONDS
        left = deadline - now
        if left <= 0:
            logger.warning(
                "restarting with %d chat(s) still working, %d of them on a job, after %.0f s: "
                "the next process ends or resumes their turns",
                len(held),
                jobs,
                now - started,
            )
            return RestartWait(
                held=first, waited=now - started, still_working=len(held), on_a_job=jobs
            )
        await sleep(min(poll, left))
        held = _held(activities)
    waited = clock() - started
    logger.info("restart: all %d chat(s) finished their work in %.0f s", first, waited)
    return RestartWait(held=first, waited=waited, still_working=0, on_a_job=0)


__all__ = ["RestartWait", "job_ceiling", "wait_for_in_flight"]
