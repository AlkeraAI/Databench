"""Fail-closed guard: a test session may only touch a DISPOSABLE database, and the
per-xdist-worker databases a parallel session provisions for itself.

The suite has no per-test rollback. Fixtures commit real rows, and a good number of
them clear whole tables to get a deterministic count — ``delete(ComputeAllocation)``,
``delete(ComputeMachineType)``, ``delete(PlatformUsageMonth)``, ``delete(EnterprisePlan)``
and friends. Not one of them knows WHICH database it is pointed at, and the default
one is the workspace's own dev database (``make`` generates ``.env.workspace``, which
names it), so a bare ``pytest`` with no DSN exported is destructive by default. That
is not hypothetical: one such run recreated ``compute_machine_types`` with fresh ids,
deleted a live compute grant and left thousands of test orgs behind in the database a
running app was serving.

So the decision is made ONCE, from data, before anything connects: a database is
disposable when its NAME matches one of :data:`DISPOSABLE_DATABASE_PATTERNS`, or when
:data:`ALLOW_ENV_VAR` names it exactly. Everything else is refused — a database whose
name we cannot even read most of all.

The patterns keep their separators (``alkera_test_*`` rather than ``alkera_test*``,
``alkera_gw[0-9]*`` rather than ``alkera_gw*``) so that the allowlist cannot widen into
"anything that starts like a test database": ``alkera_gateway`` is a plausible database
name, and it is not a test database.

Nothing here imports :mod:`alkera_core.config`. The root ``conftest.py`` calls it
before any app import, precisely so no settings instance is cached against the wrong
database; an import of the settings singleton from here would defeat that.

:func:`provision_worker_database` lives here for the same reason: it is the other half
of the same decision — having proven a database disposable, it is what gives each
parallel worker a private one — and it must stay importable without pulling in the
settings singleton the guard exists to protect. So does
:func:`provision_migration_template`, which gives a session the one database its
migration tests copy: a database nothing in the session connects to.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shlex
import socket
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from alkera_core.process import SpawnSpec, run

#: Names ONE database (exactly — no patterns) that this session may destroy anyway.
#: The escape hatch for a database the patterns do not cover.
ALLOW_ENV_VAR = "ALKERA_TEST_ALLOW_DB"

#: The database names a test session may write to. Data, not a chain of ifs: the
#: refusal quotes this list, and a caller with its own throwaway naming passes its own.
#:
#: - ``alkera_test`` / ``alkera_test_*`` — CI's throwaway database, and the
#:                                         ``alkera_test_suite`` ``make test-py`` provisions.
#: - ``alkera_lane_*``                   — a parallel worktree's private database.
#: - ``alkera_gw[0-9]*``                 — the per-xdist-worker databases an older root
#:                                         ``conftest.py`` created (``alkera_<worker id>``).
#: - ``*_test_gw[0-9]*``                 — the per-xdist-worker databases the root
#:                                         ``conftest.py`` creates now: the session's own
#:                                         disposable name plus the worker id, so two runs
#:                                         on one Postgres never drop each other's workers.
#: - ``alkera_migtest_*``                — the scratch databases the alembic tests drive.
#: - ``*_test`` / ``test_*``             — the general convention, incl. CI's ``alkera_test``.
DISPOSABLE_DATABASE_PATTERNS: tuple[str, ...] = (
    "alkera_test",
    "alkera_test_*",
    "alkera_lane_*",
    "alkera_gw[0-9]*",
    "*_test_gw[0-9]*",
    "alkera_migtest_*",
    "*_test",
    "test_*",
)

#: The database ``make test-py`` provisions and runs against. Named apart from the
#: connector suites' ``alkera_test`` warehouse so the two never share a database.
DEFAULT_TEST_DATABASE = "alkera_test_suite"

_DEFAULT_SYNC_URL = "postgresql+psycopg://alkera:alkera@localhost:5432/alkera"
_DEFAULT_PORT = 5432

#: How long provisioning may spend on a server that is not answering, in seconds.
#: ``ensure`` runs at the head of every ``make`` pytest target and the drift job has
#: no Postgres at all, so "there is nothing there" has to cost seconds. libpq's own
#: minimum is two seconds and it applies the value per connection attempt, so this
#: is a few of them rather than a fraction of one.
UNREACHABLE_SERVER_SECONDS = 3


class NonDisposableDatabaseError(RuntimeError):
    """A test session named a database it has not been cleared to destroy."""


def database_name(url: str | None) -> str | None:
    """The database a URL names, or ``None`` when it names none we can read.

    Fail-closed by construction: an empty, blank or unparseable URL, and one with no
    database component, all come back ``None`` — which every caller here treats as
    "not proven disposable" rather than "probably fine".
    """
    if url is None:
        return None
    text = url.strip()
    if not text:
        return None
    try:
        parsed = make_url(text)
    except (ArgumentError, ValueError):
        return None
    name = (parsed.database or "").strip()
    return name or None


def worker_database_name(base: str | None, worker: str) -> str:
    """The database an xdist worker provisions for itself.

    Scoped under the session's OWN disposable name (``alkera_test_suite`` →
    ``alkera_test_suite_gw0``, ``alkera_lane_b`` → ``alkera_lane_b_gw0``): every
    worker drops and recreates its database at import, so a name shared across
    sessions (the old ``alkera_gw0``) let two runs on one Postgres — two worktrees,
    two lanes — destroy each other mid-run. A base the session could not read
    falls back to the old spelling, which the allowlist still admits.
    """
    if not base:
        return f"alkera_{worker}"
    return f"{base}_{worker}"


def is_disposable_database(
    name: str | None,
    *,
    patterns: Sequence[str] = DISPOSABLE_DATABASE_PATTERNS,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Whether a database of this name may be destroyed by a test session."""
    if not name:
        return False
    allowed = (os.environ if env is None else env).get(ALLOW_ENV_VAR)
    if allowed and allowed == name:
        return True
    folded = name.lower()
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in patterns)


