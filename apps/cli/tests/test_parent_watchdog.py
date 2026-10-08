"""The seed-worker parent-death watchdog.

The daemon spawns ``alkera lineage|context seed`` workers with
``ALKERA_SEED_PARENT_PID`` set. macOS has no OS parent-death signal, so each worker
watches that PID and exits the instant the daemon is gone — never keeping a parse/embed
running as an orphan that would race the next daemon's seed of the same store. A worker
run directly from the CLI (no env var) must be a no-op.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import pytest
from alkera_cli.host import parent_watchdog
from alkera_cli.host.parent_watchdog import SEED_PARENT_PID_ENV, watch_parent


def test_noop_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SEED_PARENT_PID_ENV, raising=False)
    assert watch_parent() is False  # a direct CLI run has no parent to watch


@pytest.mark.parametrize("value", ["", "not-an-int", "0", "-5"])
def test_noop_for_absent_or_invalid_pid(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEED_PARENT_PID_ENV, value)
    assert watch_parent() is False


def test_exits_immediately_when_parent_is_already_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    # The daemon died between spawn and our startup — don't even begin the work.
    monkeypatch.setenv(SEED_PARENT_PID_ENV, "424242")
    monkeypatch.setattr(parent_watchdog, "process_alive", lambda _pid: False)
    monkeypatch.setattr(parent_watchdog.os, "_exit", _raise_systemexit)
    with pytest.raises(SystemExit):
        watch_parent()


def test_watch_loop_polls_then_exits_the_worker_when_the_parent_dies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Driven synchronously (no live thread): the loop polls while the parent is alive, then
    # hard-exits the moment it's gone. os._exit is the only way out, so a dead parent ALWAYS
    # stops the worker — it can't keep parsing/embedding as an orphan.
    liveness: Iterator[bool] = iter([True, True, False])
    monkeypatch.setattr(parent_watchdog, "process_alive", lambda _pid: next(liveness))
    monkeypatch.setattr(parent_watchdog.time, "sleep", lambda _s: None)
    monkeypatch.setattr(parent_watchdog.os, "_exit", _raise_systemexit)
    with pytest.raises(SystemExit):
        parent_watchdog._watch_loop(424242, 0.01)


def test_starts_a_daemon_watchdog_thread_when_the_parent_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Watching THIS test process (always alive) → the thread parks in the poll loop and never
    # exits, so there's no live os._exit to risk killing the suite — we only assert it spawned.
    monkeypatch.setenv(SEED_PARENT_PID_ENV, str(os.getpid()))
    assert watch_parent(poll_interval=0.05) is True
    assert any(t.name == "seed-parent-watchdog" for t in threading.enumerate())


def _raise_systemexit(_code: int) -> None:
    raise SystemExit(0)
