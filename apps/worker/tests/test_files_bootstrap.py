"""The Files family's production wiring, against real rows.

The cores refuse to invent a principal or a bucket, so something has to hand
them one; these pin that the worker's own boot is that something, that it does
so only when the deployment serves Files, and that a store the settings name
but cannot address is refused at boot rather than on the first janitor tick.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alkera_core.config import Settings
from alkera_core.config import settings as process_settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import OperationId
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.sweepers import JANITOR_ORDER, run_janitor
from alkera_core.models._enums import TeamRole
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from worker.files_bootstrap import (
    FilesSettingsError,
    build_store_factory,
    domain_of,
    janitor_context,
    wire_files_jobs,
)
from worker.tasks import files as tasks
from worker.temporal import queues, runner

pytestmark = pytest.mark.usefixtures("unwired_after")

EPOCH_CLOCK = FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def unwired_after() -> AsyncIterator[None]:
    """The deps factory is process-global; no test may leak its wiring."""
    yield None
    tasks.reset_deps_factory()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(process_settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def seed_org_with_a_drive(db: AsyncSession, org_id: uuid.UUID | None = None) -> uuid.UUID:
    """Commit a tenant that owns a Files drive — what `orgs_with_drives` looks for.

    ``org_id`` is for the cases that page the fleet: the paging is by org id, so
    a case that wants to name the cursor immediately before its own orgs has to
    choose where they sort.
    """
    org = Team(
        id=org_id or uuid.uuid4(), parent_team_id=None, name=f"files-w-{uuid.uuid4().hex[:8]}"
    )
    db.add(org)
    await db.flush()
    user = User(
        id=uuid.uuid4(),
        home_org_team_id=org.id,
        email=f"admin-{uuid.uuid4().hex[:12]}@files.test",
        email_domain="files.test",
        first_name="Admin",
        last_name="Tester",
    )
    db.add(user)
    await db.flush()
    db.add(TeamMembership(user_id=user.id, team_id=org.id, role=TeamRole.ADMIN))
    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/files-bootstrap-test/{uuid.uuid4().hex}",
        region="",
        capabilities={},
        transfer_modes=["single", "proxied"],
    )
    db.add(store)
    await db.flush()
    domain = DedupDomain(id=uuid.uuid4(), org_team_id=org.id, region="", store_id=store.id)
    db.add(domain)
    await db.flush()
    db.add(
        FileDrive(
            id=uuid.uuid4(),
            org_team_id=org.id,
            kind="org",
            store_id=store.id,
            dedup_domain_id=domain.id,
            quota_bytes=1 << 40,
            quota_nodes=1_000_000,
            next_ino=2,
        )
    )
    await db.commit()
    return uuid.UUID(str(org.id))


async def forget_org_with_a_drive(db: AsyncSession, org_team_id: uuid.UUID) -> None:
    """Undo :func:`seed_org_with_a_drive`, rows first, in dependency order.

    The seed commits, so nothing rolls it back: without this the org stays in
    `file_drives` for the life of the database and every janitor pass any later
    test runs sweeps it. A janitor task fans out over every org that owns a
    drive, so a module that leaks one org per case makes its own cost — and the
    cost of every other suite sharing the database — grow with every run.
    """
    # Whatever the case wrote inside the org goes first: a drive a node still
    # names cannot be deleted, and a drive left behind is the row that grows
    # every later janitor pass.
    for statement in (
        "DELETE FROM file_ops WHERE org_team_id = :org",
        "DELETE FROM file_history WHERE org_team_id = :org",
        "DELETE FROM file_versions WHERE org_team_id = :org",
    ):
        await db.execute(text(statement), {"org": org_team_id})
    await db.execute(
        text("UPDATE file_drives SET root_node_id = NULL WHERE org_team_id = :org"),
        {"org": org_team_id},
    )
    await db.execute(text("DELETE FROM file_nodes WHERE org_team_id = :org"), {"org": org_team_id})
    await db.execute(text("DELETE FROM file_drives WHERE org_team_id = :org"), {"org": org_team_id})
    # The store is named by the domain, so the domain goes first and the store
    # ids have to be read before the row that names them is gone.
    store_ids = [
        row[0]
        for row in await db.execute(
            text("SELECT store_id FROM dedup_domains WHERE org_team_id = :org"),
            {"org": org_team_id},
        )
    ]
    await db.execute(
        text("DELETE FROM dedup_domains WHERE org_team_id = :org"), {"org": org_team_id}
    )
    if store_ids:
        await db.execute(text("DELETE FROM file_stores WHERE id = ANY(:ids)"), {"ids": store_ids})
    await db.execute(
        text(
            "DELETE FROM team_memberships tm USING users u "
            "WHERE tm.user_id = u.id AND u.org_team_id = :org"
        ),
        {"org": org_team_id},
    )
    await db.execute(text("DELETE FROM users WHERE org_team_id = :org"), {"org": org_team_id})
    await db.execute(text("DELETE FROM teams WHERE id = :org"), {"org": org_team_id})
    await db.commit()


@pytest.fixture
async def org_with_a_drive(db: AsyncSession) -> AsyncIterator[uuid.UUID]:
    """A tenant that owns a Files drive, removed again when the case ends."""
    org_team_id = await seed_org_with_a_drive(db)
    try:
        yield org_team_id
    finally:
        await forget_org_with_a_drive(db, org_team_id)


@pytest.fixture
async def drive_id(db: AsyncSession, org_with_a_drive: uuid.UUID) -> uuid.UUID:
    row = (
        await db.execute(
            text("SELECT id FROM file_drives WHERE org_team_id = :org"),
            {"org": org_with_a_drive},
        )
    ).first()
    assert row is not None
    return uuid.UUID(str(row[0]))


#: Every Files setting at the value the code declares, not at whatever this
#: developer's ``.env`` happens to carry. An ambient ``FILES_STORE_ENDPOINT``
#: would otherwise hand the "refused at boot" cases the very setting they are
#: meant to be missing, and they would stop refusing.
_FILES_DEFAULTS: dict[str, object] = {
    name: field.get_default()
    for name, field in Settings.model_fields.items()
    if name.startswith("files_")
}


def _files_settings(**overrides: object) -> Settings:
    """The process's settings with the whole Files block reset, then the case's
    own values on top: a case names every Files setting its outcome depends on
    and inherits none of them."""
    return process_settings.model_copy(update={**_FILES_DEFAULTS, **overrides})


# -- what a pass costs ------------------------------------------------------


async def test_a_seeded_org_is_forgotten_so_a_later_pass_does_not_sweep_it(
    db: AsyncSession,
) -> None:
    """A janitor pass fans out over every org that owns a drive, so a seed that
    outlives its case is charged to every pass any later test runs. The seed
    commits — nothing rolls it back — so the module has to take it away again,
    and the set `orgs_with_drives` answers must come back to what it was."""
    before = await tasks.orgs_with_drives(db)

    org_team_id = await seed_org_with_a_drive(db)
    assert org_team_id in await tasks.orgs_with_drives(db)

    await forget_org_with_a_drive(db, org_team_id)

    assert await tasks.orgs_with_drives(db) == before


# -- the fleet, paged -------------------------------------------------------

#: Three ids that sort after every org a random uuid4 seed can produce, so a
#: case can name the cursor that lands immediately before its own orgs and page
#: exactly them. The fleet is shared with every other suite on this database;
#: pinning the tail is what makes a page's contents predictable at all.
TAIL_ORG_IDS = tuple(uuid.UUID(f"ffffff00-0000-4000-8000-0000000000{n:02d}") for n in (1, 2, 3))


@pytest.fixture
async def three_orgs_at_the_tail(db: AsyncSession) -> AsyncIterator[tuple[uuid.UUID, ...]]:
    """Three tenants that own drives and sort last, taken away again after."""
    seeded: list[uuid.UUID] = []
    try:
        for org_id in TAIL_ORG_IDS:
            seeded.append(await seed_org_with_a_drive(db, org_id=org_id))
        yield tuple(seeded)
    finally:
        for org_id in reversed(seeded):
            await forget_org_with_a_drive(db, org_id)


async def _cursor_before(db: AsyncSession, first_of_mine: uuid.UUID) -> uuid.UUID | None:
    """The org the fleet holds immediately before this one, or None if it is
    the first — what a case pages from so it pages only its own orgs."""
    fleet = await tasks.orgs_with_drives(db)
    at = fleet.index(first_of_mine)
    return fleet[at - 1] if at else None


async def test_the_real_statement_pages_the_fleet_in_id_order_covering_each_org_once(
    db: AsyncSession, three_orgs_at_the_tail: tuple[uuid.UUID, ...]
) -> None:
    """The paging is the janitor's whole resumability story, and it is SQL: an
    ORDER BY that is not by id, an `after` comparison the wrong way round, or a
    page taken by the wrong column all break it, and only the real statement
    against real rows can say they did not."""
    fleet = await tasks.orgs_with_drives(db)

    assert fleet == sorted(fleet), "the fleet must come back in id order"
    assert [org for org in fleet if org in set(three_orgs_at_the_tail)] == list(
        three_orgs_at_the_tail
    )

    # Paged two at a time from the org immediately before mine, the pages are
    # my three orgs in order, and the page after the last one is empty.
    before_mine = await _cursor_before(db, three_orgs_at_the_tail[0])
    first = await tasks.orgs_with_drives(db, after=before_mine, limit=2)
    assert first == list(three_orgs_at_the_tail[:2])
    second = await tasks.orgs_with_drives(db, after=first[-1], limit=2)
    assert second == [three_orgs_at_the_tail[2]], "a short page is the end of the fleet"
    assert await tasks.orgs_with_drives(db, after=second[-1], limit=2) == []

    # And over the whole fleet, the pages partition it exactly once.
    seen: list[uuid.UUID] = []
    after: uuid.UUID | None = None
    while page := await tasks.orgs_with_drives(db, after=after, limit=2):
        seen.extend(page)
        after = page[-1]
    assert seen == fleet
    assert len(set(seen)) == len(seen), "no org may appear on two pages"


async def test_a_real_janitor_pass_resumes_at_its_cursor_and_sweeps_no_org_twice(
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    three_orgs_at_the_tail: tuple[uuid.UUID, ...],
) -> None:
    """The deployment's own budget, over real drives: a pass covers at most
    FILES_JANITOR_ORG_BUDGET orgs and names where it stopped, the pass resumed
    from that cursor takes the rest, and between them every org is swept once."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: _files_settings(files_enabled=True, files_janitor_org_budget=2),
    )
    before_mine = await _cursor_before(db, three_orgs_at_the_tail[0])

    first = await tasks.janitor(after=before_mine)

    assert first.orgs == three_orgs_at_the_tail[:2], "a pass stops at its org budget"
    assert first.cursor == three_orgs_at_the_tail[1]
    assert len(first.reports) == 2, "one report per org swept"

    second = await tasks.janitor(after=first.cursor)

    assert second.orgs == three_orgs_at_the_tail[2:], "the resumed pass takes the rest"
    assert second.cursor is None, "a page short of its budget is the end of the fleet"
    assert len(second.reports) == 1

    swept = first.orgs + second.orgs
    assert sorted(swept) == list(three_orgs_at_the_tail)
    assert len(set(swept)) == 3, "no org may be swept twice"


