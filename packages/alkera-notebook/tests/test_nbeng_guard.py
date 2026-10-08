"""The memory guard, with a fake source and fake kernels: who is killed, when."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from alkera_notebook.engine.config import MemoryPolicy
from alkera_notebook.memory.guard import MemoryGuard, threshold_bytes
from alkera_notebook.memory.source import ProcessRssSource

MiB = 1024 * 1024
GB = 1_000_000_000


@dataclass
class FakeKernel:
    kernel_id: str
    rss: int
    pid: int = 100
    alive: bool = True
    # What the kernel claims about itself; the guard must never read it.
    reported_rss: int = 0
    kills: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def rss_bytes(self) -> int:
        return self.rss

    def kill(self, reason: str, **data: Any) -> None:
        self.kills.append((reason, data))
        self.alive = False


class FakeSource:
    def __init__(self, limit: int, usage: int = 0) -> None:
        self.limit = limit
        self.usage = usage
        self.ooms = 0

    def usage_bytes(self) -> int:
        return self.usage

    def limit_bytes(self) -> int:
        return self.limit

    def oom_events(self) -> int:
        return self.ooms


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.parametrize(
    ("limit", "policy", "expected"),
    [
        # 1.5 GB/s x (0.1 + 0.15 + 0.15) s = 600 MB beats 10% of 4 GiB.
        pytest.param(4 * 1024 * MiB, MemoryPolicy(), 4 * 1024 * MiB - 600_000_000, id="rate"),
        # 10% of 64 GiB beats 600 MB.
        pytest.param(
            64 * 1024 * MiB, MemoryPolicy(), 64 * 1024 * MiB - 64 * 1024 * MiB // 10, id="tenth"
        ),
        pytest.param(1000, MemoryPolicy(reserve_bytes=100), 900, id="pinned"),
        pytest.param(100, MemoryPolicy(reserve_bytes=500), 0, id="never-negative"),
        pytest.param(
            10 * GB,
            MemoryPolicy(
                max_alloc_rate_bytes_s=10 * GB, interval_s=0.1, kill_latency_s=0.0, jitter_s=0.0
            ),
            9 * GB,
            id="rate-uses-interval",
        ),
    ],
)
def test_threshold_follows_the_reserve_formula(
    limit: int, policy: MemoryPolicy, expected: int
) -> None:
    assert threshold_bytes(limit, policy) == expected


def make(
    kernels: list[FakeKernel], source: FakeSource, **kw: Any
) -> tuple[MemoryGuard, dict[str, list[Any]]]:
    seen: dict[str, list[Any]] = {"kills": [], "warnings": [], "ooms": [], "all": []}
    clock = kw.pop("clock", Clock())
    guard = MemoryGuard(
        source,
        kw.pop("policy", MemoryPolicy(reserve_bytes=0, second_stage_s=0.3)),
        lambda: kernels,
        kill_all=lambda: seen["all"].append(True),
        on_kill=seen["kills"].append,
        on_warning=lambda u, t: seen["warnings"].append((u, t)),
        on_oom_event=lambda: seen["ooms"].append(True),
        monotonic=clock,
    )
    return guard, seen


def test_under_threshold_nothing_is_killed() -> None:
    k = FakeKernel("a", 100)
    guard, seen = make([k], FakeSource(1000, usage=999))
    assert guard.sample() is None
    assert k.alive and seen["kills"] == []


def test_largest_launcher_measured_kernel_is_killed_first() -> None:
    small = FakeKernel("a", 300, pid=1, reported_rss=10_000)
    big = FakeKernel("b", 600, pid=2, reported_rss=1)
    guard, _ = make([small, big], FakeSource(800, usage=900))
    kill = guard.sample()
    assert kill is not None and kill.kernel_ids == ("b",) and kill.stage == 1
    assert small.alive and not big.alive
    reason, data = big.kills[0]
    assert reason == "out_of_memory"
    assert data == {"peak_rss_bytes": 600, "limit_bytes": 800, "largest_process": 2}


def test_peak_is_the_largest_seen_not_the_last_sample() -> None:
    k = FakeKernel("a", 700)
    source = FakeSource(1000, usage=500)
    guard, _ = make([k], source)
    guard.sample()
    k.rss = 400
    source.usage = 1100
    kill = guard.sample()
    assert kill is not None and kill.peak_rss_bytes == 700


def test_second_stage_kills_everything_only_after_its_delay() -> None:
    a, b = FakeKernel("a", 500), FakeKernel("b", 400)
    clock = Clock()
    source = FakeSource(800, usage=900)
    guard, seen = make([a, b], source, clock=clock)
    assert guard.sample().kernel_ids == ("a",)  # type: ignore[union-attr]
    clock.t = 0.2
    assert guard.sample() is None  # the first kill has not had time to free memory
    assert b.alive and seen["all"] == []
    clock.t = 0.31
    kill = guard.sample()
    assert kill is not None and kill.stage == 2 and kill.kernel_ids == ("b",)
    assert not b.alive and seen["all"] == [True]


def test_second_stage_is_not_reached_when_memory_recovers() -> None:
    a, b = FakeKernel("a", 500), FakeKernel("b", 400)
    clock = Clock()
    source = FakeSource(800, usage=900)
    guard, seen = make([a, b], source, clock=clock)
    guard.sample()
    source.usage = 100
    clock.t = 1.0
    assert guard.sample() is None
    assert b.alive and seen["all"] == []


def test_warning_fires_once_per_excursion_at_80_percent() -> None:
    source = FakeSource(1000, usage=799)
    guard, seen = make([FakeKernel("a", 1)], source)
    guard.sample()
    assert seen["warnings"] == []
    source.usage = 800
    guard.sample()
    guard.sample()
    assert seen["warnings"] == [(800, 1000)]
    source.usage = 100
    guard.sample()
    source.usage = 850
    guard.sample()
    assert len(seen["warnings"]) == 2


def test_an_oom_events_increase_is_reported_once() -> None:
    source = FakeSource(1000, usage=0)
    guard, seen = make([FakeKernel("a", 1)], source)
    guard.sample()
    assert seen["ooms"] == []
    source.ooms = 2
    guard.sample()
    guard.sample()
    assert seen["ooms"] == [True]


def test_dead_kernels_are_neither_measured_nor_killed() -> None:
    dead = FakeKernel("a", 900, alive=False)
    live = FakeKernel("b", 100)
    guard, _ = make([dead, live], FakeSource(500, usage=600))
    kill = guard.sample()
    assert kill is not None and kill.kernel_ids == ("b",)
    assert dead.kills == []


def test_process_rss_source_sums_the_guards_measurement() -> None:
    a, b = FakeKernel("a", 300), FakeKernel("b", 250)
    source = ProcessRssSource(1000)
    guard, _ = make([a, b], source)  # type: ignore[arg-type]
    assert guard.usage() == 550
    assert guard.admit(400) and not guard.admit(500)
    with pytest.raises(ValueError):
        ProcessRssSource(0)
