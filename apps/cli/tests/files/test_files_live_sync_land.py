"""A file the transcript is about to name is on the drive before the reply is.

The agent writes ``charts/revenue.png`` and, a moment later, a reply that
shows it. Left to the content queue, a file that fresh waits out the settle
time before anyone reads it, so the reply reached the reader first and the
picture was a placeholder until something asked the box for the bytes.
:meth:`LiveSync.land` is what the transcript calls before publishing such a
reply: the named files are read now and sent first, and the caller is woken
the moment the drive holds them.

The fakes (``files._live_sync_fakes``) are a small drive: every assertion is
about what the drive holds, and when.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from alkera_cli.files import live_sync
from alkera_cli.files.live_sync import LiveCadence, LiveSync
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

CHART = b"\x89PNG\r\n\x1a\n" + b"chart" * 2000
#: Long enough that a file written this instant is still "being written" for
#: the whole test: only a landing may read it.
SETTLE = LiveCadence(settle_ms=60_000)
#: A bound on a hang, never a wait the assertions depend on.
HANG = 30.0


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def _fresh(sync: LiveSync, tree: Path, relative: str, data: bytes) -> Path:
    """A file the agent has just written, as the watcher reports it."""
    path = _write(tree, relative, data)
    now = time.time_ns()
    os.utime(path, ns=(now, now))
    sync.classify(Change.added, str(path))
    return path


def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + HANG
    while not predicate():
        assert time.monotonic() < deadline, "the landing never reached the sync"
        threading.Event().wait(0.001)


def _land_with_a_round(sync: LiveSync, paths: Iterable[str]) -> frozenset[str]:
    """``land`` from the transcript's side while the sync's own thread — this
    one — runs the round that sends the bytes."""
    wanted = list(paths)
    answer: list[frozenset[str]] = []
    caller = threading.Thread(target=lambda: answer.append(sync.land(wanted, timeout=HANG)))
    caller.start()
    _until(lambda: set(wanted) <= sync.promotions)
    sync.flush()
    caller.join(HANG)
    assert not caller.is_alive(), "the landing was not woken by the round that landed it"
    return answer[0]


def test_a_chart_written_this_instant_is_on_the_drive_when_land_answers(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=SETTLE)
    _fresh(sync, tree, "charts/revenue.png", CHART)

    landed = _land_with_a_round(sync, ["charts/revenue.png"])

    assert landed == frozenset({"charts/revenue.png"})
    assert api.stored == {"charts/revenue.png": CHART}
    assert clock.now == 1_000.0, "no settle time was waited out"


def test_without_a_landing_the_same_fresh_chart_waits_out_the_settle_time(tree: Path) -> None:
    """The control: the round above sent the chart because it was landed, not
    because a round sends whatever is queued."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=SETTLE)
    _fresh(sync, tree, "charts/revenue.png", CHART)

    sync.flush()

    assert api.stored == {}
    assert "charts/revenue.png" in sync.pending


def test_a_landing_sends_only_what_it_names_ahead_of_the_rest(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=SETTLE)
    _fresh(sync, tree, "charts/a.png", CHART)
    _fresh(sync, tree, "charts/b.png", CHART + b"b")
    _fresh(sync, tree, "scratch.log", b"still going")

    landed = _land_with_a_round(sync, ["charts/a.png", "charts/b.png"])

    assert landed == frozenset({"charts/a.png", "charts/b.png"})
    assert set(api.stored) == {"charts/a.png", "charts/b.png"}
    assert "scratch.log" in sync.pending


