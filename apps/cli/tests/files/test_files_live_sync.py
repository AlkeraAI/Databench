"""The live push, driven against a fake drive that behaves like the real one.

The fakes (``files._live_sync_fakes``) are a small server, not mocks: every
assertion is about what the *drive* ended up holding or being asked for —
never about how many times a method was called for its own sake. The metadata
queue that lists rows ahead of the bytes has its own module,
``test_files_live_sync_metadata``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from alkera_cli.files.inbound_backoff import INBOUND_RETRY_FIRST
from alkera_cli.files.live_sync import (
    InboundEntry,
    LiveBatchAnswer,
    LiveCadence,
    LiveEntry,
    LiveSync,
    RestLiveApi,
    hash_file,
    live_watch_filter,
)
from alkera_cli.files.mount import LeaseSupersededError, MountRecord, SelfFence
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import (
    FakeClock,
    FakeInboundApi,
    FakeLiveApi,
    FakeWatcher,
    RefusedError,
)
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


def test_five_writes_to_one_path_are_one_row_and_one_upload(tree: Path) -> None:
    """The agent rewriting a report five times inside one window is one row on
    the drive holding the last bytes, not five versions of a half-written file."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    for attempt in range(5):
        _write(tree, "report.md", f"draft {attempt}".encode())
        sync.classify(Change.modified if attempt else Change.added, str(tree / "report.md"))
    sync.flush()

    assert [[entry.path for entry in batch] for batch in api.trees] == [["report.md"]]
    assert api.uploads == ["report.md"]
    assert api.stored["report.md"] == b"draft 4"
    assert api.states() == {api.nodes["report.md"]: "uploading"}


def test_three_hundred_changes_flush_in_two_batches_in_order(tree: Path) -> None:
    """A batch has a cap, so a burst is split — and the split keeps the order
    the states were decided in, or the drive would show a later state first."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=0, max_batch_entries=256))

    for index in range(300):
        name = f"f{index:03d}.txt"
        _write(tree, name, b"x")
        sync.classify(Change.added, str(tree / name))
    sync.flush()

    assert [len(batch) for batch in api.batches] == [256, 44]
    reported = [entry.node_id for batch in api.batches for entry in batch]
    assert reported == [api.nodes[f"f{index:03d}.txt"] for index in range(300)]
    assert len(api.uploads) == 300


def test_a_deleted_path_is_trashed_once_and_lands_in_tombstones(tree: Path) -> None:
    """A file the box deletes is trashed on the drive, and remembered — the
    checkpoint push must not put it back."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    path = _write(tree, "notes.txt", b"hello")
    sync.classify(Change.added, str(path))
    sync.flush()
    node = api.nodes["notes.txt"]

    path.unlink()
    sync.classify(Change.deleted, str(path))
    sync.flush()

    assert api.trashed == [node]
    assert sync.tombstones == {"notes.txt"}


def test_a_delete_plus_an_add_of_the_same_bytes_is_one_rename(tree: Path) -> None:
    """The agent moving a file is a rename on the drive: no second upload, no
    trash, and the node that carried the history keeps carrying it."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    first = _write(tree, "draft.md", b"the same bytes")
    sync.classify(Change.added, str(first))
    sync.flush()
    node = api.nodes["draft.md"]
    api.uploads.clear()

    second = _write(tree, "final.md", b"the same bytes")
    first.unlink()
    sync.classify(Change.deleted, str(first))
    sync.classify(Change.added, str(second))
    sync.flush()

    assert api.renames == [(node, "draft.md", "final.md")]
    assert api.nodes["final.md"] == node
    assert api.uploads == []
    assert api.trashed == []
    assert sync.tombstones == set()


def test_a_tmp_that_never_had_a_node_is_a_modify_of_the_final_path(tree: Path) -> None:
    """Write-to-temp-then-rename is how careful programs save. The temp file
    never reached the drive, so there is nothing to rename and nothing to
    trash — only the final path's bytes."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    temporary = _write(tree, "report.md.tmp", b"rendered")
    final = _write(tree, "report.md", b"rendered")
    temporary.unlink()
    sync.classify(Change.added, str(temporary))
    sync.classify(Change.deleted, str(temporary))
    sync.classify(Change.added, str(final))
    sync.flush()

    assert api.renames == []
    assert api.trashed == []
    assert api.uploads == ["report.md"]
    assert api.stored["report.md"] == b"rendered"


class _RecordingHasher:
    """The real hasher, plus the list of files it actually read.

    Which files a round OPENS is the thing under test: a coalesced event names
    a folder, so the plane is handed every file in it and has to decide which
    ones are worth reading. The digests are the real ones — nothing here
    answers from a script.
    """

    def __init__(self) -> None:
        self.read: list[str] = []

    def __call__(self, path: Path) -> tuple[str, int]:
        self.read.append(path.name)
        return hash_file(path)


def _settled(tree: Path, api: FakeLiveApi, clock: FakeClock, names: Sequence[str]) -> LiveSync:
    """A sync whose drive already holds ``names``, ready for the next event."""
    hasher = _RecordingHasher()
    sync = _sync(tree, api, clock, hasher=hasher)
    for name in names:
        sync.classify(Change.added, str(_write(tree, name, f"{name} v1".encode())))
    sync.flush()
    hasher.read.clear()
    return sync


def test_a_coalesced_folder_event_does_not_reread_the_files_the_drive_holds(
    tree: Path,
) -> None:
    """The stat answers for a file whose bytes have not moved.

    macOS hands the plane one event for the FOLDER, so a chat folder of a
    thousand files is offered whole every time the agent touches one of them.
    Hashing to learn what the size and mtime already say reads the entire
    directory off disk to send nothing at all.
    """
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _settled(tree, api, clock, ["a.txt", "b.txt", "c.txt"])
    hasher = sync.hasher
    assert isinstance(hasher, _RecordingHasher)

    sync.classify(Change.modified, str(tree))
    sync.flush()

    assert hasher.read == []
    assert api.uploads == ["a.txt", "b.txt", "c.txt"]


def test_a_coalesced_folder_event_still_sends_the_file_whose_bytes_moved(
    tree: Path,
) -> None:
    """The pre-check may not cost the plane a write.

    The only file read is the one whose stamp moved, and it is the one that
    reaches the drive — a folder event that skipped it because its neighbours
    were untouched would lose the agent's work.
    """
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _settled(tree, api, clock, ["a.txt", "b.txt", "c.txt"])
    hasher = sync.hasher
    assert isinstance(hasher, _RecordingHasher)
    api.uploads.clear()

    _write(tree, "b.txt", b"b.txt rewritten, and longer than it was")
    sync.classify(Change.modified, str(tree))
    sync.flush()

    # b.txt is the only file opened at all: the round that decides it moved and
    # the upload that sends it both read it, and nothing else is touched.
    assert set(hasher.read) == {"b.txt"}
    assert api.uploads == ["b.txt"]
    assert api.stored["b.txt"] == b"b.txt rewritten, and longer than it was"


def test_a_file_rewritten_to_its_own_bytes_is_read_once_and_then_not_again(
    tree: Path,
) -> None:
    """A stamp that moved is a question, not an answer.

    Rewriting a file with the bytes it already had moves its mtime, so the
    pre-check cannot answer and the file is hashed — which finds the drive's
    own digest and sends nothing. The stamp taken at that hash is what makes
    the NEXT folder event free.
    """
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _settled(tree, api, clock, ["a.txt"])
    hasher = sync.hasher
    assert isinstance(hasher, _RecordingHasher)
    api.uploads.clear()

    (tree / "a.txt").write_bytes(b"a.txt v1")
    os.utime(tree / "a.txt", (time.time() + 2, time.time() + 2))
    sync.classify(Change.modified, str(tree))
    sync.flush()
    assert hasher.read == ["a.txt"]
    assert api.uploads == []

    sync.classify(Change.modified, str(tree))
    sync.flush()
    assert hasher.read == ["a.txt"]
    assert api.uploads == []


def test_a_file_the_drive_never_had_is_hashed_on_the_folder_event(tree: Path) -> None:
    """No agreed digest, no stamp: a new file is read and sent."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _settled(tree, api, clock, ["a.txt"])
    hasher = sync.hasher
    assert isinstance(hasher, _RecordingHasher)
    api.uploads.clear()

    _write(tree, "fresh.txt", b"written while the watch was coalescing")
    sync.classify(Change.modified, str(tree))
    sync.flush()

    assert "fresh.txt" in hasher.read
    assert api.uploads == ["fresh.txt"]


def test_a_write_during_hashing_is_requeued_and_the_last_bytes_land(tree: Path) -> None:
    """A file still being written must not be published as a finished version.
    The round that catches it moving sends nothing; the next one sends what
    the writer ended up with."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    path = _write(tree, "growing.csv", b"a,b\n1,2\n")
    moved: list[str] = []

    def hasher_that_writes(target: Path) -> tuple[str, int]:
        digest = hash_file(target)
        if not moved:
            moved.append(target.name)
            target.write_bytes(b"a,b\n1,2\n3,4\n")
            os.utime(target, (time.time() + 2, time.time() + 2))
        return digest

    sync = _sync(tree, api, clock, hasher=hasher_that_writes)
    sync.classify(Change.added, str(path))
    sync.flush()

    assert api.uploads == []
    assert "growing.csv" in sync.pending

    sync.flush()
    assert api.uploads == ["growing.csv"]
    assert api.stored["growing.csv"] == b"a,b\n1,2\n3,4\n"


