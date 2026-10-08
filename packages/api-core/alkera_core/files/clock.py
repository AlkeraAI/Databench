"""Injectable time and id generation.

Trash windows, lease TTLs, upload-session expiry and `last_seen_at` day rolls
are all decided against a clock, and every one of them has a bug that only
shows up on the far side of a boundary. So Files takes its clock and its id
source as parameters: production passes `SystemClock()` / `SystemIdSource()`,
tests pass `FakeClock` / `SeededIdSource` and step across the boundary on
purpose. Code that cannot take a clock (a library grabbing `datetime.now()`
internally) is pinned with `freezegun` instead.
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    """Wall time for stamps and deadlines, monotonic time for durations."""

    def now(self) -> datetime:
        """The current instant, timezone-aware and in UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin that never moves backwards."""
        ...


class SystemClock:
    """The production clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """A clock a test drives by hand.

    `advance` moves wall time and monotonic time together. `move_to` sets wall
    time to an instant that may be *earlier* than the current one — a real host
    does that when NTP steps it — but monotonic time only ever moves forward,
    so a deadline measured with `monotonic()` cannot be un-expired by a clock
    step.
    """

    def __init__(self, now: datetime, monotonic: float = 0.0) -> None:
        self._now = _as_utc(now, "now")
        self._monotonic = monotonic

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: timedelta) -> None:
        """Move both clocks forward by `delta`."""
        if delta < timedelta(0):
            msg = f"a clock cannot advance backwards (got {delta!r})"
            raise ValueError(msg)
        self._now += delta
        self._monotonic += delta.total_seconds()

    def move_to(self, when: datetime) -> None:
        """Set wall time to `when`; monotonic time follows only if that is later."""
        target = _as_utc(when, "when")
        forward = (target - self._now).total_seconds()
        if forward > 0:
            self._monotonic += forward
        self._now = target


def _as_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field} must be an aware datetime (got {value!r})"
        raise ValueError(msg)
    return value.astimezone(UTC)


class IdSource(Protocol):
    """Where Files gets a new id, so a test can make a run reproducible."""

    def uuid(self) -> UUID:
        """A fresh id."""
        ...


class SystemIdSource:
    """The production id source."""

    def uuid(self) -> UUID:
        return uuid.uuid4()


class SeededIdSource:
    """Deterministic uuid4-shaped ids, so a failing run replays identically.

    The bits come from `random.Random(seed)` — reproducible, and deliberately
    not a source of randomness anything security-sensitive may use.
    """

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)  # noqa: S311 — reproducibility, not secrecy

    def uuid(self) -> UUID:
        return UUID(int=self._random.getrandbits(128), version=4)