def assert_disposable_database(
    url: str | None,
    *,
    setting: str = "DATABASE_URL",
    patterns: Sequence[str] = DISPOSABLE_DATABASE_PATTERNS,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the database ``url`` names, having proven it disposable.

    Raises :class:`NonDisposableDatabaseError` otherwise, naming the database and both
    ways forward. The message never carries the password.
    """
    name = database_name(url)
    if is_disposable_database(name, patterns=patterns, env=env):
        assert name is not None  # is_disposable_database is False for None
        return name
    raise NonDisposableDatabaseError(_refusal(url, name, setting=setting, patterns=patterns))


def server_reachable(url: str | None, *, timeout: float = 1.5) -> bool:
    """Whether a TCP connection to the URL's host opens inside ``timeout``.

    Used only to decide whether there is anything to protect: a database on a server
    nothing is listening on cannot be damaged, so a session that never connects (the
    file-semantics subsets, the doc/snapshot generators) is not worth aborting. Fails
    CLOSED — a URL we cannot read a host out of counts as reachable.
    """
    if url is None:
        return True
    try:
        parsed = make_url(url.strip())
    except (ArgumentError, ValueError):
        return True
    host = parsed.host
    if not host:
        return True
    try:
        with socket.create_connection((host, parsed.port or _DEFAULT_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _redacted(url: str | None) -> str:
    """The URL with its password removed, or a placeholder when it cannot be read."""
    if url is None:
        return "<unset>"
    try:
        return make_url(url.strip()).render_as_string(hide_password=True)
    except (ArgumentError, ValueError):
        return "<unreadable>"


def _refusal(
    url: str | None,
    name: str | None,
    *,
    setting: str,
    patterns: Sequence[str],
) -> str:
    if name is None:
        detail = f"{setting} names no database we can read ({_redacted(url)})"
    else:
        detail = f'{setting} names the database "{name}" ({_redacted(url)})'
    example = name or DEFAULT_TEST_DATABASE
    return (
        f"tests run only against a disposable database, and this is not one: {detail}.\n"
        "\n"
        "This suite has no per-test rollback: its fixtures commit real rows and several "
        "of them clear whole tables, so it must never run against a database anything "
        "else is using — the dev database `make dev-all` serves, a demo, staging, prod.\n"
        "\n"
        "Two ways forward:\n"
        "  1. point DATABASE_URL and DATABASE_URL_SYNC at a disposable database — one "
        "whose name matches " + ", ".join(patterns) + ".\n"
        "     `make test-py` does it for you: it creates, migrates and runs against "
        f"`{DEFAULT_TEST_DATABASE}` on this workspace's Postgres.\n"
        f"  2. if you really do mean this database, name it exactly: {ALLOW_ENV_VAR}={example}"
    )


# --- the private database one xdist worker runs against -------------------------------


#: How long the fallback migration below may run before it is called a failure. It is a
#: whole ``alembic upgrade head`` on a cold database — 137 revisions today — on a box
#: already running one worker per core, so the budget is deliberately generous: it is
#: here so that a wedged migration surfaces as an error instead of a hung session, not
#: to police how long a slow machine takes.
WORKER_MIGRATION_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class WorkerDatabase:
    """The private database one xdist worker runs its session against."""

    #: The database's name on the server, e.g. ``alkera_test_suite_gw3``.
    name: str
    #: The two DSNs to export, as ``DATABASE_URL_SYNC`` and ``DATABASE_URL``.
    sync_url: str
    async_url: str
    #: The ``postgres`` database on the same server — where a ``CREATE``/``DROP``
    #: DATABASE for this one is issued from, since neither may run from inside it.
    maintenance_url: str
    #: Whether this database had to be migrated to head itself, because the template
    #: was behind (or absent). ``False`` on the fast path: it arrived at head.
    migrated: bool
    #: The base this database was copied from, when the copy arrived at head — which
    #: proves the base is at head too, and nothing connects to the base for the rest
    #: of a parallel run, so it is the database the worker's migration tests may take
    #: THEIR copies from (:func:`provision_migration_template`). ``None`` when the
    #: clone had to be migrated: the base was behind or absent, and a copy of it would
    #: be too.
    template: str | None = None


def drop_database(maintenance_url: str, name: str, *, setting: str) -> None:
    """Drop a database, having first proven the name disposable.

    The one statement in the suite that destroys a whole database, so the proof is
    taken here and never on the caller's word — ``setting`` only decides how the
    refusal names it. ``WITH (FORCE)`` terminates whatever is still connected
    (Postgres 13+), which is what makes a crashed worker's leftovers reclaimable.
    """
    assert_disposable_database(_with_database(maintenance_url, name), setting=setting)
    _autocommit_ddl(maintenance_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def provision_worker_database(base_url: str, worker: str, *, alembic_root: Path) -> WorkerDatabase:
    """Give one xdist worker a private database at head, and return its DSNs.

    The database is a CLONE of the one ``base_url`` names — ``CREATE DATABASE …
    TEMPLATE`` — rather than an empty database plus ``alembic upgrade head``. Every
    ``make`` pytest target brings the base to head before pytest starts, so the schema
    each worker needs already exists on this server, and copying it costs a fraction
    of replaying 137 revisions per worker: on a 32-worker Windows shard those
    concurrent migrations were about 45 seconds of every shard's startup.

    Postgres refuses to copy a template some other session is connected to, and the
    clones all run at once. Nothing connects to the base during a parallel run: the
    ``make`` target's own migration is a subprocess that has exited by the time pytest
    starts, each worker's maintenance connection is to ``postgres``, and a worker is
    redirected at its clone before it opens a single application connection. Copying
    one template many times over at once is allowed — the share lock a copy takes on
    its template is there to keep the template from being dropped underneath it, and
    it does not conflict with another copy's.

    Fail closed on drift. A developer's base is whatever their last run left, so the
    clone's stamped revision is compared with the script directory's head and a clone
    that came out behind is migrated the old way. Without that check a stale base would
    quietly make the whole suite prove itself against an old schema — green, and
    meaningless. The same fallback covers a base that does not exist yet (a bare
    ``uv run pytest -n`` against a server nothing has provisioned): there is then
    nothing to copy, so the worker gets an empty database and migrates it.

    A clone carries the base's ROWS as well as its schema. That is the arrangement the
    serial path already has — ``make test-py ARGS=-p no:xdist``, ``make e2e`` and
    ``make live`` all run against the base and none of them drops it afterwards, so a
    test that cannot tolerate rows an earlier run left is already failing serially. It
    does mean a clone costs what the base HOLDS, which is a reason to drop a base that
    a long series of serial runs has grown: every ``make`` pytest target recreates and
    migrates it.
    """
    template = assert_disposable_database(base_url, setting="the template database")
    name = worker_database_name(template, worker)
    maintenance_url = _with_database(base_url, "postgres")
    sync_url = _with_database(base_url, name)
    async_url = _with_database(base_url, name, driver="postgresql+asyncpg")
    # Proven before the DROP below, not after: the worker id arrives from the
    # environment, and a name that stopped looking like a worker database must never
    # reach a statement that destroys one.
    assert_disposable_database(sync_url, setting=f"the {worker} worker database")

    drop_database(maintenance_url, name, setting=f"the {worker} worker database")
    _create_from_template(maintenance_url, name, template)

    head = _script_head(alembic_root)
    migrated = head is None or _stamped_revisions(sync_url) != {head}
    if migrated:
        _upgrade_to_head(sync_url, async_url, alembic_root)
    return WorkerDatabase(
        name=name,
        sync_url=sync_url,
        async_url=async_url,
        maintenance_url=maintenance_url,
        migrated=migrated,
        template=None if migrated else template,
    )


def _with_database(url: str, name: str, *, driver: str | None = None) -> str:
    """The same server, pointed at another database (and optionally another driver)."""
    parsed = make_url(url).set(database=name)
    if driver is not None:
        parsed = parsed.set(drivername=driver)
    return parsed.render_as_string(hide_password=False)


def _autocommit_ddl(maintenance_url: str, statement: str) -> None:
    """Run one DDL statement against the maintenance database.

    ``CREATE``/``DROP DATABASE`` cannot run inside a transaction block, hence
    AUTOCOMMIT.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


def _create_from_template(maintenance_url: str, name: str, template: str) -> None:
    """Create ``name`` as a copy of ``template``, or empty when there is no template."""
    from sqlalchemy import create_engine, text

    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": template}
            ).scalar()
            clause = f' TEMPLATE "{template}"' if exists else ""
            conn.execute(text(f'CREATE DATABASE "{name}"{clause}'))
    finally:
        engine.dispose()