def test_a_path_escaping_through_a_symlink_is_never_sent(tmp_path: Path, tree: Path) -> None:
    """A link planted in the folder cannot make the holder publish a file from
    elsewhere on the host under an innocent name."""
    secret = tmp_path / "outside" / "id_rsa"
    secret.parent.mkdir()
    secret.write_bytes(b"PRIVATE KEY")
    (tree / "keys").symlink_to(secret.parent)

    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    assert sync.classify(Change.added, str(tree / "keys" / "id_rsa")) is None
    assert sync.classify(Change.added, str(tree / ".." / "outside" / "id_rsa")) is None
    sync.flush()

    assert api.trees == []
    assert api.uploads == []
    assert api.batches == []


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(".lock", id="the-chat-lock"),
        pytest.param(".lock.stale.1726000000.ab12", id="a-lock-rotation"),
        pytest.param(".lock.reclaim", id="the-reclaim-guard"),
        pytest.param("chart.alkerareport", id="a-pointer-file"),
    ],
)
def test_machine_local_state_and_pointers_are_skipped(tree: Path, relative: str) -> None:
    """Locks are keyed to a pid on this host and pointers are derived state:
    neither means anything on the drive, and pushing a pointer back would turn
    a rendered artefact into authoritative bytes."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    path = _write(tree, relative, b"local")
    assert sync.classify(Change.added, str(path)) is None
    sync.flush()
    assert api.uploads == []


def test_a_host_link_inside_the_folder_is_skipped(tree: Path) -> None:
    """A link is the host's arrangement of its own disk, not a file the drive
    can keep."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    target = _write(tree, "real.txt", b"bytes")
    link = tree / "shortcut.txt"
    link.symlink_to(target)

    assert sync.classify(Change.added, str(link)) is None
    sync.flush()
    assert api.uploads == []


def test_a_file_over_the_cap_is_reported_on_box_without_an_upload(tree: Path) -> None:
    """A file too big for the live plane still gets a row — the reader learns
    it exists and where it is — but its bytes stay on the box until the
    checkpoint."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=0, max_file_bytes=8))

    big = _write(tree, "dump.bin", b"0123456789")
    small = _write(tree, "note.txt", b"ok")
    sync.classify(Change.added, str(big))
    sync.classify(Change.added, str(small))
    sync.flush()

    assert api.states()[api.nodes["dump.bin"]] == "on_box"
    assert api.states()[api.nodes["note.txt"]] == "uploading"
    assert api.uploads == ["note.txt"]


def test_over_the_bandwidth_window_the_rest_is_deferred(tree: Path) -> None:
    """The window the server serves is the window the holder obeys: what does
    not fit is reported as waiting and stays queued, not dropped."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    waited: list[str] = []
    sync = _sync(
        tree,
        api,
        clock,
        cadence=LiveCadence(settle_ms=0, bandwidth_bytes_per_minute=6),
        on_deferred=waited.append,
    )

    _write(tree, "a.txt", b"12345")
    _write(tree, "b.txt", b"67890")
    sync.classify(Change.added, str(tree / "a.txt"))
    sync.classify(Change.added, str(tree / "b.txt"))
    sync.flush()

    assert api.uploads == ["a.txt"]
    assert api.states()[api.nodes["b.txt"]] == "deferred"
    assert waited == ["b.txt"]
    assert "b.txt" in sync.pending

    clock.advance(61.0)
    sync.fence.beat()  # the beats kept landing while the window drained
    sync.flush()
    assert api.uploads == ["a.txt", "b.txt"]
    assert api.stored["b.txt"] == b"67890"


def test_a_silent_fence_sends_nothing_and_a_beat_re_arms_it(tree: Path) -> None:
    """A holder that stops hearing from the server cannot be told it lost the
    folder, so it stops itself — and starts again only when a beat lands."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    _write(tree, "late.txt", b"bytes")
    sync.classify(Change.added, str(tree / "late.txt"))
    clock.advance(31.0)
    sync.flush()

    assert api.trees == []
    assert api.uploads == []

    sync.fence.beat()
    sync.flush()
    assert api.uploads == ["late.txt"]


def test_a_fence_that_closes_says_so_once_and_the_holder_reads_as_fenced(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A beat loop that died must not be silent.

    Both directions fail closed with the queue intact, which is right — and
    indistinguishable from a folder nobody touched. That silence is why weeks
    of these failures read as "the runner was slow". The closure now names the
    folder once, and the holder answers for itself so the service above it can
    say the box went deaf rather than guess.
    """
    caplog.set_level(logging.WARNING, logger="alkera_cli.files.live_sync")
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)

    _write(tree, "late.txt", b"bytes")
    sync.classify(Change.added, str(tree / "late.txt"))
    sync.flush()
    assert sync.fenced is False
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []

    clock.advance(31.0)
    _write(tree, "later.txt", b"more")
    sync.classify(Change.added, str(tree / "later.txt"))
    sync.flush()
    # The inbound direction is deaf too, and says nothing further: one outage
    # is one warning, not one per flush and drain for as long as it lasts.
    assert sync.pull_inbound() == []
    sync.flush()

    assert sync.fenced is True
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    said = warnings[0].getMessage()
    assert "chat-node" in said and str(tree) in said
    assert "heartbeat" in said

    # A beat re-arms it — the queue was owed, not lost — and a second outage is
    # a second warning rather than a closure swallowed because an earlier one
    # was already reported.
    sync.fence.beat()
    assert sync.fenced is False
    sync.flush()
    assert api.uploads == ["late.txt", "later.txt"]

    clock.advance(31.0)
    _write(tree, "latest.txt", b"last")
    sync.classify(Change.added, str(tree / "latest.txt"))
    sync.flush()

    assert sync.fenced is True
    assert len([record for record in caplog.records if record.levelno >= logging.WARNING]) == 2
    assert api.uploads == ["late.txt", "later.txt"]


def test_a_fenced_refusal_stops_the_flush_and_leaves_the_queue_intact(tree: Path) -> None:
    """Losing the lease mid-flush is not a lost queue: the work is still owed
    to the drive, and the caller must stop rather than keep writing."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, refuse="files.lease_fenced", refuse_on="upload")
    sync = _sync(tree, api, clock)

    _write(tree, "one.txt", b"first")
    _write(tree, "two.txt", b"second")
    sync.classify(Change.added, str(tree / "one.txt"))
    sync.classify(Change.added, str(tree / "two.txt"))

    with pytest.raises(LeaseSupersededError):
        sync.flush()

    assert api.uploads == []
    assert set(sync.pending) == {"one.txt", "two.txt"}

    api.refuse = None
    sync.flush()
    assert sorted(api.uploads) == ["one.txt", "two.txt"]


def test_a_refusal_that_is_not_the_fence_is_not_swallowed(tree: Path) -> None:
    """Only the fence means "stop writing". Any other refusal is a fault to
    surface, never a supersession."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, refuse="files.quota_exceeded", refuse_on="upload")
    sync = _sync(tree, api, clock)

    _write(tree, "one.txt", b"first")
    sync.classify(Change.added, str(tree / "one.txt"))

    with pytest.raises(RefusedError):
        sync.flush()