# -- wired ------------------------------------------------------------------


async def test_a_worker_with_files_configured_sweeps_a_real_orgs_rows(
    tmp_path: Path, org_with_a_drive: uuid.UUID
) -> None:
    """The whole point of the wiring: a real `files.janitor` pass runs every
    sweeper for an org that owns a drive, on this process's own settings."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )

    reports = await tasks.janitor()

    assert reports, "a janitor pass with a seeded drive reported nothing"
    # Every sweeper the ledger names ran, in that order, for every org visited.
    for report in reports:
        assert report.order == tuple(kind.name for kind in JANITOR_ORDER)


#: The sweepers that do nothing unless the deployment hands them the service
#: whose behaviour they run. Each one reports `skipped` rather than a zero when
#: its dependency is missing, so a janitor wired with no services sweeps
#: nothing and says so — which is exactly what the deployed pass was doing.
DELEGATING_SWEEPERS = (
    "deleted_expiry",
    "expired_sessions",
    "trash_purge",
    "operation_watchdog",
    "dir_stats_aggregate",
)


async def test_the_deployed_pass_runs_every_service_backed_sweeper(
    tmp_path: Path, db: AsyncSession, org_with_a_drive: uuid.UUID
) -> None:
    """The wiring is the point: a pass built by the deployment's own factory
    delegates to the real services, so none of the sweepers that exist only to
    call one reports itself skipped."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )

    deps = await tasks._deps(db, org_with_a_drive)
    assert deps.sweep is not None, "the deployed factory built no sweeper dependencies"
    report = await run_janitor(deps.sweep, datetime.now(UTC))

    skipped = {outcome.name: outcome.reason for outcome in report.outcomes if outcome.skipped}
    assert [name for name in DELEGATING_SWEEPERS if name in skipped] == []


