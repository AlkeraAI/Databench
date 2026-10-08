"""The holder's journal: a restart between a write and its flush loses nothing.

Driven against the fake drive (``files._live_sync_fakes``), which applies a
batch id once as the route does, so "resent" and "applied twice" are told
apart by what the drive holds. A crash is a ``BaseException`` raised through
the sync — the one kind of failure nothing in it catches.
"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.files.journal import BUSY_BACKOFF, PRUNE_AFTER, LiveJournal, journal_path
from alkera_cli.files.live_sync import LiveCadence, TreeAnswer, TreeEntry
from alkera_cli.files.mount import mount_record_path, mounts_dir
from alkera_cli.files.tree_watch import Change
from files._live_sync_fakes import FakeClock, FakeLiveApi, FakeWatcher
from files._live_sync_fakes import make_sync as _sync
from files._live_sync_fakes import write as _write

#: A foreign-lock wait long enough that a scheduler tick is noise beside it.
_WAIT = 0.5


def lock_witness(locked: list[int]) -> type[sqlite3.Connection]:
    """A connection class that notes, for every statement that found the file
    locked, how long SQLite was allowed to wait for it (``busy_timeout``, ms).

    SQLite's busy handler gives up only once that allowance is spent, so a
    non-zero entry is a statement that waited the whole of it and a zero one
    answered at once — what the watcher paid, counted without a clock a loaded
    runner can stretch."""

    class Witness(sqlite3.Connection):
        def _noting(self, error: sqlite3.OperationalError) -> None:
            if "locked" in str(error):
                allowed = super().execute("PRAGMA busy_timeout").fetchone()[0]
                locked.append(int(allowed))

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as error:
                self._noting(error)
                raise

        def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
            try:
                return super().executemany(sql, parameters)
            except sqlite3.OperationalError as error:
                self._noting(error)
                raise

    return Witness


class Crash(BaseException):
    """The process dying: nothing in the sync may catch it."""


class Wall:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class CrashBeforeTheDrive(FakeLiveApi):
    """A drive the process dies on the way to: the first batch never arrives."""

    crashes: int = 1

    def tree(self, batch_id: str, entries: Sequence[TreeEntry], *, gzip_above: int) -> TreeAnswer:
        if self.crashes:
            self.crashes -= 1
            raise Crash
        return super().tree(batch_id, entries, gzip_above=gzip_above)


class CrashingWatcher:
    """Yields its batches, then the process dies."""

    def __init__(self, batches: list[set[tuple[Change, str]]]) -> None:
        self.batches = batches

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        async def stream() -> AsyncIterator[set[tuple[Change, str]]]:
            for batch in self.batches:
                yield batch
            raise Crash

        return stream()


class WitnessWatcher(FakeWatcher):
    """Notes what the drive and the queue held when the watch was armed."""

    def __init__(self, api: FakeLiveApi, sync_box: list[Any]) -> None:
        super().__init__([])
        self.api = api
        self.sync_box = sync_box
        self.at_arm: dict[str, Any] = {}

    async def changes(self) -> AsyncIterator[set[tuple[Change, str]]]:
        self.at_arm = {
            "batch_ids": list(self.api.batch_ids),
            "pending": set(self.sync_box[0].pending),
        }
        return await super().changes()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    return root


@pytest.fixture
def journal_file(tmp_path: Path) -> Path:
    return tmp_path / "home" / "files" / "mounts" / "mount.journal"


def test_a_crash_after_the_drive_applied_a_batch_resends_the_same_batch_id(
    tree: Path, journal_file: Path
) -> None:
    """The drive answered, the ack never made it to disk: the start resends
    the batch under its own id, and the drive applies it once."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree, crash_after_tree=Crash())
    first = _sync(tree, api, clock, journal=LiveJournal(journal_file))
    _write(tree, "report.md", b"q3")
    first.classify(Change.added, str(tree / "report.md"))
    with pytest.raises(Crash):
        first.flush()
    sent = api.batch_ids[0]

    journal = LiveJournal(journal_file)
    assert [batch.batch_id for batch in journal.unacked()] == [sent]
    second = _sync(tree, api, clock, journal=journal)
    second.recover()

    assert api.batch_ids == [sent, sent]
    assert len(api.trees) == 1, "a resent batch id must not be applied twice"
    assert journal.unacked() == []
    assert [batch.live_seq_acked for batch in journal.batches()] == [1]


