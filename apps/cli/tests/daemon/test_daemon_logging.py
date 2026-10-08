"""The daemon's stdlib-logging scrub filter redacts secrets/paths from records
(the harness logs via stdlib, which feed the rotating daemon.log file)."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alkera_cli.daemon.logging_setup import (
    LOG_DIR_MODE,
    LOG_FILE_MODE,
    PrivateRotatingFileHandler,
    _ScrubbingFilter,
)


def test_filter_redacts_secrets_and_home_paths() -> None:
    f = _ScrubbingFilter()
    rec = logging.makeLogRecord(
        {"msg": "opened /Users/bob/.alkera/auth.yml with Bearer abcdef0123456789"}
    )
    assert f.filter(rec) is True
    out = rec.getMessage()
    assert "/Users/bob" not in out
    assert "~/.alkera/auth.yml" in out
    assert "abcdef0123456789" not in out


def test_filter_leaves_clean_messages_untouched() -> None:
    f = _ScrubbingFilter()
    rec = logging.makeLogRecord({"msg": "harness started ok"})
    f.filter(rec)
    assert rec.getMessage() == "harness started ok"


def test_filter_scrubs_through_args() -> None:
    f = _ScrubbingFilter()
    rec = logging.makeLogRecord({"msg": "spawn at %s", "args": ("/Users/bob/dist/alkera",)})
    f.filter(rec)
    out = rec.getMessage()
    assert "/Users/bob" not in out
    assert out == "spawn at ~/dist/alkera"


_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32", reason="mode bits are a POSIX concept; chmod is a no-op on Windows"
)


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _log_through(handler: logging.Handler, message: str) -> None:
    handler.emit(logging.makeLogRecord({"msg": message, "levelno": logging.INFO}))
    handler.flush()


@_POSIX_MODES
def test_the_daemon_log_and_its_rollovers_are_owner_only(
    tmp_path: Path, permissive_umask: None
) -> None:
    log = tmp_path / "logs" / "daemon.log"
    log.parent.mkdir()
    handler = PrivateRotatingFileHandler(log, maxBytes=64, backupCount=2, encoding="utf-8")
    try:
        _log_through(handler, "chat 1 opened relation analytics.orders")
        assert _mode(log) == LOG_FILE_MODE == 0o600
        for n in range(4):
            _log_through(handler, f"line {n} " + "x" * 64)
    finally:
        handler.close()
    rotated = tmp_path / "logs" / "daemon.log.1"
    assert rotated.exists()
    assert {_mode(log), _mode(rotated)} == {0o600}


@_POSIX_MODES
def test_a_world_readable_log_and_directory_left_by_an_earlier_build_are_closed(
    tmp_path: Path, permissive_umask: None
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o755)
    logs.chmod(0o755)
    log = logs / "daemon.log"
    log.write_text("old line\n")
    log.chmod(0o644)
    handler = PrivateRotatingFileHandler(log, maxBytes=1 << 20, backupCount=1, encoding="utf-8")
    try:
        _log_through(handler, "new line")
    finally:
        handler.close()
    assert _mode(log) == 0o600
    assert _mode(logs) == LOG_DIR_MODE == 0o700
    assert log.read_text().splitlines()[0] == "old line"
