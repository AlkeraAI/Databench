"""A migration test moves a copy of the session's template, never the worker's database.

One xdist worker runs one database with no per-test rollback, so a test that
downgrades that database in place and does not come back cleanly hands every
later test on the worker a schema from the past. It did: a downgrade to 0132
that 0155's history guard refused part way left the worker stamped at head with
0162's allocation-state CHECK narrowed, and every later test that wrote an
``asleep`` allocation failed.

So the harness copies a database for each trip and drops the copy afterwards --
and the database it copies is the session's migration template, not the worker's
own. Postgres copies a database only while nothing else is connected to it, and
a worker's database is never free of connections once its first test has run:
every engine keeps its checked-in connections open between tests, so an idle
pool left by any earlier module blocked every trip behind a full worker. The
template is a database at head that nothing in the session connects to.

The property is pinned dynamically -- a trip that succeeds, one whose body
raises and one whose downgrade a real guard refuses half way all leave the
worker byte-for-byte where it was and the copy gone; a trip opens while an idle
pooled connection holds the worker's database, and leaves that connection
alone; nothing is connected to the template while a copy is taken -- and
statically, by reading every test module and refusing one that points Alembic
at the worker.
"""

from __future__ import annotations

import ast
import gc
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.db.testing import (
    MIGRATION_TEMPLATE_PREFIX,
    MigrationTemplate,
    copy_database,
    drop_database,
    is_disposable_database,
    worker_database_name,
)
from alkera_core.db.tls import asyncpg_connect_args
from alkera_core.models import RateLimitWindow
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import create_engine, delete, pool, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.conftest import OrgWithAdmin, login
from tests.migration_harness import (
    SCRATCH_PREFIX,
    ScratchDatabase,
    alembic_config,
    current_revision,
    downgraded_to,
    migration_scratch,
    migration_template,
    script_head,
    settle_template_file_sessions,
)

_TESTS = Path(__file__).resolve().parent

#: 0162 widens the allocation-state CHECK to admit ``asleep``; its downgrade
#: narrows it back and validates outside the transaction, which commits.
_ALLOCATION_STATE_CHECK = "ck_compute_allocations_state"
#: Far enough down that the trip steps over 0162 and then reaches 0155, whose
#: guard refuses a ``file_history.kind`` its older vocabulary never knew.
_BELOW_THE_HISTORY_GUARD = "0132"


def _worker_name() -> str:
    """The database the suite itself runs on: the one a trip must never touch."""
    return make_url(settings.database_url_sync).database or ""


def _parent_of_head() -> str:
    """One step below head: enough to prove a trip, cheap enough to run."""
    script = ScriptDirectory.from_config(alembic_config())
    down = script.get_revision(script_head()).down_revision
    assert isinstance(down, str), "head has no single parent revision to step back to"
    return down


def _schema(url: str | None = None) -> tuple[tuple[str, ...], ...]:
    """Every column, constraint and index of the public schema, as the catalog says.

    Compared whole before and after a trip: a worker that lost a CHECK value, an
    index or a column to someone else's downgrade differs here even when its
    stamp still says head.
    """
    engine = create_engine(url or settings.database_url_sync, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            columns = conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable "
                    "FROM information_schema.columns WHERE table_schema = 'public'"
                )
            ).all()
            constraints = conn.execute(
                text(
                    "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
                )
            ).all()
            indexes = conn.execute(
                text(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public'"
                )
            ).all()
            grants = conn.execute(
                text(
                    "SELECT table_name, grantee, privilege_type "
                    "FROM information_schema.role_table_grants WHERE table_schema = 'public'"
                )
            ).all()
    finally:
        engine.dispose()
    return tuple(
        sorted(tuple(str(v) for v in row) for row in (*columns, *constraints, *indexes, *grants))
    )


def _sql(statement: str, **params: object) -> list[tuple[object, ...]]:
    """One statement on a fresh connection to the worker's database, nothing pooled."""
    engine = create_engine(settings.database_url_sync, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(statement), params).all()]
    finally:
        engine.dispose()


