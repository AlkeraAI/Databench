"""A pytest session gets the template its migration tests copy — proven on a REAL session.

The provisioning is unit-tested next to the code
(``packages/api-core/tests/test_worker_database.py``) and the harness's use of it in
``test_migration_harness.py``. What this file proves is the wiring in the root
``conftest.py``, started the way a developer or CI starts a session, on both paths:

* serial (``-p no:xdist``): a copy of the session's own database is made at the end
  of collection — before any pool has opened — under the session's own template name,
  a trip opens from it while an idle pooled connection holds the session's database,
  and the copy is dropped when the session ends;
* parallel (``-n 1``): the worker borrows the base it was cloned from, makes no copy,
  and leaves the base in place.

The child session reports the template it was handed to a file, since a green run
alone would not say WHICH database the trip copied.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alkera_core.config import settings
from alkera_core.db.testing import (
    copy_database,
    drop_database,
    migration_template_name,
    worker_database_name,
)
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import make_url
from tests.migration_harness import migration_template

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The session under test lives INSIDE the repo so the run resolves the repo's rootdir
#: and loads the root ``conftest.py`` — the thing under test; ``/scratch/`` is
#: gitignored, so a crashed run leaves the tree clean.
SCRATCH = REPO_ROOT / "scratch"

#: The environment variable the child reports through.
REPORT = "MIGRATION_TEMPLATE_REPORT"

CHILD_SESSION = '''
"""Runs inside the child session: uses the template it was handed, then reports it."""

import json
import os
from pathlib import Path

from alkera_core.config import settings
from alkera_core.db.testing import published_migration_template
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.migration_harness import migration_scratch, script_head


async def test_a_trip_opens_from_the_template_the_session_prepared() -> None:
    template = published_migration_template()
    assert template is not None, "the session prepared no template"
    # An idle pooled connection on the session's database, as any earlier test leaves.
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        async with migration_scratch() as scratch:
            assert scratch.revision() == script_head()
            async with engine.connect() as conn:
                on_template = (
                    await conn.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE datname = :name"),
                        {"name": template.name},
                    )
                ).scalar_one()
        assert on_template == 0, "something in the session connected to the template"
    finally:
        await engine.dispose()
    Path(os.environ["MIGRATION_TEMPLATE_REPORT"]).write_text(
        json.dumps({"name": template.name, "owned": template.owned}), encoding="utf-8"
    )
'''


def _maintenance() -> str:
    return (
        make_url(settings.database_url_sync)
        .set(database="postgres")
        .render_as_string(hide_password=False)
    )


def _dsn(database: str, *, driver: str) -> str:
    url = make_url(settings.database_url_sync).set(drivername=driver, database=database)
    return url.render_as_string(hide_password=False)


def _database_exists(name: str) -> bool:
    engine = create_engine(_maintenance(), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return (
                conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                ).scalar()
                is not None
            )
    finally:
        engine.dispose()


@contextmanager
def _child_base() -> Iterator[str]:
    """A database at head that nothing connects to, for the child session to run on:
    a copy of THIS session's own template, which is copyable by construction.

    Dropped afterwards, with what the child leaves behind by design — its xdist
    worker's database, and (should the child have died before its teardown) the
    template a serial child made."""
    template = migration_template()
    name = f"alkera_migtest_child_{secrets.token_hex(4)}"
    copy_database(template.maintenance_url, template.name, name)
    try:
        yield name
    finally:
        setting = "a child session's database"
        drop_database(template.maintenance_url, worker_database_name(name, "gw0"), setting=setting)
        drop_database(template.maintenance_url, migration_template_name(name), setting=setting)
        drop_database(template.maintenance_url, name, setting=setting)


@contextmanager
def _session_directory() -> Iterator[Path]:
    directory = SCRATCH / f"migration-template-{secrets.token_hex(4)}"
    directory.mkdir(parents=True)
    try:
        (directory / "test_child_session.py").write_text(CHILD_SESSION, encoding="utf-8")
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _run_child(
    directory: Path, database: str, *, parallel: bool
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    """Run the child session on ``database`` and return it with the child's report."""
    report = directory / "template.json"
    env = dict(os.environ)
    env["DATABASE_URL"] = _dsn(database, driver="postgresql+asyncpg")
    env["DATABASE_URL_SYNC"] = _dsn(database, driver="postgresql+psycopg")
    env[REPORT] = str(report)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The child is a session of its own, not a worker of this one: the root conftest
    # reads the worker id at import and would clone THIS worker's database.
    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_CURRENT_TEST",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        env.pop(name, None)
    mode = ["-n", "1"] if parallel else ["-p", "no:xdist"]
    relative = directory.relative_to(REPO_ROOT) / "test_child_session.py"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *mode, str(relative)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    written = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    return completed, written


def test_a_serial_session_makes_its_own_template_and_drops_it_when_it_ends() -> None:
    with _child_base() as database, _session_directory() as directory:
        completed, report = _run_child(directory, database, parallel=False)
        output = completed.stdout + completed.stderr

        assert completed.returncode == 0, output
        assert "1 passed" in output, output
        assert report == {"name": migration_template_name(database), "owned": True}, output
        assert not _database_exists(report["name"]), "the session's template outlived it"
        assert _database_exists(database), "the session dropped the database it ran on"


def test_a_parallel_worker_borrows_the_base_it_was_cloned_from() -> None:
    with _child_base() as database, _session_directory() as directory:
        completed, report = _run_child(directory, database, parallel=True)
        output = completed.stdout + completed.stderr

        assert completed.returncode == 0, output
        assert "1 passed" in output, output
        assert report == {"name": database, "owned": False}, output
        assert _database_exists(database), "a borrowed template was dropped"
        assert not _database_exists(migration_template_name(database)), (
            "the worker made a copy although the base was at head"
        )
        assert not _database_exists(migration_template_name(worker_database_name(database, "gw0")))
