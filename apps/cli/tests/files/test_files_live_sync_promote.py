"""The content queue's order, the settle rule, and promotions.

A reader on the drive asking for a file whose bytes are still only on the box
has that file PROMOTED: it goes to the front of the content queue and may move
past the bandwidth window up to a burst. Everything else keeps the queue's own
order — small files first in the order they were seen, the rest smallest
first, packed history last — and no file is read while it is still being
written, promoted or not.

The fakes (``files._live_sync_fakes``) are a small drive: every assertion is
about what the drive was sent, in what order, holding which bytes.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alkera_cli.files.live_sync import LiveCadence, LiveSync, hash_file
from alkera_cli.files.settle import LIVE_SETTLE_MS, SETTLE_MAX_WAIT
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

MIB = 1_048_576


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def _queue(sync: LiveSync, tree: Path, relative: str, data: bytes) -> Path:
    path = _write(tree, relative, data)
    sync.classify(Change.added, str(path))
    return path


class WallClock:
    """The wall clock a file's modification time is compared against."""

    def __init__(self) -> None:
        self.ns = 1_758_625_000_000_000_000

    def __call__(self) -> int:
        return self.ns

    def advance(self, seconds: float) -> None:
        self.ns += int(seconds * 1e9)


def _stamp(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


# -- promotion --------------------------------------------------------------


def test_a_promoted_file_uploads_before_an_earlier_smaller_queued_file(tree: Path) -> None:
    """Without the promotion the note goes first — it is small and was seen
    first. The reader waiting on the dataset is what moves it ahead."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, "note.md", b"first")
    _queue(sync, tree, "data/big.csv", b"x" * (2 * MIB))

    sync.promote("data/big.csv")
    sync.flush()

    assert api.uploads == ["data/big.csv", "note.md"]
    assert sync.promotions == frozenset()


def test_without_a_promotion_the_same_queue_goes_smallest_first(tree: Path) -> None:
    """The control for the case above: the order it overturns is real."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, "note.md", b"first")
    _queue(sync, tree, "data/big.csv", b"x" * (2 * MIB))

    sync.flush()

    assert api.uploads == ["note.md", "data/big.csv"]


def test_promotions_go_in_the_order_they_were_asked_for(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, "a.txt", b"a")
    _queue(sync, tree, "b.txt", b"bb")
    _queue(sync, tree, "c.txt", b"ccc")

    sync.promote("c.txt")
    sync.promote("a.txt")
    sync.flush()

    assert api.uploads == ["c.txt", "a.txt", "b.txt"]


def test_a_promoted_file_the_watcher_never_reported_is_taken_from_disk(tree: Path) -> None:
    """The drive lists the row (the metadata queue sent it long ago) and the
    holder has nothing queued for it — a file whose bytes were deferred and
    then forgotten by a restart. The promotion alone brings it."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _write(tree, "reports/q3.html", b"<h1>Q3</h1>")

    sync.promote("reports/q3.html")
    sync.flush()

    assert api.stored == {"reports/q3.html": b"<h1>Q3</h1>"}


def test_a_promoted_file_the_holder_thinks_the_drive_has_is_sent_again(tree: Path) -> None:
    """The holder's belief that the drive holds these bytes is the one thing a
    promotion contradicts: the drive is asking because it does not."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, "report.md", b"final")
    sync.flush()
    assert api.uploads == ["report.md"]

    sync.promote("report.md")
    sync.flush()

    assert api.uploads == ["report.md", "report.md"]
    assert sync.promotions == frozenset()


def test_a_promoted_link_out_of_the_root_is_never_read(tmp_path: Path, tree: Path) -> None:
    """The promotion goes through the same classifier a watcher event does, so
    a link planted in the tree cannot make the holder upload a file from
    elsewhere on the host — and the promotion does not linger as owed."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"host credentials")
    (tree / "innocent.txt").symlink_to(secret)
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    sync.promote("innocent.txt")
    sync.flush()

    assert api.uploads == []
    assert api.trees == []
    assert sync.promotions == frozenset()


def test_a_promotion_of_a_file_since_deleted_is_let_go(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    path = _queue(sync, tree, "gone.txt", b"soon gone")
    sync.promote("gone.txt")
    path.unlink()
    sync.classify(Change.deleted, str(path))

    sync.flush()

    assert api.uploads == []
    assert sync.promotions == frozenset()


def test_a_promotion_bypasses_the_window_up_to_the_burst_and_not_beyond(tree: Path) -> None:
    """The window is ten bytes a minute; the burst a hundred and fifty. The
    first promotion moves past the window on the burst, the second does not
    fit in what the burst has left and waits, and a file nobody asked for
    waits for the window exactly as before."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    deferred: list[str] = []
    sync = _sync(
        tree,
        api,
        clock,
        cadence=LiveCadence(
            settle_ms=0, bandwidth_bytes_per_minute=10, live_promote_burst_bytes=150
        ),
        on_deferred=deferred.append,
    )
    _queue(sync, tree, "asked-first.bin", b"a" * 100)
    _queue(sync, tree, "asked-second.bin", b"b" * 80)
    _queue(sync, tree, "unasked.bin", b"u" * 100)
    _queue(sync, tree, "tiny.txt", b"t" * 5)
    sync.promote("asked-first.bin")
    sync.promote("asked-second.bin")

    sync.flush()

    assert api.uploads == ["asked-first.bin", "tiny.txt"]
    assert sorted(deferred) == ["asked-second.bin", "unasked.bin"]
    assert sync.promotions == frozenset({"asked-second.bin"})

    # Nothing moves until the minute has passed: the burst is spent, and the
    # window is still spent by the tiny file.
    clock.advance(30)
    sync.fence.beat()
    sync.flush()
    assert api.uploads == ["asked-first.bin", "tiny.txt"]

    clock.advance(31)
    sync.fence.beat()
    sync.flush()
    assert api.uploads[2] == "asked-second.bin"
    assert sync.promotions == frozenset()


def test_the_burst_is_not_spent_by_a_promotion_the_window_had_room_for(tree: Path) -> None:
    """A promotion that fits in the window is charged to the window, so the
    burst is still whole for the next one that does not."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(
        tree,
        api,
        clock,
        cadence=LiveCadence(
            settle_ms=0, bandwidth_bytes_per_minute=50, live_promote_burst_bytes=100
        ),
    )
    _queue(sync, tree, "small.bin", b"s" * 40)
    sync.promote("small.bin")
    sync.flush()
    _queue(sync, tree, "large.bin", b"l" * 100)
    sync.promote("large.bin")
    sync.flush()

    assert api.uploads == ["small.bin", "large.bin"]


def test_a_promotion_does_not_wait_for_the_batch_window(tree: Path) -> None:
    """A reader is waiting: the round goes on the next batch the watcher
    hands over, not when a minute of coalescing has passed."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _write(tree, "wanted.csv", b"a,b\n1,2\n")
    seen_by_second_batch: list[str] = []

    class Watcher:
        async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
            async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
                yield set()
                seen_by_second_batch.extend(api.uploads)
                yield set()

            return stream()

    sync = _sync(
        tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=60_000), watcher=Watcher()
    )
    # The sweep at the top of the loop queues the file; the window is a minute.
    sync.promote("wanted.csv")

    asyncio.run(sync.run())

    assert seen_by_second_batch == ["wanted.csv"]


