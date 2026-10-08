"""Driving a real Alembic revision from a test, on a database of the test's own.

A migration test is the one kind of test that changes a schema, and this suite
runs one database per xdist worker with no per-test rollback. A test that moved
that database down and back left every later test on the worker reading
whatever the round trip left behind -- and a round trip can leave a lot behind.
A downgrade that fails part way stops where it stops; a revision that commits
half of itself through an ``autocommit_block`` and then raises is stamped as
still applied, so the upgrade back to head steps over it and the worker keeps a
CHECK, a grant or an index from the past for the rest of the run. Every test on
the worker that then writes an ``asleep`` allocation, or a Files request that
appends to the outbox, fails for a reason none of them caused.

So a test never migrates the worker's database. :func:`migration_scratch` makes
a copy of the session's *migration template* -- ``CREATE DATABASE ... TEMPLATE``
-- and hands back a :class:`ScratchDatabase`: an Alembic ``Config`` and a
session factory bound to the copy and nothing else. The test seeds, downgrades,
upgrades and asserts there, and the copy is dropped on the way out whatever
happened inside. :func:`downgraded_to` is the same thing with the downgrade
already done, for a test that has nothing to seed at head first.

The copy is NOT taken from the worker's database, although that is the one at
hand. Postgres copies a database only while no other session is connected to
it, and the worker's database is never free of connections once the first
test has run: every ``create_async_engine`` keeps its checked-in connections
open between tests, so an idle pool left by any earlier module on the worker
blocks the copy for the rest of the run -- which is exactly how every
migration test failed behind a full worker. The template is a database at head
that nothing in the session connects to: under xdist the base every worker was
cloned from, and otherwise a copy of the session's own database the root
conftest takes at the end of collection, before the first pool opens (see
``alkera_core.db.testing.provision_migration_template``). This module asks for
one at import, which is why a test module imports it at module level.

So the copy holds the template's rows and not the worker's: a row a test wrote
through the app before the trip is not in it, and whatever a trip needs is
seeded inside it (:func:`seed_org_admin` makes the org a route test logs in
to). The template's residue can still include rows a downgrade refuses on
purpose: a narrowing revision will not remap a value its older vocabulary
cannot name, and 0106's path index cannot hold a path much past sixty labels.
:meth:`ScratchDatabase.downgrade` moves exactly those rows out of the way first
-- in the copy, which holds no history worth keeping -- and the guards
themselves stay live: a row the test writes after the downgrade is refused as
it would be in production.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alkera_core.config import settings
from alkera_core.db import session as db_session
from alkera_core.db.testing import (
    MigrationTemplate,
    assert_disposable_database,
    copy_database,
    drop_database,
    published_migration_template,
    request_migration_template,
)
from alkera_core.db.tls import asyncpg_connect_args, psycopg_connect_args
from sqlalchemy import Connection, Engine, create_engine, pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from tests.conftest import OrgWithAdmin

# Importing this module is a test module's way of saying it will take a copy:
# the root conftest reads this at the end of collection, the last moment a copy
# of the session's own database can still be made.
request_migration_template()

_BACKEND = Path(__file__).resolve().parents[1]

#: Every copy is named into the ``alkera_migtest_*`` family, which the root
#: conftest's disposable-database allowlist admits and nothing else uses.
SCRATCH_PREFIX = "alkera_migtest_trip_"


def alembic_config() -> Config:
    """The backend's Alembic config over the worker's own database.

    For reading the script directory and for ``alembic check``, which migrate
    nothing. A test that moves the schema does it through a
    :class:`ScratchDatabase`'s ``config`` instead.
    """
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url_sync)
    return config


def script_head(config: Config | None = None) -> str:
    """The revision the migration scripts on disk end at."""
    head = ScriptDirectory.from_config(config or alembic_config()).get_current_head()
    assert head is not None, "the migration tree names no head"
    return head


def current_revision(url: str | None = None) -> str | None:
    """The revision a database is stamped with -- the worker's unless ``url`` says.

    Read on a fresh short-lived sync engine of its own, so it answers from a
    ``finally`` that is unwinding a failure and never borrows a pooled
    connection that may be mid-teardown.
    """
    engine = create_engine(url or settings.database_url_sync, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _with_database(url: str, name: str) -> str:
    return make_url(url).set(database=name).render_as_string(hide_password=False)


def _maintenance_url() -> str:
    """The server's ``postgres`` database, where a database is created and dropped."""
    return _with_database(settings.database_url_sync, "postgres")