def _exists(database: str) -> bool:
    return _sql("SELECT 1 FROM pg_database WHERE datname = :name", name=database) == [(1,)]


def _backends_of_this_process() -> set[int]:
    """The server pid behind every open database connection this process holds.

    Found by walking the live objects rather than read off the server: the
    server's own view names every session on the cluster, and under xdist the
    other workers' processes -- and anything else the runner starts -- share it,
    so a session of theirs caught mid-connect on the template read as ours.
    Every connection this process opens is a driver object on its heap: pooled,
    checked out, or held raw."""
    pids: set[int] = set()
    for obj in gc.get_objects():
        if isinstance(obj, psycopg.Connection | psycopg.AsyncConnection):
            if not obj.closed:
                pids.add(obj.info.backend_pid)
        elif isinstance(obj, asyncpg.Connection) and not obj.is_closed():
            pids.add(obj.get_server_pid())
    return pids


def _sessions_on(database: str) -> list[tuple[object, ...]]:
    """``(pid, state, query)`` of every session THIS process holds on
    ``database``. The server's own workers are not sessions of ours (an
    autovacuum worker visits any database at any moment), and neither are the
    sessions of another process on the same server."""
    ours = _backends_of_this_process()
    rows = _sql(
        "SELECT pid, state, left(query, 80) FROM pg_stat_activity "
        "WHERE datname = :name AND pid <> pg_backend_pid() "
        "AND backend_type = 'client backend'",
        name=database,
    )
    return [row for row in rows if row[0] in ours]


def _allocation_states(url: str | None = None) -> str:
    engine = create_engine(url or settings.database_url_sync, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :c"),
                    {"c": _ALLOCATION_STATE_CHECK},
                ).scalar_one()
            )
    finally:
        engine.dispose()


@pytest.fixture
def worker_schema() -> tuple[tuple[str, ...], ...]:
    """The worker's schema before the test; the premise every case below rests on."""
    assert current_revision() == script_head(), "an earlier module already moved this worker"
    return _schema()


@dataclass(frozen=True)
class _HistoryRow:
    """A ``file_history`` row of kind ``conflict`` and the rows it hangs off."""

    history_id: uuid.UUID
    node_id: uuid.UUID
    drive_id: uuid.UUID
    domain_id: uuid.UUID
    store_id: uuid.UUID


async def _seed_history_row(session: AsyncSession) -> _HistoryRow:
    """Write a ``file_history`` row of kind ``conflict``, on an org of its own.

    ``conflict`` is a kind 0155 added, so a downgrade below 0155 refuses while
    the row exists -- the shape another module leaves behind on a worker.
    """
    org_team_id = uuid.uuid4()
    # The rows reference each other before a flush would assign column defaults.
    row = _HistoryRow(*(uuid.uuid4() for _ in range(5)))
    rows = (
        FileStore(id=row.store_id, driver="filesystem", bucket=f"harness-{org_team_id.hex[:8]}"),
        DedupDomain(id=row.domain_id, org_team_id=org_team_id, store_id=row.store_id),
        FileDrive(
            id=row.drive_id,
            org_team_id=org_team_id,
            store_id=row.store_id,
            dedup_domain_id=row.domain_id,
            kind="org",
        ),
        FileNode(
            id=row.node_id,
            ino=1,
            drive_id=row.drive_id,
            org_team_id=org_team_id,
            kind="folder",
            name=b"",
            path_ids="n1",
        ),
        FileHistory(
            id=row.history_id,
            org_team_id=org_team_id,
            node_id=row.node_id,
            seq=1,
            kind="conflict",
            acting_principal=uuid.uuid4(),
        ),
    )
    # One flush per row, in reference order: file_drives and file_nodes point
    # at each other, so the unit of work has no single order to sort into.
    for entity in rows:
        session.add(entity)
        await session.flush()
    await session.commit()
    return row


