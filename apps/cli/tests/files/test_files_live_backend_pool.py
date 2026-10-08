"""The in-test backend must hand every Postgres backend back when it stops.

The Files CLI tests each run the FastAPI app on their own event loop, and the
connection pool is a process-wide global that outlives every one of them. A
connection can only be closed from the loop that made it, so a pool merely
*replaced* between tests strands its sockets: asyncpg's abort defers the
``close()`` onto a loop that will never run again, and the server keeps the
backend. ``max_connections`` is finite and shared, so a handful of stranded
sockets per test is enough to make every later request — the drives route
first, because it is the first one every test sends — fail with a 500.

How the backends are read matters as much as what is read:

* ``pg_stat_activity`` is a snapshot taken at the first read in a transaction
  and held until it ends, so every reading here is its own transaction
  (``AUTOCOMMIT``). Read inside one transaction, the count never moves, and a
  "wait until it settles" loop returns its first reading however long it waits.
* A client's ``close()`` returning does not mean the server has let the backend
  go. psycopg's close does not wait for the server at all, and even a close
  that waits for the socket to drop returns before the exiting backend clears
  its ``pg_stat_activity`` row (Postgres closes the socket in an earlier exit
  step than the one that removes the row). On a loaded runner that gap is
  long enough to be read.
* So backends are told apart by pid, not counted: a backend the cycle opened
  counts only if it is still there once a bounded wait runs out. One on its way
  out leaves well inside the bound; a stranded one — idle on a socket nobody
  will close — never does, and is named with what it last ran.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alkera_core.config import settings
from files._live_backend import live_backend
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url

#: How long a backend the cycle opened may take to leave before it counts as stranded.
#: Generous: a backend on its way out is gone in milliseconds on an idle machine and
#: well under a second on a loaded one, it is spent only while one is still listed, and
#: a stranded backend outlasts any bound.
_REAP_BUDGET_SECONDS = 15.0

#: Between readings while a backend is still leaving.
_POLL_SECONDS = 0.05


def _client_backends(connection: Connection) -> dict[int, str]:
    """Client backends on the lane database other than the probe's own, by pid.

    ``backend_type`` keeps out what Postgres runs by itself: an autovacuum worker
    lists the database it is vacuuming too, and comes and goes on its own schedule.
    """
    rows = connection.execute(
        text(
            "SELECT pid, coalesce(application_name, ''), coalesce(state, ''), "
            "left(coalesce(query, ''), 120) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND backend_type = 'client backend'"
        )
    ).all()
    return {
        int(pid): f"pid {pid} app={app!r} state={state!r} last query={query!r}"
        for pid, app, state, query in rows
    }


def _stranded(connection: Connection, before: set[int], *, budget: float) -> dict[int, str]:
    """The backends not in ``before`` that are still connected once ``budget`` runs out.

    Returns as soon as none is left, so the budget is spent only while a backend is
    still on its way out.
    """
    deadline = time.monotonic() + budget
    while True:
        new = {pid: row for pid, row in _client_backends(connection).items() if pid not in before}
        if not new or time.monotonic() >= deadline:
            return new
        time.sleep(_POLL_SECONDS)


@pytest.fixture
def probe() -> Iterator[Connection]:
    """One connection for every reading, each in its own transaction."""
    engine = create_engine(settings.database_url_sync, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def test_the_live_backend_leaves_no_connection_behind(tmp_path: Path, probe: Connection) -> None:
    # One cycle first: it drains whatever an earlier test parked in the shared
    # pool, so what the measured cycles are judged against is the harness alone.
    with live_backend(tmp_path / "warmup"):
        pass

    for cycle in range(2):
        before = set(_client_backends(probe))
        with live_backend(tmp_path / f"cycle-{cycle}"):
            pass
        left = _stranded(probe, before, budget=_REAP_BUDGET_SECONDS)
        assert not left, (
            f"cycle {cycle} stranded {len(left)} Postgres backend(s) past "
            f"{_REAP_BUDGET_SECONDS:.0f}s; the pool was replaced instead of closed on its "
            "own loop:\n" + "\n".join(left.values())
        )


def _libpq_dsn() -> str:
    return (
        make_url(settings.database_url_sync)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def test_a_backend_still_leaving_is_not_counted_as_stranded(probe: Connection) -> None:
    """The race the cycle test must not lose: a client closed, its backend not yet gone.

    The backend is kept busy past its client's close — the ``Terminate`` it was sent
    is read only once the sleep ends — which is the reap lag a loaded runner shows,
    made long enough to read on any machine.
    """
    before = set(_client_backends(probe))
    leaving = psycopg.connect(_libpq_dsn())
    pid = leaving.info.backend_pid
    leaving.pgconn.send_query(b"SELECT pg_sleep(1.5)")
    while leaving.pgconn.flush():
        pass
    leaving.close()

    assert pid in _client_backends(probe), "the backend left before it could be read"
    assert _stranded(probe, before, budget=_REAP_BUDGET_SECONDS) == {}


def test_a_backend_nobody_closes_is_named_as_stranded(probe: Connection) -> None:
    before = set(_client_backends(probe))
    with psycopg.connect(_libpq_dsn(), application_name="left-open") as held:
        held.execute("SELECT 1")
        held.commit()
        left = _stranded(probe, before, budget=0.5)
        assert list(left) == [held.info.backend_pid]
        assert "app='left-open'" in left[held.info.backend_pid]
