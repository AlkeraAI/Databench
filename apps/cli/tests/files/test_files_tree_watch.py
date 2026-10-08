"""The link-blind directory watch the box's live sync and the daemon run on.

The box froze for good when a chat folder held ``ln -s / root``: arming a
``watchfiles`` watch walked the whole machine following the link, inside a
constructor that held the interpreter lock, so the heartbeat thread, the lease
beats and the log all stopped behind it. These tests pin what replaced it:
arming never follows a link and never stops another thread, a name that is not
UTF-8 is one more path rather than the end of the watch, and batches keep the
shape the live sync's loop expects.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from alkera_cli.files.tree_watch import (
    Change,
    PollSource,
    Source,
    inotify_available,
    open_source,
    watch_tree,
)

Event = tuple[Change, str]

BACKENDS = [
    pytest.param(True, id="poll"),
    pytest.param(
        False,
        id="inotify",
        marks=pytest.mark.skipif(not inotify_available(), reason="inotify is Linux-only"),
    ),
]


def _link_storm(root: Path, outside: Path) -> None:
    """What the QA storm left in a chat folder: links out, links in a loop."""
    storm = root / "storm"
    storm.mkdir(parents=True)
    (storm / "link-root").symlink_to("/")
    (storm / "link-outside").symlink_to(outside)
    (storm / "loop-a").symlink_to("loop-b")
    (storm / "loop-b").symlink_to("loop-a")
    (storm / "self-loop").symlink_to(".")


def _drain(source: Source, *, until: Callable[[set[Event]], bool], within: float) -> set[Event]:
    seen: set[Event] = set()
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and not until(seen):
        seen.update(source.read(0.1))
    return seen


def test_arming_over_a_link_to_the_root_returns_at_once() -> None:
    """Arming the watch on a folder holding ``ln -s / root`` must finish in a
    blink. It runs in a child so a regression is a timeout this test reports,
    not a frozen test process: the old watch never returned on Linux and froze
    every thread while it tried."""
    script = textwrap.dedent(
        """
        import sys, tempfile, threading, time, pathlib
        from alkera_cli.files.tree_watch import open_source
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "link-root").symlink_to("/")
        (root / "self-loop").symlink_to(".")
        gaps, done = [0.0], threading.Event()
        def tick():
            last = time.monotonic()
            while not done.is_set():
                time.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        ticker = threading.Thread(target=tick)
        ticker.start()
        source = open_source([str(root)], watch_filter=None,
                             force_polling=sys.argv[1] == "poll", poll_interval=0.1)
        done.set()
        ticker.join()
        source.close()
        print(max(gaps))
        """
    )
    for backend in ("poll", "inotify") if inotify_available() else ("poll",):
        finished = subprocess.run(
            [sys.executable, "-c", script, backend],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        # Generous for a loaded runner; the old watch's gap was the whole walk.
        assert float(finished.stdout.strip()) < 1.0, backend


@pytest.mark.parametrize("force_polling", BACKENDS)
def test_a_change_behind_a_link_is_never_reported(tmp_path: Path, force_polling: bool) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    _link_storm(root, outside)
    source = open_source(
        [str(root)], watch_filter=None, force_polling=force_polling, poll_interval=0.05
    )
    try:
        (outside / "deep" / "secret.txt").write_text("not ours")
        (root / "storm" / "ours.txt").write_text("ours")
        seen = _drain(
            source, until=lambda got: any(p.endswith("ours.txt") for _, p in got), within=5
        )
    finally:
        source.close()
    paths = {path for _, path in seen}
    assert str(root / "storm" / "ours.txt") in paths
    # The only road to secret.txt runs through link-outside.
    assert not [p for p in paths if "secret" in p]


@pytest.mark.parametrize("force_polling", BACKENDS)
def test_adds_modifies_and_deletes_are_told_apart(tmp_path: Path, force_polling: bool) -> None:
    (tmp_path / "keep.txt").write_text("v1")
    (tmp_path / "gone.txt").write_text("bye")
    source = open_source(
        [str(tmp_path)], watch_filter=None, force_polling=force_polling, poll_interval=0.05
    )
    try:
        time.sleep(0.02)
        (tmp_path / "keep.txt").write_text("v2, longer")
        (tmp_path / "gone.txt").unlink()
        (tmp_path / "new.txt").write_text("hi")
        wanted = {
            (Change.modified, str(tmp_path / "keep.txt")),
            (Change.deleted, str(tmp_path / "gone.txt")),
            (Change.added, str(tmp_path / "new.txt")),
        }
        seen = _drain(source, until=lambda got: wanted <= got, within=5)
    finally:
        source.close()
    assert wanted <= seen
    assert (Change.deleted, str(tmp_path / "keep.txt")) not in seen


@pytest.mark.parametrize("force_polling", BACKENDS)
def test_a_directory_made_after_arming_is_watched_with_what_is_already_in_it(
    tmp_path: Path, force_polling: bool
) -> None:
    source = open_source(
        [str(tmp_path)], watch_filter=None, force_polling=force_polling, poll_interval=0.05
    )
    try:
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "early.txt").write_text("written before any watch was on b")
        early = (Change.added, str(nested / "early.txt"))
        _drain(source, until=lambda got: early in got, within=5)
        (nested / "late.txt").write_text("written after")
        late = (Change.added, str(nested / "late.txt"))
        seen = _drain(source, until=lambda got: late in got, within=5)
    finally:
        source.close()
    assert late in seen


@pytest.mark.parametrize("force_polling", BACKENDS)
def test_a_directory_the_filter_refuses_is_not_walked(tmp_path: Path, force_polling: bool) -> None:
    (tmp_path / ".runtime" / "deep").mkdir(parents=True)

    def no_runtime(_change: Change, path: str) -> bool:
        return ".runtime" not in Path(path).parts

    source = open_source(
        [str(tmp_path)], watch_filter=no_runtime, force_polling=force_polling, poll_interval=0.05
    )
    try:
        (tmp_path / ".runtime" / "deep" / "state.db").write_text("x")
        (tmp_path / "work.txt").write_text("y")
        seen = _drain(
            source, until=lambda got: any(p.endswith("work.txt") for _, p in got), within=5
        )
    finally:
        source.close()
    assert not [p for _, p in seen if "state.db" in p]


@pytest.mark.parametrize("force_polling", BACKENDS)
def test_a_root_named_through_a_link_is_watched(tmp_path: Path, force_polling: bool) -> None:
    """Only what is found beneath a root is never followed: a project opened
    through a link is the directory the caller chose."""
    real = tmp_path / "real"
    real.mkdir()
    named = tmp_path / "named"
    named.symlink_to(real)
    source = open_source(
        [str(named)], watch_filter=None, force_polling=force_polling, poll_interval=0.05
    )
    try:
        (real / "a.txt").write_text("x")
        wanted = (Change.added, str(named / "a.txt"))
        seen = _drain(source, until=lambda got: wanted in got, within=5)
    finally:
        source.close()
    assert wanted in seen


@pytest.mark.skipif(sys.platform == "darwin", reason="APFS refuses a file name that is not UTF-8")
@pytest.mark.parametrize("force_polling", BACKENDS)
def test_a_name_that_is_not_utf8_is_one_more_path_and_the_watch_goes_on(
    tmp_path: Path, force_polling: bool
) -> None:
    source = open_source(
        [str(tmp_path)], watch_filter=None, force_polling=force_polling, poll_interval=0.05
    )
    try:
        bad = os.fsencode(tmp_path) + b"/bad\xffutf8"
        os.close(os.open(bad, os.O_CREAT | os.O_WRONLY, 0o644))
        (tmp_path / "beside.txt").write_text("same batch")
        bad_event = (Change.added, os.fsdecode(bad))
        beside = (Change.added, str(tmp_path / "beside.txt"))
        first = _drain(source, until=lambda got: {bad_event, beside} <= got, within=5)
        (tmp_path / "after.txt").write_text("later")
        after = (Change.added, str(tmp_path / "after.txt"))
        later = _drain(source, until=lambda got: after in got, within=5)
    finally:
        source.close()
    assert {bad_event, beside} <= first
    assert after in later


# -- batching ------------------------------------------------------------------


class _Scripted:
    """A source that hands out scripted reads, then nothing."""

    def __init__(self, reads: list[list[Event]]) -> None:
        self.reads = reads
        self.closed = False
        self.degraded = False

    def arm(self) -> None:
        return None

    def read(self, timeout: float) -> list[Event]:
        if self.reads:
            return self.reads.pop(0)
        time.sleep(min(timeout, 0.01))
        return []

    def close(self) -> None:
        self.closed = True


async def _take(stream: AsyncIterator[set[Event]], count: int) -> list[set[Event]]:
    taken: list[set[Event]] = []
    async for batch in stream:
        taken.append(batch)
        if len(taken) == count:
            break
    return taken


async def test_a_batch_closes_once_a_step_passes_with_nothing_new() -> None:
    a, b, c = (Change.added, "/w/a"), (Change.added, "/w/b"), (Change.added, "/w/c")
    source = _Scripted([[a], [b], [], [c]])
    stream = watch_tree("/w", source_factory=lambda: source, step_ms=1, debounce_ms=10_000)
    batches = await asyncio.wait_for(_take(stream, 2), timeout=5)
    assert batches == [{a, b}, {c}]


async def test_a_quiet_watch_ticks_with_an_empty_batch_only_when_asked() -> None:
    ticking = watch_tree("/w", source_factory=lambda: _Scripted([]), tick_ms=20)
    assert await asyncio.wait_for(_take(ticking, 2), timeout=5) == [set(), set()]

    silent = watch_tree("/w", source_factory=lambda: _Scripted([]))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_take(silent, 1), timeout=0.3)


async def test_the_filter_drops_events_before_they_count() -> None:
    kept, dropped = (Change.added, "/w/keep"), (Change.added, "/w/.lock")
    source = _Scripted([[dropped], [dropped, kept]])
    stream = watch_tree(
        "/w",
        source_factory=lambda: source,
        step_ms=1,
        watch_filter=lambda _c, path: not path.endswith(".lock"),
    )
    assert await asyncio.wait_for(_take(stream, 1), timeout=5) == [{kept}]


async def test_a_set_stop_event_ends_the_watch_and_releases_the_source() -> None:
    stop = threading.Event()
    source = _Scripted([])

    async def run() -> list[set[Event]]:
        return [
            batch
            async for batch in watch_tree(
                "/w", source_factory=lambda: source, stop_event=stop, tick_ms=10
            )
        ]

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert source.closed


async def test_a_watch_that_runs_out_of_kernel_watches_moves_to_a_poll(tmp_path: Path) -> None:
    starved = _Scripted([])
    starved.degraded = True
    stream = watch_tree(
        tmp_path, source_factory=lambda: starved, step_ms=1, poll_delay_ms=20, tick_ms=50
    )

    async def first_real_change() -> set[Event]:
        async for batch in stream:
            if batch:
                return batch
            (tmp_path / "after.txt").write_text("heard by the poll")
        raise AssertionError("the watch ended")

    batch = await asyncio.wait_for(first_real_change(), timeout=5)
    assert (Change.added, str(tmp_path / "after.txt")) in batch
    assert starved.closed


def test_poll_source_reports_nothing_after_close(tmp_path: Path) -> None:
    source = PollSource([str(tmp_path)], watch_filter=None, interval=0.01)
    source.arm()
    source.close()
    (tmp_path / "x").write_text("x")
    assert source.read(0.05) == []


def test_no_module_of_the_cli_imports_watchfiles() -> None:
    """``watchfiles``' Rust watch follows links while holding the interpreter
    lock, so every watch in the CLI goes through the tree watch, which also owns
    the ``Change`` enum; the project watch owns its own filter. The package is
    not a dependency of the CLI, and an import of it would only resolve where
    something else happened to install it."""
    import re

    import alkera_cli

    package = Path(alkera_cli.__file__).parent
    importing = re.compile(r"^\s*(?:from\s+watchfiles[\s.]|import\s+watchfiles\b)", re.MULTILINE)
    offenders = [
        str(source_file.relative_to(package))
        for source_file in package.rglob("*.py")
        if importing.search(source_file.read_text(encoding="utf-8"))
    ]
    assert offenders == []
