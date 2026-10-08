"""The agent database travels as one file.

The agent keeps its session in a WAL-mode SQLite database. The write-ahead log
beside it holds everything committed since the last checkpoint — everything,
when the agent was killed rather than stopped — and it is one process's view
of the file: it never travels with the chat's folder. Before the folder is
handed back the box folds the log into the database, so a copy of the main
file alone, opened on the next box, carries the whole session.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from alkera_cli.harness.opencode_db import (
    AGENT_DB_RELATIVE,
    fold_agent_databases,
    fold_write_ahead_log,
)

ROWS = [("ses_1",), ("ses_2",), ("ses_3",)]


def _agent_database(where: Path) -> tuple[sqlite3.Connection, Path]:
    """A WAL-mode database whose writer never checkpoints — the shape an
    agent that is still running, or was killed, leaves on disk."""
    db = where / AGENT_DB_RELATIVE
    db.parent.mkdir(parents=True)
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    writer.executemany("INSERT INTO session VALUES (?)", ROWS)
    return writer, db


def _sessions_in_a_copy_of_the_main_file_alone(db: Path, into: Path) -> list[str]:
    """What the next box reads: the main file copied by itself."""
    into.mkdir()
    copied = into / db.name
    shutil.copyfile(db, copied)
    reader = sqlite3.connect(copied)
    try:
        return [row[0] for row in reader.execute("SELECT id FROM session ORDER BY id")]
    except sqlite3.OperationalError as exc:
        return [f"unreadable: {exc}"]
    finally:
        reader.close()


def test_the_main_file_alone_carries_the_session_once_the_log_is_folded(tmp_path: Path) -> None:
    writer, db = _agent_database(tmp_path / "chat")
    wal = db.with_name(db.name + "-wal")
    assert wal.stat().st_size > 0, "the log holds nothing; the test proves nothing"
    assert _sessions_in_a_copy_of_the_main_file_alone(db, tmp_path / "before") != [
        "ses_1",
        "ses_2",
        "ses_3",
    ], "the main file already carried the rows; the test proves nothing"

    assert fold_write_ahead_log(db) is True

    assert wal.stat().st_size == 0, "the log was not truncated"
    assert _sessions_in_a_copy_of_the_main_file_alone(db, tmp_path / "after") == [
        "ses_1",
        "ses_2",
        "ses_3",
    ]
    # The writer that was still open goes on working against the folded file.
    writer.execute("INSERT INTO session VALUES ('ses_4')")
    assert writer.execute("SELECT count(*) FROM session").fetchone() == (4,)
    writer.close()


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("no-database", id="no-database"),
        pytest.param("no-log", id="a-database-with-no-log"),
        pytest.param("empty-log", id="a-log-already-folded"),
    ],
)
def test_nothing_to_fold_is_no_fold(tmp_path: Path, shape: str) -> None:
    db = tmp_path / AGENT_DB_RELATIVE
    if shape != "no-database":
        db.parent.mkdir(parents=True)
        plain = sqlite3.connect(db)
        plain.execute("CREATE TABLE session (id TEXT)")
        plain.commit()
        plain.close()
    if shape == "empty-log":
        db.with_name(db.name + "-wal").write_bytes(b"")
    assert fold_write_ahead_log(db) is False


def test_the_fold_looks_where_the_agent_keeps_its_database_from_either_root(
    tmp_path: Path,
) -> None:
    """A chat is read against two roots — the folder and its working
    directory — and the agent's database sits under whichever the harness
    ran in. Both are looked at; a folder with neither folds nothing."""
    assert fold_agent_databases(tmp_path / "empty") == []

    writer, db = _agent_database(tmp_path / "chat" / "scratch")
    try:
        assert fold_agent_databases(tmp_path / "chat") == [db]
        assert fold_agent_databases(tmp_path / "chat") == [], "folded twice"
    finally:
        writer.close()


def test_a_database_that_cannot_be_read_is_left_and_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / AGENT_DB_RELATIVE
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database at all, but long enough to be read as one" * 4)
    db.with_name(db.name + "-wal").write_bytes(b"frames")
    with caplog.at_level("WARNING"):
        assert fold_agent_databases(tmp_path) == []
    assert any("kept its write-ahead log" in r.getMessage() for r in caplog.records)
