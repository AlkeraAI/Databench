"""What the OS watch says, and what the live push has to do about it.

The live plane's only source of truth about what an agent wrote is the OS
watch, and the OS is not always straight with it. Two ways it is not, both
pinned here:

* it names the FOLDER instead of the file — a burst coalesced into one event
  for the directory it happened in, or a subtree the platform has decided has
  to be rescanned. A directory carries no bytes, so a plane that refuses the
  event refuses the writes it stands for: the file is on disk, the turn ends,
  and the drive was never told;
* it says nothing at all — macOS's event stream drops a creation by another
  process outright some runs and not others, which is how a file goes missing
  from a drive only sometimes;
* it was not listening yet — nothing arms the watch until the stream is asked
  for its first batch, and a poller diffs against the snapshot it took when it
  armed, so a file written in that window is never named by any event. The
  first batch sweeps the tree for exactly this.

The first is driven with a scripted watcher, so the coalescing is a fact of
the test rather than a race: the file is written, and the ONLY thing the sync
is ever handed is the folder it landed in. The second is driven with the
watcher production uses and a real subprocess, repeated, because what it
guards against is a drop rather than a delay.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.live_sync import (
    InboundEntry,
    LiveBatchAnswer,
    LiveCadence,
    LiveEntry,
    LiveSync,
    TreeAnswer,
    TreeEntry,
    TreeWatcher,
)
from alkera_cli.files.mount import MountRecord, SelfFence
from alkera_cli.files.tree_watch import Change

#: No settle: the clock here is moved by hand and never reaches it, and what
#: is proven is which files a folder event carries, not when they are read.
CADENCE = LiveCadence(debounce_ms=50, batch_every_ms=0, settle_ms=0)

#: The cadence the real watcher runs under here — the served numbers scaled to
#: one turn, so a round leaves in tens of milliseconds rather than a second.
CADENCE_REAL = LiveCadence(debounce_ms=50, batch_every_ms=50)

PAGE = "q3-report.html"
PAGE_TEXT = b"<!doctype html><title>Q3</title><h1>Q3 revenue</h1>"


class _Clock:
    """A clock the test moves by hand, so the cadence is not a wait."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


class _ScriptedWatcher:
    """Exactly the batches the test hands it, and nothing else."""

    def __init__(self, batches: Sequence[set[tuple[Change, str]]]) -> None:
        self.batches = list(batches)

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for batch in self.batches:
                yield batch

        return stream()


@dataclass
class _Drive:
    """The drive side: it reads the bytes off disk the way the real one does."""

    root: Path

    stored: dict[str, bytes] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    nodes: dict[str, str] = field(default_factory=dict)
    _next: int = 0

    def _node(self, path: str) -> str:
        if path not in self.nodes:
            self._next += 1
            self.nodes[path] = f"node-{self._next}"
        return self.nodes[path]

    def tree(self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int) -> TreeAnswer:
        for entry in entries:
            if entry.op != "upsert":
                raise AssertionError(f"nothing here is deleted or renamed ({entry})")
            self._node(entry.path)
        return TreeAnswer(live_seq=1, applied=len(entries))

    def resolve(self, paths: Sequence[str]) -> dict[str, str]:
        return {path: self.nodes[path] for path in paths if path in self.nodes}

    def upload(self, rel_path: str, node_id: str, size: int, **_: Any) -> None:
        self.stored[rel_path] = (self.root / rel_path).read_bytes()

    def live_batch(self, entries: Sequence[LiveEntry]) -> LiveBatchAnswer:
        for entry in entries:
            self.states[entry.node_id] = entry.state
        return LiveBatchAnswer(live_seq=1, pending=len(entries))

    def inbound(self) -> list[InboundEntry]:
        return []

    def item(self, node_id: str) -> dict[str, Any]:
        return {"id": node_id}

    def download(self, node_id: str, into: Path, **_: Any) -> None:
        raise AssertionError("this sync takes nothing from the drive")

    def state_of(self, relative: str) -> str:
        return self.states.get(self.nodes.get(relative, ""), "")


