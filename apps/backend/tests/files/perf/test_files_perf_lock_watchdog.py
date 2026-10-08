"""The watchdog that turns a stalled benchmark row into evidence.

A budget row that stops making progress used to cost the whole session timeout and
say nothing: the fault handler's dump showed the worker parked in the event
loop, which is where a coroutine awaiting Postgres always is. The watchdog is
what makes the next occurrence answerable — it prints who is waiting on whom and
cancels the waiter, so the stalled statement raises inside the test.

This module proves it against a real lock rather than a described one: one
connection takes the table, another asks for it and blocks, and the watchdog is
called the way its timer calls it. A green here means a cancelled statement, a
named pair of pids, and the blocked query in the log — the three things the
thirty-minute stall did not produce.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import psycopg
import pytest
from _lock_watchdog import cancel_backends_waiting_on_locks
from alkera_core.config import settings
from sqlalchemy import create_engine, text

# One xdist worker for the whole perf directory: its conftest hands every module a
# module-scoped teardown that purges the deep-path rows the module built, and that
# purge scans the table — splitting a module per test pays it once per worker.
pytestmark = pytest.mark.xdist_group("files_perf")

#: The relation the two connections contend over. Any table would do; this one
#: is the suite's own and is never written by anything outside a test.
CONTENDED = "file_nodes"

#: How long the blocked backend may take to show up as waiting. Generous for a
#: loaded box, short enough that a watchdog that never fires is a failure rather
#: than a stall of its own.
APPEARS_WITHIN = 10.0


def _wait_for_the_lock_wait(conn: Any, pid: int) -> str | None:
    """Poll until ``pid`` is recorded as waiting on a lock, or give up."""
    deadline = time.monotonic() + APPEARS_WITHIN
    seen: str | None = None
    while time.monotonic() < deadline:
        seen = conn.execute(
            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": pid},
        ).scalar_one_or_none()
        if seen == "Lock":
            return seen
        time.sleep(0.05)
    return seen


@pytest.mark.timeout(60)
def test_the_watchdog_cancels_a_backend_that_is_waiting_on_a_lock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The waiter's statement raises, and the log names both sides.

    The holder is left alone on purpose: cancelling it would roll back whatever
    a fixture was in the middle of and hide which side was at fault. It is still
    named in the snapshot, which is the whole point — the pid that blocks is the
    one a reader needs.
    """
    holder = create_engine(settings.database_url_sync)
    waiter = create_engine(settings.database_url_sync)
    raised: list[BaseException] = []
    waiter_pid: list[int] = []
    asking = threading.Event()

    def ask_for_the_table() -> None:
        with waiter.connect() as conn:
            waiter_pid.append(int(conn.execute(text("SELECT pg_backend_pid()")).scalar_one()))
            asking.set()
            try:
                conn.execute(text(f"SELECT count(*) FROM {CONTENDED}"))
            except BaseException as exc:  # the cancellation is the assertion
                raised.append(exc)
            finally:
                conn.rollback()

    blocked = threading.Thread(target=ask_for_the_table, name="lock-watchdog-waiter")
    try:
        with holder.connect() as held:
            holder_pid = int(held.execute(text("SELECT pg_backend_pid()")).scalar_one())
            held.execute(text(f"LOCK TABLE {CONTENDED} IN ACCESS EXCLUSIVE MODE"))

            blocked.start()
            assert asking.wait(APPEARS_WITHIN), "the second connection never opened"
            assert _wait_for_the_lock_wait(held, waiter_pid[0]) == "Lock", (
                "the second connection is not waiting on a lock, so this test is "
                "not standing over the stall the watchdog is for"
            )

            with caplog.at_level(logging.ERROR):
                cancelled = cancel_backends_waiting_on_locks()

            blocked.join(APPEARS_WITHIN)
            assert not blocked.is_alive(), "the blocked statement outlived the cancellation"
            held.rollback()
    finally:
        holder.dispose()
        waiter.dispose()

    assert cancelled == (waiter_pid[0],), (
        f"the watchdog cancelled {cancelled}, not the one backend that was waiting"
    )
    assert raised, "the blocked statement returned rows; nothing was cancelled"
    assert isinstance(getattr(raised[0], "orig", None), psycopg.errors.QueryCanceled), (
        f"the blocked statement failed with {raised[0]!r}, not a cancellation"
    )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"pid={waiter_pid[0]}" in logged, "the log does not name the backend that was waiting"
    assert f"pid={holder_pid}" in logged, "the log does not name the backend that was holding"
    assert f"blocked_by=[{holder_pid}]" in logged, "the log does not say who was blocking"
    assert CONTENDED in logged, "the log does not carry the statement that was waiting"
