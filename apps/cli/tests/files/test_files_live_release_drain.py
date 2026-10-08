"""The release drain: what can still land does, and the rest is named.

A release gives the content queue a bounded time on the clock. The fake drive
takes the bytes; the bandwidth window is what holds a file back, so the drain
is seen landing it once the window opens again — or naming it when the budget
ran out first.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alkera_cli.files.live_sync import LiveCadence, LiveSync
from alkera_cli.files.mount import unsynced_fields
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

#: Ten bytes a minute: two five-byte files fit, the third waits a minute.
NARROW = LiveCadence(settle_ms=0, bandwidth_bytes_per_minute=10)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(root, name, b"12345")
    return root


def _beating(tree: Path, api: FakeLiveApi, clock: FakeClock, **kwargs: object) -> LiveSync:
    """A sync whose waits pass on the fake clock with the heartbeat landing
    through them, as the box's beat goes on while a release drains."""
    box: list[LiveSync] = []

    def wait(seconds: float) -> None:
        clock.advance(seconds)
        box[0].fence.beat()

    sync = _sync(tree, api, clock, cadence=NARROW, sleep=wait, **kwargs)  # type: ignore[arg-type]
    box.append(sync)
    return sync


def _queued(tree: Path, api: FakeLiveApi, clock: FakeClock) -> LiveSync:
    sync = _beating(tree, api, clock)
    for name in ("a.txt", "b.txt", "c.txt"):
        sync.classify(Change.added, str(tree / name))
    return sync


def test_files_queued_at_release_land_within_the_drain(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _queued(tree, api, clock)

    left = sync.drain(120.0)

    assert left == []
    assert sorted(api.stored) == ["a.txt", "b.txt", "c.txt"]
    # It took the window reopening, and no longer than that.
    assert 60.0 <= clock.now - 1_000.0 <= 61.0


def test_what_the_drain_could_not_land_is_reported_by_path(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _queued(tree, api, clock)

    left = sync.drain(30.0)

    assert left == ["c.txt"]
    assert sync.remainder == ["c.txt"]
    assert sorted(api.stored) == ["a.txt", "b.txt"]
    assert clock.now - 1_000.0 <= 30.5, "the drain ran past its budget"


def test_a_drain_through_a_closed_fence_ends_on_time_with_everything_named(
    tree: Path,
) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    # No beat lands through the waits, and none has for longer than the grace.
    sync = _sync(tree, api, clock, cadence=NARROW, sleep=clock.advance)
    for name in ("a.txt", "b.txt", "c.txt"):
        sync.classify(Change.added, str(tree / name))
    clock.advance(31.0)

    left = sync.drain(5.0)

    assert left == ["a.txt", "b.txt", "c.txt"]
    assert api.stored == {}
    assert clock.now - 1_031.0 <= 5.5


def test_the_loop_drains_when_asked_before_its_watcher_ends(tree: Path) -> None:
    """Asked for a drain, the loop's last act is the drain; unasked, one round
    — which is what leaves the third file behind here."""
    for asked, expected in ((120.0, []), (None, ["c.txt"])):
        clock = FakeClock()
        api = FakeLiveApi(root=tree)
        batch = {(Change.added, str(tree / name)) for name in ("a.txt", "b.txt", "c.txt")}
        sync = _beating(tree, api, clock, watcher=FakeWatcher([batch]))
        if asked is not None:
            sync.request_drain(asked)
        asyncio.run(sync.run())
        assert sync.unsynced() == expected


def test_the_release_body_carries_the_exact_count_and_at_most_two_hundred_paths() -> None:
    paths = [f"scratch/f{index:03d}.txt" for index in range(250)]

    fields = unsynced_fields(paths)

    assert fields["unsynced_count"] == 250
    assert fields["unsynced_paths"] == paths[:200]
    assert unsynced_fields(None) == {}
    assert unsynced_fields([]) == {"unsynced_count": 0, "unsynced_paths": []}


def test_the_drain_budget_is_served_with_the_grant() -> None:
    assert LiveCadence.from_grant({"releaseDrainMs": 5000}).release_drain_ms == 5000
    assert LiveCadence.from_grant({}).release_drain_ms == 120_000