@pytest.fixture
async def a_worker_history_row() -> AsyncIterator[_HistoryRow]:
    """A history row seeded on the WORKER, the way another module leaves one, and
    removed afterwards."""
    async with AsyncSessionLocal() as session:
        row = await _seed_history_row(session)
    try:
        yield row
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(FileHistory).where(FileHistory.id == row.history_id))
            await session.execute(delete(FileNode).where(FileNode.id == row.node_id))
            await session.execute(delete(FileDrive).where(FileDrive.id == row.drive_id))
            await session.execute(delete(DedupDomain).where(DedupDomain.id == row.domain_id))
            await session.execute(delete(FileStore).where(FileStore.id == row.store_id))
            await session.commit()


async def _history_kind(session_scope: ScratchDatabase | None, history_id: uuid.UUID) -> str | None:
    """The row's kind where ``session_scope`` says -- the worker for ``None`` -- or
    ``None`` when that database has no such row."""
    session = AsyncSessionLocal() if session_scope is None else session_scope.session()
    async with session:
        return (
            await session.execute(select(FileHistory.kind).where(FileHistory.id == history_id))
        ).scalar_one_or_none()


# --- the copy -----------------------------------------------------------------------


async def test_a_scratch_is_a_copy_of_the_template_and_not_of_the_worker(
    a_worker_history_row: _HistoryRow,
) -> None:
    """What the copy holds is the template's rows: a row written on the worker
    before the trip is not in it, and a row written in it never reaches the
    worker. Both directions, because a copy of the worker would pass the second
    and fail the first."""
    async with migration_scratch() as scratch:
        assert scratch.name.startswith(SCRATCH_PREFIX)
        assert scratch.revision() == script_head()
        assert await _history_kind(scratch, a_worker_history_row.history_id) is None
        async with scratch.session() as session:
            seeded = await _seed_history_row(session)
        assert await _history_kind(scratch, seeded.history_id) == "conflict"
        assert await _history_kind(None, seeded.history_id) is None
    assert await _history_kind(None, a_worker_history_row.history_id) == "conflict"


async def test_serving_points_the_apps_sessions_at_the_copy_and_back() -> None:
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            seeded = await _seed_history_row(session)
        with scratch.serving():
            # The app's own factory -- what every route and service opens.
            assert await _history_kind(None, seeded.history_id) == "conflict"
        assert await _history_kind(None, seeded.history_id) is None
    assert await _history_kind(None, seeded.history_id) is None


async def test_a_round_trip_moves_the_copy_and_never_the_worker(
    worker_schema: tuple[tuple[str, ...], ...],
) -> None:
    parent = _parent_of_head()
    async with downgraded_to(parent) as scratch:
        assert scratch.revision() == parent, "the body must actually see the old schema"
        assert current_revision() == script_head(), "the downgrade reached the worker"
        # The copy has moved when ITS revision row says so; its schema may still
        # equal the worker's, because a head revision that only stamps rows
        # (0168 marks a chat's records) leaves the DDL alone on the way down.
        assert scratch.name != _worker_name(), "the trip must run on a copy"
        await scratch.upgrade()
        assert scratch.revision() == script_head()
        name = scratch.name
    assert _schema() == worker_schema
    assert not _exists(name), "the copy outlived its trip"


async def test_a_body_that_raises_leaves_the_worker_as_it_was_and_the_copy_gone(
    worker_schema: tuple[tuple[str, ...], ...],
) -> None:
    names: list[str] = []
    with pytest.raises(RuntimeError, match="the assertion this test makes"):
        async with downgraded_to(_parent_of_head()) as scratch:
            names.append(scratch.name)
            raise RuntimeError("the assertion this test makes")
    assert _schema() == worker_schema
    assert names and not _exists(names[0])