def test_the_loop_flushes_on_the_served_cadence(tree: Path) -> None:
    """Two bursts a cadence apart reach the drive as they happen, not only at
    the end — which is the whole point of watching."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    _write(tree, "first.txt", b"one")
    _write(tree, "second.txt", b"two")

    class TickingWatcher(FakeWatcher):
        async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
            batches = self.batches

            async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
                for batch in batches:
                    clock.advance(1.0)
                    yield batch

            return stream()

    watcher = TickingWatcher(
        [
            {(Change.added, str(tree / "first.txt"))},
            {(Change.added, str(tree / "second.txt"))},
        ]
    )
    sync = _sync(
        tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500), watcher=watcher
    )

    asyncio.run(sync.run())

    assert [batch[0].node_id for batch in api.batches] == [
        api.nodes["first.txt"],
        api.nodes["second.txt"],
    ]
    assert api.uploads == ["first.txt", "second.txt"]


class _BlindWatcher(FakeWatcher):
    """The polling watcher's blind second, as a script.

    It reports the first save, and then only ticks: a rewrite inside the second
    the poller last looked in moves no event, so whatever the drive learns of
    it, it learns without one. ``between`` runs after each batch has been
    consumed — the loop has flushed it — which is where the test rewrites the
    file.
    """

    def __init__(
        self,
        clock: FakeClock,
        first: set[tuple[Change, str]],
        *,
        ticks: int,
        between: Callable[[int], None],
    ) -> None:
        super().__init__([first, *[set() for _ in range(ticks)]])
        self.clock = clock
        self.between = between

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for index, batch in enumerate(self.batches):
                self.clock.advance(0.6)
                yield batch
                self.between(index)

        return stream()


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(b"<h1>Q3 revenue, revised</h1>", id="a-new-size"),
        # Same length: the stamp that moves is the modification time alone.
        pytest.param(b"<h1>Q3 REVENUE</h1>", id="the-same-size"),
    ],
)
def test_a_rewrite_the_watcher_never_reports_still_lands_as_the_last_bytes(
    tree: Path, rewrite: bytes
) -> None:
    """Write, land, rewrite in the same breath: the drive ends with the rewrite.

    macOS's polling watcher compares a file's modification time to the whole
    second, so an agent that saves its report and saves it again inside that
    second gets one event. The first save lands and the second is never
    offered unless the holder looks again on its own; the drive then serves
    the superseded draft for as long as the chat lives.
    """
    first = b"<h1>Q3 revenue</h1>"
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    path = _write(tree, "report.html", first)

    def between(index: int) -> None:
        if index == 0:
            assert api.stored.get("report.html") == first, "the first save never landed"
            before = path.stat()
            path.write_bytes(rewrite)
            # A modification time the watcher could not tell from the first,
            # but that a stat still can.
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000))

    # Enough ticks for the second look's window to run out after the rewrite.
    watcher = _BlindWatcher(clock, {(Change.added, str(path))}, ticks=6, between=between)
    sync = _sync(
        tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500), watcher=watcher
    )
    sync.recheck_window = 2.0

    asyncio.run(sync.run())

    assert api.stored["report.html"] == rewrite, "the drive still holds the first save"
    assert set(api.uploaded_to.values()) == {api.nodes["report.html"]}, (
        "the rewrite landed on a node other than the one the first save made"
    )


def test_only_a_file_written_just_now_is_watched_past_its_event(tree: Path) -> None:
    """The second look is for files inside the blind second. A tree that was on
    disk long before the plane started is swept once and none of it is kept —
    its next change lies outside that second and arrives as an event — while
    the file written a moment ago is still looked at."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, clock=clock)
    old = _write(tree, "old.txt", b"from yesterday")
    hour_ago = time.time_ns() - 3_600 * 1_000_000_000
    os.utime(old, ns=(hour_ago, hour_ago))
    fresh = _write(tree, "fresh.txt", b"just now")

    def between(index: int) -> None:
        if index == 0:
            old.write_bytes(b"from yesterday, edited")
            os.utime(old, ns=(hour_ago, hour_ago))
            fresh.write_bytes(b"just now, again")

    watcher = _BlindWatcher(clock, set(), ticks=2, between=between)
    sync = _sync(
        tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500), watcher=watcher
    )
    sync.recheck_window = 2.0

    asyncio.run(sync.run())

    assert api.stored["fresh.txt"] == b"just now, again"
    assert api.stored["old.txt"] == b"from yesterday"


def test_the_cadence_reads_the_served_block_and_keeps_defaults(tree: Path) -> None:
    """A server that serves half the block — or a key this build never heard
    of — leaves a usable cadence behind rather than an error."""
    cadence = LiveCadence.from_grant({"maxFileBytes": 42, "inbound": True, "somethingNew": 1})

    assert cadence.max_file_bytes == 42
    assert cadence.inbound is True
    assert cadence.batch_every_ms == LiveCadence().batch_every_ms
    assert LiveCadence.from_grant(None) == LiveCadence()


# -- what the watcher refuses to look at ---------------------------------------


@pytest.mark.parametrize(
    ("name", "wanted"),
    [
        pytest.param("notes.md", True, id="the chats own work"),
        pytest.param("deep/nested/report.csv", True, id="nested work"),
        pytest.param(".lock", False, id="the write lock"),
        pytest.param("manifest.json", False, id="the folders manifest"),
        pytest.param("trace.digest.json", False, id="the trace digest"),
        pytest.param(".runtime/agent.sock", False, id="inside the runtime dir"),
        pytest.param("deep/.runtime/state.json", False, id="a nested runtime dir"),
        pytest.param("manifest.json.bak", True, id="a file merely named like one"),
    ],
)
def test_the_watcher_refuses_the_boxs_own_records(tree: Path, name: str, wanted: bool) -> None:
    """A lock rewritten on every open would wake a flush a second on an idle
    chat, and the folder's records describe it rather than living in it."""
    assert live_watch_filter(Change.modified, str(tree / name)) is wanted


def test_a_rotated_lock_is_refused_under_every_name_it_takes(tree: Path) -> None:
    """A reclaim leaves the dead holder's lock beside the live one for
    forensics; each rotation would otherwise be a change to stream."""
    rotated = [
        path
        for path in (".lock", ".lock.stale.1758000000", ".lock.reclaim", ".lock.reclaim.stale.7")
        if live_watch_filter(Change.added, str(tree / path))
    ]

    assert rotated == []


# -- the live plane over the real wire -----------------------------------------


def _rest(
    tree: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    files: Any | None = None,
    pushed: list[list[str]] | None = None,
) -> RestLiveApi:
    """A live API on a client whose transport is the test's own server."""

    def record(**kwargs: Any) -> None:
        if pushed is not None:
            pushed.append([Path(path).name for path in kwargs["paths"]])

    return RestLiveApi(
        files=files or _FakeFiles(),
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://drive"),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Chats/c",
        push=record,
    )


class _FakeFiles:
    """The Files namespace slice the live API reads through."""

    def __init__(self, nodes: dict[str, str] | None = None) -> None:
        self.nodes = nodes or {}
        self.asked: list[str] = []

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self.asked.append(item_path)
        node_id = self.nodes.get(item_path)
        if node_id is None:
            raise RuntimeError(f"GET {item_path} returned 404 — {{}}")
        return {"id": node_id, "etag": f"etag-{node_id}"}

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        """The step below the leased node, which the helper files at ``Chats/c``."""
        assert item_id == "lease-9", item_id
        return self.item_by_path(drive_id, f"Chats/c/{item_path}".rstrip("/"))

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return {"id": item_id, "etag": f"etag-{item_id}"}


class _CutFiles(_FakeFiles):
    """The drive as a box on a chat's lease meets it: the folder's path is
    spelled from the deepest ancestor the box may read — its bare name — so
    the absolute walk names nothing, and only the step below the leased node
    reaches a file."""

    def item_by_path(self, drive_id: str, item_path: str) -> dict[str, Any]:
        self.asked.append(f"root:/{item_path}")
        raise RuntimeError(f"GET {item_path} returned 404 — {{}}")

    def item_under(self, drive_id: str, item_id: str, item_path: str) -> dict[str, Any]:
        assert item_id == "lease-9", item_id
        self.asked.append(f"{item_id}:/{item_path}")
        node_id = self.nodes.get(f"Chats/c/{item_path}".rstrip("/"))
        if node_id is None:
            raise RuntimeError(f"GET {item_path} returned 404 — {{}}")
        return {"id": node_id, "etag": f"etag-{node_id}"}


def test_a_path_is_resolved_from_the_leased_node_not_from_the_drive_root(tree: Path) -> None:
    """On the node the live plane asked the root for ``<bare name>/scratch/
    hello.txt`` and was told 404 on every round; the file the agent had just
    written never left the box."""
    files = _CutFiles({"Chats/c/scratch/hello.txt": "node-hello", "Chats/c/scratch": "node-s"})
    api = RestLiveApi(
        files=files,
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Kickoff.alkerachat",
        inside="scratch",
        push=lambda **_kwargs: None,
    )

    assert api.resolve(["hello.txt", "missing.txt"]) == {"hello.txt": "node-hello"}
    assert files.asked == ["lease-9:/scratch/hello.txt", "lease-9:/scratch/missing.txt"], (
        "asked as the step below the lease, never as a path from the root"
    )


def test_the_live_upload_hands_the_push_the_leases_drive(tree: Path) -> None:
    """The push asks for the caller's own drive when handed none, and a box on
    its machine credential has none: the round that carried the agent's file
    was refused on ``GET /files/drives`` and the file waited on the box."""
    handed: list[dict[str, Any]] = []

    def record(**kwargs: Any) -> Any:
        handed.append(kwargs)
        return SimpleNamespace(agreed={"hello.txt": "etag-1"})

    api = RestLiveApi(
        files=_FakeFiles(),
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        root=tree,
        drive_id="drive-1",
        lease_node_id="lease-9",
        dest="Kickoff.alkerachat",
        inside="scratch",
        push=record,
    )
    (tree / "hello.txt").write_bytes(b"hi\n")

    assert api.upload("hello.txt", "node-hello", 3) == "etag-1"
    (call,) = handed
    assert call["drive_id"] == "drive-1"
    assert call["node_id"] == "lease-9" and call["inside"] == "scratch"