def _sync(root: Path, drive: _Drive, batches: Sequence[set[tuple[Change, str]]]) -> LiveSync:
    return LiveSync(
        root=root,
        root_path="home/dana/Chats/e2e/scratch",
        record=MountRecord(node_id="chat-node", org_path="home/dana/Chats/e2e"),
        cadence=CADENCE,
        api=drive,
        watcher=_ScriptedWatcher(batches),
        fence=SelfFence(grace=30.0),
        clock=_Clock(),
        sleep=lambda _seconds: None,
    )


async def test_a_folder_event_carries_the_file_written_inside_it(tmp_path: Path) -> None:
    """The watcher names only the folder, and the drive still gets the bytes."""
    root = tmp_path / "scratch"
    root.mkdir()
    (root / PAGE).write_bytes(PAGE_TEXT)

    drive = _Drive(root=root)
    await _sync(root, drive, [{(Change.added, str(root))}]).run()

    assert drive.stored.get(PAGE) == PAGE_TEXT, (
        f"a folder event dropped the file written inside it (the drive holds "
        f"{sorted(drive.stored)})"
    )
    assert drive.state_of(PAGE) == "uploading", (
        f"the drive was never told what {PAGE} was doing (states={drive.states})"
    )


async def test_a_folder_event_carries_a_file_written_in_a_child_folder(tmp_path: Path) -> None:
    """The folder the burst is reported for is the one the file is in, at any depth."""
    root = tmp_path / "scratch"
    nested = root / "out"
    nested.mkdir(parents=True)
    (nested / PAGE).write_bytes(PAGE_TEXT)

    drive = _Drive(root=root)
    await _sync(root, drive, [{(Change.added, str(nested))}]).run()

    assert drive.stored.get(f"out/{PAGE}") == PAGE_TEXT, (
        f"a folder event dropped the file written inside it (the drive holds "
        f"{sorted(drive.stored)})"
    )


async def test_a_folder_event_does_not_re_offer_what_the_drive_already_holds(
    tmp_path: Path,
) -> None:
    """A second burst over an unchanged folder says nothing about its files.

    A coalesced event names no file, so every neighbour is looked at — and a
    reader must not be shown rows going ``uploading`` for files nothing is
    uploading. Only what actually changed since the last round may be reported.
    """
    root = tmp_path / "scratch"
    root.mkdir()
    (root / PAGE).write_bytes(PAGE_TEXT)
    quiet = root / "notes.md"
    quiet.write_bytes(b"# notes\n")

    drive = _Drive(root=root)
    folder = {(Change.added, str(root))}
    sync = _sync(root, drive, [folder, folder])
    await sync.run()
    assert sorted(drive.stored) == ["notes.md", PAGE], (
        f"the first round did not push both files ({sorted(drive.stored)})"
    )

    # The same sync meets the same folder again, with one file rewritten:
    # only the bytes that moved are offered.
    drive.states.clear()
    drive.stored.clear()
    (root / PAGE).write_bytes(PAGE_TEXT + b"<p>revised</p>")
    sync.watcher = _ScriptedWatcher([folder])
    await sync.run()
    assert sorted(drive.stored) == [PAGE], (
        f"an unchanged neighbour was pushed again ({sorted(drive.stored)})"
    )
    assert drive.state_of("notes.md") == "", (
        f"an unchanged neighbour was reported as moving (states={drive.states})"
    )


