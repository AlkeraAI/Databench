"""The sliding windows a realtime socket is held to: how often it may send
frames, and how many bytes."""

from __future__ import annotations

from collections import deque
from typing import Final

MAX_FRAMES_PER_WINDOW: Final = 200
FRAME_WINDOW_SECONDS: Final = 10.0


class FrameRate:
    """A sliding window over frame arrival times."""

    def __init__(
        self, *, limit: int = MAX_FRAMES_PER_WINDOW, window: float = FRAME_WINDOW_SECONDS
    ) -> None:
        self._limit = limit
        self._window = window
        self._times: deque[float] = deque()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window(self) -> float:
        return self._window

    def allow(self, now: float) -> bool:
        cutoff = now - self._window
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()
        if len(self._times) >= self._limit:
            return False
        self._times.append(now)
        return True


class ByteRate:
    """A sliding window over the bytes a socket has sent.

    The frame window says how OFTEN a peer may send and the frame cap how large
    one frame may be; neither says how MUCH. Inside both a socket still sends
    the frame cap times the frame budget per window — tens of megabytes of JSON
    this process parses at several times its wire size, and one user may hold
    several such sockets. This is the missing term, and it is judged per socket
    like the other two.
    """

    def __init__(self, *, budget: int, window: float = FRAME_WINDOW_SECONDS) -> None:
        self._budget = budget
        self._window = window
        self._sent: deque[tuple[float, int]] = deque()
        self._total = 0

    @property
    def budget(self) -> int:
        return self._budget

    def allow(self, now: float, size: int) -> bool:
        cutoff = now - self._window
        while self._sent and self._sent[0][0] <= cutoff:
            self._total -= self._sent.popleft()[1]
        if self._total + size > self._budget:
            return False
        self._sent.append((now, size))
        self._total += size
        return True


__all__ = ["FRAME_WINDOW_SECONDS", "MAX_FRAMES_PER_WINDOW", "ByteRate", "FrameRate"]
