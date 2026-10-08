"""How an Alembic environment of this platform migrates a database.

Shared by the open chain (``apps/backend/alembic``) and any further chain an
installation adds with its own version table: one migration mutex for every
runner on a database, a login report, and one transaction per revision.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from alembic.config import Config
from alkera_core.config import settings
from alkera_core.db.tls import psycopg_connect_args
from sqlalchemy import Connection, MetaData, engine_from_config, pool, text

from backend.migration_safety import RUNBOOK, login_bypasses_row_security

log = logging.getLogger("alembic.env")

#: A fixed, arbitrary key for the migration mutex (see ``_acquire_migration_lock``).
#: Any two runners that share this database contend on this one advisory lock,
#: whichever chain they run, so two chains never migrate one database at once.
MIGRATION_LOCK_KEY = 0x616C6B72  # "alkr"

#: Alembic's ``include_object`` hook: which schema objects a chain owns.
IncludeObject = Callable[[Any, str | None, str, bool, Any], bool]


class MigrationEnvironment(Protocol):
    """What a runner uses of Alembic's environment. An ``env.py`` passes the
    ``alembic.context`` module itself, which proxies the running
    ``EnvironmentContext``; a test may pass an ``EnvironmentContext``."""

    config: Config
    configure: Callable[..., None]
    begin_transaction: Callable[[], AbstractContextManager[Any]]
    run_migrations: Callable[..., None]


def _acquire_migration_lock(connection: Connection) -> None:
    """Serialize concurrent migration runners on one session-level advisory lock.

    Two runners against one database (every replica's init container on a Helm
    roll, two CI legs) must never apply the same revision at once, or they race
    on the same DDL. The loser waits here; by the time it holds the lock the
    winner has stamped head, so Alembic finds nothing to do and it exits 0.

    It is SESSION-level (``pg_try_advisory_lock``), not transaction-level: held
    by the CONNECTION for the WHOLE run, so it spans every revision including
    any that opens an ``autocommit_block()`` -- a transaction-level lock would be
    released the instant such a block suspends the surrounding transaction.
    Acquired OUTSIDE Alembic's transaction so it covers the entire run, and
    released explicitly by the caller.

    Polled rather than blocked on, so the wait is bounded by a setting, says so
    in the log, and ends in a message an operator can act on rather than in a
    connection that hangs for as long as the other runner does.
    """
    deadline = time.monotonic() + settings.migration_runner_wait_seconds
    waiting = False
    while True:
        held = connection.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}
        ).scalar()
        # Commit so the probe's transaction closes before Alembic opens its own;
        # the SESSION lock is held by the connection regardless of that commit.
        connection.commit()
        if held:
            if waiting:
                log.info("the other migration runner finished; this one now holds the lock")
            return
        if not waiting:
            waiting = True
            log.info(
                "another `alembic` run holds the migration lock on this database; "
                "waiting up to %ss for it to finish (MIGRATION_RUNNER_WAIT_SECONDS)",
                settings.migration_runner_wait_seconds,
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "gave up waiting for the migration lock after "
                f"{settings.migration_runner_wait_seconds}s: another `alembic` run is still "
                "migrating this database (or a crashed one left its session open -- look for "
                "it in pg_stat_activity). Nothing was changed by this run."
            )
        time.sleep(0.5)


def _report_login(connection: Connection) -> None:
    """Say, on every run, whether this login can carry out a data migration.

    Additive revisions run under any owner, so this only warns; a revision that
    rewrites rows under a row-security policy refuses for itself.
    """
    bypasses = login_bypasses_row_security(connection)
    login = connection.execute(text("SELECT current_user")).scalar()
    connection.commit()
    if bypasses:
        log.info("migrating as %r, which bypasses row-level security", login)
    else:
        log.warning(
            "migrating as %r, which does NOT bypass row-level security: a revision that "
            "back-fills a Files tenant table will refuse to run. See %s.",
            login,
            RUNBOOK,
        )


def _migrate(
    context: MigrationEnvironment,
    connection: Connection,
    target_metadata: MetaData,
    version_table: str,
    include_object: IncludeObject | None,
) -> None:
    """Run the revisions over ``connection``, one runner at a time.

    Every revision runs in a transaction of its own
    (``transaction_per_migration``), and its version stamp is written in that
    same transaction. That is what keeps the stamp and the schema together when
    a run fails part way: a revision that leaves its transaction for an
    ``autocommit_block`` (``backend.migration_safety``) commits everything the
    surrounding transaction holds at that moment, and with one transaction for
    the whole run that would include the stamps of every revision before it. A
    later revision's failure then rolls the stamp back past DDL that is already
    committed, and the next ``upgrade`` steps over a revision whose schema is no
    longer there. Per revision, a failure takes back only the revision it
    happened in, and the stamp names the last one whose DDL is complete.
    """
    _acquire_migration_lock(connection)
    try:
        _report_login(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transaction_per_migration=True,
            version_table=version_table,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        # A revision that raised has had its own transaction rolled back by
        # Alembic; anything else still open on the connection goes with it,
        # so the unlock below runs on a clean session.
        connection.rollback()
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
        connection.commit()


def run_offline(
    context: MigrationEnvironment,
    target_metadata: MetaData,
    version_table: str = "alembic_version",
    include_object: IncludeObject | None = None,
) -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        transaction_per_migration=True,
        version_table=version_table,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online(
    context: MigrationEnvironment,
    target_metadata: MetaData,
    version_table: str = "alembic_version",
    include_object: IncludeObject | None = None,
) -> None:
    """Migrate the database the settings name, or a connection the caller hands in.

    A caller driving Alembic in-process may pass an open connection as
    ``config.attributes["connection"]`` (Alembic's own convention for it) to
    migrate a database other than the one this process's settings are bound
    to -- the test suite migrates throwaway copies of its database that way.
    The caller owns that connection; it is used, never closed, here.
    """
    config = context.config
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _migrate(context, supplied, target_metadata, version_table, include_object)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=psycopg_connect_args(),
    )
    with connectable.connect() as connection:
        _migrate(context, connection, target_metadata, version_table, include_object)
