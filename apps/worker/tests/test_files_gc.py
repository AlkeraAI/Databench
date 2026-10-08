"""The Files collection job: the core against real rows, and its wiring.

The library's own cases (``packages/api-core/tests/files/gc/``) pin what the
collector takes and refuses. What these pin is the worker layer around it: that
"which domains still exist" is answered by the drive rows and nothing else, that
the job refuses rather than inventing a bucket when the deployment serves no
Files, that the boot which wires the per-org jobs wires this one from the SAME
store, and that the activity, its retry policy and its schedule say what the
design says they say.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alkera_core.config import settings as process_settings
from alkera_core.files import gc
from alkera_core.files.clock import SystemClock
from alkera_core.files.gc import ORPHAN_GRACE
from alkera_core.files.ownership import (
    deployment_id_for,
    marker_body,
    marker_key,
    store_identity,
)
from alkera_core.files.store.scoped import FilesystemScoped
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.team import Team
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from worker.activities import files as activities
from worker.schedules import SCHEDULES
from worker.tasks import files as tasks
from worker.temporal.retry import policy_for

pytestmark = pytest.mark.usefixtures("unwired")

NOW = datetime.now(UTC)
OLD = NOW - ORPHAN_GRACE - timedelta(hours=1)


@pytest.fixture
def unwired() -> AsyncIterator[None]:
    """The store factory is process-global; no case may leak its wiring."""
    yield None
    tasks.reset_store_factory()
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


async def seed_domain_with_a_drive(db: AsyncSession) -> uuid.UUID:
    """Commit a tenant whose drive names a dedup domain; returns the domain id.

    The drive row is the whole subject here: it is what makes a domain 'still
    named', and dropping it is what makes the same bytes collectable.
    """
    org = Team(id=uuid.uuid4(), parent_team_id=None, name=f"files-gc-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/files-gc-test/{uuid.uuid4().hex}",
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
    return uuid.UUID(str(domain.id))


async def forget_domain(db: AsyncSession, domain_id: uuid.UUID) -> None:
    """Drop the rows the seed committed, drive first — exactly the shape a
    tenant deletion (or a dropped test database) leaves behind."""
    row = (
        await db.execute(
            text("SELECT org_team_id, store_id FROM dedup_domains WHERE id = :id"),
            {"id": domain_id},
        )
    ).first()
    if row is None:
        return
    org_team_id, store_id = row
    await db.execute(text("DELETE FROM file_drives WHERE dedup_domain_id = :id"), {"id": domain_id})
    await db.execute(text("DELETE FROM dedup_domains WHERE id = :id"), {"id": domain_id})
    await db.execute(text("DELETE FROM file_stores WHERE id = :id"), {"id": store_id})
    await db.execute(text("DELETE FROM teams WHERE id = :id"), {"id": org_team_id})
    await db.commit()


@pytest.fixture
async def domain(db: AsyncSession) -> AsyncIterator[uuid.UUID]:
    domain_id = await seed_domain_with_a_drive(db)
    try:
        yield domain_id
    finally:
        await forget_domain(db, domain_id)


@pytest.fixture
def bucket(tmp_path: Path) -> Path:
    """This case's object store: a filesystem bucket above every domain."""
    root = tmp_path / "bucket"
    root.mkdir()
    tasks.set_store_factory(FilesystemScoped(root, clock=SystemClock()))
    return root


def gc_config(bucket: Path, **overrides: object) -> object:
    """Settings for a deployment that serves Files from ``bucket`` with the
    pass switched on and the breaker opened wide; a case about the switch or
    the breaker overrides exactly that."""
    return process_settings.model_copy(
        update={
            "files_enabled": True,
            "files_gc_enabled": True,
            "files_gc_max_orphan_fraction": 1.0,
            "files_store_provider": "filesystem",
            "files_store_root": bucket,
            **overrides,
        }
    )