async def test_the_deployed_pass_sweeps_unreferenced_objects(
    tmp_path: Path, db: AsyncSession, org_with_a_drive: uuid.UUID
) -> None:
    """The reachability row of the ledger is on the deployed pass too: without
    it, `Janitor.sweep(dry_run=False)` has no caller outside the tests and no
    unreferenced byte is ever released in production."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )

    deps = await tasks._deps(db, org_with_a_drive)
    assert deps.sweep is not None
    report = await run_janitor(deps.sweep, datetime.now(UTC))

    assert report.by_name("reachability").skipped is False


async def test_the_wired_deps_carry_the_platform_principal_and_the_orgs_store(
    tmp_path: Path, db: AsyncSession, org_with_a_drive: uuid.UUID
) -> None:
    """What the factory hands a job: the org's own scope, a service principal
    that is nobody's user, and a store bound to the org's dedup domain."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )

    deps = await tasks._deps(db, org_with_a_drive)

    assert deps.repo.scope.org_team_id == org_with_a_drive
    assert deps.ctx.org_id == org_with_a_drive
    assert deps.ctx.effective_user_id is None
    assert isinstance(deps.store, DomainStore)
    domain = await domain_of(db, org_with_a_drive)
    assert deps.store.domain_id == domain


async def test_a_second_org_gets_its_own_scope_from_one_wiring(
    tmp_path: Path, db: AsyncSession, org_with_a_drive: uuid.UUID
) -> None:
    """One factory, many tenants: the scope comes from the argument, never from
    the wiring, so a pass can never sweep one org under another's scope."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )
    other = uuid.uuid4()

    first = await tasks._deps(db, org_with_a_drive)
    second = await tasks._deps(db, other)

    assert first.repo.scope.org_team_id == org_with_a_drive
    assert second.repo.scope.org_team_id == other
    # No drive, no domain — and therefore no store handle vended for a tenant
    # that has never used Files.
    assert second.store is None


def test_the_janitor_acts_as_the_platform_not_as_a_user() -> None:
    org = uuid.uuid4()

    ctx = janitor_context(org)

    assert ctx.effective_user_id is None
    assert ctx.delegating_user is None
    assert ctx.org_id == org
    assert ctx.acting_principal.credential is None


# -- unwired ----------------------------------------------------------------


async def test_a_worker_without_files_configured_boots_and_the_jobs_refuse(
    org_with_a_drive: uuid.UUID,
) -> None:
    """FILES_ENABLED=false is not a misconfiguration: the worker serves every
    other family, and a Files job that reaches it refuses rather than guessing."""
    assert wire_files_jobs(_files_settings(files_enabled=False)) is False

    # The queue registries are untouched by the wiring: every workflow type is
    # still discovered and served, Files included.
    registered = {workflow for queue in TaskQueue for workflow in queues.WORKFLOWS_BY_QUEUE[queue]}
    assert len(registered) == len(QUEUE_FOR)
    assert queues.ACTIVITIES_BY_QUEUE[QUEUE_FOR[WorkflowType.FILES_JANITOR]]

    with pytest.raises(tasks.FilesJobsNotWiredError):
        await tasks.janitor()


# -- refused at boot --------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        pytest.param(
            {"files_store_provider": "s3_compatible", "files_store_bucket": "b"},
            "FILES_STORE_ENDPOINT",
            id="s3-compatible-without-an-endpoint",
        ),
        pytest.param(
            {"files_store_provider": "s3_compatible", "files_store_endpoint": "http://s3:8333"},
            "FILES_STORE_BUCKET",
            id="s3-compatible-without-a-bucket",
        ),
        pytest.param(
            {"files_store_provider": "aws", "files_store_bucket": "b"},
            "FILES_VEND_ROLE_ARN",
            id="aws-without-a-vend-role",
        ),
    ],
)
def test_an_unaddressable_store_is_refused_at_boot_by_name(
    overrides: dict[str, object], named: str
) -> None:
    with pytest.raises(FilesSettingsError, match=named):
        wire_files_jobs(_files_settings(files_enabled=True, **overrides))

    # And nothing was installed by the attempt.
    assert tasks._deps_factory is tasks._unwired


def test_a_complete_s3_configuration_builds_its_factory() -> None:
    """The negative cases above are refusals, not an inability to build one."""
    factory = build_store_factory(
        _files_settings(
            files_enabled=True,
            files_store_provider="s3_compatible",
            files_store_endpoint="http://s3:8333",
            files_store_bucket="alkera-files",
        ),
        clock=EPOCH_CLOCK,
    )

    assert factory.admin() is not None


def test_the_runner_exits_one_when_the_store_settings_are_unaddressable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal has to reach the process's exit code, and it has to happen
    before a single queue is served."""
    served: list[object] = []

    def refuse() -> bool:
        raise FilesSettingsError("FILES_STORE_ENDPOINT must be set")

    monkeypatch.setattr(runner, "wire_files_jobs", refuse)
    monkeypatch.setattr(runner, "boot", lambda: None)
    monkeypatch.setattr(runner.asyncio, "run", lambda *a, **k: served.append(a))

    code = runner.main_run(
        [TaskQueue.DEFAULT], health_host="127.0.0.1", health_port=0, sync_schedules_on_boot=False
    )

    assert code == 1
    assert served == []