async def test_a_folder_event_leaves_the_boxs_own_records_alone(tmp_path: Path) -> None:
    """The records beside a chat are not the chat's work, whatever names the burst.

    Expanding a folder event must not become a second way for the manifest, the
    write lock and the harness's per-chat storage to reach the drive — the
    watch filter's refusal has to hold on this path too.
    """
    root = tmp_path / "scratch"
    (root / ".runtime" / "agent").mkdir(parents=True)
    (root / ".runtime" / "agent" / "state.json").write_bytes(b"{}")
    (root / "manifest.json").write_bytes(b"{}")
    (root / ".lock").write_bytes(b"{}")
    (root / PAGE).write_bytes(PAGE_TEXT)

    drive = _Drive(root=root)
    await _sync(
        root,
        drive,
        [{(Change.added, str(root))}, {(Change.added, str(root / ".runtime" / "agent"))}],
    ).run()

    assert sorted(drive.stored) == [PAGE], (
        f"a folder event published the box's own records ({sorted(drive.stored)})"
    )


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="no symlinks on this platform")
async def test_a_folder_event_does_not_follow_a_link_out_of_the_root(tmp_path: Path) -> None:
    """A link in the folder is still the host's arrangement of its own disk."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.env"
    secret.write_bytes(b"TOKEN=live")

    root = tmp_path / "scratch"
    root.mkdir()
    (root / "secret.env").symlink_to(secret)
    (root / PAGE).write_bytes(PAGE_TEXT)

    drive = _Drive(root=root)
    await _sync(root, drive, [{(Change.added, str(root))}]).run()

    assert sorted(drive.stored) == [PAGE], (
        f"a folder event followed a link out of the root ({sorted(drive.stored)})"
    )


async def test_a_folder_event_over_an_empty_folder_sends_nothing(tmp_path: Path) -> None:
    """A burst that left nothing behind is a round with nothing to say."""
    root = tmp_path / "scratch"
    root.mkdir()

    drive = _Drive(root=root)
    started = time.monotonic()
    await _sync(root, drive, [{(Change.added, str(root))}]).run()

    assert drive.stored == {}, f"an empty folder pushed something ({sorted(drive.stored)})"
    assert drive.states == {}, f"an empty folder reported something ({drive.states})"
    assert time.monotonic() - started < 10.0, "an empty folder event should not wait on anything"


class _WatcherThatArmsLate:
    """A watcher whose files land while the stream is being armed.

    The window this stands for is the real one: the plane is started, the
    thread is running, and the OS watch is not listening yet — so a file the
    agent writes right then is never named by any event. The files are written
    inside the generator, after ``changes()`` has been awaited and before the
    first batch, so a sweep that ran any earlier than the arming would not see
    them.
    """

    def __init__(self, root: Path, files: Mapping[str, bytes]) -> None:
        self.root = root
        self.files = dict(files)

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for relative, content in self.files.items():
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            # The empty batch `yield_on_timeout` produces on a silent disk:
            # the watch is armed, and it has nothing to report.
            yield set()

        return stream()


async def test_a_file_written_before_the_watch_armed_is_still_pushed(tmp_path: Path) -> None:
    """The first batch sweeps what is already on disk, at any depth.

    Nothing about a file written in that window ever reaches the stream, so
    without the sweep it waits for the next checkpoint push — and is lost if
    the box goes away first.
    """
    root = tmp_path / "scratch"
    root.mkdir()

    drive = _Drive(root=root)
    sync = _sync(root, drive, [])
    sync.watcher = _WatcherThatArmsLate(root, {PAGE: PAGE_TEXT, "out/deep/notes.md": b"# notes\n"})
    await sync.run()

    assert sorted(drive.stored) == ["out/deep/notes.md", PAGE], (
        f"a file written before the watch armed never reached the drive ({sorted(drive.stored)})"
    )
    assert drive.state_of(PAGE) == "uploading", (
        f"the drive was never told what {PAGE} was doing (states={drive.states})"
    )


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="no symlinks on this platform")
async def test_the_first_sweep_refuses_what_every_other_path_refuses(tmp_path: Path) -> None:
    """The sweep is not a second way past containment or the record filter."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.env").write_bytes(b"TOKEN=live")

    root = tmp_path / "scratch"
    (root / ".runtime" / "agent").mkdir(parents=True)
    (root / ".runtime" / "agent" / "state.json").write_bytes(b"{}")
    (root / "manifest.json").write_bytes(b"{}")
    (root / ".lock").write_bytes(b"{}")
    (root / "secret.env").symlink_to(outside / "secret.env")
    (root / "linked").symlink_to(outside)
    (root / PAGE).write_bytes(PAGE_TEXT)

    drive = _Drive(root=root)
    sync = _sync(root, drive, [])
    sync.watcher = _WatcherThatArmsLate(root, {})
    await sync.run()

    assert sorted(drive.stored) == [PAGE], (
        f"the first sweep published what the plane refuses everywhere else ({sorted(drive.stored)})"
    )