def _script_head(alembic_root: Path) -> str | None:
    """The revision the migration scripts end at, or ``None`` when there are none.

    Built from an absolute ``script_location`` alone rather than from ``alembic.ini``:
    reading the head needs nothing else the file configures, and loading it here would
    apply its ``prepend_sys_path`` to this process for no reason. The migration itself
    runs the ini through the real alembic command, in its own directory.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(alembic_root / "alembic"))
    head: str | None = ScriptDirectory.from_config(config).get_current_head()
    return head


def _stamped_revisions(sync_url: str) -> set[str]:
    """The revisions a database says it is at — empty when it was never migrated."""
    from sqlalchemy import create_engine, text

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT to_regclass('alembic_version')")).scalar() is None:
                return set()
            return {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}
    finally:
        engine.dispose()


def _upgrade_to_head(sync_url: str, async_url: str, alembic_root: Path) -> None:
    """Run ``alembic upgrade head`` against exactly the database these DSNs name.

    In a SUBPROCESS, and not ``alembic.command.upgrade`` in this one, because the
    backend's ``env.py`` sets ``sqlalchemy.url`` from the settings singleton and
    ignores whatever the caller put in the Config. In-process, the database that gets
    migrated is therefore whichever one this process's settings were first built
    against — the worker's own when provisioning runs before any application import
    (it does), but the session's own when anything built settings earlier, which is
    every caller that is not the root conftest. A child process builds its settings
    from the environment handed to it, so this can only ever reach the database named
    here.
    """
    import shutil

    scripts = str(Path(sys.executable).parent)
    alembic = shutil.which("alembic", path=scripts)
    if alembic is None:
        raise RuntimeError(f"the alembic console script is not in {scripts}")
    completed = run(
        SpawnSpec(
            argv=[alembic, "upgrade", "head"],
            cwd=str(alembic_root),
            env={**os.environ, "DATABASE_URL_SYNC": sync_url, "DATABASE_URL": async_url},
            stdout="pipe",
            stderr="pipe",
        ),
        timeout=WORKER_MIGRATION_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        said = (completed.stdout + b"\n" + completed.stderr).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"could not migrate {database_name(sync_url)} to head "
            f"(alembic exited {completed.returncode}):\n{said}"
        )


# --- the database a session's migration tests copy -------------------------------------
#
# A migration test moves a schema down and back up, so it runs on a throwaway copy
# (``CREATE DATABASE … TEMPLATE``) rather than on the database the session runs on.
# Postgres copies a template only while no other session is connected to it, and the
# session's own database is never free of connections once the first pool has opened:
# ``create_async_engine`` keeps its checked-in connections alive between tests, so an
# idle pool from ANY earlier test on the worker blocks a copy of the worker's database
# for as long as the worker lives. The copy therefore comes from a database nothing in
# the session ever connects to — the template below — which is either borrowed (under
# xdist, the base every worker was cloned from) or made once per session from the
# session's own database before its first pool opens.

#: The name family of a session's own migration template — under ``alkera_migtest_*``,
#: which the allowlist admits and nothing but the migration tests use.
MIGRATION_TEMPLATE_PREFIX = "alkera_migtest_template_"

#: Postgres truncates an identifier longer than this, silently, to this many bytes.
_MAX_IDENTIFIER_BYTES = 63

#: ``object_in_use``: the SQLSTATE Postgres answers a ``CREATE DATABASE … TEMPLATE``
#: with while another session is connected to the template.
_OBJECT_IN_USE = "55006"


class MigrationTemplateInUseError(RuntimeError):
    """A migration template could not be copied: something was connected to it."""


@dataclass(frozen=True, slots=True)
class MigrationTemplate:
    """A database at head that nothing in the session connects to.

    There is deliberately no DSN to it here — the one thing a holder of this object
    must never do is open a connection on it — only its name, and the maintenance
    database a copy of it is issued from.
    """

    name: str
    maintenance_url: str
    #: ``True`` when this session created the template and drops it at its end;
    #: ``False`` when it borrowed one that outlives the session (the base a parallel
    #: run's workers were cloned from).
    owned: bool


def migration_template_name(session_database: str) -> str:
    """The template a session running on ``session_database`` makes for itself.

    Deterministic, so a session that dies before its teardown leaves ONE leftover,
    under a name the next session on the same database replaces — the arrangement
    the worker databases already have. Kept inside Postgres's identifier length: a
    name it would truncate is shortened with a digest of the whole, so two long
    session names that share a prefix cannot resolve to one template.
    """
    name = f"{MIGRATION_TEMPLATE_PREFIX}{session_database}"
    if len(name.encode()) <= _MAX_IDENTIFIER_BYTES:
        return name
    import hashlib

    digest = hashlib.blake2b(session_database.encode(), digest_size=6).hexdigest()
    room = _MAX_IDENTIFIER_BYTES - len(MIGRATION_TEMPLATE_PREFIX) - len(digest) - 1
    return f"{MIGRATION_TEMPLATE_PREFIX}{session_database[:room]}_{digest}"


def provision_migration_template(
    session_sync_url: str, *, borrow: str | None = None
) -> MigrationTemplate:
    """The database this session's migration tests copy, borrowed or made.

    ``borrow`` names a database already proven to be at head that nothing in this
    session will connect to — under xdist, the base the worker's own database was
    cloned from (:attr:`WorkerDatabase.template`). It is used as it is, and never
    dropped by this session.

    Otherwise the session's own database is copied NOW, under
    :func:`migration_template_name`, replacing a leftover of that name. The copy has
    to be taken before anything in the session opens a connection on that database;
    the root ``conftest.py`` takes it at the end of collection, when every test module
    has been imported and no fixture has run. Anything connected at that point is a
    defect at session start, so it is named in the error rather than waited out.
    """
    maintenance_url = _with_database(session_sync_url, "postgres")
    if borrow is not None:
        assert_disposable_database(
            _with_database(session_sync_url, borrow), setting="the migration template"
        )
        return MigrationTemplate(name=borrow, maintenance_url=maintenance_url, owned=False)

    source = assert_disposable_database(session_sync_url, setting="the session database")
    name = migration_template_name(source)
    setting = "the session's migration template"
    drop_database(maintenance_url, name, setting=setting)
    copy_database(maintenance_url, source, name)
    return MigrationTemplate(name=name, maintenance_url=maintenance_url, owned=True)


def copy_database(maintenance_url: str, template: str, name: str) -> None:
    """``CREATE DATABASE name TEMPLATE template``, or say who is connected to the template.

    A refusal names every session on the template — pid, application name, state and
    the last statement — because the fix is always to find the holder, never to wait:
    a pooled connection stays idle until its engine is disposed, and terminating it
    would take a connection a test elsewhere is about to reuse.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError

    from alkera_core.db.errors import sqlstate_of

    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            try:
                conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
            except DBAPIError as error:
                if sqlstate_of(error) != _OBJECT_IN_USE:
                    raise
                holders = conn.execute(
                    text(
                        "SELECT pid, application_name, state, left(query, 160) "
                        "FROM pg_stat_activity WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": template},
                ).all()
                raise MigrationTemplateInUseError(
                    f"cannot copy {template} while other sessions are connected to it: "
                    f"{[tuple(row) for row in holders]}. A database is copied only while "
                    "nothing else is connected: a migration template is never connected to "
                    "by the session, and the session's own database is free of connections "
                    "only until its first test runs."
                ) from error
    finally:
        engine.dispose()