def test_a_crash_before_the_drive_saw_a_batch_lands_the_row_on_restart(
    tree: Path, journal_file: Path
) -> None:
    """Written down, never sent: the row the drive never heard of appears
    once the sync starts again, before the watch is armed."""
    clock = FakeClock()
    api = CrashBeforeTheDrive(root=tree)
    first = _sync(tree, api, clock, journal=LiveJournal(journal_file))
    _write(tree, "notes/plan.md", b"plan")
    first.classify(Change.added, str(tree / "notes" / "plan.md"))
    with pytest.raises(Crash):
        first.flush()
    assert "notes/plan.md" not in api.nodes

    box: list[Any] = []
    watcher = WitnessWatcher(api, box)
    second = _sync(tree, api, clock, journal=LiveJournal(journal_file), watcher=watcher)
    box.append(second)
    asyncio.run(second.run())

    assert "notes/plan.md" in api.nodes
    # Resent before the watch was armed, and under the id it was written with.
    assert len(watcher.at_arm["batch_ids"]) == 1


def test_a_queued_upload_survives_a_restart(tree: Path, journal_file: Path) -> None:
    """A file the content queue owed when the process died is owed by the next
    one — back on the queue before the watch is armed, and landed."""
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    # Neither queue is due: the batch window is a minute long.
    quiet = LiveCadence(settle_ms=0, batch_every_ms=60_000, metadata_every_ms=60_000)
    _write(tree, "data.csv", b"a,b\n1,2\n")
    first = _sync(
        tree,
        api,
        clock,
        cadence=quiet,
        journal=LiveJournal(journal_file),
        watcher=CrashingWatcher([{(Change.added, str(tree / "data.csv"))}]),  # type: ignore[arg-type]
    )
    with pytest.raises(Crash):
        asyncio.run(first.run())
    assert api.uploads == []

    box: list[Any] = []
    watcher = WitnessWatcher(api, box)
    journal = LiveJournal(journal_file)
    second = _sync(tree, api, clock, journal=journal, watcher=watcher)
    box.append(second)
    asyncio.run(second.run())

    assert watcher.at_arm["pending"] == {"data.csv"}
    assert api.stored["data.csv"] == b"a,b\n1,2\n"
    assert journal.queued_intents() == []


def test_an_owed_file_that_is_gone_by_the_restart_is_let_go(tree: Path, journal_file: Path) -> None:
    journal = LiveJournal(journal_file)
    journal.queue_intents([("vanished.txt", 3, 1, None)])
    api = FakeLiveApi(root=tree)
    sync = _sync(tree, api, FakeClock(), journal=journal)

    sync.recover()

    assert sync.pending == {}
    assert journal.queued_intents() == []


def test_a_journaled_batch_refused_on_resend_goes_back_on_the_queue(
    tree: Path, journal_file: Path
) -> None:
    """A resend the drive will not take is not resent forever: its rows go
    back on the metadata queue, to leave under a new id."""
    journal = LiveJournal(journal_file)
    journal.record_batch("b-1", [TreeEntry(op="upsert", path="x", kind="dir").to_journal()])
    api = FakeLiveApi(root=tree, refuse="files.unavailable", refuse_on="tree")
    sync = _sync(tree, api, FakeClock(), journal=journal)

    sync.recover()

    assert journal.batches() == []
    assert sync.metadata == {"x": TreeEntry(op="upsert", path="x", kind="dir")}


def test_a_refused_batch_is_never_resent_under_its_old_id(tree: Path, journal_file: Path) -> None:
    """The route refused it, so its rows are requeued in memory and leave under
    a new id; the journal keeps no promise for the refused one."""
    journal = LiveJournal(journal_file)
    api = FakeLiveApi(root=tree, refuse="files.unavailable", refuse_on="tree")
    sync = _sync(tree, api, FakeClock(), journal=journal)
    _write(tree, "a.txt", b"a")
    sync.classify(Change.added, str(tree / "a.txt"))

    sync._flush_metadata(hold=False)

    assert journal.batches() == []
    assert "a.txt" in sync.metadata


def test_settled_rows_older_than_a_day_are_pruned_and_promises_never_are(
    journal_file: Path,
) -> None:
    wall = Wall()
    journal = LiveJournal(journal_file, wall=wall)
    journal.record_batch("acked", [])
    journal.ack_batch("acked", 3)
    journal.record_batch("unacked", [])
    journal.queue_intents([("done.txt", 1, 1, None), ("owed.txt", 1, 1, None)])
    journal.done_intent("done.txt")

    wall.now += PRUNE_AFTER - 1
    assert journal.prune() == 0
    wall.now += 2
    assert journal.prune() == 2

    assert [batch.batch_id for batch in journal.batches()] == ["unacked"]
    assert [intent.relative for intent in journal.intents()] == ["owed.txt"]