# -- settling ---------------------------------------------------------------


def _settling_sync(
    tree: Path, api: FakeLiveApi, clock: FakeClock, wall: WallClock, read: list[Path]
) -> LiveSync:
    def hasher(path: Path) -> tuple[str, int]:
        read.append(path)
        return hash_file(path)

    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=2000), hasher=hasher)
    sync.wall_ns = wall
    return sync


def test_a_promoted_file_still_being_written_lands_when_it_settles_not_before(
    tree: Path,
) -> None:
    """The writer is still going: the first look finds it half a second old,
    the second finds it rewritten. Neither round reads it. Once it has been
    still for the settle time, the round sends the bytes the writer finished
    with."""
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree)
    read: list[Path] = []
    sync = _settling_sync(tree, api, clock, wall, read)
    path = _queue(sync, tree, "export.csv", b"id\n1\n")
    _stamp(path, wall.ns - 500_000_000)
    sync.promote("export.csv")

    sync.flush()
    assert api.uploads == []
    assert read == []
    assert "export.csv" in sync.pending

    wall.advance(1.5)
    clock.advance(1.5)
    path.write_bytes(b"id\n1\n2\n3\n")
    _stamp(path, wall.ns)
    sync.flush()
    assert api.uploads == []
    assert read == []

    wall.advance(2.0)
    clock.advance(2.0)
    sync.flush()
    assert api.stored == {"export.csv": b"id\n1\n2\n3\n"}
    assert sync.promotions == frozenset()


