"""``scripts/db_backup.py`` against a real database: back up, restore, serve.

The app runs every Files statement and every authenticated request as a tenant
role. A dump that loses the grants restores tables those roles cannot read, and
the app answers 500 on drives, objects and the trash. So the proof that a
backup is usable is not that the rows came back: it is that the app serves a
Files read from the restored database.

Each test takes a copy of the migration template at head, makes an org in it
through the app, and drives the script's own entry point with the real
``pg_dump`` / ``pg_restore``. Restores land in a database of the test's own.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alkera_core.config import settings
from alkera_core.db import session as db_session
from alkera_core.db.row_security import (
    CONTENT_PROBE_TABLE,
    FILES_APP_ROLE,
    PROBE_TABLE,
    TENANT_APP_ROLE,
)
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import OrgWithAdmin, app_client, login
from tests.migration_harness import ScratchDatabase, migration_scratch, seed_org_admin

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import db_backup  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("db_backup_restore")]

_BASE = make_url(settings.database_url_sync)


def _libpq(database: str) -> str:
    return _BASE.set(drivername="postgresql", database=database).render_as_string(
        hide_password=False
    )


def _async_url(database: str) -> str:
    return (
        make_url(settings.database_url).set(database=database).render_as_string(hide_password=False)
    )


def _admin() -> psycopg.Connection[Any]:
    return psycopg.connect(_libpq("postgres"), autocommit=True)


def _server_major() -> int:
    with _admin() as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    assert row is not None
    return int(row[0]) // 10000


def _client_major() -> int | None:
    found = shutil.which("pg_dump")
    if found is None:
        return None
    out = subprocess.run([found, "--version"], capture_output=True, text=True, check=True)
    return db_backup.major(out.stdout)


@pytest.fixture(scope="module")
def pg_tools() -> None:
    """The script refuses client tools of another major version than the
    server (it is the rule that makes a restore reliable), so these tests need
    a matching ``pg_dump`` on PATH or in ``PG_BIN_DIR``."""
    import os

    if os.environ.get("PG_BIN_DIR"):
        return
    client, server = _client_major(), _server_major()
    if client != server:
        pytest.skip(
            f"needs the Postgres {server} client tools (found {client}); "
            "set PG_BIN_DIR to a directory holding pg_dump, pg_restore and psql"
        )


@pytest.fixture
def target() -> Iterator[str]:
    """The name of a database for a restore to land in. It does not exist yet,
    and whatever is there afterwards is dropped."""
    name = f"alkera_migtest_restore_{secrets.token_hex(5)}"
    try:
        yield name
    finally:
        with _admin() as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def _serving(database: str) -> Iterator[None]:
    """Serve the in-process app from ``database``, as ``ScratchDatabase.serving``
    does for a scratch copy."""
    engine = create_async_engine(_async_url(database), poolclass=pool.NullPool)
    factory = db_session.AsyncSessionLocal
    previous = factory.kw.get("bind")
    factory.configure(bind=engine)
    try:
        yield
    finally:
        factory.configure(bind=previous)


async def _drives(org: OrgWithAdmin) -> tuple[int, Any]:
    """The org admin's ``GET /files/drives``: a Files read, as the tenant role."""
    async with app_client() as client:
        await login(client, org.admin_email, org.admin_password)
        resp = await client.get("/api/v1/files/drives")
        return resp.status_code, resp.json()


def _scalar(database: str, query: str, *params: Any) -> Any:
    with psycopg.connect(_libpq(database), autocommit=True) as conn:
        row = conn.execute(query, params).fetchone()
        return None if row is None else row[0]


def _relations(database: str) -> int:
    return int(
        _scalar(
            database,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'v')",
        )
    )


async def _source_with_an_org(scratch: ScratchDatabase) -> tuple[OrgWithAdmin, Any]:
    org = await seed_org_admin(scratch)
    with scratch.serving():
        status, drives = await _drives(org)
    assert status == 200, drives
    assert drives, "the org admin should have a drive to read back after the restore"
    return org, drives


def _run(*args: str) -> int:
    return db_backup.main(list(args))


def _roles_file(dump: Path) -> str:
    return Path(str(dump) + db_backup.ROLES_SUFFIX).read_text(encoding="utf-8")


