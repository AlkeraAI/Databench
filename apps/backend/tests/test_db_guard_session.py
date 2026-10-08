"""A pytest session refuses a database it may not destroy — before a fixture runs.

The predicate is unit-tested next to the code
(``packages/api-core/tests/test_disposable_database.py``). What this file proves is
the wiring: that a REAL pytest session, started the way a developer or a lane starts
one, dies at collection when ``DATABASE_URL`` names a database outside the allowlist,
and that it dies EARLY ENOUGH — the canary table planted in that database still holds
its row, because the session's table-clearing fixture never got to run.

The second case is the control: with ``ALKERA_TEST_ALLOW_DB`` naming that same
database exactly, the identical session runs and the fixture DOES empty the canary.
Without it, "the row survived" would prove nothing about the guard.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from alkera_core.config import settings
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The session under test writes here so it is never confused with a real suite
#: directory; ``/scratch/`` is gitignored, so a crashed run leaves the tree clean.
SCRATCH = REPO_ROOT / "scratch"

#: A pytest session that clears a table on every test — the shape of the fixtures
#: that made this guard necessary (``delete(ComputeAllocation)``, ``TRUNCATE`` …).
CANARY_SESSION = '''
"""Stands in for the suite's table-clearing fixtures."""

import os

import psycopg
import pytest


@pytest.fixture(autouse=True)
def _clear_the_table() -> None:
    with psycopg.connect(os.environ["GUARD_CANARY_DSN"], autocommit=True) as conn:
        conn.execute("TRUNCATE guard_canary")


def test_the_fixture_ran() -> None:
    assert True
'''

#: A pytest session that never opens a database — the file-semantics subsets and the
#: snapshot generators are shaped like this, and they run where no Postgres exists.
DATABASE_FREE_SESSION = '''
"""Touches no database at all."""


def test_it_ran() -> None:
    assert True
'''

#: Nothing listens here (port 1 is privileged and unbound), so a connection to it is
#: refused immediately rather than waiting out a timeout.
DEAD_PORT = 1


def _libpq(database: str) -> str:
    """A psycopg URL for ``database`` on the same server this suite is using."""
    url = make_url(settings.database_url_sync).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


def _dsn(database: str, *, driver: str, port: int | None = None) -> str:
    url = make_url(settings.database_url_sync).set(drivername=driver, database=database)
    if port is not None:
        url = url.set(port=port)
    return url.render_as_string(hide_password=False)


@contextmanager
def _database_the_guard_refuses() -> Iterator[str]:
    """A REAL database whose name matches no disposable pattern — a stand-in for the
    dev database ``make dev-all`` serves — holding one canary row. Dropped on exit."""
    name = f"alkera_demo_{secrets.token_hex(4)}"
    admin = psycopg.connect(_libpq("postgres"), autocommit=True)
    try:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
        with psycopg.connect(_libpq(name), autocommit=True) as conn:
            conn.execute("CREATE TABLE guard_canary (id int primary key)")
            conn.execute("INSERT INTO guard_canary (id) VALUES (1)")
        yield name
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def _canary_rows(database: str) -> int:
    with psycopg.connect(_libpq(database)) as conn:
        row = conn.execute("SELECT count(*) FROM guard_canary").fetchone()
    assert row is not None
    return int(row[0])


@contextmanager
def _session_directory() -> Iterator[Path]:
    """The tiny session lives INSIDE the repo, so the run resolves the repo's rootdir
    and loads the root ``conftest.py`` — the thing under test."""
    directory = SCRATCH / f"db-guard-{secrets.token_hex(4)}"
    directory.mkdir(parents=True)
    try:
        (directory / "test_canary_session.py").write_text(CANARY_SESSION, encoding="utf-8")
        (directory / "test_free_session.py").write_text(DATABASE_FREE_SESSION, encoding="utf-8")
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _run_session(
    directory: Path,
    database: str,
    *,
    allow: str | None = None,
    module: str = "test_canary_session.py",
    port: int | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = _dsn(database, driver="postgresql+asyncpg", port=port)
    env["DATABASE_URL_SYNC"] = _dsn(database, driver="postgresql+psycopg", port=port)
    env["GUARD_CANARY_DSN"] = _libpq(database)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_XDIST_WORKER", None)
    if allow is None:
        env.pop("ALKERA_TEST_ALLOW_DB", None)
    else:
        env["ALKERA_TEST_ALLOW_DB"] = allow
    relative = directory.relative_to(REPO_ROOT) / module
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(relative)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_session_pointed_at_a_non_disposable_database_dies_before_any_fixture_runs() -> None:
    with _database_the_guard_refuses() as database, _session_directory() as directory:
        result = _run_session(directory, database)
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert database in output, output
        assert "ALKERA_TEST_ALLOW_DB" in output, output
        # The whole point: it died before the fixture could clear the table.
        assert _canary_rows(database) == 1, output
        assert "1 passed" not in output, output


def test_the_same_session_runs_when_the_allow_variable_names_that_database() -> None:
    """The control. The refusal above is the guard's doing, not a broken session:
    named explicitly, the identical run collects, the fixture fires, the row goes."""
    with _database_the_guard_refuses() as database, _session_directory() as directory:
        result = _run_session(directory, database, allow=database)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert _canary_rows(database) == 0, output


def test_a_session_whose_database_server_is_not_even_up_is_not_aborted() -> None:
    """The one exemption, and the reason it is sound: nothing is listening, so there
    is no data to protect. The subsets that run where no Postgres exists (the WSL
    file-semantics lane, the snapshot generators) keep working; a test that does need
    a database still fails at its first connection, as it always did."""
    with _session_directory() as directory:
        result = _run_session(
            directory, "alkera_demo_not_running", module="test_free_session.py", port=DEAD_PORT
        )
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert "1 passed" in output, output