# -- the batched subtree move ------------------------------------------------


async def test_a_move_that_already_finished_is_reported_not_refused(
    tmp_path: Path, db: AsyncSession, org_with_a_drive: uuid.UUID, drive_id: uuid.UUID
) -> None:
    """A lost activity completion sends the job round again on a subtree that
    has already moved. Refusing there would fail a move that happened; the job
    reports the terminal state instead, and the retry budget stays for the
    crashes it exists for."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )
    actor = (
        await db.execute(
            text("SELECT id FROM users WHERE org_team_id = :org LIMIT 1"),
            {"org": org_with_a_drive},
        )
    ).scalar_one()
    op_id = OperationId(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO file_ops (id, org_team_id, drive_id, kind, actor, inverse, "
            "undoable_until, state, progress, errors, conflicts, result) VALUES "
            "(:id, :org, :drive, 'move', :actor, '{}'::jsonb, NULL, 'done', "
            "'{}'::jsonb, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb)"
        ),
        {"id": op_id, "org": org_with_a_drive, "drive": drive_id, "actor": actor},
    )
    await db.commit()

    assert await tasks.large_move(op_id, org_with_a_drive) == "done"


def test_the_batched_move_is_retried_on_the_default_queue() -> None:
    """It is resumable by cursor, so a crash is worth retrying — unlike the
    janitor, whose five-minute schedule is its recovery."""
    from worker.temporal.retry import ACTIVITY_POLICIES, FILES_NON_RETRYABLE

    policy = ACTIVITY_POLICIES[WorkflowType.FILES_LARGE_MOVE.value]

    assert QUEUE_FOR[WorkflowType.FILES_LARGE_MOVE] is TaskQueue.DEFAULT
    assert policy.retry.maximum_attempts > 1
    assert policy.retry.non_retryable_error_types == list(FILES_NON_RETRYABLE)
    assert policy.heartbeat_timeout is not None


# -- the whole ledger, wired ------------------------------------------------


async def test_no_sweeper_on_the_deployed_pass_reports_itself_skipped(
    tmp_path: Path, db: AsyncSession, org_with_a_drive: uuid.UUID
) -> None:
    """`skipped` is the report's word for "this row of the ledger has no reaper
    in this deployment". A pass built by the worker's own factory against a
    real store has every dependency there is, so not one sweeper may say it —
    the two that needed a driver's admin surface (`incoming_orphans`,
    `store_multipart_aborts`) included."""
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )

    deps = await tasks._deps(db, org_with_a_drive)
    assert deps.sweep is not None
    report = await run_janitor(deps.sweep, datetime.now(UTC))

    skipped = {outcome.name: outcome.reason for outcome in report.outcomes if outcome.skipped}
    assert skipped == {}
    assert {outcome.name for outcome in report.outcomes} == {kind.name for kind in JANITOR_ORDER}


# -- the marker repair, through the pass that actually runs it ---------------


async def _extra_domains(db: AsyncSession, org_team_id: uuid.UUID, count: int) -> list[uuid.UUID]:
    """More unresolved domains for an org that already owns a drive, oldest
    first — the shape the column's first ticks meet on a real deployment."""
    store_id = (
        await db.execute(
            text("SELECT store_id FROM dedup_domains WHERE org_team_id = :org LIMIT 1"),
            {"org": org_team_id},
        )
    ).scalar_one()
    made: list[uuid.UUID] = []
    for index in range(count):
        domain_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO dedup_domains "
                "(id, org_team_id, region, store_id, chunker_seed, hmac_key_id, created_at) "
                "VALUES (:id, :org, :region, :store, ''::bytea, '', :created)"
            ),
            {
                "id": domain_id,
                "org": org_team_id,
                "region": f"r{index}",
                "store": store_id,
                "created": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            },
        )
        made.append(domain_id)
    await db.commit()
    return made