async def test_a_later_sweep_says_nothing_about_files_the_drive_already_holds(
    tmp_path: Path,
) -> None:
    """A sweep is not "upload the tree": only bytes the drive has not seen.

    A plane restarted over an unchanged folder must not show a reader every
    file going ``uploading`` for nothing.
    """
    root = tmp_path / "scratch"
    root.mkdir()
    (root / PAGE).write_bytes(PAGE_TEXT)
    (root / "out").mkdir()
    (root / "out" / "notes.md").write_bytes(b"# notes\n")

    drive = _Drive(root=root)
    sync = _sync(root, drive, [])
    # The sweep, not the polling watcher's second look, has to find the rewrite:
    # Linux's inotify watch has no second look, so with it on a Mac would pass
    # on a path Linux never takes.
    sync.recheck_window = 0.0
    sync.watcher = _WatcherThatArmsLate(root, {})
    await sync.run()
    assert sorted(drive.stored) == ["out/notes.md", PAGE], (
        f"the first sweep did not push the tree ({sorted(drive.stored)})"
    )

    drive.stored.clear()
    drive.states.clear()
    (root / "out" / "notes.md").write_bytes(b"# notes\n- revised\n")
    sync.watcher = _WatcherThatArmsLate(root, {})
    await sync.run()

    assert sorted(drive.stored) == ["out/notes.md"], (
        f"a sweep re-sent bytes the drive already holds ({sorted(drive.stored)})"
    )
    assert drive.state_of(PAGE) == "", (
        f"an unchanged file was reported as moving (states={drive.states})"
    )


async def test_the_real_watcher_sees_a_file_another_process_writes(tmp_path: Path) -> None:
    """The watch production uses, over a file a subprocess leaves in the folder.

    This is the whole promise of the live plane reduced to one claim: the agent
    is another process, it writes into the folder after the watch is armed, and
    the watch has to say so within the window a turn gives it. Repeated,
    because the failure it guards is a drop rather than a delay — macOS's event
    stream loses a creation like this outright some runs and not others, and a
    watch that is only usually listening loses a file only sometimes, which is
    the worst way for a drive to be wrong.
    """
    watched: list[str] = []
    for round_number in range(6):
        root = tmp_path / f"scratch-{round_number}"
        root.mkdir()
        stop = threading.Event()
        arrived = asyncio.Event()

        async def pump(
            root: Path = root, stop: threading.Event = stop, arrived: asyncio.Event = arrived
        ) -> None:
            stream = await TreeWatcher(root, cadence=CADENCE_REAL, stop=stop).changes()
            async for batch in stream:
                if any(PAGE in path for _change, path in batch):
                    arrived.set()

        task = asyncio.ensure_future(pump())
        await asyncio.sleep(0.5)
        # A real second process, spawned the same way on every platform: the
        # interpreter running the suite, given the path as an argument rather than
        # interpolated into a shell word (there is no /bin/sh on Windows, and a
        # backslashed path would be eaten by the one on POSIX).
        writer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys, pathlib; pathlib.Path(sys.argv[1]).write_bytes(b'page')",
            str(root / PAGE),
        )
        assert await writer.wait() == 0, "the writer subprocess failed"
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(arrived.wait(), timeout=5.0)
        stop.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)
        watched.append(PAGE if arrived.is_set() else "MISSED")

    assert watched == [PAGE] * 6, (
        f"the watch lost a file another process wrote into the folder ({watched})"
    )