def test_the_live_batch_carries_every_state_and_reads_the_servers_counters(tree: Path) -> None:
    """The holder tells the drive what it is doing to each node, and the drive
    answers with where its live sequence got to and how much is still owed."""
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"liveSeq": 7, "pending": 2})

    answer = _rest(tree, handler).live_batch(
        [
            LiveEntry(node_id="n1", state="uploading", box_size=12),
            LiveEntry(node_id="n2", state="on_box"),
        ]
    )

    assert answer == LiveBatchAnswer(live_seq=7, pending=2)
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/api/v1/files/drives/drive-1/items/lease-9/lease/live")
    assert body == {
        "entries": [
            {"nodeId": "n1", "state": "uploading", "boxSize": 12},
            {"nodeId": "n2", "state": "on_box"},
        ]
    }


def test_a_fenced_refusal_anywhere_on_the_live_plane_ends_the_flush(tree: Path) -> None:
    """A holder whose epoch has been retired must stop writing, not retry: the
    folder belongs to somebody else now, and the queue is owed to them."""
    api = _rest(
        tree,
        lambda request: httpx.Response(409, json={"code": "files.lease_fenced"}),
        files=_FakeFiles({"Chats/c": "lease-9", "Chats/c/late.txt": "node-late"}),
    )
    sync = LiveSync(
        root=tree,
        record=MountRecord(heartbeat_every=15.0),
        cadence=LiveCadence(settle_ms=0),
        api=api,
        watcher=FakeWatcher([]),
        fence=_beaten(),
        clock=FakeClock(),
    )
    (tree / "late.txt").write_text("x")
    sync.classify(Change.added, str(tree / "late.txt"))

    with pytest.raises(LeaseSupersededError):
        sync.flush()

    assert set(sync.pending) == {"late.txt"}


def test_a_refusal_that_is_not_the_fence_is_left_alone(tree: Path) -> None:
    """A 500 on the live plane is the API being unwell, not the lease moving —
    reading it as a superseded lease would stop a holder that still holds."""
    api = _rest(tree, lambda request: httpx.Response(500, json={"code": "internal"}))

    with pytest.raises(httpx.HTTPStatusError):
        api.live_batch([LiveEntry(node_id="n1", state="uploading")])


def test_the_inbound_read_keeps_the_states_it_knows_and_drops_the_rest(tree: Path) -> None:
    """A newer server naming a state this build cannot apply must not have it
    silently treated as one this build can."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "entries": [
                    {"nodeId": "n1", "state": "inbound", "seq": 4},
                    {"nodeId": "n2", "state": "inbound_delete", "seq": 5},
                    {"nodeId": "n3", "state": "something_new", "seq": 6},
                    {"nodeId": "", "state": "inbound", "seq": 7},
                ]
            },
        )

    entries = _rest(tree, handler).inbound()

    assert entries == [
        InboundEntry(node_id="n1", state="inbound", seq=4),
        InboundEntry(node_id="n2", state="inbound_delete", seq=5),
    ]
    assert "inbound=true" in seen[0]


def test_resolving_paths_reads_the_rows_the_drive_lists_and_makes_nothing(tree: Path) -> None:
    """Rows are made by the tree batch, never by a lookup: a path the drive
    does not list is absent from the answer, and nothing is pushed or posted
    to make it exist."""
    pushed: list[list[str]] = []
    posted: list[str] = []
    files = _FakeFiles({"Chats/c/b.txt": "node-b", "Chats/c/deep/c.txt": "node-c"})

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(201, json=[])

    api = _rest(tree, handler, files=files, pushed=pushed)
    answer = api.resolve(["a.txt", "b.txt", "deep/c.txt"])

    assert answer == {"b.txt": "node-b", "deep/c.txt": "node-c"}
    assert pushed == []
    assert posted == []


def test_uploading_one_path_sends_that_path_and_no_other(tree: Path) -> None:
    """The per-file upload is deliberately not a push of everything the batch
    named: the live sync re-queues a file that moved under its own hash, and a
    push of the whole batch would publish those half-written bytes anyway."""
    pushed: list[list[str]] = []
    api = _rest(tree, lambda request: httpx.Response(200, json={}), pushed=pushed)

    api.upload("deep/report.csv", "node-7", 12)

    assert pushed == [["report.csv"]]


def test_a_download_lands_the_whole_stream_on_the_box(tree: Path) -> None:
    """Streamed rather than read whole: an inbound file is whatever somebody
    dropped on the web, and holding it in memory is a limit the drive lacks."""
    api = _rest(
        tree,
        lambda request: httpx.Response(200, content=b"chunk-one chunk-two"),
    )
    into = tree / "pulled" / "dropped.bin"

    api.download("node-3", into)

    assert into.read_bytes() == b"chunk-one chunk-two"


def _beaten() -> SelfFence:
    fence = SelfFence(grace=60.0)
    fence.beat()
    return fence


def _pushed(sync: LiveSync, tree: Path, relative: str, data: bytes) -> str:
    """Write ``relative`` on the box and let the drive learn about it."""
    _write(tree, relative, data)
    sync.classify(Change.added, str(tree / relative))
    sync.flush()
    return api_of(sync).nodes[relative]


def api_of(sync: LiveSync) -> FakeInboundApi:
    """The fake drive behind a sync, typed for the assertions below."""
    assert isinstance(sync.api, FakeInboundApi)
    return sync.api


def test_an_inbound_file_is_never_visible_half_written(tree: Path) -> None:
    """The drive's bytes replace the box's file in one step. A reader on the
    box — the agent, mid-turn — meets the old file or the new one, never the
    first half of the new one under the name it is about to read."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-report", state="inbound", seq=1)]
    api.paths = {"n-report": "Home/work/report.md"}
    api.contents = {"n-report": b"the whole rewritten report"}
    seen: list[bytes | None] = []

    def look(node_id: str, into: Path, **_: Any) -> None:
        final = tree / "report.md"
        seen.append(final.read_bytes() if final.exists() else None)

    api.on_download = look
    sync = _sync(tree, api, clock, root_path="Home/work")

    assert sync.pull_inbound() == [LiveEntry(node_id="n-report", state="applied")]
    assert (tree / "report.md").read_bytes() == b"the whole rewritten report"
    assert seen == [None]
    assert api.states() == {"n-report": "applied"}


def test_an_inbound_delete_unlinks_a_file_the_drive_already_had(tree: Path) -> None:
    """Someone trashed the file on the web while the box held the folder. The
    box's copy goes too, or the next checkpoint would put it straight back."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "notes.txt", b"the agreed bytes")

    api.queued = [InboundEntry(node_id=node, state="inbound_delete", seq=2)]
    api.paths = {node: "Home/work/notes.txt"}
    api.batches.clear()

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    assert not (tree / "notes.txt").exists()


def test_an_inbound_rename_moves_the_file_by_node_id(tree: Path) -> None:
    """A rename on the web is a move on the box — the same bytes under the new
    name, with no download and no second copy under the old one."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    node = _pushed(sync, tree, "draft.md", b"the same bytes")

    api.queued = [InboundEntry(node_id=node, state="inbound_rename", seq=2)]
    api.paths = {node: "Home/work/final/report.md"}
    api.batches.clear()

    assert sync.pull_inbound() == [LiveEntry(node_id=node, state="applied")]
    assert not (tree / "draft.md").exists()
    assert (tree / "final" / "report.md").read_bytes() == b"the same bytes"
    assert api.downloads == []


def test_an_inbound_rename_of_a_node_the_box_never_had_downloads_it(tree: Path) -> None:
    """A node this holder never wrote has nothing to move, so the new name is
    filled from the drive rather than left as a gap in the folder."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-old", state="inbound_rename", seq=1)]
    api.paths = {"n-old": "Home/work/renamed.md"}
    api.contents = {"n-old": b"bytes from the drive"}
    sync = _sync(tree, api, clock, root_path="Home/work")

    assert sync.pull_inbound() == [LiveEntry(node_id="n-old", state="applied")]
    assert (tree / "renamed.md").read_bytes() == b"bytes from the drive"


@pytest.mark.parametrize(
    "wire_path",
    [
        pytest.param("Home/work/bc/secret.txt", id="a-sibling-that-shares-a-prefix"),
        pytest.param("Home/workshop/b/secret.txt", id="a-sibling-of-an-ancestor"),
        pytest.param("Home/work/b/../../../secret.txt", id="a-walk-back-out"),
        pytest.param("Elsewhere/secret.txt", id="another-branch-entirely"),
        pytest.param("Home/work/b", id="the-lease-root-itself"),
    ],
)
def test_an_inbound_item_outside_the_lease_root_is_refused(tree: Path, wire_path: str) -> None:
    """Containment is decided segment by segment, so ``/a/bc`` is not inside
    ``/a/b``. An item the server names outside the folder this holder leased
    writes nothing and is not answered for — the holder has no business
    touching it and no standing to report on it."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-out", state="inbound", seq=1)]
    api.paths = {"n-out": wire_path}
    api.contents = {"n-out": b"PRIVATE KEY"}
    sync = _sync(tree, api, clock, root_path="Home/work/b")

    assert sync.pull_inbound() == []
    assert api.downloads == []
    assert api.batches == []
    assert list(tree.rglob("*")) == []


