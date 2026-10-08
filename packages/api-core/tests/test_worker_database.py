"""A parallel worker's database is a copy of the base, and never an older schema.

Under ``-n``, each xdist worker runs against a private database that the root
``conftest.py`` provisions for it at import. It used to build that database from
nothing — ``CREATE DATABASE`` plus a whole ``alembic upgrade head`` — which on a
32-worker Windows shard cost about 45 seconds of every shard's startup, replaying 137
revisions per worker to arrive at the schema the ``make`` target had already put on
the same server minutes earlier. It now COPIES that schema instead.

Two things have to hold for the copy to be worth taking, and both are proven here
against a real Postgres rather than argued:

* what a worker gets really is the base — its schema AND the rows it held — and is its
  own database afterwards, so one worker's writes reach neither the base nor a sibling;
* a base that is NOT at head cannot quietly hand the suite an old schema to prove
  itself against. The stamped revision is compared with the script directory's head,
  and a database that came out behind (a developer whose serial database is stale, or
  one that was never migrated at all) is migrated before it is handed over.

The whole point of the change is the work NOT done, and that is invisible in a result:
a clone and a re-migration both end at head. So the fast path is pinned by
``migrated is False`` plus a row that only a COPY of the base could be carrying — a
freshly migrated database has the schema and not the row.

The second half of this file is the migration template: the database a session's
migration tests copy for each trip. It is either the base itself, borrowed by a worker
whose clone came out at head (so nothing here is dropped), or a copy of the session's
own database made under a name of its own — and a copy is refused, naming the holder,
while anything is connected to the source, because waiting for an idle pool is waiting
forever and terminating it would take a connection some test is about to reuse.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from alkera_core.db.testing import (
    MIGRATION_TEMPLATE_PREFIX,
    MigrationTemplateInUseError,
    NonDisposableDatabaseError,
    WorkerDatabase,
    drop_database,
    drop_migration_template,
    migration_template_name,
    provision_migration_template,
    provision_worker_database,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# One worker: `migrated_base` is a module-scoped database that a real `alembic upgrade
# head` builds once, and every test here reads or copies it.
pytestmark = pytest.mark.xdist_group("worker_database")

_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The directory holding `alembic.ini` and `alembic/` — what provisioning migrates from.
BACKEND_ROOT = _REPO_ROOT / "apps" / "backend"

_FALLBACK_DSN = "postgresql+psycopg://alkera:alkera@localhost:5432/alkera"


def server_url(database: str, *, driver: str = "postgresql+psycopg") -> str:
    """A DSN for ``database`` on the Postgres this session is already running against."""
    base = os.environ.get("DATABASE_URL_SYNC") or _FALLBACK_DSN
    return (
        make_url(base)
        .set(drivername=driver, database=database)
        .render_as_string(hide_password=False)
    )


def head_revision() -> str:
    """The revision the migration scripts end at, read here rather than imported from
    the code under test — an assertion against the same call it is checking proves
    nothing."""
    config = Config()
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None, "the backend has no migrations"
    return head


def sql(url: str, statement: str, **params: Any) -> list[tuple[Any, ...]]:
    """Run one statement (AUTOCOMMIT, so DDL is allowed) and return any rows."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(statement), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def stamped(database: str) -> set[str]:
    """The revisions a database says it is at."""
    return {row[0] for row in sql(server_url(database), "SELECT version_num FROM alembic_version")}


