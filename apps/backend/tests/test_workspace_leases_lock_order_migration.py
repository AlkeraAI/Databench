"""Revision 0179 beside the previous build's lease traffic.

The previous build's lease path reads ``compute_allocations`` and then
``file_leases`` in one transaction. 0179 alters both tables. Held in one
transaction in the other order, the two meet head on and Postgres ends one of
them as a deadlock: a failed deploy, or a failed lease request on a box.

This drives the real ``alembic upgrade`` against a scratch database while a
session plays that lease path, and holds the revision to never keeping one
table's lock while it waits for the other's.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
import pytest
from tests.test_migration_safety import (  # noqa: F401 -- fixtures
    _Database,
    _libpq,
    _Runner,
    _template,
    database,
)

pytestmark = pytest.mark.xdist_group("migration_safety")

_PARENT = "0178"
_REVISION = "0179"

#: The runner's sessions that hold or await a table lock, by table.
_RUNNER_LOCKS = """
    SELECT c.relname, l.granted
    FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    WHERE l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.mode = 'AccessExclusiveLock'
      AND c.relname IN ('compute_allocations', 'file_leases')
      AND l.pid <> pg_backend_pid()
    ORDER BY c.relname
"""


def _await_locks(db: _Database, runner: _Runner, expected: list[tuple[str, bool]]) -> None:
    """Until the runner's locks on the two tables are ``expected``. A runner that
    exits first fails the wait with what it wrote."""
    deadline = time.monotonic() + 120
    seen: list[tuple[Any, ...]] = []
    while time.monotonic() < deadline:
        seen = db.rows(_RUNNER_LOCKS)
        if seen == expected:
            return
        if runner.poll() is not None:
            code, output = runner.finish()
            raise AssertionError(
                f"the runner exited ({code}) before its locks were {expected}:\n{output}"
            )
        time.sleep(0.05)
    raise AssertionError(f"the runner's table locks were {seen}, expected {expected}")


def test_the_revision_holds_no_table_lock_while_it_waits_for_another(
    database: _Database,  # noqa: F811
) -> None:
    database.must("upgrade", _PARENT)
    # The lease path, first half: a read of compute_allocations, in a
    # transaction that stays open.
    app = psycopg.connect(_libpq(database.name))
    try:
        app.execute("SELECT count(*) FROM compute_allocations").fetchone()

        runner = database.start("upgrade", _REVISION, MIGRATION_LOCK_TIMEOUT_MS="30000")
        # The revision is now waiting for compute_allocations, and holds
        # nothing on file_leases while it does.
        _await_locks(database, runner, [("compute_allocations", False)])

        # The lease path, second half. It must not wait on the migration: a
        # wait here is the cycle Postgres breaks by ending one of the two.
        app.execute("SET LOCAL statement_timeout = '15s'")
        assert app.execute("SELECT count(*) FROM file_leases").fetchone() == (2,)
        app.commit()

        code, output = runner.finish()
    finally:
        app.close()
    assert code == 0, output
    assert "deadlock" not in output.lower()
    assert database.revision() == _REVISION
    assert (
        database.scalar(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'compute_allocations' AND column_name = 'capabilities_json'"
        )
        == 1
    )
    # The widened CHECK is in place.
    assert database.rows(
        "SELECT pg_get_constraintdef(oid) LIKE '%%workspace%%' "
        "FROM pg_constraint WHERE conname = 'ck_file_leases_purpose'"
    ) == [(True,)]


def test_the_way_down_holds_no_table_lock_while_it_waits_for_another(
    database: _Database,  # noqa: F811
) -> None:
    database.must("upgrade", _REVISION)
    app = psycopg.connect(_libpq(database.name))
    try:
        app.execute("SELECT count(*) FROM compute_allocations").fetchone()
        runner = database.start("downgrade", _PARENT, MIGRATION_LOCK_TIMEOUT_MS="30000")
        _await_locks(database, runner, [("compute_allocations", False)])
        app.execute("SET LOCAL statement_timeout = '15s'")
        assert app.execute("SELECT count(*) FROM file_leases").fetchone() == (2,)
        app.commit()
        code, output = runner.finish()
    finally:
        app.close()
    assert code == 0, output
    assert "deadlock" not in output.lower()
    assert database.revision() == _PARENT


def test_a_run_that_died_between_its_two_steps_runs_again(
    database: _Database,  # noqa: F811
) -> None:
    """The first step commits on its own, so a deploy killed after it leaves the
    column in place and the revision unstamped. The next run finishes."""
    database.must("upgrade", _PARENT)
    with database.connect() as conn:
        conn.execute("ALTER TABLE compute_allocations ADD COLUMN capabilities_json JSONB")
    database.must("upgrade", _REVISION)
    assert database.revision() == _REVISION
    assert database.rows(
        "SELECT pg_get_constraintdef(oid) LIKE '%%workspace%%' "
        "FROM pg_constraint WHERE conname = 'ck_file_leases_purpose'"
    ) == [(True,)]
