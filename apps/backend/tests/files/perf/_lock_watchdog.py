"""Evidence for a benchmark row that stops making progress.

A budget row normally runs in about two minutes. Twice in five CI runs one of them
instead sat until the session's own timeout fired half an hour later, and the
fault handler's dump showed the worker's main thread parked in the event loop —
waiting on Postgres, with nothing to say about what it was waiting for. Every
xdist worker owns its database, so whatever held the lock was one of the
worker's own connections: the app's async pool, the live backend some rows
drive, or a sync session an earlier module left idle in a transaction.

The next occurrence should not cost another thirty minutes and still say
nothing. :func:`cancel_backends_waiting_on_locks` takes the snapshot the dump
could not — who is waiting, on whom, for how long, and on what statement —
prints it where a failing test shows it, and then cancels the waiters so the
awaited statement raises inside the test instead of sitting to the cap. A
backend that is merely idle in a transaction is named and left alone: cancelling
one of those tells the test nothing and could break a fixture that is mid-setup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alkera_core.config import settings
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

#: How long a budget row may run before the watchdog goes looking for a lock. Twice
#: the slowest row at the smoke size, so a merely loaded box never trips it.
LOCK_WATCHDOG_SECONDS = 240.0

#: How long a backend may sit inside a transaction doing nothing before the
#: snapshot calls it out. It is not cancelled — a fixture between two statements
#: looks exactly like this — but it is the shape that strands a lock, so the log
#: has to name it.
IDLE_IN_TRANSACTION_SECONDS = 120.0

_SNAPSHOT = text("""
SELECT pid,
       state,
       wait_event_type,
       wait_event,
       pg_blocking_pids(pid) AS blocked_by,
       extract(epoch FROM (now() - xact_start)) AS xact_seconds,
       extract(epoch FROM (now() - state_change)) AS state_seconds,
       backend_type,
       application_name,
       left(query, 4000) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY pid
""")

_CANCEL = text("SELECT pg_cancel_backend(:pid)")


@dataclass(frozen=True)
class _Backend:
    """One row of the snapshot, in the shape the log line prints."""

    pid: int
    state: str | None
    wait_event_type: str | None
    wait_event: str | None
    blocked_by: tuple[int, ...]
    xact_seconds: float | None
    state_seconds: float | None
    backend_type: str | None
    application_name: str | None
    query: str | None

    @property
    def waiting_on_a_lock(self) -> bool:
        return self.wait_event_type == "Lock"

    def idle_in_transaction_for(self, seconds: float) -> bool:
        return (self.state or "").startswith("idle in transaction") and (
            self.state_seconds or 0.0
        ) >= seconds

    def line(self) -> str:
        blocked = ",".join(str(pid) for pid in self.blocked_by) or "-"
        return (
            f"  pid={self.pid} state={self.state!r} "
            f"wait={self.wait_event_type}/{self.wait_event} blocked_by=[{blocked}] "
            f"xact={_seconds(self.xact_seconds)} state_for={_seconds(self.state_seconds)} "
            f"backend={self.backend_type} app={self.application_name!r} "
            f"query={(self.query or '').strip()!r}"
        )


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def cancel_backends_waiting_on_locks(
    *, idle_seconds: float = IDLE_IN_TRANSACTION_SECONDS
) -> tuple[int, ...]:
    """Log every backend on this database, then cancel the ones stuck on a lock.

    Answers the pids it cancelled. Runs on a connection of its own — the pool
    the stalled test is waiting on is the last thing that can be asked — and
    disposes it, so nothing of the watchdog's outlives the call.
    """
    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            backends = [
                _Backend(
                    pid=row.pid,
                    state=row.state,
                    wait_event_type=row.wait_event_type,
                    wait_event=row.wait_event,
                    blocked_by=tuple(row.blocked_by or ()),
                    xact_seconds=row.xact_seconds,
                    state_seconds=row.state_seconds,
                    backend_type=row.backend_type,
                    application_name=row.application_name,
                    query=row.query,
                )
                for row in conn.execute(_SNAPSHOT)
            ]
            waiting = [backend for backend in backends if backend.waiting_on_a_lock]
            stranding = [
                backend for backend in backends if backend.idle_in_transaction_for(idle_seconds)
            ]
            logger.error(
                "a benchmark row has been running for %.0fs; every backend on this "
                "database follows. %d waiting on a lock, %d idle in a transaction for "
                "more than %.0fs (named, not touched — a fixture between two statements "
                "looks the same).\n%s",
                LOCK_WATCHDOG_SECONDS,
                len(waiting),
                len(stranding),
                idle_seconds,
                "\n".join(backend.line() for backend in backends) or "  (no other backend)",
            )
            cancelled: list[int] = []
            for backend in waiting:
                # The waiter, never the holder: cancelling the statement the
                # test is awaiting turns the stall into an exception the test
                # reports, while cancelling the holder would hide which side was
                # at fault and roll back work another fixture is mid-way through.
                conn.execute(_CANCEL, {"pid": backend.pid})
                cancelled.append(backend.pid)
            if cancelled:
                logger.error(
                    "cancelled %s so the awaited statement raises here rather than "
                    "running out the session timeout",
                    ", ".join(str(pid) for pid in cancelled),
                )
            return tuple(cancelled)
    finally:
        engine.dispose()