def drop_migration_template(template: MigrationTemplate) -> None:
    """Drop a template this session made; a borrowed one is left to its owner."""
    if template.owned:
        drop_database(
            template.maintenance_url, template.name, setting="the session's migration template"
        )


# The per-process record of this session's template. The root ``conftest.py`` is the
# writer: it publishes the borrowed base at import on an xdist worker, and otherwise
# makes one at the end of collection — but only when a migration test module asked
# for one at import, so a session with no migration test never pays for a copy.
_MIGRATION_TEMPLATE: MigrationTemplate | None = None
_MIGRATION_TEMPLATE_REQUESTED = False


def request_migration_template() -> None:
    """Say, at import, that this session will need a migration template.

    Called by the migration harness when it is imported, which a test module does
    at ITS import — i.e. during collection, before any test has opened a pool. That
    is what lets the conftest still make a copy of the session's database.
    """
    global _MIGRATION_TEMPLATE_REQUESTED
    _MIGRATION_TEMPLATE_REQUESTED = True


def migration_template_requested() -> bool:
    return _MIGRATION_TEMPLATE_REQUESTED


def publish_migration_template(template: MigrationTemplate) -> None:
    global _MIGRATION_TEMPLATE
    _MIGRATION_TEMPLATE = template


def published_migration_template() -> MigrationTemplate | None:
    """The template this session prepared, or ``None`` when it prepared none."""
    return _MIGRATION_TEMPLATE


