"""The holder's journal: what a live sync has promised the drive, on disk.

The live push keeps its queues in memory, which a box restart between a write
and its flush would lose. The journal is the part that must survive: every
metadata batch is written here *before* it is sent and marked acked with the
``live_seq`` the drive answered, and every file the content queue owes the
drive is noted as an intent until its bytes land. A sync that starts on a
folder with a journal behind it resends the batches nobody acked (under their
own ``batch_id``, which the route applies once) and puts the owed files back on
its content queue before it listens to the disk again.

One SQLite file per mount, beside the mount record, written only by the
holder; the drive knows nothing of it. Stdlib ``sqlite3`` in WAL mode, created
0600 because it names every path the folder holds.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar

from alkera_cli.files.mount import mount_record_path

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

__all__ = [
    "PRUNE_AFTER",
    "JournalBatch",
    "JournalIntent",
    "LiveJournal",
    "journal_path",
]

#: How old a settled row is before a start prunes it. A day: long enough that a
#: person asking what a box sent this morning can still read it off the disk.
PRUNE_AFTER: Final = 86_400.0

#: How long a statement waits on a lock another process holds on the journal
#: (a second sync on the same mount) before giving up. The journal is the net
#: under the in-memory queues, never a gate on them: a promise it cannot write
#: now is written on the next attempt.
BUSY_WAIT: Final = 0.05

#: After a statement found the journal locked, how long every statement gives
#: up at once instead of waiting :data:`BUSY_WAIT` again. One flush makes
#: several journal writes; without this each of them paid the full wait, and
#: on Windows, where SQLite's busy handler sleeps in whole scheduler ticks,
#: five 50 ms waits came to more than a second of the watcher standing still.
BUSY_BACKOFF: Final = 5.0

_SCHEMA: Final = (
    "CREATE TABLE IF NOT EXISTS batches ("
    " batch_id TEXT PRIMARY KEY,"
    " live_seq_acked INTEGER,"
    " entries_json TEXT NOT NULL,"
    " created_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS intents ("
    " relative TEXT PRIMARY KEY,"
    " size INTEGER NOT NULL,"
    " mtime_ns INTEGER NOT NULL,"
    " node_id TEXT,"
    " state TEXT NOT NULL CHECK (state IN ('queued', 'done')),"
    " created_at REAL NOT NULL)",
)


def journal_path(root: Path, *, home: Path | None = None) -> Path:
    """Where the journal of the mount at ``root`` lives: beside its record."""
    return mount_record_path(root, home=home).with_suffix(".journal")


@dataclass(frozen=True, slots=True)
class JournalBatch:
    """A metadata batch written before it was sent."""

    batch_id: str
    entries: list[dict[str, Any]]
    live_seq_acked: int | None
    created_at: float


@dataclass(frozen=True, slots=True)
class JournalIntent:
    """A file the content queue owes the drive."""

    relative: str
    size: int
    mtime_ns: int
    node_id: str | None
    state: str
    created_at: float


class LiveJournal:
    """One mount's journal. Safe to share between the sync's thread and the
    one that stops it: every statement runs under one lock.

    While another process holds the file, at most one statement per
    :data:`BUSY_BACKOFF` window waits ``busy_wait`` for it; the rest answer
    their fallback at once. Reads are not refused in that window: in WAL mode
    a writer elsewhere never blocks them.

    ``connection`` is the :class:`sqlite3.Connection` class the file is opened
    with, so a test can watch which statements found the file locked and how
    long each one was allowed to wait, without timing a wait on the clock.
    """

    def __init__(
        self,
        path: Path,
        *,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        busy_wait: float = BUSY_WAIT,
        connection: type[sqlite3.Connection] = sqlite3.Connection,
    ) -> None:
        self.path = path
        self.wall = wall
        self.monotonic = monotonic
        self.busy_wait = busy_wait
        # Statements wait for a foreign lock only from this monotonic instant.
        self._patient_from = float("-inf")
        self._waiting_ms: int | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        # The mode is set when the file is made, never after: the rows name
        # every path in the folder, and a window where they are world-readable
        # is a window.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_wait,
            factory=connection,
        )
        self._ready = False
        with self._lock:
            self._prepare()

    def _set_patience(self) -> None:
        """Wait ``busy_wait`` on a foreign lock, or not at all inside the
        backoff that follows a statement that found the file locked."""
        waiting_ms = round(self.busy_wait * 1000) if self.monotonic() >= self._patient_from else 0
        if waiting_ms != self._waiting_ms:
            self._db.execute(f"PRAGMA busy_timeout = {waiting_ms}")
            self._waiting_ms = waiting_ms

    def _found_busy(self) -> None:
        self._patient_from = self.monotonic() + BUSY_BACKOFF

    def _prepare(self) -> bool:
        """Set the file up once; ``False`` while another process holds it."""
        if self._ready:
            return True
        try:
            self._set_patience()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            for statement in _SCHEMA:
                self._db.execute(statement)
        except sqlite3.OperationalError as busy:
            self._found_busy()
            logger.info("journal %s is busy (%s); set up on the next use", self.path, busy)
            return False
        self._ready = True
        return True

    def _run(self, work: Callable[[], _T], fallback: _T) -> _T:
        """Run ``work`` under the lock, or answer ``fallback`` when the file
        is locked by another process or not set up yet."""
        with self._lock:
            if not self._prepare():
                return fallback
            try:
                self._set_patience()
                return work()
            except sqlite3.OperationalError as busy:
                self._found_busy()
                if self._db.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        self._db.execute("ROLLBACK")
                logger.info("journal %s is busy (%s); left for the next attempt", self.path, busy)
                return fallback

    # -- batches --------------------------------------------------------

    def record_batch(self, batch_id: str, entries: Sequence[dict[str, Any]]) -> bool:
        """Write a batch down before it leaves. Idempotent by ``batch_id``.
        ``False`` when the journal was busy and the batch leaves unwritten."""
        payload = json.dumps(list(entries), separators=(",", ":"))

        def work() -> bool:
            self._db.execute(
                "INSERT OR IGNORE INTO batches (batch_id, live_seq_acked, entries_json, "
                "created_at) VALUES (?, NULL, ?, ?)",
                (batch_id, payload, self.wall()),
            )
            return True

        return self._run(work, False)

    def ack_batch(self, batch_id: str, live_seq: int) -> bool:
        """The drive applied the batch: note the ``live_seq`` it answered.
        Unnoted, the batch is resent under its own id, which applies nothing."""

        def work() -> bool:
            self._db.execute(
                "UPDATE batches SET live_seq_acked = ? WHERE batch_id = ?", (live_seq, batch_id)
            )
            return True

        return self._run(work, False)

    def forget_batch(self, batch_id: str) -> bool:
        """A batch the drive refused: its rows are owed again under a new id,
        or were left off the drive on purpose — either way, never resent."""

        def work() -> bool:
            self._db.execute(
                "DELETE FROM batches WHERE batch_id = ? AND live_seq_acked IS NULL", (batch_id,)
            )
            return True

        return self._run(work, False)

    def unacked(self) -> list[JournalBatch]:
        """The batches written and never acked, oldest first."""
        return [batch for batch in self.batches() if batch.live_seq_acked is None]

    def batches(self) -> list[JournalBatch]:
        rows: list[Any] = self._run(
            lambda: self._db.execute(
                "SELECT batch_id, entries_json, live_seq_acked, created_at FROM batches "
                "ORDER BY created_at, rowid"
            ).fetchall(),
            [],
        )
        return [
            JournalBatch(
                batch_id=row[0],
                entries=json.loads(row[1]),
                live_seq_acked=row[2],
                created_at=row[3],
            )
            for row in rows
        ]

    # -- content intents ------------------------------------------------

    def queue_intents(self, intents: Sequence[tuple[str, int, int, str | None]]) -> bool:
        """Note ``(relative, size, mtime_ns, node_id)`` as owed, in one transaction.
        ``False`` when the journal was busy and nothing was noted."""
        if not intents:
            return True
        now = self.wall()

        def work() -> bool:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT INTO intents (relative, size, mtime_ns, node_id, state, created_at) "
                    "VALUES (?, ?, ?, ?, 'queued', ?) ON CONFLICT (relative) DO UPDATE SET "
                    "size = excluded.size, mtime_ns = excluded.mtime_ns, "
                    "node_id = COALESCE(excluded.node_id, intents.node_id), "
                    "state = 'queued', created_at = excluded.created_at",
                    [(relative, size, mtime, node, now) for relative, size, mtime, node in intents],
                )
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            return True

        return self._run(work, False)

    def done_intent(self, relative: str) -> bool:
        """The file's bytes landed, or nothing is owed for it any more.
        Unnoted, the next start re-offers the file, which lands nothing new."""

        def work() -> bool:
            self._db.execute(
                "UPDATE intents SET state = 'done', created_at = ? WHERE relative = ? "
                "AND state = 'queued'",
                (self.wall(), relative),
            )
            return True

        return self._run(work, False)

    def queued_intents(self) -> list[JournalIntent]:
        """The files still owed, in the order they were noted."""
        return [intent for intent in self.intents() if intent.state == "queued"]

    def intents(self) -> list[JournalIntent]:
        rows: list[Any] = self._run(
            lambda: self._db.execute(
                "SELECT relative, size, mtime_ns, node_id, state, created_at FROM intents "
                "ORDER BY created_at, rowid"
            ).fetchall(),
            [],
        )
        return [JournalIntent(*row) for row in rows]

    # -- housekeeping ---------------------------------------------------

    def prune(self) -> int:
        """Drop acked batches and done intents older than :data:`PRUNE_AFTER`.

        Only what is settled is ever pruned: an unacked batch or a queued
        intent is a promise, and it stays until it is kept.
        """
        cutoff = self.wall() - PRUNE_AFTER

        def work() -> int:
            gone = self._db.execute(
                "DELETE FROM batches WHERE live_seq_acked IS NOT NULL AND created_at < ?",
                (cutoff,),
            ).rowcount
            gone += self._db.execute(
                "DELETE FROM intents WHERE state = 'done' AND created_at < ?", (cutoff,)
            ).rowcount
            return int(gone)

        return self._run(work, 0)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def remove(self) -> None:
        """Close and delete the journal: the lease it served was handed back."""
        with contextlib.suppress(sqlite3.Error):
            self.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(f"{self.path}{suffix}").unlink()
