"""When an org's worker that keeps failing is in a crash loop.

One rule, from one record per org of its worker's failures: an exit nobody
asked for, or a start that did not happen. It says two things, at two
thresholds, because two readers ask:

* **failing**, for the machine's beat (placement and the fleet page read
  it): :data:`CRASH_LOOP_FAILURES` failures in a row with no worker settling
  in between. A worker settles by staying up :data:`SETTLED_SECONDS`; one
  that settles, or is asked to stop, ends the streak, and a failure after a
  settled run starts a new streak at one.
* **looping**, for the ``supervisor.worker.crash_loop`` alarm ops are paged
  on: more than :data:`CRASH_LOOP_EXITS` failures within
  :data:`CRASH_LOOP_WINDOW_SECONDS`, however long each run lasted, so a worker
  that dies every two minutes is caught too. Only time ends it.

Nothing here reads a clock: every call is told the time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Final

#: This many failures in a row, none after a settled run, and the org's worker
#: is failing: the beat says so until one settles.
CRASH_LOOP_FAILURES: Final = 3
#: A worker up this long has settled: what failed before it is over, for the
#: streak and for the wait before the next start.
SETTLED_SECONDS: Final = 60.0
#: More than this many failures within the window is a crash loop.
CRASH_LOOP_EXITS: Final = 5
CRASH_LOOP_WINDOW_SECONDS: Final = 15 * 60.0


@dataclass(frozen=True, slots=True)
class Failure:
    """What one failure made of its org's record."""

    #: Failures in a row, this one included.
    streak: int
    #: This is the failure that made the org failing.
    began_failing: bool
    #: The failures in the window when they make a crash loop, else ``None``.
    looping: int | None


class CrashLoop:
    """Each org's worker failures, and what they add up to."""

    def __init__(
        self,
        *,
        failures: int = CRASH_LOOP_FAILURES,
        settled_seconds: float = SETTLED_SECONDS,
        exits: int = CRASH_LOOP_EXITS,
        window: float = CRASH_LOOP_WINDOW_SECONDS,
    ) -> None:
        self._failures = failures
        self._settled = settled_seconds
        self._exits = exits
        self.window: Final = window
        self._streak: dict[str, int] = {}
        self._seen: dict[str, deque[float]] = {}

    @property
    def failing(self) -> frozenset[str]:
        """The orgs whose worker is failing."""
        return frozenset(org for org, n in self._streak.items() if n >= self._failures)

    def settled(self, ran: float) -> bool:
        """Whether a worker that has run ``ran`` seconds has settled."""
        return ran >= self._settled

    def failed(self, org_id: str, *, now: float, ran: float | None) -> Failure:
        """An org's worker exited unasked at ``now`` after ``ran`` seconds, or
        (``ran`` is ``None``) could not be started."""
        was = org_id in self.failing
        fresh = ran is not None and self.settled(ran)
        streak = 1 if fresh else self._streak.get(org_id, 0) + 1
        self._streak[org_id] = streak
        seen = self._seen.setdefault(org_id, deque())
        seen.append(now)
        while seen[0] <= now - self.window:
            seen.popleft()
        return Failure(
            streak=streak,
            began_failing=not was and streak >= self._failures,
            looping=len(seen) if len(seen) > self._exits else None,
        )

    def up(self, org_id: str, *, ran: float) -> None:
        """The org's worker has been up ``ran`` seconds: settled, its streak
        is over."""
        if self.settled(ran):
            self._streak.pop(org_id, None)

    def stopped(self, org_id: str) -> None:
        """The org's worker ended because it was asked to."""
        self._streak.pop(org_id, None)

    def forget(self, org_id: str) -> None:
        """The org left the box: nothing of it is kept."""
        self._streak.pop(org_id, None)
        self._seen.pop(org_id, None)


__all__ = [
    "CRASH_LOOP_EXITS",
    "CRASH_LOOP_FAILURES",
    "CRASH_LOOP_WINDOW_SECONDS",
    "SETTLED_SECONDS",
    "CrashLoop",
    "Failure",
]
