"""A wall clock that never runs backward.

``time.time()`` is not monotonic. On Windows it can step backward when the system clock
is adjusted (PEP 418), which breaks any code that orders events by wall time. This
clamps each reading to the highest one seen, so callers get wall-clock values that only
move forward.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


def make_monotonic_clock(base: Callable[[], float] = time.time) -> Callable[[], float]:
    """Wrap ``base`` so its readings never decrease.

    The max-seen state is locked, so one clock is safe to share across threads.
    """
    lock = threading.Lock()
    last = 0.0

    def _clock() -> float:
        nonlocal last
        with lock:
            now = base()
            if now > last:
                last = now
            return last

    return _clock


__all__ = ["make_monotonic_clock"]