async def test_a_real_pass_moves_past_the_domains_the_store_would_not_answer_for(
    tmp_path: Path,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    three_orgs_at_the_tail: tuple[uuid.UUID, ...],
) -> None:
    """The head-of-line case, driven by the job a schedule actually runs.

    Everything between the activity and the rows is real here — the deps the
    boot wires, the sweeper, its statement and the ordering it depends on, and
    the ``file_stores`` row the repair resolves this deployment's id from. The
    one thing standing in is the STORE's verdict, which is the boundary the
    sweeper injects anyway, so "the store will not answer for these two" can be
    stated at all.

    Tick one asks about the two oldest and gets nothing back; they stay owed.
    Tick two, with nothing handed between them, asks about the two behind
    them — which is the whole of the fix: without it the pass re-reads the same
    unanswerable page every five minutes and the rest are never settled.
    """
    org_team_id = three_orgs_at_the_tail[0]
    assert wire_files_jobs(
        _files_settings(
            files_enabled=True, files_store_provider="filesystem", files_store_root=tmp_path
        )
    )
    # The same store the boot was wired with: the pass resolves its own
    # deployment id from these settings, so a root that disagreed with the
    # wiring would look up a `file_stores` row for a different bucket.
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: _files_settings(
            files_enabled=True,
            files_janitor_org_budget=1,
            files_store_provider="filesystem",
            files_store_root=tmp_path,
        ),
    )
    # The row the repair reads its own deployment id from: without one there is
    # nothing to write into a marker, and the pass correctly asks the store
    # nothing at all.
    await db.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, endpoint, region, "
            "capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', '', :endpoint, '', '{}'::jsonb, ARRAY['single'])"
        ),
        {"id": uuid.uuid4(), "endpoint": str(tmp_path)},
    )
    await db.commit()
    owed = await _extra_domains(db, org_team_id, 4)
    silent = set(owed[:2])
    asked: list[uuid.UUID] = []

    async def verdict(*args: object, **kwargs: object) -> str:
        domain_id = args[2] if len(args) > 2 else kwargs["domain_id"]
        assert isinstance(domain_id, uuid.UUID)
        asked.append(domain_id)
        return "unknown" if domain_id in silent else "ours"

    monkeypatch.setattr(tasks, "stamp_domain_marker", verdict)
    before_mine = await _cursor_before(db, org_team_id)

    try:
        await tasks.janitor(datetime(2026, 9, 21, 12, 0, tzinfo=UTC), budget=2, after=before_mine)
        first_tick = list(asked)
        asked.clear()
        await tasks.janitor(datetime(2026, 9, 21, 12, 5, tzinfo=UTC), budget=2, after=before_mine)
    finally:
        await db.execute(
            text("DELETE FROM file_stores WHERE endpoint = :endpoint"),
            {"endpoint": str(tmp_path)},
        )
        await db.commit()

    assert first_tick == owed[:2]
    assert asked == owed[2:], "the second tick must not re-read the page the first could not settle"
    left = (
        await db.execute(
            text(
                "SELECT id FROM dedup_domains WHERE org_team_id = :org "
                "AND owner_marked_at IS NULL AND owner_marker_conflict_at IS NULL"
            ),
            {"org": org_team_id},
        )
    ).scalars()
    still_owed = {uuid.UUID(str(row)) for row in left}
    assert silent <= still_owed, "a store that said nothing settles nothing"
    assert still_owed.isdisjoint(owed[2:]), "the two behind it are settled and stay settled"