async def test_a_downgrade_a_guard_refuses_half_way_leaves_the_worker_as_it_was(
    worker_schema: tuple[tuple[str, ...], ...],
) -> None:
    """The failure that stranded a worker, driven for real against the copy.

    ``command.downgrade`` on the copy's own config, so no harness step moves the
    history row out of the way: the trip walks down through 0162 -- whose
    downgrade narrows the allocation-state CHECK and commits it -- and 0155's
    guard then refuses the row. The copy is left half way down; the worker never
    saw any of it.
    """
    names: list[str] = []
    with pytest.raises(RuntimeError, match=r"cannot narrow file_history\.kind: .*conflict="):
        async with migration_scratch() as scratch:
            names.append(scratch.name)
            async with scratch.session() as session:
                await _seed_history_row(session)
            command.downgrade(scratch.config, _BELOW_THE_HISTORY_GUARD)
    assert current_revision() == script_head()
    assert "asleep" in _allocation_states(), "the worker lost 0162's CHECK to the copy's trip"
    assert _schema() == worker_schema
    assert names and not _exists(names[0])


async def test_a_trip_below_a_narrowing_guard_parks_what_it_refuses_and_head_restores_it() -> None:
    """The copy carries rows the guard would refuse; the harness sets exactly
    those aside for the trip and head puts them back as they were. The guard
    itself stays live for rows written after."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            seeded = await _seed_history_row(session)
        await scratch.downgrade(_BELOW_THE_HISTORY_GUARD)
        assert await _history_kind(scratch, seeded.history_id) != "conflict"
        async with scratch.session() as session:
            with pytest.raises(DBAPIError, match="ck_file_history_kind"):
                await session.execute(
                    text(
                        "INSERT INTO file_history (id, org_team_id, node_id, seq, kind, "
                        "acting_principal) SELECT :id, org_team_id, id, 2, 'conflict', :who "
                        "FROM file_nodes WHERE id = :node"
                    ),
                    {"id": uuid.uuid4(), "who": uuid.uuid4(), "node": seeded.node_id},
                )
        await scratch.upgrade()
        assert await _history_kind(scratch, seeded.history_id) == "conflict"


async def test_a_plain_route_test_after_a_round_trip_still_sees_head(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The regression in the shape CI saw it: a migration test runs, then an
    ordinary test runs on the same worker. The autouse fixture's table and a
    route that reads the database are both still there."""
    async with downgraded_to(_parent_of_head()):
        pass

    # The exact statement the autouse `_reset_rate_limit_windows` fixture runs.
    async with AsyncSessionLocal() as session:
        await session.execute(delete(RateLimitWindow))
        await session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    me = await client.get("/api/v1/me/preferences")
    assert me.status_code == 200, me.text


# --- the template -------------------------------------------------------------------


async def test_a_scratch_opens_while_an_idle_pooled_connection_holds_the_worker() -> None:
    """The failure behind a full worker, in one test.

    An engine some earlier test built and never disposed keeps its checked-in
    connection open on the worker's database, idle, for the rest of the run;
    ``CREATE DATABASE … TEMPLATE`` of that database is refused while it exists.
    A trip has to open anyway -- from the template -- and it may not clear the
    way by terminating the connection: the next test to check it out would find
    it dead. So the holder is proven present before the trip and proven to be
    the same server session afterwards.
    """
    engine = create_async_engine(
        settings.database_url, pool_pre_ping=True, connect_args=asyncpg_connect_args()
    )
    try:
        async with engine.connect() as conn:
            holder = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        # Checked back in, not closed: the server still has it, idle.
        assert _sql(
            "SELECT state FROM pg_stat_activity WHERE pid = :pid AND datname = current_database()",
            pid=holder,
        ) == [("idle",)]

        async with migration_scratch() as scratch:
            assert scratch.revision() == script_head()

        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one() == holder, (
                "the trip took the idle connection down, and its pool reconnected"
            )
    finally:
        await engine.dispose()


async def test_the_session_never_connects_to_its_template() -> None:
    """What makes the template copyable at any point in the run: no session in
    this process opens a connection on it. Its name is one the suite may
    destroy, and it is either the base this worker was cloned from (borrowed,
    under xdist) or a copy this session made under its own name."""
    template = migration_template()
    assert is_disposable_database(template.name)
    if template.owned:
        assert template.name.startswith(MIGRATION_TEMPLATE_PREFIX)
    else:
        worker = os.environ.get("PYTEST_XDIST_WORKER")
        assert worker, "only an xdist worker borrows a template: the base it was cloned from"
        assert make_url(settings.database_url_sync).database == worker_database_name(
            template.name, worker
        )
    assert _exists(template.name)
    async with migration_scratch():
        assert _sessions_on(template.name) == []
    assert _sessions_on(template.name) == []