@pytest.fixture
async def deployment(
    db: AsyncSession, bucket: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """This deployment's identity: the ``file_stores`` row for ``bucket``, and
    a second tenant so the database names at least one domain throughout —
    the state every deployment with a customer is in."""
    config = gc_config(bucket)
    monkeypatch.setattr(tasks, "get_settings", lambda: config)
    # The row is addressed by the same four columns the API writes it under,
    # whatever this machine's environment happens to configure -- and another
    # suite on this database may already own that identity, so it is read
    # first and created only when absent, exactly as the API does.
    existing = await deployment_id_for(db, config)  # type: ignore[arg-type]
    await db.rollback()
    created: uuid.UUID | None = None
    if existing is None:
        created = uuid.uuid4()
        db.add(
            FileStore(
                id=created,
                capabilities={},
                transfer_modes=["single", "proxied"],
                **store_identity(config),  # type: ignore[arg-type]
            )
        )
        await db.commit()
    anchor = await seed_domain_with_a_drive(db)
    try:
        yield existing or str(created)
    finally:
        await forget_domain(db, anchor)
        if created is not None:
            await db.execute(text("DELETE FROM file_stores WHERE id = :id"), {"id": created})
            await db.commit()


def put(
    root: Path,
    domain_id: uuid.UUID,
    relative: str,
    *,
    at: datetime,
    owner: str | None = None,
) -> str:
    """Write an object, and with ``owner`` stamp its domain as that deployment's."""
    import os

    key = f"domains/{domain_id}/{relative}"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"bytes")
    os.utime(path, (at.timestamp(), at.timestamp()))
    if owner is not None:
        marker = root / marker_key(str(domain_id))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(marker_body(owner, written_at=at))
        os.utime(marker, (at.timestamp(), at.timestamp()))
    return key