def test_a_file_that_is_not_promoted_settles_the_same_way(tree: Path) -> None:
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree)
    read: list[Path] = []
    sync = _settling_sync(tree, api, clock, wall, read)
    path = _queue(sync, tree, "log.txt", b"line 1\n")
    _stamp(path, wall.ns)

    sync.flush()
    assert api.uploads == [] and read == []

    wall.advance(2.0)
    clock.advance(2.0)
    sync.flush()
    assert api.uploads == ["log.txt"]


def test_a_held_file_is_sent_once_it_settles_without_waiting_for_the_batch_window(
    tree: Path,
) -> None:
    """A file held back to settle goes the moment it has, on a quiet folder.

    The watcher says nothing after the write and the batch window is a
    minute, so nothing but the settle itself can bring the file's next look:
    held until the next event or tick, a just-saved file waited up to a whole
    window more than it had to. Both clocks are the test's; it moves them in
    small steps, and the upload is checked against them, not against how fast
    this machine runs.
    """
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree, clock=clock)
    stop = asyncio.Event()

    class QuietWatcher:
        async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
            async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
                yield set()
                await stop.wait()

            return stream()

    sync = _sync(
        tree,
        api,
        clock,
        cadence=LiveCadence(settle_ms=300, batch_every_ms=60_000),
        watcher=QuietWatcher(),
    )
    sync.wall_ns = wall
    clock.advance(60.0)  # the window has passed once: the first round goes
    sync.fence.beat()  # and the holder has heard from the server meanwhile
    path = _queue(sync, tree, "report.md", b"# Q3\n")
    _stamp(path, wall.ns)
    written_at = clock.now
    sync.flush()
    assert api.uploads == [], "a file just written was read before it settled"

    async def scenario() -> list[str]:
        running = asyncio.ensure_future(sync.run())
        try:
            # A hang guard, never a speed bound: two hundred steps of 50 ms on
            # the test's clocks is ten seconds of their time for a 300 ms settle.
            for _ in range(200):
                if api.uploads:
                    break
                await asyncio.sleep(0.01)
                clock.advance(0.05)
                wall.advance(0.05)
            # Read before the watch ends: the sync's last round on the way out
            # sends whatever is still queued, which proves nothing here.
            return list(api.uploads)
        finally:
            stop.set()
            await running

    while_running = asyncio.run(scenario())

    assert while_running == ["report.md"], (
        "a file held back to settle was never sent while the folder was quiet"
    )
    sent = [at for kind, what, at in api.events if kind == "upload" and what == "report.md"]
    assert sent[0] - written_at >= 0.3, "sent before it had settled"
    assert sent[0] - written_at < 60.0, "sent only when the batch window came round"
    assert api.stored == {"report.md": b"# Q3\n"}


def test_a_file_already_old_is_not_held_back(tree: Path) -> None:
    """Settling costs only the files that are fresh: one untouched for an hour
    goes on the first round."""
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree)
    sync = _settling_sync(tree, api, clock, wall, [])
    path = _queue(sync, tree, "old.txt", b"from last week")
    _stamp(path, wall.ns - 3_600_000_000_000)

    sync.flush()

    assert api.uploads == ["old.txt"]


def test_a_file_stamped_in_the_future_settles_on_the_holders_own_watch(tree: Path) -> None:
    """A modification time ahead of the wall clock never ages. The holder's own
    monotonic watch of an unchanged stamp is what lets it go."""
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree)
    sync = _settling_sync(tree, api, clock, wall, [])
    path = _queue(sync, tree, "skewed.txt", b"from a box with a fast clock")
    _stamp(path, wall.ns + 86_400_000_000_000)

    sync.flush()
    assert api.uploads == []
    clock.advance(1.0)
    sync.flush()
    assert api.uploads == []

    clock.advance(1.0)
    sync.flush()
    assert api.uploads == ["skewed.txt"]