def test_the_start_prunes(tree: Path, journal_file: Path) -> None:
    wall = Wall()
    journal = LiveJournal(journal_file, wall=wall)
    journal.record_batch("old", [])
    journal.ack_batch("old", 1)
    wall.now += PRUNE_AFTER + 1

    _sync(tree, FakeLiveApi(root=tree), FakeClock(), journal=journal).recover()

    assert journal.batches() == []


def test_the_journal_lives_beside_the_mount_record_0600_in_wal_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    where = journal_path(tmp_path / "folder", home=home)
    journal = LiveJournal(where)

    assert where.parent == mounts_dir(home=home)
    assert where.stem == mount_record_path(tmp_path / "folder", home=home).stem
    if sys.platform != "win32":  # NTFS keeps no owner-only mode bits to read back
        assert stat.S_IMODE(where.stat().st_mode) == 0o600
    probe = sqlite3.connect(where)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        probe.close()
    journal.remove()
    assert not where.exists()


def test_a_journal_another_process_holds_locked_never_stops_the_watcher(
    tree: Path, journal_file: Path
) -> None:
    """Two syncs on one mount share one journal file. While the other one holds
    its write lock, this one's promises cannot be written down — but the
    watcher still runs, the file still lands, and the watcher pays for the
    lock once, not once per journal write: the journal is the net under the
    in-memory queues, never a gate on them.

    Landing one file makes five journal writes (the prune, the intent, the
    batch, its ack, the intent done), and every one of them finds the file
    locked. What the watcher paid is read off the statements themselves — how
    long each locked one was allowed to wait — not off the wall clock around
    the whole run, which on a loaded Windows runner came to 1.9 s with a
    single wait in it.
    """
    clock = FakeClock()
    api = FakeLiveApi(root=tree)
    locked: list[int] = []
    journal = LiveJournal(journal_file, busy_wait=_WAIT, connection=lock_witness(locked))
    other = sqlite3.connect(journal_file, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        _write(tree, "data.csv", b"a,b\n1,2\n")
        sync = _sync(
            tree,
            api,
            clock,
            journal=journal,
            watcher=FakeWatcher([{(Change.added, str(tree / "data.csv"))}]),
        )
        asyncio.run(sync.run())
    finally:
        other.execute("ROLLBACK")
        other.close()

    assert api.stored["data.csv"] == b"a,b\n1,2\n"
    assert "data.csv" in api.nodes
    assert len(locked) > 1, f"the lock stood in front of {len(locked)} journal write(s)"
    waits = [allowed for allowed in locked if allowed]
    assert waits == [round(_WAIT * 1000)], (
        f"{len(waits)} of the {len(locked)} journal writes the lock refused waited for it "
        f"(allowed ms per locked write: {locked}); only the first may"
    )


def test_a_locked_journal_waits_once_then_gives_up_at_once_until_the_backoff_passes(
    journal_file: Path,
) -> None:
    """The first write that finds the file locked waits for it; the ones after
    it, inside :data:`BUSY_BACKOFF`, answer ``False`` straight away, while
    reads still answer and a write the lock no longer stands in front of
    still lands. Past the backoff a write is patient again."""
    ticks = Wall()
    locked: list[int] = []
    journal = LiveJournal(
        journal_file, monotonic=ticks, busy_wait=_WAIT, connection=lock_witness(locked)
    )
    assert journal.record_batch("before", [])
    other = sqlite3.connect(journal_file, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        assert journal.record_batch("first", []) is False
        assert time.monotonic() - started >= _WAIT / 2, "the first write must wait for the lock"

        for n in range(4):
            assert journal.queue_intents([(f"f{n}.txt", 1, 1, None)]) is False
        assert locked[1:] == [0, 0, 0, 0], f"writes inside the backoff waited again: {locked}"
        # A writer elsewhere never blocks a read in WAL mode.
        assert [batch.batch_id for batch in journal.batches()] == ["before"]
    finally:
        other.execute("ROLLBACK")

    assert journal.record_batch("freed", []), "the backoff refused a write nothing blocked"

    other.execute("BEGIN IMMEDIATE")
    try:
        ticks.now += BUSY_BACKOFF
        started = time.monotonic()
        assert journal.record_batch("later", []) is False
        assert time.monotonic() - started >= _WAIT / 2, "past the backoff a write waits again"
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert [batch.batch_id for batch in journal.batches()] == ["before", "freed"]