@dataclass(frozen=True)
class _BoundConfig:
    """An Alembic config bound to a database of this test's making through the
    connection seam -- never the worker's."""

    config: object


async def test_a_template_behind_head_still_yields_a_copy_at_head(
    worker_schema: tuple[tuple[str, ...], ...],
) -> None:
    """A serial session on a database nobody migrated would hand every trip a
    stale copy, and a revision test would then prove its step against the wrong
    neighbours. The copy is brought to head first; the template is left as it is."""
    session_template = migration_template()
    stale = f"alkera_migtest_stale_{uuid.uuid4().hex[:8]}"
    parent = _parent_of_head()
    copy_database(session_template.maintenance_url, session_template.name, stale)
    try:
        stale_url = make_url(settings.database_url_sync).set(database=stale).render_as_string(False)
        engine = create_engine(stale_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as conn:
                config = alembic_config()
                config.set_main_option("sqlalchemy.url", stale_url)
                config.attributes["connection"] = conn
                bound = _BoundConfig(config=config)
                settle_template_file_sessions(conn)
                command.downgrade(bound.config, parent)
        finally:
            engine.dispose()
        assert current_revision(stale_url) == parent

        template = MigrationTemplate(
            name=stale, maintenance_url=session_template.maintenance_url, owned=True
        )
        async with migration_scratch(template=template) as scratch:
            assert scratch.revision() == script_head()
            assert _schema(scratch.sync_url) == worker_schema
        assert current_revision(stale_url) == parent, "the template itself was migrated"
    finally:
        drop_database(session_template.maintenance_url, stale, setting="a stale template")


# --- the static half ----------------------------------------------------------------

_MIGRATING_COMMANDS = frozenset({"downgrade", "upgrade", "stamp"})


def _alembic_command_calls(tree: ast.AST) -> list[ast.Call]:
    """``command.downgrade(...)`` / ``.upgrade`` / ``.stamp`` -- the calls that move a schema."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MIGRATING_COMMANDS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"command", "alembic_cmd"}
    ]


def _on_a_scratch(call: ast.Call) -> bool:
    """Whether the config the call migrates is a scratch database's ``.config``."""
    return (
        bool(call.args)
        and isinstance(call.args[0], ast.Attribute)
        and call.args[0].attr == "config"
    )


def _test_modules() -> list[Path]:
    return sorted(_TESTS.rglob("test_*.py"))


def test_the_scan_sees_the_modules_that_migrate() -> None:
    """A guard on the guard: a scan that matches nothing proves nothing."""
    migrating = [
        path
        for path in _test_modules()
        if _alembic_command_calls(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert Path(__file__) in migrating
    assert _TESTS / "test_files_migration.py" in migrating


@pytest.mark.parametrize("module", _test_modules(), ids=lambda p: p.relative_to(_TESTS).as_posix())
def test_no_test_points_alembic_at_the_worker_database(module: Path) -> None:
    """Every in-process ``command.downgrade`` / ``upgrade`` / ``stamp`` in a test
    migrates a :class:`ScratchDatabase`'s ``config`` -- a copy the harness drops
    afterwards. A config built over the settings' URL is the worker's database,
    and a trip that fails on it strands every later test on the worker."""
    source = module.read_text(encoding="utf-8")
    calls = _alembic_command_calls(ast.parse(source))
    on_the_worker = [f"line {call.lineno}" for call in calls if not _on_a_scratch(call)]
    assert not on_the_worker, (
        f"{module.relative_to(_TESTS)} migrates a config that is not a scratch database's "
        f"at {', '.join(on_the_worker)}. Drive the trip through "
        "tests.migration_harness.migration_scratch / downgraded_to and pass scratch.config."
    )