def test_a_slow_download_that_arrives_whole_lands(tree: Path) -> None:
    """Bytes that took a long time to arrive are still the drive's bytes.

    A ceiling of thirty seconds meant a file dropped into the chat over a slow
    link was fetched, thrown away, and asked for again on every beat — it never
    landed. The ceiling is now the transfer's, generous, and reached only by a
    stalled stream; a download that finished is kept however long it took.
    """
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-slow", state="inbound", seq=1)]
    api.paths = {"n-slow": "Home/work/big.csv"}
    api.contents = {"n-slow": b"a,b\n1,2\n"}
    api.on_download = lambda node_id, into: clock.advance(45.0)
    sync = _sync(tree, api, clock, root_path="Home/work")

    assert sync.pull_inbound() == [LiveEntry(node_id="n-slow", state="applied")]
    assert (tree / "big.csv").read_bytes() == b"a,b\n1,2\n"
    assert list(tree.rglob("*")) == [tree / "big.csv"]


def test_the_download_ceiling_is_the_files_size_over_a_slow_link_with_a_high_floor(
    tree: Path,
) -> None:
    """The deadline handed to the download grows with the file, and never
    drops below the floor: a small file on a cold link and a large one on a
    fast link both get a ceiling only a stalled transfer meets."""
    from alkera_cli.files.live_sync import (
        INBOUND_DOWNLOAD_FLOOR,
        INBOUND_MIN_BYTES_PER_SECOND,
        inbound_deadline,
    )

    assert inbound_deadline(None) == INBOUND_DOWNLOAD_FLOOR
    assert inbound_deadline(0) == INBOUND_DOWNLOAD_FLOOR
    assert inbound_deadline(1_000) == INBOUND_DOWNLOAD_FLOOR
    assert inbound_deadline(2**30) == 2**30 / INBOUND_MIN_BYTES_PER_SECOND
    assert inbound_deadline(2**30) > INBOUND_DOWNLOAD_FLOOR

    clock = FakeClock()
    seen: list[float] = []

    @dataclass
    class SizedApi(FakeInboundApi):
        def item(self, node_id: str) -> dict[str, Any]:
            return {"id": node_id, "pathBytes": self.paths.get(node_id, ""), "size": 2**30}

        def download(self, node_id: str, into: Path, **kwargs: Any) -> None:
            seen.append(float(kwargs["deadline"]))
            super().download(node_id, into)

    api = SizedApi(root=tree)
    api.queued = [InboundEntry(node_id="n-big", state="inbound", seq=1)]
    api.paths = {"n-big": "Home/work/big.bin"}
    api.contents = {"n-big": b"x"}
    sync = _sync(tree, api, clock, root_path="Home/work")

    sync.pull_inbound()

    assert seen == [inbound_deadline(2**30)]


def test_a_stream_still_arriving_past_the_ceiling_is_cut_and_left_owed(tree: Path) -> None:
    """The wire half of the ceiling: chunks are checked against it as they
    arrive, so a transfer that has stalled ends rather than holding the drain
    forever — and nothing half-written is left in the folder."""
    ticks = iter([0.0, 0.0, 1_000.0, 1_000.0, 1_000.0])

    def body() -> Any:
        yield b"first"
        yield b"second"
        yield b"third"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    api = _rest(tree, handler)
    api.monotonic = lambda: next(ticks)

    with pytest.raises(TimeoutError):
        api.download("n-1", tree / "landing.bin", deadline=600.0)


def test_a_closed_fence_is_said_once_and_its_reopening_sends_what_was_withheld(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The fence closing is on the record, once, with what it is holding; the
    beat that opens it again is on the record too, and the withheld changes
    go on the very next flush. Silence was how a box went deaf for hours."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    assert sync.fenced is False

    _write(tree, "late.txt", b"bytes")
    sync.classify(Change.added, str(tree / "late.txt"))
    clock.advance(31.0)
    with caplog.at_level(logging.INFO, logger="alkera_cli.files.live_sync"):
        sync.flush()
        sync.flush()
        assert sync.pull_inbound() == []
        assert sync.fenced is True
        assert api.uploads == []
        paused = [r for r in caplog.records if "paused" in r.getMessage()]
        assert len(paused) == 1 and paused[0].levelno == logging.WARNING
        assert "1 change(s)" in paused[0].getMessage()

        sync.fence.beat()
        assert sync.fenced is False
        sync.flush()

    assert api.uploads == ["late.txt"]
    resumed = [r for r in caplog.records if "resumed" in r.getMessage()]
    assert len(resumed) == 1


class _RefusingOnce(FakeLiveApi):
    """A drive that refuses the first upload with something that is not the
    fence, then behaves."""

    refused: int = 0

    def upload(self, rel_path: str, node_id: str, size: int, **kwargs: Any) -> str | None:
        if self.refused == 0:
            self.refused += 1
            raise RefusedError("files.store_unavailable")
        return super().upload(rel_path, node_id, size, **kwargs)


def test_the_loop_survives_a_round_that_does_not_land_and_sends_it_later(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One refusal that is not the fence used to end the plane for the life of
    the lease, with the queue emptied on the way out. Now the round is kept,
    said once, and offered again — and the loop is still running to offer it."""
    clock = FakeClock()
    api = _RefusingOnce(root=tree)
    _write(tree, "first.txt", b"one")
    _write(tree, "second.txt", b"two")

    class TickingWatcher(FakeWatcher):
        async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
            batches = self.batches

            async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
                for batch in batches:
                    clock.advance(2.0)
                    yield batch

            return stream()

    watcher = TickingWatcher(
        [
            {(Change.added, str(tree / "first.txt"))},
            {(Change.added, str(tree / "second.txt"))},
        ]
    )
    sync = _sync(
        tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500), watcher=watcher
    )

    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        asyncio.run(sync.run())

    assert sorted(api.uploads) == ["first.txt", "second.txt"]
    assert api.stored["first.txt"] == b"one"
    assert sync.pending == {}
    assert sync.failing is False
    said = [r for r in caplog.records if "did not land" in r.getMessage()]
    assert len(said) == 1


class _Unreachable(FakeLiveApi):
    """A drive nothing answers for (the API restarting) until told otherwise."""

    down: bool = True

    def upload(self, rel_path: str, node_id: str, size: int, **kwargs: Any) -> str | None:
        if self.down:
            raise httpx.ConnectError("connection refused")
        return super().upload(rel_path, node_id, size, **kwargs)


def test_a_round_nothing_answered_is_tried_again_soon_and_later_on_a_widening_wait(
    tree: Path,
) -> None:
    """An API restarting answers nothing for a second or two. Waiting 1 s,
    then 2 s, then 4 s after each failed round held the agent's edit (with
    people watching for it) up to three seconds past the API's return; a
    connect costs the server nothing, so the first seconds of such a streak
    are retried every half second. A drive that stays unreachable is asked
    on the widening wait like any other refusal, and a refusal the server
    did answer never shortens it."""
    clock = FakeClock()
    api = _Unreachable(root=tree)
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500))
    _write(tree, "one.txt", b"first")
    sync.classify(Change.added, str(tree / "one.txt"))

    clock.advance(1.0)
    for _ in range(8):
        sync._flush_contained()
        assert sync.failing is True
        assert sync._flush_due() is False
        clock.advance(0.5)
        assert sync._flush_due() is True
    # Past the first seconds of the streak the wait widens again.
    for _ in range(30):
        sync._flush_contained()
        clock.advance(0.5)
    sync._flush_contained()
    clock.advance(0.9)
    assert sync._flush_due() is False

    api.down = False
    sync._flush_contained()
    assert api.uploads == ["one.txt"]
    assert sync.failing is False

    # A refusal the server answered keeps its own wait.
    refusing = FakeLiveApi(root=tree, refuse="files.store_unavailable", refuse_on="upload")
    other = _sync(tree, refusing, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500))
    _write(tree, "two.txt", b"second")
    other.classify(Change.added, str(tree / "two.txt"))
    clock.advance(1.0)
    other._flush_contained()
    clock.advance(0.5)
    assert other._flush_due() is False


def test_a_round_that_does_not_land_waits_before_it_is_offered_again(tree: Path) -> None:
    """A drive refusing every call is not asked twice a second per chat: the
    wait widens per failure and is reset by the round that lands."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, refuse="files.store_unavailable", refuse_on="upload")
    sync = _sync(tree, api, clock, cadence=LiveCadence(settle_ms=0, batch_every_ms=500))
    _write(tree, "one.txt", b"first")
    sync.classify(Change.added, str(tree / "one.txt"))

    clock.advance(1.0)
    sync._flush_contained()
    assert sync.failing is True and set(sync.pending) == {"one.txt"}
    assert sync._flush_due() is False
    clock.advance(0.9)
    assert sync._flush_due() is False
    clock.advance(0.2)
    assert sync._flush_due() is True

    api.refuse = None
    sync._flush_contained()
    assert api.uploads == ["one.txt"]
    assert sync.failing is False


def test_a_keepalive_goes_out_only_once_the_wire_has_been_quiet(tree: Path) -> None:
    """An empty batch says the plane is alive. Sent when nothing else has for
    the keepalive window, never sooner, and never through a closed fence."""
    from alkera_cli.files.live_sync import KEEPALIVE_EVERY

    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock)
    _write(tree, "one.txt", b"first")
    sync.classify(Change.added, str(tree / "one.txt"))
    sync.flush()
    rounds = len(api.batches)

    # The beats keep landing throughout: the keepalive rides them, and it is
    # the wire's quiet that is being measured here, not the fence's.
    assert sync.keepalive() is False
    clock.advance(KEEPALIVE_EVERY - 1.0)
    sync.fence.beat()
    assert sync.keepalive() is False
    assert len(api.batches) == rounds

    clock.advance(1.0)
    sync.fence.beat()
    assert sync.keepalive() is True
    assert api.batches[-1] == []
    assert sync.keepalive() is False, "the keepalive itself counts as the wire speaking"

    # Through a closed fence nothing goes, the keepalive included: a holder
    # that cannot prove the folder is its own has no business saying it is fine.
    clock.advance(KEEPALIVE_EVERY)
    sync.fence = SelfFence(grace=0.5, monotonic=clock.monotonic)
    clock.advance(1.0)
    sent = len(api.batches)
    assert sync.keepalive() is False
    assert len(api.batches) == sent


