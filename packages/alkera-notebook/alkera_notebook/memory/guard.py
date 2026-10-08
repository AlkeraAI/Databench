"""The memory guard: kill the largest kernel before memory runs out.

Every ``interval_s`` the guard reads usage and limit from its
:class:`MemorySource`. Above the threshold it kills the kernel whose
**launcher-measured** process-group RSS is largest; if usage is still above
the threshold ``second_stage_s`` later, it kills every kernel (in the
platform, the whole kernel sandbox). Numbers a kernel reports about itself
are never used. At ``warn_fraction`` of the threshold it warns once until
usage falls back below. An increase of the source's ``oom_events`` (the
system killed something) is reported so kernels that died by ``SIGKILL`` at
that moment are recorded as out of memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from alkera_notebook.engine.config import MemoryPolicy
from alkera_notebook.memory.source import MemorySource, ProcessRssSource

log = logging.getLogger(__name__)


class GuardedKernel(Protocol):
    kernel_id: str

    @property
    def alive(self) -> bool: ...

    @property
    def pid(self) -> int: ...

    def rss_bytes(self) -> int: ...

    def kill(self, reason: str, **data: Any) -> None: ...


@dataclass(frozen=True)
class GuardKill:
    kernel_ids: tuple[str, ...]
    peak_rss_bytes: int
    limit_bytes: int
    largest_process: int
    usage_bytes: int
    stage: int  # 1: the largest kernel; 2: every kernel


def threshold_bytes(limit: int, policy: MemoryPolicy) -> int:
    reserve = policy.reserve_bytes
    if reserve is None:
        window = policy.interval_s + policy.kill_latency_s + policy.jitter_s
        reserve = max(limit // 10, int(policy.max_alloc_rate_bytes_s * window))
    return max(0, limit - reserve)


class MemoryGuard:
    def __init__(
        self,
        source: MemorySource,
        policy: MemoryPolicy,
        kernels: Callable[[], Sequence[GuardedKernel]],
        *,
        kill_all: Callable[[], None] | None = None,
        on_kill: Callable[[GuardKill], None] = lambda _k: None,
        on_warning: Callable[[int, int], None] = lambda _u, _t: None,
        on_oom_event: Callable[[], None] = lambda: None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._source = source
        self._policy = policy
        self._kernels = kernels
        self._kill_all = kill_all
        self._on_kill = on_kill
        self._on_warning = on_warning
        self._on_oom_event = on_oom_event
        loop_time: Callable[[], float] = lambda: asyncio.get_running_loop().time()  # noqa: E731
        self._now = monotonic or loop_time
        self._peaks: dict[str, int] = {}
        self._warned = False
        self._first_kill_at: float | None = None
        self._ooms = source.oom_events()
        self._task: asyncio.Task[None] | None = None
        self._last: dict[str, int] = {}
        if isinstance(source, ProcessRssSource):
            source.bind(lambda: sum(self._last.values()))

    @property
    def threshold(self) -> int:
        return threshold_bytes(self._source.limit_bytes(), self._policy)

    def _measure(self) -> dict[str, int]:
        return {k.kernel_id: k.rss_bytes() for k in self._kernels() if k.alive}

    def usage(self) -> int:
        self._last = self._measure()
        return self._source.usage_bytes()

    def admit(self, extra_bytes: int | None = None) -> bool:
        """Whether a new kernel may start now."""
        extra = self._policy.kernel_start_bytes if extra_bytes is None else extra_bytes
        return self.usage() + extra <= self.threshold

    def sample(self) -> GuardKill | None:
        """One tick: measure, warn, and kill if over."""
        return self.decide(self._measure())

    def decide(self, sizes: dict[str, int]) -> GuardKill | None:
        self._last = sizes
        for kid, rss in sizes.items():
            self._peaks[kid] = max(self._peaks.get(kid, 0), rss)
        ooms = self._source.oom_events()
        if ooms > self._ooms:
            self._ooms = ooms
            self._on_oom_event()
        usage = self._source.usage_bytes()
        limit = self.threshold
        if usage >= self._policy.warn_fraction * limit:
            if not self._warned:
                self._warned = True
                self._on_warning(usage, limit)
        else:
            self._warned = False
        if usage <= limit:
            self._first_kill_at = None
            return None
        live = {k.kernel_id: k for k in self._kernels() if k.alive}
        now = self._now()
        if (
            self._first_kill_at is not None
            and now - self._first_kill_at >= self._policy.second_stage_s
        ):
            return self._stage_two(live, usage, limit)
        if self._first_kill_at is not None:
            return None  # the first kill has not had time to free memory yet
        candidates = {kid: rss for kid, rss in sizes.items() if kid in live}
        if not candidates:
            return None
        largest_id = max(candidates, key=lambda kid: (candidates[kid], kid))
        victim = live[largest_id]
        kill = GuardKill(
            kernel_ids=(largest_id,),
            peak_rss_bytes=self._peaks.get(largest_id, candidates[largest_id]),
            limit_bytes=limit,
            largest_process=victim.pid,
            usage_bytes=usage,
            stage=1,
        )
        victim.kill(
            "out_of_memory",
            peak_rss_bytes=kill.peak_rss_bytes,
            limit_bytes=kill.limit_bytes,
            largest_process=kill.largest_process,
        )
        self._peaks.pop(largest_id, None)
        self._first_kill_at = now
        self._on_kill(kill)
        return kill

    def _stage_two(self, live: dict[str, GuardedKernel], usage: int, limit: int) -> GuardKill:
        ids = tuple(sorted(live))
        peak = max((self._peaks.get(k, 0) for k in ids), default=0)
        largest = max(live.values(), key=lambda k: self._peaks.get(k.kernel_id, 0), default=None)
        for kid, k in live.items():
            k.kill(
                "out_of_memory",
                peak_rss_bytes=self._peaks.get(kid, 0),
                limit_bytes=limit,
                largest_process=k.pid,
            )
        if self._kill_all is not None:
            self._kill_all()
        self._first_kill_at = None
        kill = GuardKill(ids, peak, limit, largest.pid if largest else 0, usage, stage=2)
        self._on_kill(kill)
        return kill

    async def _loop(self) -> None:
        while True:
            try:
                # Measuring runs `ps` on some launchers: keep it off the loop.
                sizes = await asyncio.to_thread(self._measure)
                self.decide(sizes)
            except Exception:
                log.exception("memory guard tick failed")
            await asyncio.sleep(self._policy.interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