def test_a_file_rewritten_faster_than_it_settles_still_leaves_within_the_cap(
    tree: Path,
) -> None:
    """An agent saving every second never lets a file sit for the two-second
    settle time. It used to wait until the writer stopped (minutes); it now
    goes after at most :data:`SETTLE_MAX_WAIT`, the bytes it holds then."""
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree)
    sync = _settling_sync(tree, api, clock, wall, [])
    path = _queue(sync, tree, "ticks.log", b"0\n")
    lines = b"0\n"
    waited = 0.0
    while not api.uploads and waited < SETTLE_MAX_WAIT + 2:
        _stamp(path, wall.ns)
        sync.flush()
        wall.advance(1.0)
        clock.advance(1.0)
        waited += 1.0
        lines += b"%d\n" % int(waited)
        path.write_bytes(lines)
    assert api.uploads == ["ticks.log"]
    assert waited <= SETTLE_MAX_WAIT + 1


def test_a_file_a_live_document_is_open_on_settles_on_the_short_window(tree: Path) -> None:
    """People are watching the document for the agent's edit: once the drive
    has said a co-edited document writes this file back, a fresh change to it
    goes after :data:`LIVE_SETTLE_MS`, not the served two seconds. Any other
    file still waits the two seconds."""
    clock = FakeClock()
    wall = WallClock()
    api = FakeLiveApi(root=tree)
    sync = _settling_sync(tree, api, clock, wall, [])
    live = _queue(sync, tree, "plan.md", b"# plan\n")
    other = _queue(sync, tree, "notes.md", b"# notes\n")
    sync.flush()  # both are fresh: both wait, and both get a node once they go
    sync._session_heads.add(sync._nodes.setdefault("plan.md", "node-plan"))
    _stamp(live, wall.ns)
    _stamp(other, wall.ns)
    sync.flush()
    assert api.uploads == []

    step = (LIVE_SETTLE_MS + 50) / 1000
    wall.advance(step)
    clock.advance(step)
    sync.flush()
    assert api.uploads == ["plan.md"]


# -- the queue's order --------------------------------------------------------


def test_small_files_go_first_in_the_order_seen_then_large_then_packed_history(
    tree: Path,
) -> None:
    """``second.md`` is smaller than ``note.md`` but was seen after it, and a
    small file goes in the order it was seen — so a reader who saw the note
    appear first gets it first. The large files follow smallest first, and a
    pack, however small, is last."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, ".git/objects/pack/pack-1.pack", b"p" * 10)
    _queue(sync, tree, "larger.bin", b"L" * (3 * MIB))
    _queue(sync, tree, "note.md", b"a longer note")
    _queue(sync, tree, "large.bin", b"l" * (2 * MIB))
    _queue(sync, tree, "second.md", b"short")

    sync.flush()

    assert api.uploads == [
        "note.md",
        "second.md",
        "large.bin",
        "larger.bin",
        ".git/objects/pack/pack-1.pack",
    ]


@pytest.mark.parametrize(
    ("relative", "cold"),
    [
        pytest.param(".git/objects/pack/pack-1.pack", True, id="the-repos-pack"),
        pytest.param("vendor/lib/.git/objects/pack/p.idx", True, id="a-nested-repos-pack"),
        pytest.param(".git/objects/ab/cdef", False, id="a-loose-object"),
        pytest.param("papers/.git/objects/pack", False, id="the-pack-directory-name-itself"),
        pytest.param("git/objects/pack/p.pack", False, id="not-a-dot-git"),
    ],
)
def test_only_packed_history_is_cold(tree: Path, relative: str, cold: bool) -> None:
    """A pack goes behind a three-megabyte file; anything else of its size
    goes ahead of one."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, "large.bin", b"l" * (3 * MIB))
    _queue(sync, tree, relative, b"p" * 10)

    sync.flush()

    expected = ["large.bin", relative] if cold else [relative, "large.bin"]
    assert api.uploads == expected


def test_a_small_file_rewritten_after_it_landed_goes_behind_what_was_seen_since(
    tree: Path,
) -> None:
    """First seen means first seen since it last left the queue: a file that
    landed and was written again is a new arrival."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _queue(sync, tree, "a.txt", b"12345")
    sync.flush()
    _queue(sync, tree, "b.txt", b"123")
    _queue(sync, tree, "a.txt", b"1234")

    sync.flush()

    assert api.uploads == ["a.txt", "b.txt", "a.txt"]