def migration_template() -> MigrationTemplate:
    """The database this session's trips copy, or a refusal that says how to get one.

    The root conftest publishes it: the base a parallel run's workers were cloned
    from, or a copy of the session's database taken at the end of collection. A
    session that has neither reached this point through a module that imported
    this harness lazily, after collection -- too late for a copy to be taken --
    or through a pytest run without the repo's root conftest.
    """
    template = published_migration_template()
    if template is None:
        raise RuntimeError(
            "no migration template was prepared for this session, so a trip has no "
            "database to copy. The root conftest prepares one at the end of collection "
            "when tests.migration_harness is loaded: import it at module level (not "
            "inside a test), and run under the repo's root conftest."
        )
    return template


#: Revision 0107's downgrade rebuilds the pre-0107 path index, a GiST index over
#: the whole ``file_nodes.path_ids`` value; a GiST tuple has to fit in one page,
#: so the rebuild fails outright while any path much past sixty labels exists.
_INDEX_REBUILDING_REVISION = "0107"
#: How deep a path may be for that index to hold it. A downgrade test steps
#: over 0107 on a worker database other modules wrote to, and the chains the
#: perf and readable-ids rows build are residue, not fixtures the test owns.
DEEPEST_INDEXABLE_PATH = 64


#: The tables whose rows the purge deletes outright. Every foreign key pointing
#: at one of them has to be cleared first, so a test holds this list against the
#: live catalogue: a reference added later that nothing here takes care of is a
#: ``ForeignKeyViolation`` that aborts the whole purge, and the guard turns that
#: into a named failure before CI has to find it the expensive way.
PURGE_ROOTS: tuple[str, ...] = (
    "file_nodes",
    "file_versions",
    "file_leases",
    "file_upload_sessions",
)

_MATERIALISE_DOOMED: tuple[str, ...] = (
    """
    CREATE TEMP TABLE doomed_file_nodes ON COMMIT DROP AS
    SELECT id FROM file_nodes WHERE nlevel(path_ids) > :depth
    """,
    """
    CREATE TEMP TABLE doomed_file_versions ON COMMIT DROP AS
    SELECT id FROM file_versions WHERE node_id IN (SELECT id FROM doomed_file_nodes)
    """,
)