def _forget_roles_file(dump: Path) -> None:
    Path(str(dump) + db_backup.ROLES_SUFFIX).unlink()


def _dump_without_privileges(database: str, dump: Path) -> None:
    """A dump as ``make backup`` made one before it kept privileges."""
    subprocess.run(
        [
            db_backup._tool("pg_dump"),
            "-Fc",
            "--no-owner",
            "--no-privileges",
            "-d",
            _libpq(database),
            "-f",
            str(dump),
        ],
        check=True,
    )


async def test_a_restored_database_serves_a_files_read(
    pg_tools: None, target: str, tmp_path: Path
) -> None:
    dump = tmp_path / "alkera.dump"
    extra = f"alkera_bk_{secrets.token_hex(4)}"
    try:
        async with migration_scratch() as scratch:
            org, drives = await _source_with_an_org(scratch)
            revision = scratch.revision()
            # A role only this database's privileges name, to stand for a
            # runtime login a new cluster does not have.
            with psycopg.connect(_libpq(scratch.name), autocommit=True) as conn:
                conn.execute(f'CREATE ROLE "{extra}" NOLOGIN')
                conn.execute(f'GRANT SELECT ON users TO "{extra}"')
            assert _run("backup", "--url", scratch.sync_url, "--file", str(dump)) == 0
        # The source is gone, and so is the role: the restore meets a cluster
        # that has never heard of it.
        with _admin() as admin:
            admin.execute(f'DROP ROLE "{extra}"')
        assert f'CREATE ROLE "{extra}" NOLOGIN NOSUPERUSER NOBYPASSRLS' in _roles_file(dump)

        assert _run("restore", "--url", _libpq(target), "--file", str(dump)) == 0

        assert _scalar(target, "SELECT version_num FROM alembic_version") == revision
        # The grants, the forced row security and the policies all came back.
        for role, table in ((FILES_APP_ROLE, PROBE_TABLE), (TENANT_APP_ROLE, CONTENT_PROBE_TABLE)):
            assert _scalar(target, "SELECT has_table_privilege(%s, %s, 'SELECT')", role, table)
            assert _scalar(
                target,
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                "WHERE oid = %s::regclass",
                table,
            )
            assert (
                _scalar(
                    target, "SELECT count(*) FROM pg_policy WHERE polrelid = %s::regclass", table
                )
                >= 1
            )
        assert _scalar(target, "SELECT has_table_privilege(%s, 'users', 'SELECT')", extra)
        assert not _scalar(target, "SELECT has_table_privilege(%s, 'users', 'INSERT')", extra)
        # A table a later migration creates still reaches the tenant role.
        with psycopg.connect(_libpq(target), autocommit=True) as conn:
            conn.execute("CREATE TABLE made_after_restore (id int)")
        assert _scalar(
            target,
            "SELECT has_table_privilege(%s, 'made_after_restore', 'SELECT')",
            TENANT_APP_ROLE,
        )

        with _serving(target):
            status, restored = await _drives(org)
        assert status == 200, restored
        assert restored == drives
    finally:
        with _admin() as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
            admin.execute(f'DROP ROLE IF EXISTS "{extra}"')


