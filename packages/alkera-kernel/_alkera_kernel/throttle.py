"""Last-wins rate limiting for ``cell.output`` replaces.

At most one value is sent per interval. A value offered inside the interval
waits; a newer one replaces it; it is sent when the interval has passed
(``due``) or when the cell ends (``flush``). The clock is injected, so the
policy is checked without waiting on real time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")
#: Clock readings are floats; an interval that has passed must not miss by rounding.
_EPSILON = 1e-9


class ReplaceThrottle(Generic[T]):
    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = interval
        self._clock = clock
        self._last: float | None = None
        self._pending: T | None = None
        self._has_pending = False

    @property
    def has_pending(self) -> bool:
        return self._has_pending

    def offer(self, value: T) -> tuple[T | None, float | None]:
        """``(value to send now, None)`` when the interval has passed, else
        ``(None, delay)``: the value waits and ``due`` should be called after
        ``delay`` seconds."""
        now = self._clock()
        if self._last is None or now - self._last >= self.interval - _EPSILON:
            self._last = now
            self._pending, self._has_pending = None, False
            return value, None
        self._pending, self._has_pending = value, True
        return None, self._last + self.interval - now

    def due(self) -> tuple[T | None, float | None]:
        """The waiting value if its interval has passed; otherwise
        ``(None, delay until it has)`` (or ``(None, None)`` when nothing waits)."""
        if not self._has_pending:
            return None, None
        now = self._clock()
        assert self._last is not None
        if now - self._last < self.interval - _EPSILON:
            return None, self._last + self.interval - now
        value = self._pending
        self._last = now
        self._pending, self._has_pending = None, False
        return value, None

    def flush(self) -> T | None:
        """The waiting value now, whatever the interval (the cell ended)."""
        if not self._has_pending:
            return None
        value = self._pending
        self._last = self._clock()
        self._pending, self._has_pending = None, False
        return value
