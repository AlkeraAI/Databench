"""Where the memory guard reads usage from.

A :class:`MemorySource` reports usage and the limit it is measured against,
plus a running count of out-of-memory kills the system itself performed. In
the core it is :class:`ProcessRssSource`: the limit is a configured budget
and usage is the sum of the launcher-measured process-group RSS of the
kernels the guard watches. A sandboxing launcher can supply a cgroup source
instead (``memory.current``, ``memory.max``, ``memory.events``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class MemorySource(Protocol):
    def usage_bytes(self) -> int: ...

    def limit_bytes(self) -> int: ...

    def oom_events(self) -> int:
        """How many OOM kills the system performed so far (monotonic)."""
        ...


class ProcessRssSource:
    """Usage is the sum of RSS over the kernels the guard measures."""

    def __init__(self, budget_bytes: int) -> None:
        if budget_bytes <= 0:
            raise ValueError("budget_bytes must be positive")
        self._budget = budget_bytes
        self._measure: Callable[[], int] = lambda: 0

    def bind(self, measure: Callable[[], int]) -> None:
        """The guard binds the sum of its kernels' launcher-measured RSS."""
        self._measure = measure

    def usage_bytes(self) -> int:
        return self._measure()

    def limit_bytes(self) -> int:
        return self._budget

    def oom_events(self) -> int:
        return 0