async def test_a_restore_never_writes_over_a_database_that_holds_anything(
    pg_tools: None, target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reported damage: a dump restored over a database that had moved on
    left some tables at the newer schema with their newer rows and the rest
    reverted, stamped at the old revision. A target that holds anything is
    refused whole, and only a named, confirmed recreate replaces it."""
    dump = tmp_path / "alkera.dump"
    async with migration_scratch() as scratch:
        org, _ = await _source_with_an_org(scratch)
        assert _run("backup", "--url", scratch.sync_url, "--file", str(dump)) == 0
    assert _run("restore", "--url", _libpq(target), "--file", str(dump)) == 0

    # The restored database moves on: a later schema change and a later row.
    with psycopg.connect(_libpq(target), autocommit=True) as conn:
        conn.execute("ALTER TABLE users ADD COLUMN added_after_backup text")
        conn.execute("CREATE TABLE written_after_backup (note text)")
        conn.execute("INSERT INTO written_after_backup VALUES ('kept')")
    before = _relations(target)
    capsys.readouterr()

    for refused in (
        ("restore", "--url", _libpq(target), "--file", str(dump)),
        ("restore", "--url", _libpq(target), "--file", str(dump), "--recreate"),
        ("restore", "--url", _libpq(target), "--file", str(dump), "--recreate", "--confirm", "x"),
        ("restore", "--url", _libpq(target), "--file", str(dump), "--confirm", target),
    ):
        assert _run(*refused) == db_backup.EXIT_REFUSED, refused
        assert _relations(target) == before
        assert _scalar(target, "SELECT note FROM written_after_backup") == "kept"
        assert (
            _scalar(
                target,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'added_after_backup'",
            )
            == 1
        )
    assert "is not empty" in capsys.readouterr().err

    assert (
        _run(
            "restore",
            "--url",
            _libpq(target),
            "--file",
            str(dump),
            "--recreate",
            "--confirm",
            target,
        )
        == 0
    )
    # Whole and only the dump: nothing of the later state is left.
    assert _scalar(target, "SELECT to_regclass('written_after_backup')") is None
    assert (
        _scalar(
            target,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'added_after_backup'",
        )
        == 0
    )
    with _serving(target):
        status, body = await _drives(org)
    assert status == 200, body


async def test_a_restore_that_fails_leaves_nothing_behind(
    pg_tools: None, target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One transaction, stopped at the first error: a restore that cannot
    finish (here the dump grants to a role the cluster lacks, and the roles
    file that would have made it is missing) says so and restores nothing."""
    dump = tmp_path / "alkera.dump"
    extra = f"alkera_bk_{secrets.token_hex(4)}"
    try:
        async with migration_scratch() as scratch:
            await _source_with_an_org(scratch)
            with psycopg.connect(_libpq(scratch.name), autocommit=True) as conn:
                conn.execute(f'CREATE ROLE "{extra}" NOLOGIN')
                conn.execute(f'GRANT SELECT ON users TO "{extra}"')
            assert _run("backup", "--url", scratch.sync_url, "--file", str(dump)) == 0
        with _admin() as admin:
            admin.execute(f'DROP ROLE "{extra}"')
        _forget_roles_file(dump)
        capsys.readouterr()

        assert (
            _run("restore", "--url", _libpq(target), "--file", str(dump)) == db_backup.EXIT_FAILED
        )

        err = capsys.readouterr().err
        assert extra in err
        assert "holds nothing from this dump" in err
        assert _relations(target) == 0
    finally:
        with _admin() as admin:
            admin.execute(f'DROP ROLE IF EXISTS "{extra}"')


async def test_a_dump_without_privileges_is_reported_and_regrant_repairs_it(
    pg_tools: None, target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dump made the way ``make backup`` used to make one. Its restore must
    not pass for a usable database, and ``regrant`` puts back exactly what the
    migrations grant, after which the app serves from it."""
    dump = tmp_path / "old-style.dump"
    async with migration_scratch() as scratch:
        org, drives = await _source_with_an_org(scratch)
        _dump_without_privileges(scratch.name, dump)
    capsys.readouterr()

    assert _run("restore", "--url", _libpq(target), "--file", str(dump)) == db_backup.EXIT_FAILED
    assert "carries no privileges" in capsys.readouterr().err
    assert not _scalar(
        target, "SELECT has_table_privilege(%s, %s, 'SELECT')", FILES_APP_ROLE, PROBE_TABLE
    )
    with _serving(target):
        status, _ = await _drives(org)
    assert status == 500

    assert _run("regrant", "--url", _libpq(target)) == 0

    for role, table in ((FILES_APP_ROLE, PROBE_TABLE), (TENANT_APP_ROLE, CONTENT_PROBE_TABLE)):
        assert _scalar(target, "SELECT has_table_privilege(%s, %s, 'SELECT')", role, table)
    with _serving(target):
        status, restored = await _drives(org)
    assert status == 200, restored
    assert restored == drives
    # The scratch database regrant built from the migrations is gone.
    with _admin() as admin:
        left = admin.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'alkera\\_regrant\\_%'"
        ).fetchone()
    assert left == (0,)
