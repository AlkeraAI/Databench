"""How long to wait before trying again, for every part of the CLI.

One owner for the growing wait and for reading a server's ``Retry-After``, so
the files sync, the box, the supervisor, the harness and the plugins compute
both the same way. Each caller keeps its own tuning (first wait, ceiling,
whether to jitter); what is shared is the arithmetic. Everything that reads a
clock, sleeps or draws a random number takes it as a parameter, so a test can
pin all three.

Nothing here imports anything else from ``alkera_cli``: it sits under every
package that retries.

The reconnect backoff deserves its own word. A socket that comes back for a
moment and drops again would, with the usual "reset on connect" rule, hammer a
struggling server at the floor delay on every flap, and a whole fleet of
clients arrives at once the instant the server lets them in. So :class:`ReconnectBackoff` only
steps its attempt counter DOWN after the connection has stayed healthy for a
sustained period, one step per period, and the delay is jittered so a fleet
reconnecting after one outage spreads out instead of arriving as a single
wave. Its three bounds are the one thing read from the environment, and only
when the caller names none of them: a box behind a link that takes a minute to
come back has to be able to say so, and it cannot reach every construction
site.
"""

from __future__ import annotations

import math
import os
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

from alkera_cli.host.limits import env_seconds

T = TypeVar("T")

#: The floor and the ceiling on the wait between socket reconnects, and how
#: long a connection must stay up before the attempt counter steps down one
#: level. The delay is jittered over ``[0.5, 1.5)`` of the computed wait, which
#: is what spreads a fleet reconnecting after one outage — the spread is part
#: of the algorithm and is not tunable; what it is applied TO is.
ENV_RECONNECT_BASE_SECONDS = "ALKERA_CLOUD_RECONNECT_BASE_SECONDS"
DEFAULT_RECONNECT_BASE_SECONDS = 2.0
ENV_RECONNECT_CAP_SECONDS = "ALKERA_CLOUD_RECONNECT_CAP_SECONDS"
DEFAULT_RECONNECT_CAP_SECONDS = 60.0
ENV_RECONNECT_HEALTHY_PERIOD_SECONDS = "ALKERA_CLOUD_RECONNECT_HEALTHY_PERIOD_SECONDS"
DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS = 300.0


# --- the growing wait ---------------------------------------------------------


def exponential_delay(attempt: int, *, first: float, cap: float) -> float:
    """The wait before retry number ``attempt`` (0 for the first retry):
    ``first * 2**attempt``, capped at ``cap``."""
    if attempt < 0:
        raise ValueError("attempt counts from zero")
    # Past the cap the exponent is irrelevant, and a large one overflows.
    if first > 0 and cap > 0 and attempt > math.log2(cap / first) + 1:
        return float(cap)
    return float(min(cap, first * (2**attempt)))


def doubled(previous: float, *, floor: float, cap: float) -> float:
    """The wait after one that lasted ``previous``: twice as long, no shorter
    than ``floor`` (so a streak's first wait, from zero, is the floor) and no
    longer than ``cap``."""
    return float(min(cap, max(floor, previous * 2)))


def doubling_wait(failures: int, *, base: float, cap: float) -> float:
    """The wait after ``failures`` refusals in a row of something only a
    change elsewhere can fix: ``base * 2**failures``, capped at ``cap``. No
    jitter — these are per-chat retries on a box's own poll, not a fleet
    reconnecting at once."""
    if failures < 1:
        return 0.0
    return exponential_delay(failures, first=base, cap=cap)