# --- `python -m alkera_core.db.testing ensure <name>` ---------------------------------
# Used by the Makefile's pytest targets: it prints the DSNs for a disposable database on
# the server the workspace is already configured for, so no test target has to inherit
# the dev database by default.


def _base_url(explicit: str | None) -> str:
    return (
        explicit
        or os.environ.get("DATABASE_URL_SYNC")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_SYNC_URL
    )


def _create_database_if_missing(url: URL) -> None:
    """Best effort: create the database if the server is up and does not have it.

    Deliberately forgiving — a target that only needs the NAME to be disposable (the
    snapshot generators) must keep working with no server running at all. A real
    failure surfaces at the first connection instead.

    Forgiving, but bounded, and bounded twice because there are two ways for a server
    that is not there to hold a ``make`` target open. A plain socket answers "is
    anything listening" in one dial with a timeout we choose, which is the question
    the driver is otherwise asked to answer with whatever its platform does about a
    connection that never completes — one such attempt took 130 s on a Windows runner
    where the same refused socket took two. And a host that does accept TCP but never
    speaks the protocol gets past that dial, so the connection itself carries
    ``connect_timeout``; without it libpq waits for the startup reply forever.
    """
    from sqlalchemy import create_engine, text

    name = url.database
    assert name is not None
    dsn = url.render_as_string(hide_password=False)
    if not server_reachable(dsn, timeout=UNREACHABLE_SERVER_SECONDS):
        print(
            f"# could not provision {name}: nothing is listening on {_redacted(dsn)}",
            file=sys.stderr,
        )
        return
    engine = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": UNREACHABLE_SERVER_SECONDS},
    )
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # any driver/server error is non-fatal here
        print(f"# could not provision {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        engine.dispose()


def _ensure(name: str, *, base: str | None) -> int:
    try:
        url = make_url(_base_url(base))
    except (ArgumentError, ValueError) as exc:
        print(f"cannot read a database URL from the environment: {exc}", file=sys.stderr)
        return 2
    sync = url.set(drivername="postgresql+psycopg", database=name)
    asynchronous = url.set(drivername="postgresql+asyncpg", database=name)
    try:
        assert_disposable_database(
            sync.render_as_string(hide_password=False), setting="the requested test database"
        )
    except NonDisposableDatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _create_database_if_missing(sync)
    print(f"DATABASE_URL={shlex.quote(asynchronous.render_as_string(hide_password=False))}")
    print(f"DATABASE_URL_SYNC={shlex.quote(sync.render_as_string(hide_password=False))}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alkera_core.db.testing",
        description="Provision a disposable test database and print its DSNs for `eval`.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure", help="create (if missing) a disposable database")
    ensure.add_argument("name", nargs="?", default=DEFAULT_TEST_DATABASE)
    ensure.add_argument(
        "--from-url",
        default=None,
        help="the server to place it on (default: DATABASE_URL_SYNC / DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    name: str = args.name
    base: str | None = args.from_url
    return _ensure(name, base=base)


if __name__ == "__main__":  # pragma: no cover -- exercised through the Makefile
    raise SystemExit(main())