def test_a_lease_with_too_many_nodes_in_flight_defers_the_states_and_keeps_streaming(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The drive's ceiling on rows is a "not now": the bytes still go, and
    each one that lands clears a row on the drive. Ending the plane on it was
    the one outcome that could never drain."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, refuse="files.live_too_many", refuse_on="live_batch")
    sync = _sync(tree, api, clock)
    _write(tree, "one.txt", b"first")
    _write(tree, "two.txt", b"second")
    sync.classify(Change.added, str(tree / "one.txt"))
    sync.classify(Change.added, str(tree / "two.txt"))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        sync.flush()

    assert sorted(api.uploads) == ["one.txt", "two.txt"]
    assert api.batches == []
    assert sync.pending == {}
    assert sync.failing is False
    assert [r for r in caplog.records if "too many" in r.getMessage()]

    api.refuse = None
    _write(tree, "one.txt", b"first, again")
    sync.classify(Change.modified, str(tree / "one.txt"))
    sync.flush()
    assert api.states() == {api.nodes["one.txt"]: "uploading"}


def test_an_applied_inbound_version_is_not_pushed_straight_back(tree: Path) -> None:
    """Applying the drive's bytes is not a change the box made. Pushing them
    back would make every web edit cost a second version with the same
    content, and the two planes would chase each other forever."""
    clock = FakeClock()
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-report", state="inbound", seq=1)]
    api.paths = {"n-report": "Home/work/report.md"}
    api.contents = {"n-report": b"what the web edited"}
    sync = _sync(tree, api, clock, root_path="Home/work")

    sync.pull_inbound()
    sync.classify(Change.modified, str(tree / "report.md"))
    sync.flush()

    assert api.uploads == []
    assert api.trees == []


# -- the settle half over the real wire -----------------------------------------


class _PathedFiles(_FakeFiles):
    """A Files slice whose items carry the drive path the containment reads."""

    def __init__(self, paths: dict[str, str]) -> None:
        super().__init__()
        self.paths = paths

    def item(self, drive_id: str, item_id: str, *, select: str | None = None) -> dict[str, Any]:
        return {"id": item_id, "etag": f"etag-{item_id}", "pathBytes": self.paths[item_id]}


def test_a_drained_entry_is_settled_applied_once_and_a_failed_download_is_not(
    tree: Path,
) -> None:
    """Through the routes the box really calls: the inbound page is read, the
    bytes that arrive are settled ``applied`` in ONE batch, and the entry whose
    download the drive refused is left owed — reported nothing, so the next
    drain asks for it again — without taking the good one down with it."""
    posted: list[dict[str, Any]] = []
    served: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/items/lease-9/lease/live"):
            assert request.url.params.get("inbound") == "true"
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {"nodeId": "n-good", "state": "inbound", "seq": 1},
                        {"nodeId": "n-bad", "state": "inbound", "seq": 2},
                    ]
                },
            )
        if request.method == "GET" and path.endswith("/items/n-good/content"):
            served.append("n-good")
            return httpx.Response(200, content=b"name,email\n")
        if request.method == "GET" and path.endswith("/items/n-bad/content"):
            served.append("n-bad")
            return httpx.Response(500, json={"code": "files.store_unavailable"})
        if request.method == "POST" and path.endswith("/items/lease-9/lease/live"):
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"liveSeq": 3, "pending": 1})
        return httpx.Response(404, json={})

    api = _rest(
        tree,
        handler,
        files=_PathedFiles({"n-good": "Chats/c/leads.csv", "n-bad": "Chats/c/broken.csv"}),
    )
    clock = FakeClock()
    sync = _sync(tree, api, clock, root_path="Chats/c")  # type: ignore[arg-type]

    reported = sync.pull_inbound()

    assert reported == [LiveEntry(node_id="n-good", state="applied")]
    assert (tree / "leads.csv").read_bytes() == b"name,email\n"
    assert not (tree / "broken.csv").exists()
    assert served == ["n-good", "n-bad"]
    assert posted == [{"entries": [{"nodeId": "n-good", "state": "applied"}]}]

    # A drain inside the refused entry's wait does not ask for it again; one
    # after it does. What it already settled is settled again only because
    # this fake server still offers it — a real drive cleared the row.
    posted.clear()
    served.clear()
    sync.pull_inbound()
    assert served == ["n-good"]
    posted.clear()
    served.clear()
    clock.advance(INBOUND_RETRY_FIRST)
    sync.fence.beat()
    sync.pull_inbound()
    assert served == ["n-good", "n-bad"]
    assert [entry["nodeId"] for batch in posted for entry in batch["entries"]] == ["n-good"]


def test_the_drain_reads_the_route_the_drive_actually_serves(tree: Path) -> None:
    """The holder asks the live plane what it is owed.

    Pinned against the request the drive's route is declared for — the item's
    own ``/lease/live`` with ``inbound=true`` — and against the three states
    that route can put on the wire, because the two halves of this plane ship
    in different processes: a client asking the wrong path or reading the wrong
    key would be a plane that is silently always empty, which looks exactly
    like a chat nobody dropped a file into.
    """
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        return httpx.Response(
            200,
            json={
                "entries": [
                    {"nodeId": "n1", "state": "inbound", "seq": 4},
                    {"nodeId": "n2", "state": "inbound_delete", "seq": 5},
                    {"nodeId": "n3", "state": "inbound_rename", "seq": 6},
                ]
            },
        )

    owed = _rest(tree, handler).inbound()

    assert seen == [
        ("GET", "/api/v1/files/drives/drive-1/items/lease-9/lease/live", "inbound=true")
    ]
    assert owed == [
        InboundEntry(node_id="n1", state="inbound", seq=4),
        InboundEntry(node_id="n2", state="inbound_delete", seq=5),
        InboundEntry(node_id="n3", state="inbound_rename", seq=6),
    ]


@pytest.mark.parametrize(
    "row",
    [
        pytest.param({"nodeId": "n1", "state": "on_box", "seq": 1}, id="a-holder-state"),
        pytest.param({"nodeId": "n1", "state": "teleported", "seq": 1}, id="a-state-from-later"),
        pytest.param({"nodeId": "", "state": "inbound", "seq": 1}, id="an-empty-node-id"),
        pytest.param({"state": "inbound", "seq": 1}, id="no-node-id-at-all"),
        pytest.param({"nodeId": 7, "state": "inbound", "seq": 1}, id="a-node-id-that-is-not-a-str"),
    ],
)
def test_a_row_the_holder_cannot_act_on_is_left_with_the_drive(tree: Path, row: Any) -> None:
    """A drain is the list of things this build knows how to materialise.

    Dropping the rest rather than raising is what keeps a newer drive from
    stopping an older box: the row stays owed, the server keeps offering it,
    and the box applies what it understands in the meantime.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entries": [row]})

    assert _rest(tree, handler).inbound() == []


def test_a_drain_that_answers_something_else_entirely_is_not_a_crash(tree: Path) -> None:
    """The negative twin of the parse: a body with no ``entries`` list at all —
    a proxy's error page, an older drive — reads as nothing owed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"detail": "no such plane"})

    assert _rest(tree, handler).inbound() == []


