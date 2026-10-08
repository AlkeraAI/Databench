"""When a live sync round that did not land is offered again.

A failed round is not a lost queue: what it carried is still on disk and
still owed, and the wait only keeps a drive that is refusing every call from
being asked twice a second per chat. A round nothing answered (the API
restarting, a connection reset) is the exception for the first seconds of a
streak: a connect costs the server nothing, and the agent's edit a person is
watching for goes the moment it is back up. At 1, 2 and 4 s the edit waited
up to three seconds past the API's return.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx

from alkera_cli.host.backoff import doubled

__all__ = [
    "RETRY_FIRST_WAIT",
    "RETRY_MAX_WAIT",
    "TRANSPORT_RETRY_FOR",
    "TRANSPORT_RETRY_WAIT",
    "RoundRetry",
]

#: The first wait of a streak of refused rounds, doubling from there.
RETRY_FIRST_WAIT: Final = 1.0
#: The longest a round that did not land waits before it is tried again.
RETRY_MAX_WAIT: Final = 30.0
#: How soon a round nothing answered is tried again (seconds), for the first
#: :data:`TRANSPORT_RETRY_FOR` seconds of a streak of failed rounds.
TRANSPORT_RETRY_WAIT: Final = 0.5
TRANSPORT_RETRY_FOR: Final = 15.0


@dataclass
class RoundRetry:
    """A streak of rounds that did not land, and when the next may go."""

    failing: bool = False
    wait: float = 0.0
    at: float = 0.0
    since: float = 0.0

    def failed(self, failure: BaseException, now: float) -> bool:
        """A round failed at ``now`` (monotonic); whether it began a streak."""
        began = not self.failing
        if began:
            self.since = now
        if isinstance(failure, httpx.TransportError) and now - self.since < TRANSPORT_RETRY_FOR:
            self.wait = TRANSPORT_RETRY_WAIT
        else:
            self.wait = doubled(self.wait, floor=RETRY_FIRST_WAIT, cap=RETRY_MAX_WAIT)
        self.at = now + self.wait
        self.failing = True
        return began

    def landed(self) -> None:
        self.failing, self.wait, self.at = False, 0.0, 0.0

    def due(self, now: float) -> bool:
        return now >= self.at