def test_a_file_the_drive_already_holds_answers_at_once_and_is_not_sent_again(
    tree: Path,
) -> None:
    """A reply naming a chart an earlier reply already showed: no round is
    needed to answer, so none runs here, and the drive is sent nothing."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=0))
    _write(tree, "charts/revenue.png", CHART)
    sync.classify(Change.added, str(tree / "charts/revenue.png"))
    sync.flush()
    assert api.uploads == ["charts/revenue.png"]

    landed = sync.land(["charts/revenue.png"], timeout=HANG)

    assert landed == frozenset({"charts/revenue.png"})
    assert api.uploads == ["charts/revenue.png"]
    assert sync.promotions == frozenset()


def test_a_chart_rewritten_since_it_was_sent_is_sent_again(tree: Path) -> None:
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=0))
    _write(tree, "charts/revenue.png", CHART)
    sync.classify(Change.added, str(tree / "charts/revenue.png"))
    sync.flush()
    path = _write(tree, "charts/revenue.png", CHART + b"v2")
    later = time.time_ns() + 5_000_000_000
    os.utime(path, ns=(later, later))

    landed = _land_with_a_round(sync, ["charts/revenue.png"])

    assert landed == frozenset({"charts/revenue.png"})
    assert api.stored["charts/revenue.png"] == CHART + b"v2"


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("never/written.png", id="named-but-never-written"),
        pytest.param("charts", id="a-directory"),
        pytest.param("pandas.read_csv", id="code-that-only-looks-like-a-path"),
        pytest.param("../outside.png", id="outside-the-root"),
        pytest.param("link.png", id="a-link-out-of-the-root"),
    ],
)
def test_a_target_with_no_file_behind_it_answers_at_once(
    tmp_path: Path, tree: Path, target: str
) -> None:
    """Nothing to wait for, so no round is needed to be told so: a reply that
    mentions `pandas.read_csv` is not held for the sync's next beat."""
    (tmp_path / "outside.png").write_bytes(CHART)
    (tree / "link.png").symlink_to(tmp_path / "outside.png")
    (tree / "charts").mkdir()
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=SETTLE)

    assert sync.land([target], timeout=HANG) == frozenset()
    assert api.stored == {}


def test_a_file_the_drive_will_not_take_is_answered_without_landing(tree: Path) -> None:
    """Larger than the lease's per-file ceiling: it stays on the box, and the
    landing is told so by the round rather than waiting out its timeout."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=60_000, max_file_bytes=100))
    _fresh(sync, tree, "charts/huge.png", CHART)

    landed = _land_with_a_round(sync, ["charts/huge.png"])

    assert landed == frozenset()
    assert api.stored == {}


def test_a_file_too_large_to_hold_a_reply_for_is_promoted_but_not_waited_for(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_sync, "LAND_WAIT_BYTES", 100)
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=SETTLE)
    _fresh(sync, tree, "data/big.csv", b"x" * 1000)

    assert sync.land(["data/big.csv"], timeout=HANG) == frozenset()
    assert "data/big.csv" in sync.promotions

    sync.flush()
    assert api.stored == {"data/big.csv": b"x" * 1000}


def test_a_landing_no_round_answers_gives_up_at_its_timeout(tree: Path) -> None:
    """A drive that never answers must not hold the reply for ever."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=SETTLE)
    _fresh(sync, tree, "charts/revenue.png", CHART)

    assert sync.land(["charts/revenue.png"], timeout=0.01) == frozenset()
    # Still promoted: the bytes go on the next round all the same.
    assert "charts/revenue.png" in sync.promotions


def test_a_sync_that_stops_lets_a_waiting_landing_go_at_once(tree: Path) -> None:
    """The drive refuses the bytes and then the watch ends: no round will ever
    land the chart, so the reply waiting on it is let go when the sync stops,
    not a whole timeout later."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, refuse="files.store_unavailable")
    sync = _sync(tree, api, clock, cadence=SETTLE, watcher=FakeWatcher([]))
    _fresh(sync, tree, "charts/revenue.png", CHART)
    answer: list[frozenset[str]] = []
    caller = threading.Thread(
        target=lambda: answer.append(sync.land(["charts/revenue.png"], timeout=HANG))
    )
    caller.start()
    _until(lambda: "charts/revenue.png" in sync.promotions)

    asyncio.run(sync.run())

    caller.join(HANG / 2)
    assert not caller.is_alive(), "the landing waited out its timeout"
    assert answer == [frozenset()]
    assert api.stored == {}