# ---------------------------------------------------------------------------
# the ceilings the server enforces on a batch
# ---------------------------------------------------------------------------


def _refused(status: int, *, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"the server answered {status}",
        request=httpx.Request("POST", "http://files.test/lease/live"),
        response=httpx.Response(status, headers=headers or {}, json={"detail": "not now"}),
    )


@dataclass
class CappedInboundApi(FakeInboundApi):
    """A drive that refuses a batch naming more nodes than the grant allows.

    The route validates the body before it reads a single entry, so a batch one
    over the ceiling clears nothing — which is what makes the inbound reply,
    the one batch the box used to send whole, a drain that could never settle.
    """

    cap: int = 256
    refusals: list[int] = field(default_factory=list)

    def live_batch(self, entries: Sequence[LiveEntry]) -> LiveBatchAnswer:
        if len(entries) > self.cap:
            self.refusals.append(len(entries))
            raise _refused(422)
        return super().live_batch(entries)


def _queue_inbound(api: CappedInboundApi, count: int) -> list[str]:
    """``count`` files the drive is holding for this box, ready to be drained."""
    nodes: list[str] = []
    for index in range(count):
        node = f"n-{index:04d}"
        nodes.append(node)
        api.queued.append(InboundEntry(node_id=node, state="inbound", seq=index + 1))
        api.paths[node] = f"Home/work/dropped/{node}.txt"
        api.contents[node] = f"dropped {index}".encode()
    return nodes


@pytest.mark.parametrize("cap", [256, 64], ids=["the-default-ceiling", "a-smaller-served-one"])
def test_a_drain_larger_than_the_ceiling_clears_in_chunks_the_server_accepts(
    tree: Path, cap: int
) -> None:
    """Somebody drops 300 files into the chat's folder.

    The reply is the only thing that clears those rows, so a reply the server
    refuses whole is a folder that re-downloads the same 300 files on every
    beat and never settles one. Every batch stays inside the ceiling the grant
    named, and all 300 are reported applied.
    """
    api = CappedInboundApi(root=tree, cap=cap)
    nodes = _queue_inbound(api, 300)
    sync = _sync(
        tree,
        api,
        FakeClock(),
        cadence=LiveCadence.from_grant({"maxBatchEntries": cap}),
        root_path="Home/work",
    )

    reported = sync.pull_inbound()

    assert api.refusals == []
    assert [len(batch) for batch in api.batches] == [
        len(chunk) for chunk in [nodes[start : start + cap] for start in range(0, 300, cap)]
    ]
    assert max(len(batch) for batch in api.batches) <= cap
    assert [entry.node_id for entry in reported] == nodes
    assert api.states() == dict.fromkeys(nodes, "applied")
    assert (tree / "dropped" / "n-0299.txt").read_bytes() == b"dropped 299"


def test_a_throttled_batch_waits_the_time_the_server_asked_for_and_then_lands(
    tree: Path,
) -> None:
    """429 is the ceiling this box shares with every other one behind its
    address — it says nothing about this holder's right to the folder. The
    batch waits the server's own ``Retry-After`` and is offered again; the
    plane is still running afterwards."""
    waited: list[float] = []
    api = CappedInboundApi(root=tree)
    _queue_inbound(api, 1)
    refusals = {"left": 1}
    accepted = api.live_batch

    def _throttle_once(entries: Sequence[LiveEntry]) -> LiveBatchAnswer:
        if refusals["left"]:
            refusals["left"] -= 1
            raise _refused(429, headers={"Retry-After": "2"})
        return accepted(entries)

    api.live_batch = _throttle_once  # type: ignore[method-assign]
    sync = _sync(tree, api, FakeClock(), root_path="Home/work", sleep=waited.append)

    reported = sync.pull_inbound()

    assert waited == [2.0]
    assert [entry.node_id for entry in reported] == ["n-0000"]
    assert api.states() == {"n-0000": "applied"}

    # And the plane goes on: the next round of the push reaches the drive.
    _write(tree, "after.md", b"written after the throttle")
    sync.classify(Change.added, str(tree / "after.md"))
    sync.flush()
    assert "after.md" in api.uploads


def test_a_server_that_keeps_throttling_leaves_the_rows_owed_but_not_the_plane_dead(
    tree: Path,
) -> None:
    """The negative twin: the throttle never lifts. The batch is given up on —
    those rows are still the server's to offer again — and the refusal does
    NOT escape into the caller, because the thread it would end is the chat's
    whole live sync."""
    waited: list[float] = []
    api = CappedInboundApi(root=tree)
    _queue_inbound(api, 2)
    api.live_batch = lambda entries: (_ for _ in ()).throw(_refused(503))  # type: ignore[method-assign]
    sync = _sync(tree, api, FakeClock(), root_path="Home/work", sleep=waited.append)

    assert sync.pull_inbound() == []
    # Bounded, doubling, and nowhere near a wait that would stall the box.
    assert waited == [1.0, 2.0]

    # The bytes landed even though the report did not, and the sync is alive:
    # a later flush still reaches the drive.
    assert (tree / "dropped" / "n-0000.txt").exists()
    _write(tree, "after.md", b"written after the throttle")
    sync.classify(Change.added, str(tree / "after.md"))
    sync.flush()
    assert "after.md" in api.uploads


def test_a_refusal_that_is_not_a_throttle_is_still_the_callers_to_act_on(
    tree: Path,
) -> None:
    """The asymmetry that keeps the backoff honest: a 403 is not "not now".
    Waiting it out would hide a folder this box may no longer write to."""
    waited: list[float] = []
    api = CappedInboundApi(root=tree)
    _queue_inbound(api, 1)
    api.live_batch = lambda entries: (_ for _ in ()).throw(_refused(403))  # type: ignore[method-assign]
    sync = _sync(tree, api, FakeClock(), root_path="Home/work", sleep=waited.append)

    with pytest.raises(httpx.HTTPStatusError):
        sync.pull_inbound()
    assert waited == []


@dataclass
class _HeldInboundApi(FakeInboundApi):
    """A drive whose first transfer stops half-way until the test lets it go.

    What it buys is the real window the bug lived in: a beat, a turn or an
    event can ask for the same folder again while a 500 MB drop is still
    streaming, and the second ask used to start its own fetch of the same node
    into the same file.
    """

    started: Any = field(default_factory=threading.Event)
    release: Any = field(default_factory=threading.Event)
    staging: list[Path] = field(default_factory=list)

    def download(self, node_id: str, into: Path, **_: Any) -> None:
        self._maybe_refuse("download")
        self.downloads.append(node_id)
        self.staging.append(into)
        into.parent.mkdir(parents=True, exist_ok=True)
        payload = self.contents.get(node_id, b"")
        half = len(payload) // 2
        with into.open("wb") as handle:
            handle.write(payload[:half])
            handle.flush()
            if not self.started.is_set():
                self.started.set()
                assert self.release.wait(10.0)
            handle.write(payload[half:])


def _held_drop(tree: Path) -> tuple[LiveSync, _HeldInboundApi, bytes]:
    payload = bytes(index % 251 for index in range(40_000))
    api = _HeldInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-drop", state="inbound", seq=1)]
    api.paths = {"n-drop": "Home/work/drop.bin"}
    api.contents = {"n-drop": payload}
    return _sync(tree, api, FakeClock(), root_path="Home/work"), api, payload


def test_two_overlapping_drains_land_one_whole_file(tree: Path) -> None:
    """A second drain starting mid-transfer used to stream the same node into
    the same staging file: the loser's rename found nothing, the winner
    published an inode still being written, and the rest of that drain was
    abandoned. One file, one fetch, one verdict."""
    sync, api, payload = _held_drop(tree)
    answers: dict[str, list[LiveEntry]] = {}
    failures: dict[str, BaseException] = {}

    def drain(name: str) -> None:
        try:
            answers[name] = sync.pull_inbound()
        except BaseException as failure:
            failures[name] = failure

    first = threading.Thread(target=drain, args=("first",), daemon=True)
    first.start()
    assert api.started.wait(10.0)
    second = threading.Thread(target=drain, args=("second",), daemon=True)
    second.start()
    second.join(10.0)
    api.release.set()
    first.join(10.0)

    # Daemon, and proven finished. A helper thread that outlives its test is
    # joined by the interpreter at exit instead: the run stops with the loop
    # idle, no test named, and nothing in the report to read.
    assert not first.is_alive() and not second.is_alive()
    assert not failures, failures
    assert (tree / "drop.bin").read_bytes() == payload
    assert api.downloads == ["n-drop"]
    assert answers["first"] == [LiveEntry(node_id="n-drop", state="applied")]
    # The re-entry settles nothing of its own: the running pass owns the entry.
    assert answers["second"] == []
    assert api.states() == {"n-drop": "applied"}