def retry_until(
    attempt: Callable[[], T],
    *,
    retry_on: type[BaseException] | tuple[type[BaseException], ...],
    timeout: float,
    first: float,
    cap: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call ``attempt`` until it returns, sleeping a doubling wait (``first``
    up to ``cap``) after each ``retry_on`` failure. The failure that arrives
    at or past ``timeout`` seconds is raised."""
    deadline = clock() + timeout
    tries = 0
    while True:
        try:
            return attempt()
        except retry_on:
            if clock() >= deadline:
                raise
            sleep(exponential_delay(tries, first=first, cap=cap))
            tries += 1


# --- Retry-After --------------------------------------------------------------


def parse_retry_after(
    value: str | None, *, wall_clock: Callable[[], datetime] | None = None
) -> float | None:
    """The seconds a ``Retry-After`` value asks for, or ``None``.

    Delta-seconds (fractions accepted) is read as is; a negative, NaN or
    unreadable value is ``None``, so the caller's own wait applies. The
    HTTP-date form is read only when the caller passes ``wall_clock``: it is a
    wait computed by trusting this machine's clock against the server's, and a
    caller that cannot afford a skewed clock leaves it out and gets ``None``.
    A date already past asks for no wait.
    """
    if value is None:
        return None
    said = str(value).strip()
    try:
        seconds = float(said)
    except ValueError:
        if wall_clock is None:
            return None
        return _seconds_until(said, wall_clock())
    if math.isnan(seconds) or seconds < 0:
        return None
    return seconds


def retry_after_of(
    failure: BaseException, *, wall_clock: Callable[[], datetime] | None = None
) -> float | None:
    """The seconds the ``Retry-After`` on a refused request's response asks
    for, or ``None``: read from ``failure.response.headers`` (an httpx
    ``HTTPStatusError`` carries both), with :func:`parse_retry_after`'s rules."""
    response = getattr(failure, "response", None)
    headers = getattr(response, "headers", None)
    said = headers.get("Retry-After") if headers is not None else None
    return parse_retry_after(said, wall_clock=wall_clock)


def _seconds_until(date: str, now: datetime) -> float | None:
    try:
        at = parsedate_to_datetime(date)
    except (TypeError, ValueError, IndexError):
        return None
    if at.tzinfo is None or now.tzinfo is None:
        return None
    return max(0.0, (at - now).total_seconds())


# --- the reconnect backoff ----------------------------------------------------


def reconnect_bounds(env: Mapping[str, str] | None = None) -> tuple[float, float, float]:
    """``(base, cap, healthy period)`` for the socket reconnect backoff.

    Returned together because they are only valid together: the backoff refuses
    a cap below the base, and a deployment that raises one and forgets the
    other would take the box down at construction rather than at the first
    reconnect. A cap read below the base is widened to it here. Zero, negative
    and unreadable values read as the shipped default: a reconnect every zero
    seconds is a spin, not a choice.
    """
    source = os.environ if env is None else env

    def seconds(name: str, default: float) -> float:
        parsed = env_seconds(source.get(name), default=default)
        return default if parsed is None else parsed

    base = seconds(ENV_RECONNECT_BASE_SECONDS, DEFAULT_RECONNECT_BASE_SECONDS)
    cap = seconds(ENV_RECONNECT_CAP_SECONDS, DEFAULT_RECONNECT_CAP_SECONDS)
    healthy = seconds(
        ENV_RECONNECT_HEALTHY_PERIOD_SECONDS, DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS
    )
    return base, max(base, cap), healthy


class ReconnectBackoff:
    """Jittered exponential backoff: ``base * 2**attempt`` capped at ``cap``,
    scaled by a factor in ``[0.5, 1.5)``. ``next_delay`` returns the wait
    before the next attempt and counts it; ``connected`` records the moment the
    socket came up WITHOUT touching the counter; ``decay`` steps the counter
    down one level for every healthy period that has elapsed since."""

    def __init__(
        self,
        *,
        base: float | None = None,
        cap: float | None = None,
        healthy_period: float | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        env_base, env_cap, env_healthy = reconnect_bounds()
        base = env_base if base is None else base
        cap = env_cap if cap is None else cap
        healthy_period = env_healthy if healthy_period is None else healthy_period
        if base <= 0 or cap < base or healthy_period <= 0:
            raise ValueError("backoff needs 0 < base <= cap and a positive healthy period")
        self._base = base
        self._cap = cap
        self._healthy_period = healthy_period
        self._rng = rng
        self._clock = clock
        self._attempt = 0
        self._connected_at: float | None = None
        self._decayed_at: float | None = None

    @property
    def attempt(self) -> int:
        """How many delays have been handed out net of decay."""
        return self._attempt

    def next_delay(self) -> float:
        """The wait before the next connection attempt, in seconds."""
        raw = exponential_delay(self._attempt, first=self._base, cap=self._cap)
        self._attempt += 1
        self._connected_at = None
        self._decayed_at = None
        return float(raw * (0.5 + self._rng()))

    def connected(self) -> None:
        """The socket came up. Deliberately does NOT reset ``attempt``: a
        connection that flaps must keep paying the delay it earned."""
        now = self._clock()
        self._connected_at = now
        self._decayed_at = now

    def decay(self) -> int:
        """Step the counter down one level per full healthy period the current
        connection has been up. Returns the attempt count after the step."""
        if self._connected_at is None or self._decayed_at is None:
            return self._attempt
        now = self._clock()
        while self._attempt > 0 and now - self._decayed_at >= self._healthy_period:
            self._attempt -= 1
            self._decayed_at += self._healthy_period
        return self._attempt


__all__ = [
    "DEFAULT_RECONNECT_BASE_SECONDS",
    "DEFAULT_RECONNECT_CAP_SECONDS",
    "DEFAULT_RECONNECT_HEALTHY_PERIOD_SECONDS",
    "ENV_RECONNECT_BASE_SECONDS",
    "ENV_RECONNECT_CAP_SECONDS",
    "ENV_RECONNECT_HEALTHY_PERIOD_SECONDS",
    "ReconnectBackoff",
    "doubled",
    "doubling_wait",
    "exponential_delay",
    "parse_retry_after",
    "reconnect_bounds",
    "retry_after_of",
    "retry_until",
]