@pytest.fixture
def on_this_session(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the core against the case's own session, committed rows and all."""

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    monkeypatch.setattr(tasks, "_session", _session)


# -- which domains still exist ----------------------------------------------


async def test_known_domains_is_answered_by_the_drive_rows(
    db: AsyncSession, domain: uuid.UUID
) -> None:
    """A domain exists because a drive names it. Nothing else in the schema
    does, so nothing else may be consulted — and the answer must change the
    moment the drive row goes."""
    assert str(domain) in await tasks.known_domains(db)

    await forget_domain(db, domain)

    assert str(domain) not in await tasks.known_domains(db)


async def test_known_rows_carry_when_the_newest_domain_row_was_made(
    db: AsyncSession, domain: uuid.UUID
) -> None:
    """The restore guard compares markers against this instant, so it has to
    be the newest domain row's own creation time and move when a newer domain
    appears."""
    first = await tasks.known_rows(db)
    assert first.newest_at is not None
    created = (
        await db.execute(
            text("SELECT created_at FROM dedup_domains WHERE id = :id"), {"id": domain}
        )
    ).scalar_one()
    await db.rollback()
    assert first.newest_at >= created

    newer = await seed_domain_with_a_drive(db)
    try:
        second = await tasks.known_rows(db)
    finally:
        await forget_domain(db, newer)
    assert second.newest_at is not None and second.newest_at >= first.newest_at
    assert str(newer) in second.ids


@pytest.mark.usefixtures("on_this_session")
async def test_a_database_restored_from_before_a_domain_was_made_refuses_to_collect_it(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """The restore, as this worker sees it: a domain stamped with its own id
    whose marker is newer than every domain row it holds. Nothing moves and
    the verdict names the shape."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    await forget_domain(db, domain)
    stamped_after_every_row = NOW + timedelta(hours=1)
    marker = bucket / marker_key(str(domain))
    marker.write_bytes(marker_body(deployment, written_at=stamped_after_every_row))

    report = await tasks.files_gc(NOW, allow_mass_collect=True)

    assert report.verdict == gc.REFUSED_ROWS_OLDER_THAN_MARKER
    assert report.moved == ()
    assert (bucket / key).is_file()


# -- the core ---------------------------------------------------------------


@pytest.mark.usefixtures("on_this_session")
async def test_a_domain_a_drive_row_names_keeps_its_bytes(
    bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """The live tenant's objects belong to the per-org janitor, which reads
    their roots. This pass must leave them exactly where they are."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)

    report = await tasks.files_gc(NOW)

    assert str(domain) in report.scanned
    assert report.orphaned == ()
    assert report.moved == ()
    assert (bucket / key).is_file()


@pytest.mark.usefixtures("on_this_session")
async def test_the_bytes_of_a_domain_whose_rows_are_gone_are_collected(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """The debris this job exists for: a run writes objects, its rows are
    dropped, and no per-org pass will ever visit them again."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    await forget_domain(db, domain)

    report = await tasks.files_gc(NOW)

    assert report.orphaned == (str(domain),)
    assert report.moved == (key,)
    assert not (bucket / key).exists()
    assert (bucket / f"domains/{domain}/deleted/objects/aa").is_file()


@pytest.mark.usefixtures("on_this_session")
async def test_a_dry_run_reports_the_same_bytes_and_moves_none(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """What an operator runs first against a bucket they are unsure about."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    await forget_domain(db, domain)

    report = await tasks.files_gc(NOW, dry_run=True)

    assert report.moved == (key,)
    assert (bucket / key).is_file()


@pytest.mark.usefixtures("on_this_session")
async def test_an_object_inside_the_grace_survives_the_pass(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """A drive row commits after the first bytes land. Collecting on that
    window would eat an upload that is still in flight."""
    key = put(bucket, domain, "objects/fresh", at=NOW - timedelta(minutes=1), owner=deployment)
    await forget_domain(db, domain)

    report = await tasks.files_gc(NOW)

    assert report.moved == ()
    assert report.kept_young == 1
    assert (bucket / key).is_file()


async def test_the_job_refuses_when_the_deployment_wired_no_store(tmp_path: Path) -> None:
    """Which bucket a worker sweeps is a deployment decision. A job that
    guessed one could delete a stranger's objects."""
    tasks.reset_store_factory()

    with pytest.raises(tasks.FilesJobsNotWiredError, match="set_store_factory"):
        await tasks.files_gc(NOW, config=gc_config(tmp_path))  # type: ignore[arg-type]


# -- the switch -------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"files_enabled": False}, id="files-not-served"),
        pytest.param({"files_gc_enabled": False}, id="pass-switched-off"),
        pytest.param({"files_gc_enabled": None, "app_env": "production"}, id="unset-outside-local"),
    ],
)
async def test_a_deployment_with_the_pass_off_skips_cleanly(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """No store is wired here at all, which is what a deployment that serves
    no Files looks like: the job must answer ``disabled`` rather than raise,
    because a raise is a failed workflow and five retries, every day."""
    tasks.reset_store_factory()

    report = await tasks.files_gc(NOW, config=gc_config(tmp_path, **overrides))  # type: ignore[arg-type]

    assert report.verdict == gc.VERDICT_DISABLED
    assert report.scanned == () and report.moved == ()


def test_the_pass_is_on_by_default_only_on_a_developers_machine() -> None:
    local = process_settings.model_copy(
        update={"files_enabled": True, "files_gc_enabled": None, "app_env": "local"}
    )
    staging = local.model_copy(update={"app_env": "staging"})
    opted_in = staging.model_copy(update={"files_gc_enabled": True})

    assert local.files_gc_active is True
    assert staging.files_gc_active is False
    assert opted_in.files_gc_active is True


def test_no_schedule_is_created_for_a_pass_that_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_core import config as core_config
    from worker import schedules

    off = process_settings.model_copy(update={"files_enabled": False})
    monkeypatch.setattr(core_config, "get_settings", lambda: off)
    assert schedules._default_servable(WorkflowType.FILES_GC) is False
    assert schedules._default_servable(WorkflowType.FILES_JANITOR) is True

    on = process_settings.model_copy(update={"files_enabled": True, "files_gc_enabled": True})
    monkeypatch.setattr(core_config, "get_settings", lambda: on)
    assert schedules._default_servable(WorkflowType.FILES_GC) is True


# -- a database that cannot see its own rows --------------------------------


@pytest.fixture
async def as_a_login_the_policy_binds(db: AsyncSession) -> AsyncIterator[None]:
    """The session a production worker has when its login does not bypass row
    security: every Files policy applies and no tenant is set."""
    await db.execute(text("SET ROLE alkera_files_app"))
    await db.commit()
    try:
        yield None
    finally:
        await db.rollback()
        await db.execute(text("RESET ROLE"))
        await db.commit()


async def test_a_login_the_policy_binds_cannot_answer_known_domains(
    db: AsyncSession, domain: uuid.UUID, as_a_login_the_policy_binds: None
) -> None:
    """Unscoped, this login reads ``file_drives`` as empty with no error. The
    answer has to be a refusal, never the empty set."""
    filtered = (await db.execute(text("SELECT count(*) FROM file_drives"))).scalar_one()
    await db.rollback()
    assert filtered == 0, "the stand-in login is supposed to be bound by the policy"

    with pytest.raises(gc.GcRefused) as refused:
        await tasks.known_domains(db)

    assert refused.value.code == gc.REFUSED_KNOWN_UNREADABLE


async def test_a_login_the_policy_binds_cannot_list_the_orgs_the_janitor_sweeps(
    db: AsyncSession, domain: uuid.UUID, as_a_login_the_policy_binds: None
) -> None:
    """The janitor's org listing is the same unscoped read of the same table.
    Filtered to nothing it sweeps nobody and says nothing, every five minutes,
    so quota holds and trash are never released. It has to fail out loud."""
    from alkera_core.db.cross_tenant import CrossTenantReadRefused

    with pytest.raises(CrossTenantReadRefused):
        await tasks.orgs_with_drives(db)


@pytest.mark.usefixtures("on_this_session")
async def test_a_pass_under_a_login_the_policy_binds_parks_nothing(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """The whole hazard, end to end: a live tenant's aged bytes, a worker whose
    login cannot see the drive row that names them. Nothing moves, and the
    result says why."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    await db.execute(text("SET ROLE alkera_files_app"))
    await db.commit()
    try:
        report = await tasks.files_gc(NOW, allow_mass_collect=True)
    finally:
        await db.rollback()
        await db.execute(text("RESET ROLE"))
        await db.commit()

    assert report.verdict == gc.REFUSED_KNOWN_UNREADABLE
    assert report.moved == ()
    assert (bucket / key).is_file()


# -- whose bucket it is -----------------------------------------------------


@pytest.mark.usefixtures("on_this_session")
async def test_a_domain_stamped_by_another_deployment_is_left_alone(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """A second stack on the same bucket: its domain is unknown HERE, and that
    is exactly the case the stamp exists for."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=str(uuid.uuid4()))
    await forget_domain(db, domain)

    report = await tasks.files_gc(NOW)

    assert report.foreign == (str(domain),)
    assert report.orphaned == () and report.moved == ()
    assert (bucket / key).is_file()


@pytest.mark.usefixtures("on_this_session")
async def test_an_unstamped_domain_is_collected_only_after_it_is_adopted(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    key = put(bucket, domain, "objects/aa", at=OLD)
    await forget_domain(db, domain)

    before = await tasks.files_gc(NOW)
    assert before.unstamped == (str(domain),)
    assert (bucket / key).is_file()

    adopted = await tasks.files_gc_adopt((str(domain),))
    assert adopted.stamped == (str(domain),)
    again = await tasks.files_gc_adopt((str(domain),))
    assert again.stamped == () and again.already_ours == (str(domain),)

    after = await tasks.files_gc(NOW)
    assert after.moved == (key,)


@pytest.mark.usefixtures("on_this_session")
async def test_adopting_never_restamps_another_deployments_domain(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    theirs = str(uuid.uuid4())
    put(bucket, domain, "objects/aa", at=OLD, owner=theirs)

    report = await tasks.files_gc_adopt((str(domain),))

    assert report.foreign == (str(domain),)
    assert theirs.encode() in (bucket / marker_key(str(domain))).read_bytes()


@pytest.mark.usefixtures("on_this_session")
async def test_the_breaker_holds_a_lone_orphan_until_the_operator_overrides_it(
    db: AsyncSession,
    bucket: Path,
    domain: uuid.UUID,
    deployment: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default threshold, through the job: the only domain in the bucket
    reads as an orphan, which is every domain, which is refused."""
    monkeypatch.setattr(
        tasks, "get_settings", lambda: gc_config(bucket, files_gc_max_orphan_fraction=0.1)
    )
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    await forget_domain(db, domain)

    held = await tasks.files_gc(NOW)
    assert held.verdict == gc.REFUSED_MASS_COLLECT
    assert (bucket / key).is_file()

    released = await tasks.files_gc(NOW, allow_mass_collect=True)
    assert released.verdict == "ok"
    assert released.moved == (key,)


# -- the wiring -------------------------------------------------------------


async def test_the_worker_boot_wires_the_collector_at_the_store_the_settings_name(
    tmp_path: Path,
) -> None:
    """The per-org jobs and the bucket-wide one must never be pointed at
    different stores: a collector reading a bucket the writers do not use would
    call every live object an orphan. So the wiring is proven by what the
    collector's own handle can SEE — an object written where the settings say
    the store is — not by the wiring call returning True."""
    from worker.files_bootstrap import wire_files_jobs

    root = tmp_path / "store"
    config = process_settings.model_copy(
        update={
            "files_enabled": True,
            "files_store_provider": "filesystem",
            "files_store_root": root,
        }
    )
    assert wire_files_jobs(config) is True
    factory = tasks._store_factory
    assert factory is not None

    domain_id = uuid.uuid4()
    key = put(root, domain_id, "objects/aa", at=OLD)
    page = await factory.admin().list_prefix("domains/", after=None, limit=10)

    assert key in page.keys
    # And the per-org handle is the same store, one domain down.
    handle = await factory.for_domain(domain_id)  # type: ignore[arg-type]
    assert await handle.head("objects/aa") is not None


async def test_a_deployment_that_serves_no_files_leaves_the_collector_unwired() -> None:
    """The worker still boots and serves every other family; the Files jobs
    refuse instead of sweeping a store nobody configured."""
    from worker.files_bootstrap import wire_files_jobs

    config = process_settings.model_copy(update={"files_enabled": False})

    assert wire_files_jobs(config) is False
    assert tasks._store_factory is None


# -- the activity, the policy, the schedule ---------------------------------


@pytest.mark.usefixtures("on_this_session")
async def test_the_activity_reports_the_pass_as_counts(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """A page may park thousands of objects; a workflow history is not where
    their keys belong. The activity answers with counts and the cursor."""
    put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    put(bucket, domain, "objects/young", at=NOW)
    await forget_domain(db, domain)

    page = await activities.files_gc(SweepInput(now=NOW))

    assert page.domains_scanned == 1
    assert page.orphan_domains == 1
    assert page.parked == 1
    assert page.bytes_parked == 5
    assert page.kept_inside_grace == 1
    assert page.cursor is None
    assert page.verdict == "ok"


@pytest.mark.usefixtures("on_this_session")
async def test_the_activity_passes_the_dry_run_through(
    db: AsyncSession, bucket: Path, domain: uuid.UUID, deployment: str
) -> None:
    """The operator's ``--dry-run`` has to survive the whole stack, or it moves
    bytes it promised not to."""
    key = put(bucket, domain, "objects/aa", at=OLD, owner=deployment)
    await forget_domain(db, domain)

    page = await activities.files_gc(SweepInput(now=NOW), None, True)

    assert page.parked == 1
    assert (bucket / key).is_file()


def test_the_collector_runs_on_the_housekeeping_queue() -> None:
    """Not the money queue and not a queue of its own: it is housekeeping, and
    every deployment that runs the janitor already serves this one."""
    assert QUEUE_FOR[WorkflowType.FILES_GC] is TaskQueue.DEFAULT
    assert tasks.GC_QUEUE is TaskQueue.DEFAULT


def test_the_collector_retries_a_transient_store_failure() -> None:
    """Unlike the janitor, whose five-minute schedule is its recovery: a daily
    job that gave up on one browned-out call would leave the bytes for a day.
    Safe to retry because a parked object is no longer where the walk reads."""
    policy = policy_for(WorkflowType.FILES_GC.value)

    assert policy.retry.maximum_attempts is not None
    assert policy.retry.maximum_attempts > 1
    assert policy.heartbeat_timeout is not None
    assert policy.start_to_close >= timedelta(minutes=10)


def test_the_collector_is_a_daily_entry_in_the_catalog() -> None:
    """It is scheduled, on both prod and dev, by the same catalog every other
    recurring job lives in — not by an operator remembering to run it."""
    entry = next(e for e in SCHEDULES if e.workflow is WorkflowType.FILES_GC)

    assert entry.id == "files-gc"
    assert entry.cron is not None
    assert entry.queue is TaskQueue.DEFAULT
    assert entry.execution_timeout >= timedelta(hours=1)


def test_the_collector_is_not_folded_into_the_five_minute_janitor() -> None:
    """A bucket-wide listing every five minutes is a cost with no benefit: a
    collected object is a week from erasure either way."""
    janitor = next(e for e in SCHEDULES if e.workflow is WorkflowType.FILES_JANITOR)

    assert janitor.every == timedelta(minutes=5)
    assert next(e for e in SCHEDULES if e.workflow is WorkflowType.FILES_GC).every is None
