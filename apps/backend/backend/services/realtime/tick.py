"""How a socket's periodic tick treats the database, and when a socket's
session ends.

Every per-tick database call on a socket (the standing recheck, the machine
check, the presence heartbeat) follows one rule. A contention error (a held
lock, a full pool, a database restarting) skips the beat: the socket stays
open and asks again on the next one. Closing it instead made every open tab
reconnect at once, into the very queue that refused the read. Only a definite
"no" closes the socket, or a failure that is not contention, or contention
that has outlasted :data:`BEAT_SKIP_LIMIT` beats in a row: a socket whose
standing nobody has been able to check for that long fails closed.

The session deadline is spread so that sockets reconnected together after a
restart do not all expire together again.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from alkera_core.db.errors import contention

#: Beats in a row a socket may skip on contention before it is closed.
BEAT_SKIP_LIMIT = 4
#: The share of the session ceiling a deadline may come early by.
DEADLINE_SPREAD = 0.1


def session_deadline_seconds(ceiling: float, draw: Callable[[], float] = random.random) -> float:
    """How long this socket's session runs: the ceiling, shortened by up to
    :data:`DEADLINE_SPREAD` of it. Never longer, since the ceiling is a bound
    on how long one admission is trusted; earlier is always safe."""
    return ceiling * (1.0 - DEADLINE_SPREAD * draw())


@dataclass(slots=True)
class BeatFaults:
    """The tick's count of beats skipped in a row."""

    limit: int = BEAT_SKIP_LIMIT
    skipped: int = 0

    def tolerate(self, exc: BaseException) -> bool:
        """Whether a beat that raised ``exc`` is skipped (``True``) rather
        than closing the socket."""
        if contention(exc) is None:
            return False
        self.skipped += 1
        return self.skipped <= self.limit

    def landed(self) -> None:
        """A beat reached the database: the count starts again."""
        self.skipped = 0


__all__ = ["BEAT_SKIP_LIMIT", "DEADLINE_SPREAD", "BeatFaults", "session_deadline_seconds"]