#: ``(table, statement)`` in the order the constraints admit: what points at the
#: doomed nodes' versions and leases before those go, then what points at the
#: nodes, then the nullable back-references held by rows that survive. The table
#: is carried beside the statement so the catalogue guard can read the coverage
#: off the same list the purge runs, rather than off a second copy of it.
_CLEAR_REFERENCES: tuple[tuple[str, str], ...] = (
    # A version is referenced from further away than its own node: a grant
    # minted over it, a conflict naming it as one of three sides.
    (
        "file_content_grants",
        "DELETE FROM file_content_grants WHERE version_id IN (SELECT id FROM doomed_file_versions)",
    ),
    (
        "file_conflicts",
        """
        DELETE FROM file_conflicts
        WHERE node_id IN (SELECT id FROM doomed_file_nodes)
           OR base_version_id IN (SELECT id FROM doomed_file_versions)
           OR mine_version_id IN (SELECT id FROM doomed_file_versions)
           OR theirs_version_id IN (SELECT id FROM doomed_file_versions)
        """,
    ),
    # A lease is keyed by its node, so the rows hanging off a lease go before
    # the lease row the node is about to take with it.
    (
        "file_stage_jobs",
        "DELETE FROM file_stage_jobs WHERE lease_node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    (
        "file_lease_live_entries",
        """
        DELETE FROM file_lease_live_entries
        WHERE node_id IN (SELECT id FROM doomed_file_nodes)
           OR lease_node_id IN (SELECT id FROM doomed_file_nodes)
        """,
    ),
    # The node points back at its own head version, so the pointer is dropped
    # before the version row it names.
    (
        "file_nodes",
        "UPDATE file_nodes SET head_version_id = NULL"
        " WHERE head_version_id IN (SELECT id FROM doomed_file_versions)",
    ),
    (
        "file_versions",
        "DELETE FROM file_versions WHERE node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    (
        "file_upload_parts",
        """
        DELETE FROM file_upload_parts
        WHERE session_id IN (
            SELECT id FROM file_upload_sessions
            WHERE node_id IN (SELECT id FROM doomed_file_nodes)
               OR parent_id IN (SELECT id FROM doomed_file_nodes)
        )
        """,
    ),
    (
        "file_upload_sessions",
        """
        DELETE FROM file_upload_sessions
        WHERE node_id IN (SELECT id FROM doomed_file_nodes)
           OR parent_id IN (SELECT id FROM doomed_file_nodes)
        """,
    ),
    (
        "file_dir_stats",
        "DELETE FROM file_dir_stats WHERE node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    (
        "file_dir_stats_deltas",
        "DELETE FROM file_dir_stats_deltas WHERE node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    (
        "file_history",
        "DELETE FROM file_history WHERE node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    ("file_holds", "DELETE FROM file_holds WHERE node_id IN (SELECT id FROM doomed_file_nodes)"),
    (
        "file_lease_epoch_hwm",
        "DELETE FROM file_lease_epoch_hwm WHERE node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    ("file_leases", "DELETE FROM file_leases WHERE node_id IN (SELECT id FROM doomed_file_nodes)"),
    ("file_links", "DELETE FROM file_links WHERE node_id IN (SELECT id FROM doomed_file_nodes)"),
    ("file_locks", "DELETE FROM file_locks WHERE node_id IN (SELECT id FROM doomed_file_nodes)"),
    ("file_shares", "DELETE FROM file_shares WHERE node_id IN (SELECT id FROM doomed_file_nodes)"),
    ("file_stars", "DELETE FROM file_stars WHERE node_id IN (SELECT id FROM doomed_file_nodes)"),
    # Nullable references held by rows that are NOT going anywhere. A drive's
    # root is the shallowest row in its own tree, so that one can only ever
    # match nothing -- it is stated so the coverage the guard reads is the whole
    # catalogue rather than the whole catalogue minus one remembered exception.
    (
        "file_drives",
        "UPDATE file_drives SET root_node_id = NULL"
        " WHERE root_node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    (
        "file_ops",
        "UPDATE file_ops SET result_node_id = NULL"
        " WHERE result_node_id IN (SELECT id FROM doomed_file_nodes)",
    ),
    (
        "file_nodes",
        "UPDATE file_nodes SET target_id = NULL"
        " WHERE target_id IN (SELECT id FROM doomed_file_nodes)",
    ),
)

#: The whole doomed set in one statement. ``parent_id`` points at another row of
#: the same table and is checked at the end of the statement, and a child's path
#: is always one label longer than its parent's -- so a doomed node's children
#: are doomed too and go in the same breath.
_DELETE_DOOMED_NODES = "DELETE FROM file_nodes WHERE id IN (SELECT id FROM doomed_file_nodes)"

_DEEPEST_REMAINING = "SELECT coalesce(max(nlevel(path_ids)), 0) FROM file_nodes"


@dataclass(frozen=True)
class PurgeResult:
    """What one purge took out, and how deep the tree is once it has."""

    deleted: int
    deepest_remaining: int


def purge_paths_the_old_index_cannot_hold(
    depth: int = DEEPEST_INDEXABLE_PATH, *, url: str | None = None
) -> PurgeResult:
    """Delete every ``file_nodes`` row deeper than ``depth``, references and all.

    A node written through the Files services is never alone: the write left
    history rows, a version, dir-stat deltas, and any of a dozen other rows that
    name it. Every one of those foreign keys is ``NO ACTION``, so a bare
    ``DELETE FROM file_nodes`` raises ``ForeignKeyViolation`` the moment the
    doomed set contains anything but the raw rows a builder inserted by hand --
    which is exactly how this cleanup failed once the perf directory started
    sharing a worker with modules that drive the real routes.

    So the doomed ids are materialised once and everything that points at them
    is cleared first, in the order the constraints admit: what hangs off their
    versions and leases, then what hangs off the nodes, then the nullable
    back-references held by rows that survive. One transaction, so a failure
    half way through leaves the tree exactly as it found it, and a bounded
    ``lock_timeout`` so a lock somebody else holds is a named failure rather
    than a test that sits until the session times out.

    ``url`` names the database to purge; the worker's own when it is left out.
    """
    sync_engine = create_engine(url or settings.database_url_sync, poolclass=pool.NullPool)
    try:
        with sync_engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '20s'"))
            for statement in _MATERIALISE_DOOMED:
                conn.execute(text(statement), {"depth": depth})
            for _table, statement in _CLEAR_REFERENCES:
                conn.execute(text(statement))
            deleted = conn.execute(text(_DELETE_DOOMED_NODES)).rowcount or 0
            deepest = conn.execute(text(_DEEPEST_REMAINING)).scalar_one()
            return PurgeResult(deleted=int(deleted), deepest_remaining=int(deepest))
    finally:
        sync_engine.dispose()


def purged_reference_tables() -> frozenset[str]:
    """The tables the purge clears before it deletes a node.

    Read off the statements the purge actually runs, so the coverage a test
    checks against the live catalogue cannot drift from the coverage the purge
    has.
    """
    return frozenset(table for table, _statement in _CLEAR_REFERENCES)


#: Revisions whose ``downgrade()`` refuses to narrow a closed catalogue while
#: any row still carries a value the older vocabulary cannot express (0108 for
#: ``file_history.kind``; 0155 for ``file_history.kind`` and
#: ``file_conflicts.state``). The refusal is right for an operator -- remapping an
#: append-only history would make it say something it never said -- and wrong
#: for a copy of a worker database, whose rows were written by whatever module
#: ran earlier and which the trip is not asking to rewrite. Each revision listed
#: publishes a ``CATALOGUES`` table of ``(table, column, constraint, before,
#: after)``; the harness reads it off the module so the two cannot drift apart.
_CATALOGUE_NARROWING_REVISIONS: tuple[str, ...] = ("0108", "0155")


@dataclass(frozen=True)
class _ParkedValues:
    """Rows whose catalogue value was set aside for the length of a round trip."""

    table: str
    column: str
    originals: tuple[tuple[uuid.UUID, str], ...]


def _narrowed_catalogues(
    config: Config, stepped: frozenset[str]
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """``(table, column, old vocabulary)`` for each catalogue a trip narrows.

    Only the revisions actually stepped over count: a downgrade that stops
    above a narrowing revision never runs its guard, and parking rows it will
    not look at would hide them from the body for no reason.
    """
    script = ScriptDirectory.from_config(config)
    narrowed: list[tuple[str, str, tuple[str, ...]]] = []
    for revision in _CATALOGUE_NARROWING_REVISIONS:
        if revision not in stepped:
            continue
        catalogues = script.get_revision(revision).module.CATALOGUES
        narrowed.extend((table, column, before) for table, column, _c, before, _a in catalogues)
    return tuple(narrowed)


def _park_stranded_values(
    url: str, narrowed: tuple[tuple[str, str, tuple[str, ...]], ...]
) -> tuple[_ParkedValues, ...]:
    """Move rows out of a narrowing guard's way, remembering what each one said.

    The value is overwritten with one the old vocabulary admits rather than the
    row being deleted: history rows are referenced by node and sequence, and a
    delete would have to take that graph apart in the right order.
    """
    parked: list[_ParkedValues] = []
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            for table, column, known in narrowed:
                rows = conn.execute(
                    text(
                        f"SELECT id, {column} AS value FROM {table} WHERE {column} <> ALL(:known)"
                    ),
                    {"known": list(known)},
                ).all()
                if not rows:
                    continue
                conn.execute(
                    text(f"UPDATE {table} SET {column} = :parked WHERE {column} <> ALL(:known)"),
                    {"parked": known[0], "known": list(known)},
                )
                parked.append(
                    _ParkedValues(
                        table=table,
                        column=column,
                        originals=tuple((row.id, row.value) for row in rows),
                    )
                )
    finally:
        engine.dispose()
    return tuple(parked)


def _restore_parked_values(url: str, parked: list[_ParkedValues]) -> None:
    """Write every parked row back verbatim, once head has widened the column."""
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            for item in parked:
                for row_id, value in item.originals:
                    conn.execute(
                        text(f"UPDATE {item.table} SET {item.column} = :value WHERE id = :id"),
                        {"value": value, "id": row_id},
                    )
    finally:
        engine.dispose()


@dataclass
class ScratchDatabase:
    """A throwaway copy of the session's migration template, and the handles bound to it.

    ``config`` migrates this copy and only this copy: it carries an open
    connection to it as ``attributes["connection"]``, which the backend's
    ``env.py`` uses in place of the database the settings name. ``session()``
    opens a session on the copy. Neither can reach the worker's database.
    """

    name: str
    sync_url: str
    async_url: str
    config: Config
    _sync_engine: Engine
    _connection: Connection
    _engine: AsyncEngine
    _sessions: async_sessionmaker[AsyncSession]
    _parked: list[_ParkedValues] = field(default_factory=list)

    def session(self) -> AsyncSession:
        """A session on this copy. Close it (``async with``) before a migration."""
        return self._sessions()

    @contextmanager
    def serving(self) -> Iterator[None]:
        """Serve the app from this copy for the length of the block.

        Rebinds the one session factory every request and service opens its
        sessions from, so a route test driven through the in-process client reads
        and writes the copy. Put back on the way out, whatever happened inside.
        """
        factory = db_session.AsyncSessionLocal
        previous = factory.kw.get("bind")
        factory.configure(bind=self._engine)
        try:
            yield
        finally:
            factory.configure(bind=previous)

    def revision(self) -> str | None:
        """The revision this copy is stamped with."""
        return current_revision(self.sync_url)

    def _stepped_over(self, target: str) -> frozenset[str]:
        stamped = self.revision()
        if stamped is None:
            return frozenset()
        script = ScriptDirectory.from_config(self.config)
        return frozenset(rev.revision for rev in script.iterate_revisions(stamped, target))

    async def downgrade(self, revision: str) -> None:
        """Move this copy down to ``revision``, clearing what a guard would refuse first."""
        stepped = self._stepped_over(revision)
        self._parked.extend(
            _park_stranded_values(self.sync_url, _narrowed_catalogues(self.config, stepped))
        )
        if _INDEX_REBUILDING_REVISION in stepped:
            purge_paths_the_old_index_cannot_hold(url=self.sync_url)
        # A connection that lives through a schema change carries asyncpg
        # statement plans for tables that no longer have that shape.
        await self._engine.dispose()
        command.downgrade(self.config, revision)

    async def upgrade(self, revision: str = "head") -> None:
        """Move this copy up to ``revision``; at head, parked values go back verbatim."""
        await self._engine.dispose()
        command.upgrade(self.config, revision)
        if self._parked and self.revision() == script_head(self.config):
            _restore_parked_values(self.sync_url, self._parked)
            self._parked.clear()


@asynccontextmanager
async def migration_scratch(
    template: MigrationTemplate | None = None,
) -> AsyncIterator[ScratchDatabase]:
    """A copy of the session's migration template, at head, dropped on the way out.

    Seed through ``scratch.session()``, then ``await scratch.downgrade(...)``,
    ``await scratch.upgrade()`` and assert, all against the copy. Whatever the
    body does -- an assertion, a guard refusing a downgrade half way, a
    ``KeyboardInterrupt`` -- the worker's database is exactly as it was, and the
    copy is gone.

    ``template`` is the session's unless a test hands in one of its own. The
    copy is taken from it and from nothing else -- never from the worker's
    database, whose idle pools would block the copy -- and a copy that comes out
    behind the scripts' head is brought to head before it is handed over, so a
    stale template cannot make a trip prove a revision against the wrong
    neighbours.
    """
    source = template or migration_template()
    name = f"{SCRATCH_PREFIX}{secrets.token_hex(6)}"
    sync_url = _with_database(settings.database_url_sync, name)
    async_url = _with_database(settings.database_url, name)
    setting = "a migration test's scratch database"
    assert_disposable_database(sync_url, setting=setting)
    sync_engine: Engine | None = None
    connection: Connection | None = None
    async_engine: AsyncEngine | None = None
    try:
        copy_database(source.maintenance_url, source.name, name)
        sync_engine = create_engine(
            sync_url, poolclass=pool.NullPool, connect_args=psycopg_connect_args()
        )
        connection = sync_engine.connect()
        async_engine = create_async_engine(
            async_url, poolclass=pool.NullPool, connect_args=asyncpg_connect_args()
        )
        config = alembic_config()
        config.set_main_option("sqlalchemy.url", sync_url)
        config.attributes["connection"] = connection
        if current_revision(sync_url) != script_head(config):
            command.upgrade(config, "head")
        settle_template_file_sessions(connection)
        yield ScratchDatabase(
            name=name,
            sync_url=sync_url,
            async_url=async_url,
            config=config,
            _sync_engine=sync_engine,
            _connection=connection,
            _engine=async_engine,
            _sessions=async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False),
        )
    finally:
        try:
            if async_engine is not None:
                await async_engine.dispose()
            if connection is not None:
                connection.close()
            if sync_engine is not None:
                sync_engine.dispose()
        finally:
            # WITH (FORCE): whatever the body left connected to the copy goes with it.
            drop_database(_maintenance_url(), name, setting=setting)


def settle_template_file_sessions(connection: Connection) -> None:
    """Mark the copy's live file sessions as written back.

    Revision 0177's downgrade refuses while any file session holds edits not
    yet on the drive (they are the only copy of what people typed). A template
    a serial run used as its own database carries the sessions earlier tests
    left half written, and every trip down through 0177 then stopped on them.
    They are residue in a disposable copy, not a test's fixture; a test that
    seeds its own unsaved session does so after this, and is refused as it
    should be.
    """
    has_table = connection.execute(
        text("SELECT to_regclass('public.crdt_docs') IS NOT NULL")
    ).scalar_one()
    if not has_table:
        return
    connection.execute(
        text(
            "UPDATE crdt_docs SET source_sha256 = projection->>'sha256' "
            "WHERE doc_type = 'file' AND source_etag IS NOT NULL "
            "AND projection ? 'sha256' AND projection->>'sha256' IS DISTINCT FROM source_sha256"
        )
    )
    connection.commit()


@asynccontextmanager
async def downgraded_to(revision: str) -> AsyncIterator[ScratchDatabase]:
    """A copy of the session's migration template, already downgraded to ``revision``.

    For a test with nothing to seed at head first; see :func:`migration_scratch`.
    """
    async with migration_scratch() as scratch:
        await scratch.downgrade(revision)
        yield scratch


async def seed_org_admin(scratch: ScratchDatabase) -> OrgWithAdmin:
    """An org with a verified admin, made IN the copy -- the ``org_admin`` fixture's
    shape, where a trip can see it.

    The fixture's org lives in the worker's database, and the copy holds the
    template's rows rather than the worker's, so a trip that writes rows against
    an org, or logs in under ``serving()``, seeds the org here first. Built through
    the same service the fixture uses, so the rows are the ones the app writes.
    """
    from backend.services.org import teams as team_service
    from tests.conftest import OrgWithAdmin

    token = secrets.token_hex(6)
    email = f"scratch-admin-{token}@alkera.dev"
    password = "admin-pass-12345"
    async with scratch.session() as session:
        org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Scratch Org {token}",
            admin_email=email,
            admin_first_name="Scratch",
            admin_last_name="Admin",
            admin_password=password,
        )
        # Org-structure mutations are gated on a verified email, as the fixture models.
        admin.email_verified_at = datetime.now(UTC)
        await session.commit()
        return OrgWithAdmin(
            org_id=org.id, admin_id=admin.id, admin_email=email, admin_password=password
        )
