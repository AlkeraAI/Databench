"""The agent's SQLite store on disk: where it lives, folding its write-ahead
log into the main file so a copy of that file alone carries the session, and
setting an unreadable store aside so the next start opens a fresh one.

The agent (opencode) owns the database and writes it through its own
connection; nothing here reads its rows.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


#: Where the agent keeps its database, relative to the directory it runs in.
AGENT_DB_RELATIVE = Path(".runtime/agent/agent.db")


def fold_write_ahead_log(db: Path) -> bool:
    """Checkpoint ``db``'s write-ahead log into the database file itself.

    The log and the shared-memory index beside a WAL-mode database are one
    process's view of it and never travel with a chat's folder; what the log
    still holds — everything the agent committed since its last checkpoint,
    which is everything when the agent was killed rather than stopped — is
    folded into the main file here, so a copy of that file alone carries the
    session. ``True`` when a log with frames was folded; ``False`` when there
    was nothing to fold or another connection still held the database.
    """
    wal = db.with_name(db.name + "-wal")
    if not db.is_file() or not wal.is_file() or wal.stat().st_size == 0:
        return False
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        busy, _log_frames, _checkpointed = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        conn.close()
    return int(busy) == 0


def fold_agent_databases(root: Path) -> list[Path]:
    """Fold the write-ahead log of every agent database under a chat folder.

    Looked for where the agent keeps it from either root a chat is read
    against — the folder itself and its working directory. A database that
    cannot be folded (still held, or unreadable) is logged and left as it is:
    the main file is a consistent copy as of its last checkpoint either way.
    """
    folded: list[Path] = []
    for candidate in (root / AGENT_DB_RELATIVE, root / "scratch" / AGENT_DB_RELATIVE):
        try:
            if fold_write_ahead_log(candidate):
                folded.append(candidate)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("the agent database at %s kept its write-ahead log: %s", candidate, exc)
    return folded


#: The files SQLite keeps beside a database that go with it when it is set aside.
_AGENT_DB_SIDE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def set_aside_agent_store(db: Path, *, stamp: str) -> Path | None:
    """Move the agent's database — and the log and index files beside it —
    out of the agent's way as ``<name>.unreadable-<stamp>``, so the next
    start opens a fresh store. The unreadable store is kept, never deleted:
    it is evidence for the operator and a session an upgraded agent may read.
    ``None`` when there was no database to set aside."""
    if not db.exists():
        return None
    aside = db.with_name(f"{db.name}.unreadable-{stamp}")
    for suffix in _AGENT_DB_SIDE_SUFFIXES:
        source = db.with_name(db.name + suffix)
        if source.exists():
            source.replace(aside.with_name(aside.name + suffix))
    return aside


__all__ = [
    "AGENT_DB_RELATIVE",
    "fold_agent_databases",
    "fold_write_ahead_log",
    "set_aside_agent_store",
]