def test_every_attempt_stages_under_a_name_of_its_own(tree: Path) -> None:
    """The belt behind the single flight. Even two fetches the lock never saw —
    a second holder, a process that came back — cannot truncate each other's
    bytes, because no two attempts ever name the same staging file."""
    sync, api, payload = _held_drop(tree)
    api.started.set()
    api.release.set()

    assert sync.pull_inbound() == [LiveEntry(node_id="n-drop", state="applied")]
    (tree / "drop.bin").unlink()
    api.queued = [InboundEntry(node_id="n-drop", state="inbound", seq=2)]
    assert sync.pull_inbound() == [LiveEntry(node_id="n-drop", state="applied")]

    assert len(api.staging) == 2
    assert api.staging[0] != api.staging[1]
    # The bytes stream into the daemon's own spool, never into the chat's
    # tree, where the agent could swap a link in under the download.
    assert sync.spool is not None and sync.spool.directory is not None
    for where in api.staging:
        assert where.parent == sync.spool.directory
        assert tree not in where.parents
    assert (tree / "drop.bin").read_bytes() == payload
    assert sorted(p.name for p in tree.iterdir()) == ["drop.bin"]
    assert list(sync.spool.directory.iterdir()) == []


def test_a_drain_goes_on_past_an_entry_it_could_not_take(tree: Path) -> None:
    """One node the drive cannot describe is one file missing, not every other
    file in the same drain missing too."""

    @dataclass
    class _BrokenItemApi(FakeInboundApi):
        def item(self, node_id: str) -> dict[str, Any]:
            if node_id == "n-bad":
                raise RuntimeError("the drive lost the row for this node")
            return super().item(node_id)

    api = _BrokenItemApi(root=tree)
    api.queued = [
        InboundEntry(node_id="n-bad", state="inbound", seq=1),
        InboundEntry(node_id="n-good", state="inbound", seq=2),
    ]
    api.paths = {"n-good": "Home/work/good.txt"}
    api.contents = {"n-good": b"the file that still had to land"}
    sync = _sync(tree, api, FakeClock(), root_path="Home/work")

    assert sync.pull_inbound() == [LiveEntry(node_id="n-good", state="applied")]
    assert (tree / "good.txt").read_bytes() == b"the file that still had to land"
    assert api.states() == {"n-good": "applied"}


def test_a_drain_survives_the_flush_thread_learning_new_nodes(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The drain and the flush run on different threads, and the flush is what
    puts new nodes on the map the drain reads. Walking that map while it grew
    raised `dictionary changed size during iteration` on an entry that had
    nothing wrong with it, and the drive went on holding a file the box was
    told to take."""
    api = FakeInboundApi(root=tree)
    api.queued = [InboundEntry(node_id="n-absent", state="inbound_delete", seq=1)]
    sync = _sync(tree, api, FakeClock(), root_path="Home/work")
    # A holder that has been streaming for a while, so the walk is long enough
    # for a flush to land inside it.
    for index in range(4_000):
        sync._nodes[f"deep/file-{index}.txt"] = f"n-{index}"

    stop = threading.Event()
    drained: list[BaseException] = []
    flushed: list[BaseException] = []
    rounds = 0
    passes = 0

    def flushing() -> None:
        """What a round does to the map, without the round around it.

        ``_resolve`` puts a node id on every path the round names and a landed
        delete takes one off again; a box whose agent is writing does both
        many times a second, on the flush thread, while the drain walks here.
        """
        nonlocal rounds
        try:
            while not stop.is_set():
                rounds += 1
                for index in range(50):
                    sync._nodes[f"churn-{index}.txt"] = f"n-churn-{index}"
                for index in range(50):
                    sync._nodes.pop(f"churn-{index}.txt", None)
        except BaseException as failure:
            flushed.append(failure)

    writer = threading.Thread(target=flushing, daemon=True)
    writer.start()
    try:
        with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
            deadline = time.monotonic() + 10.0
            while passes < 2_000 and time.monotonic() < deadline:
                try:
                    assert sync.pull_inbound() == []
                except BaseException as failure:
                    drained.append(failure)
                    break
                passes += 1
    finally:
        stop.set()
        writer.join(10.0)

    assert not writer.is_alive()
    assert not drained, drained
    assert not flushed, flushed
    # Not one pass gave up on its entry: an entry the drain could not look at
    # is a file the drive goes on holding and the box never gets.
    assert [record.getMessage() for record in caplog.records] == []
    # Both threads really did their work, or the absence of a failure is empty.
    assert passes >= 2_000
    assert rounds >= 2_000


def _ids_of(records: Sequence[logging.LogRecord]) -> list[tuple[str, str, bool, bool]]:
    return [
        (
            getattr(record, "chat_id", ""),
            getattr(record, "lease_node_id", ""),
            "chat-7" in record.getMessage(),
            "chat-node" in record.getMessage(),
        )
        for record in records
    ]


def test_a_refused_live_batch_names_the_chat_and_the_lease(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One box serves many chats and their lines interleave in one log. A
    refused batch that names only a count is a line nobody can act on: it says
    neither which chat is missing its rows nor which lease is over its ceiling."""
    api = FakeLiveApi(root=tree, refuse="files.live_too_many", refuse_on="live_batch")
    sync = _sync(tree, api, FakeClock(), root_path="Home/work")
    _write(tree, "one.txt", b"first")
    sync.classify(Change.added, str(tree / "one.txt"))

    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        sync.flush()

    refused = [record for record in caplog.records if "too many" in record.getMessage()]
    assert refused
    assert _ids_of(refused) == [("chat-7", "chat-node", True, True)] * len(refused)


def test_a_throttled_live_batch_names_the_chat_and_the_lease(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same for the throttle pair — the line that says a batch is being
    offered again, and the one that gives up on it."""
    api = CappedInboundApi(root=tree)
    _queue_inbound(api, 1)
    api.live_batch = lambda entries: (_ for _ in ()).throw(_refused(503))  # type: ignore[method-assign]
    sync = _sync(tree, api, FakeClock(), root_path="Home/work", sleep=lambda _seconds: None)

    with caplog.at_level(logging.INFO, logger="alkera_cli.files.live_sync"):
        assert sync.pull_inbound() == []

    throttled = [record for record in caplog.records if "throttl" in record.getMessage()]
    assert len(throttled) == 3
    assert _ids_of(throttled) == [("chat-7", "chat-node", True, True)] * 3


def test_a_paused_plane_names_the_chat_and_the_lease(
    tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """And the fence, which is the line an operator reaches for first when a
    chat's files stop moving."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, clock, root_path="Home/work")
    _write(tree, "one.txt", b"first")
    sync.classify(Change.added, str(tree / "one.txt"))
    clock.advance(120.0)

    with caplog.at_level(logging.WARNING, logger="alkera_cli.files.live_sync"):
        sync.flush()

    paused = [record for record in caplog.records if "paused" in record.getMessage()]
    assert len(paused) == 1
    assert _ids_of(paused) == [("chat-7", "chat-node", True, True)]
    assert api.batches == []


def test_a_file_the_round_is_sending_is_left_to_the_plane_by_the_checkpoint_push(
    tree: Path,
) -> None:
    """While a round uploads a file, the file is neither queued nor agreed.

    A checkpoint push that met it in that moment fenced it on the base agreed
    before the round, so a person's change landing meanwhile was overwritten
    by the bytes this very round was already filing. The push is told to
    leave such a file to the plane, as it leaves what the plane has queued.
    """
    seen_mid_upload: list[list[str]] = []

    @dataclass
    class WatchedApi(FakeLiveApi):
        def upload(self, rel_path: str, node_id: str, size: int, **kwargs: Any) -> str | None:
            seen_mid_upload.append(sync.sent_live("scratch", running=True))
            return super().upload(rel_path, node_id, size, **kwargs)

    clock = FakeClock()
    sync = _sync(tree, WatchedApi(root=tree), clock, watcher=FakeWatcher([set()]))
    asyncio.run(sync.run())  # the first sweep has run: the plane names files, not the folder
    sync.classify(Change.added, str(_write(tree, "notes.txt", b"the agent's second line\n")))

    sync.flush()

    assert seen_mid_upload == [["scratch/notes.txt"]]
    assert sync.sent_live("scratch", running=True) == []