def tables(database: str) -> set[str]:
    return {
        row[0]
        for row in sql(
            server_url(database), "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    }


def database_exists(maintenance: str, name: str) -> bool:
    return sql(maintenance, "SELECT 1 FROM pg_database WHERE datname = :name", name=name) == [(1,)]


def alembic(database: str, *args: str) -> None:
    """Drive the real alembic against ``database``.

    A subprocess for the same reason provisioning uses one: ``env.py`` takes its URL
    from the settings singleton, which in THIS process is already bound to the
    session's own database. Only a child built from the environment handed to it
    migrates the database named here.
    """
    scripts = str(Path(sys.executable).parent)
    console_script = shutil.which("alembic", path=scripts)
    assert console_script, f"the alembic console script is not in {scripts}"
    completed = subprocess.run(
        [console_script, *args],
        cwd=str(BACKEND_ROOT),
        env={
            **os.environ,
            "DATABASE_URL_SYNC": server_url(database),
            "DATABASE_URL": server_url(database, driver="postgresql+asyncpg"),
        },
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, (
        f"alembic {' '.join(args)} on {database} failed:\n{completed.stdout}\n{completed.stderr}"
    )


def scratch_name(purpose: str) -> str:
    """A throwaway database name. ``alkera_migtest_*`` is on the disposable allowlist,
    and the random suffix keeps two runs on one Postgres out of each other's way."""
    return f"alkera_migtest_{purpose}_{secrets.token_hex(4)}"


@pytest.fixture(scope="module")
def maintenance() -> str:
    """The ``postgres`` database: where a CREATE/DROP DATABASE is issued from."""
    return server_url("postgres")


@pytest.fixture(scope="module")
def migrated_base(maintenance: str) -> Iterator[str]:
    """A scratch database at head, carrying a row of its own.

    The row is what tells a COPY from a re-migration: a database that was migrated
    rather than copied has every table the row lives in, and not the row.
    """
    name = scratch_name("base")
    sql(maintenance, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    sql(maintenance, f'CREATE DATABASE "{name}"')
    try:
        alembic(name, "upgrade", "head")
        sql(server_url(name), "CREATE TABLE base_marker (id integer PRIMARY KEY)")
        sql(server_url(name), "INSERT INTO base_marker (id) VALUES (1)")
        yield name
    finally:
        sql(maintenance, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def provision(maintenance: str) -> Iterator[Callable[[str, str], WorkerDatabase]]:
    """Provision worker databases the way the root conftest does, and drop each one
    afterwards however the test ended."""
    created: list[str] = []

    def provision_one(base: str, worker: str) -> WorkerDatabase:
        database = provision_worker_database(server_url(base), worker, alembic_root=BACKEND_ROOT)
        created.append(database.name)
        return database

    yield provision_one
    for name in created:
        sql(maintenance, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_worker_database_is_the_base_copied_rather_than_migrated_again(
    migrated_base: str, provision: Callable[[str, str], WorkerDatabase]
) -> None:
    """The fast path: the schema, the stamped revision and the rows all came across,
    and no migration ran to put them there."""
    database = provision(migrated_base, "gw0")

    assert database.name == f"{migrated_base}_gw0"
    assert database.migrated is False
    # A copy that came out at head proves the base is at head: the base is what
    # this worker's migration tests may copy for the rest of the run.
    assert database.template == migrated_base
    assert stamped(database.name) == {head_revision()} == stamped(migrated_base)
    assert tables(database.name) == tables(migrated_base)
    assert {"alembic_version", "teams", "users"} <= tables(database.name)
    # A row only a copy could be carrying — `alembic upgrade head` creates no such table.
    assert sql(server_url(database.name), "SELECT id FROM base_marker") == [(1,)]
    # The DSNs a worker exports, and the database a CREATE/DROP for it is issued from.
    assert make_url(database.sync_url).database == database.name
    assert make_url(database.async_url).drivername == "postgresql+asyncpg"
    assert make_url(database.maintenance_url).database == "postgres"


def test_a_write_to_one_worker_database_reaches_neither_the_base_nor_a_sibling(
    migrated_base: str, provision: Callable[[str, str], WorkerDatabase]
) -> None:
    """What isolation is for: two workers commit into one another's blind spots."""
    first = provision(migrated_base, "gw1")
    second = provision(migrated_base, "gw2")

    sql(server_url(first.name), "INSERT INTO base_marker (id) VALUES (2)")

    assert sql(server_url(first.name), "SELECT id FROM base_marker ORDER BY id") == [(1,), (2,)]
    assert sql(server_url(second.name), "SELECT id FROM base_marker ORDER BY id") == [(1,)]
    assert sql(server_url(migrated_base), "SELECT id FROM base_marker ORDER BY id") == [(1,)]


def test_a_base_behind_head_gets_its_clone_migrated_the_rest_of_the_way(
    migrated_base: str, maintenance: str, provision: Callable[[str, str], WorkerDatabase]
) -> None:
    """The fail-closed case: a developer whose serial database is one revision stale
    still gets a worker database at head — copying a schema must never be a way to
    prove the suite against an old one."""
    stale = scratch_name("stale")
    sql(maintenance, f'CREATE DATABASE "{stale}" TEMPLATE "{migrated_base}"')
    try:
        alembic(stale, "downgrade", "-1")
        assert stamped(stale) != {head_revision()}

        database = provision(stale, "gw3")

        assert database.migrated is True
        # A base behind head is no template: a copy of it would be behind too.
        assert database.template is None
        assert stamped(database.name) == {head_revision()}
        # The clone was brought forward, not the developer's own database.
        assert stamped(stale) != {head_revision()}
    finally:
        sql(maintenance, f'DROP DATABASE IF EXISTS "{stale}" WITH (FORCE)')


def test_a_base_that_does_not_exist_yet_still_yields_a_worker_database_at_head(
    provision: Callable[[str, str], WorkerDatabase],
) -> None:
    """The far end of the same fallback: nothing to copy, so the worker migrates.

    This is what a bare ``uv run pytest -n`` against a server no ``make`` target has
    provisioned does, and it has to keep working — the copy is an optimisation, not a
    new precondition.
    """
    database = provision(scratch_name("absent"), "gw4")

    assert database.migrated is True
    assert database.template is None
    assert stamped(database.name) == {head_revision()}
    assert "alembic_version" in tables(database.name)


def test_a_leftover_worker_database_from_a_crashed_run_is_replaced(
    migrated_base: str, maintenance: str, provision: Callable[[str, str], WorkerDatabase]
) -> None:
    """A worker that crashed leaves its database behind; the next run must not inherit
    it. The leftover here is empty, so reusing it would show up as a missing schema."""
    leftover = f"{migrated_base}_gw5"
    sql(maintenance, f'CREATE DATABASE "{leftover}"')

    database = provision(migrated_base, "gw5")

    assert database.name == leftover
    assert database.migrated is False
    assert sql(server_url(database.name), "SELECT id FROM base_marker") == [(1,)]


def test_two_workers_copy_one_base_at_the_same_time(
    migrated_base: str, provision: Callable[[str, str], WorkerDatabase]
) -> None:
    """Every worker provisions at once, and Postgres refuses to copy a template another
    session is connected to — so the concurrency is part of the contract, not a detail.
    Nothing connects to the base: a clone's maintenance connection is to ``postgres``,
    and its schema check is against the clone."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = [pool.submit(provision, migrated_base, worker) for worker in ("gw6", "gw7")]
        databases = [future.result() for future in running]

    assert {database.name for database in databases} == {
        f"{migrated_base}_gw6",
        f"{migrated_base}_gw7",
    }
    for database in databases:
        assert database.migrated is False
        assert stamped(database.name) == {head_revision()}


@pytest.mark.parametrize(
    "base",
    [
        pytest.param("alkera", id="the-dev-database"),
        pytest.param("postgres", id="the-maintenance-database"),
        pytest.param("alkera_production", id="a-database-that-only-sounds-like-ours"),
    ],
)
def test_a_template_that_is_not_disposable_is_refused_before_anything_is_created(
    base: str, maintenance: str
) -> None:
    """The guard covers the TEMPLATE, not only the database being written to: copying
    the database ``make dev-all`` serves would be destructive in its own right, and the
    refusal has to come before the first statement, not after."""
    with pytest.raises(NonDisposableDatabaseError) as refusal:
        provision_worker_database(server_url(base), "gw8", alembic_root=BACKEND_ROOT)

    assert base in str(refusal.value)
    assert not database_exists(maintenance, f"{base}_gw8")


def test_dropping_a_database_that_is_not_disposable_is_refused(maintenance: str) -> None:
    """The one statement in the suite that destroys a whole database proves the name
    itself, so a caller that passes one the allowlist does not cover gets a refusal —
    and the database is still there afterwards. Driven against a real database of this
    test's own making, named the way a database worth protecting is named."""
    protected = f"alkera_keepme_{secrets.token_hex(4)}"
    sql(maintenance, f'CREATE DATABASE "{protected}"')
    try:
        with pytest.raises(NonDisposableDatabaseError) as refusal:
            drop_database(maintenance, protected, setting="a database worth keeping")

        assert protected in str(refusal.value)
        assert database_exists(maintenance, protected)
    finally:
        sql(maintenance, f'DROP DATABASE IF EXISTS "{protected}" WITH (FORCE)')


def test_dropping_a_disposable_database_removes_it(maintenance: str) -> None:
    name = scratch_name("droppable")
    sql(maintenance, f'CREATE DATABASE "{name}"')

    drop_database(maintenance, name, setting="a scratch database")

    assert not database_exists(maintenance, name)


# --- the migration template ---------------------------------------------------------


@pytest.fixture
def templates(maintenance: str) -> Iterator[list[str]]:
    """Names of databases a test below creates as templates or their leftovers; every
    one is dropped afterwards however the test ended."""
    names: list[str] = []
    yield names
    for name in names:
        sql(maintenance, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_session_makes_its_template_as_a_copy_of_its_own_database(
    migrated_base: str, maintenance: str, templates: list[str]
) -> None:
    """The serial path. The session's database is copied under a name of its own —
    rows and all, which is how a copy is told from a re-migration — and the copy is
    the session's to drop."""
    templates.append(migration_template_name(migrated_base))

    template = provision_migration_template(server_url(migrated_base))

    assert template.owned is True
    assert template.name == migration_template_name(migrated_base)
    assert template.name.startswith(MIGRATION_TEMPLATE_PREFIX)
    assert make_url(template.maintenance_url).database == "postgres"
    assert stamped(template.name) == {head_revision()}
    assert sql(server_url(template.name), "SELECT id FROM base_marker") == [(1,)]

    drop_migration_template(template)

    assert not database_exists(maintenance, template.name)
    assert database_exists(maintenance, migrated_base), "the source was dropped with the copy"


def test_a_template_a_crashed_session_left_behind_is_replaced(
    migrated_base: str, maintenance: str, templates: list[str]
) -> None:
    """The name is deterministic so a session that never reached its teardown leaves
    one leftover, not one per run — and the next session must not inherit it: the
    leftover here is empty, so reusing it would show up as a missing schema."""
    leftover = migration_template_name(migrated_base)
    templates.append(leftover)
    sql(maintenance, f'CREATE DATABASE "{leftover}"')

    template = provision_migration_template(server_url(migrated_base))

    assert template.name == leftover
    assert sql(server_url(template.name), "SELECT id FROM base_marker") == [(1,)]


def test_a_borrowed_template_is_used_as_it_is_and_never_dropped(
    migrated_base: str, maintenance: str
) -> None:
    """The parallel path. A worker that came out of the base at head borrows the base:
    no copy is made, and dropping the template leaves the base where it was, because
    every other worker is copying from it too."""
    before = {row[0] for row in sql(maintenance, "SELECT datname FROM pg_database")}

    template = provision_migration_template(
        server_url(f"{migrated_base}_gw0"), borrow=migrated_base
    )

    assert template.owned is False
    assert template.name == migrated_base
    assert {row[0] for row in sql(maintenance, "SELECT datname FROM pg_database")} == before

    drop_migration_template(template)

    assert database_exists(maintenance, migrated_base)


@pytest.mark.parametrize(
    ("session_database", "borrow"),
    [
        pytest.param("alkera", None, id="a-session-on-the-dev-database"),
        pytest.param("alkera_test_suite_gw0", "alkera", id="borrowing-the-dev-database"),
        pytest.param("alkera_test_suite_gw0", "postgres", id="borrowing-the-maintenance-database"),
    ],
)
def test_a_migration_template_that_is_not_disposable_is_refused_before_anything_is_created(
    session_database: str, borrow: str | None, maintenance: str
) -> None:
    """Both names go through the guard: the database copied FROM and the one borrowed.
    A copy of the dev database is a destructive statement against it, and a borrowed
    name is one every trip will copy for the rest of the run."""
    with pytest.raises(NonDisposableDatabaseError) as refusal:
        provision_migration_template(server_url(session_database), borrow=borrow)

    assert (borrow or session_database) in str(refusal.value)
    assert not database_exists(maintenance, migration_template_name(session_database))


def test_a_copy_is_refused_while_a_session_is_connected_to_the_source_and_names_it(
    migrated_base: str, maintenance: str, templates: list[str]
) -> None:
    """The failure every migration test met behind a full worker, reproduced at the
    seam that now owns it: one idle connection on the source — checked in, not closed,
    exactly what an engine's pool keeps between tests — and the copy is refused with
    that connection named. It is NOT terminated: the pool it belongs to would hand a
    dead connection to the next test, so the connection still answers afterwards."""
    templates.append(migration_template_name(migrated_base))
    holder = create_engine(server_url(migrated_base))
    try:
        with holder.connect() as conn:
            pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        # Checked back into the pool: still open on the server, idle.

        with pytest.raises(MigrationTemplateInUseError) as refusal:
            provision_migration_template(server_url(migrated_base))

        assert str(pid) in str(refusal.value)
        assert migrated_base in str(refusal.value)
        assert not database_exists(maintenance, migration_template_name(migrated_base))
        with holder.connect() as conn:
            assert conn.execute(text("SELECT pg_backend_pid()")).scalar_one() == pid
    finally:
        holder.dispose()


@pytest.mark.parametrize(
    ("session_database", "expected"),
    [
        pytest.param(
            "alkera_test_suite_gw3",
            f"{MIGRATION_TEMPLATE_PREFIX}alkera_test_suite_gw3",
            id="a-name-that-fits-is-used-whole",
        ),
        pytest.param(
            "a" * (63 - len(MIGRATION_TEMPLATE_PREFIX)),
            MIGRATION_TEMPLATE_PREFIX + "a" * (63 - len(MIGRATION_TEMPLATE_PREFIX)),
            id="exactly-the-identifier-limit-is-still-whole",
        ),
    ],
)
def test_a_template_name_that_fits_is_the_prefix_plus_the_session_database(
    session_database: str, expected: str
) -> None:
    assert migration_template_name(session_database) == expected


def test_template_names_of_long_sessions_stay_inside_the_identifier_limit_and_apart() -> None:
    """Postgres truncates an identifier past 63 bytes silently, so two long session
    names sharing a prefix would name ONE template and drop each other's. A long name
    is shortened with a digest of the whole instead, deterministically."""
    shared = "alkera_test_" + "x" * 50
    first, second = f"{shared}_one", f"{shared}_two"

    names = {migration_template_name(first), migration_template_name(second)}

    assert len(names) == 2
    for name in names:
        assert len(name.encode()) <= 63
        assert name.startswith(MIGRATION_TEMPLATE_PREFIX)
    assert migration_template_name(first) == migration_template_name(first)


def test_finding_the_scripts_head_builds_no_settings() -> None:
    """Provisioning reads the head of the migration scripts BEFORE it can point the
    environment at the worker's database, and Alembic finds that head by importing
    every revision script. A revision whose imports build the settings singleton
    therefore binds it — and the engine every test then opens — to the database the
    session was pointed at, and every worker silently shares one database. A fresh
    interpreter, so nothing this process already imported can mask it."""
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from alembic.config import Config\n"
        "from alembic.script import ScriptDirectory\n"
        "config = Config()\n"
        f"config.set_main_option('script_location', {str(BACKEND_ROOT / 'alembic')!r})\n"
        "assert ScriptDirectory.from_config(config).get_current_head()\n"
        "loaded = sorted(m for m in sys.modules if m == 'alkera_core.config')\n"
        "print(loaded)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]", (
        f"importing the revision scripts built the settings singleton: {completed.stdout.strip()}"
    )
