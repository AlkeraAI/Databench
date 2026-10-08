"""Writing the ownership marker a creating request could not write.

The request that makes a dedup domain stamps its prefix under a deadline, so a
store that was unreachable leaves the statement owed. This is the half that
settles it: the worker's own write, against real bytes on a real store, driven
by the janitor's ``owner_markers`` sweeper.

The marker is the collector's only licence to touch a prefix, so the two things
that must never happen are here as their own cases: it is never overwritten,
and a prefix another deployment stamped is never reported as ours.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from alkera_core.config import Settings
from alkera_core.config import settings as process_settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.ownership import (
    OwnerStamp,
    marker_key,
    read_owner,
    store_identity,
    write_marker,
)
from alkera_core.files.store.scoped import ScopedStoreFactory
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.team import Team
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from structlog.testing import capture_logs
from worker.files_bootstrap import build_store_factory
from worker.tasks import files as tasks

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
MADE_AT = NOW - timedelta(days=3)
ANOTHER_DEPLOYMENT = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(process_settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@dataclass(frozen=True)
class StoreUnderTest:
    """One deployment's store: the settings that name it, and the factory the
    repair vends its handles from."""

    config: Settings
    factory: ScopedStoreFactory


def _filesystem_store(root: Path) -> StoreUnderTest:
    """A deployment serving Files from a directory — what a self-hosted install
    runs, and what every CI job runs, since none of them stands a bucket up."""
    root.mkdir(parents=True, exist_ok=True)
    config = process_settings.model_copy(
        update={
            "files_enabled": True,
            "files_store_provider": "filesystem",
            "files_store_root": root,
            # The filesystem driver addresses a directory, so the `file_stores`
            # row it resolves to carries no endpoint and no bucket; leaving the
            # S3 pair set would look up a row that names an object store.
            "files_store_endpoint": None,
            "files_store_bucket": None,
        }
    )
    return StoreUnderTest(config=config, factory=build_store_factory(config, clock=SystemClock()))


def _s3_store() -> StoreUnderTest:
    """The endpoint this session is configured against, when one is listening.

    Skipped rather than faked: the point of this leg is the real wire, and a
    session whose store is absent has already been redirected onto the
    filesystem driver by the root conftest — running it here would prove the
    same driver twice under a name that said otherwise.
    """
    if process_settings.files_store_provider == "filesystem":
        pytest.skip("this session runs the filesystem driver: no S3 endpoint to prove")
    return StoreUnderTest(
        config=process_settings.model_copy(update={"files_enabled": True}),
        factory=build_store_factory(process_settings, clock=SystemClock()),
    )


@pytest.fixture(params=["filesystem", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> StoreUnderTest:
    """The repair against every provider a deployment may actually run.

    Both halves matter and neither covers the other: CI has no object store, so
    only the filesystem leg runs there — and that is exactly the driver whose
    first write to a new domain was broken. A developer with the dev stack up
    runs both.
    """
    if request.param == "filesystem":
        return _filesystem_store(tmp_path / "bucket")
    return _s3_store()


@pytest.fixture
async def domain(db: AsyncSession, store: StoreUnderTest) -> AsyncIterator[uuid.UUID]:
    """A committed domain whose ``file_stores`` row is the one the settings
    above resolve to — which is what makes this database's deployment id the
    thing the marker names."""
    org = Team(id=uuid.uuid4(), parent_team_id=None, name=f"stamp-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    identity = store_identity(store.config)
    row = (
        await db.execute(
            text(
                "SELECT id FROM file_stores WHERE driver = :driver AND bucket = :bucket "
                "AND endpoint = :endpoint AND region = :region"
            ),
            identity,
        )
    ).first()
    store_row_id = uuid.uuid4() if row is None else uuid.UUID(str(row[0]))
    made_the_store_row = row is None
    if made_the_store_row:
        db.add(
            FileStore(
                id=store_row_id,
                driver=identity["driver"],
                bucket=identity["bucket"],
                endpoint=identity["endpoint"],
                region=identity["region"],
                capabilities={},
                transfer_modes=["single", "proxied"],
            )
        )
    await db.flush()
    domain_row = DedupDomain(
        id=uuid.uuid4(), org_team_id=org.id, region="", store_id=store_row_id, created_at=MADE_AT
    )
    db.add(domain_row)
    await db.flush()
    db.add(
        FileDrive(
            id=uuid.uuid4(),
            org_team_id=org.id,
            kind="org",
            store_id=store_row_id,
            dedup_domain_id=domain_row.id,
            quota_bytes=1 << 40,
            quota_nodes=1_000_000,
            next_ino=2,
        )
    )
    await db.commit()
    domain_id = uuid.UUID(str(domain_row.id))
    try:
        yield domain_id
    finally:
        await db.execute(
            text("DELETE FROM file_drives WHERE dedup_domain_id = :id"), {"id": domain_id}
        )
        await db.execute(text("DELETE FROM dedup_domains WHERE id = :id"), {"id": domain_id})
        if made_the_store_row:
            await db.execute(text("DELETE FROM file_stores WHERE id = :id"), {"id": store_row_id})
        await db.execute(text("DELETE FROM teams WHERE id = :id"), {"id": org.id})
        await db.commit()


async def marker_on(store: StoreUnderTest, domain_id: uuid.UUID) -> OwnerStamp | None:
    """What the store actually holds for ``domain_id``, read back through the
    same driver the collector reads it with."""
    return await read_owner(store.factory.admin(), str(domain_id))


async def test_an_unmarked_prefix_is_stamped_with_this_deployments_id(
    db: AsyncSession, store: StoreUnderTest, domain: uuid.UUID
) -> None:
    """The repair itself: bytes land under the domain's prefix naming this
    database's own ``file_stores`` row, which is the id the collector compares
    before it will consider the prefix at all."""
    config = store.config
    expected = (
        await db.execute(
            text(
                "SELECT id FROM file_stores WHERE driver = :driver AND bucket = :bucket "
                "AND endpoint = :endpoint AND region = :region"
            ),
            store_identity(config),
        )
    ).scalar_one()

    stamped = await tasks.stamp_domain_marker(
        db, store.factory, domain, created_at=MADE_AT, config=config
    )

    assert stamped == "ours"
    assert await marker_on(store, domain) == OwnerStamp(
        deployment_id=str(expected), written_at=MADE_AT
    )


async def test_a_prefix_this_deployment_already_marked_is_confirmed_not_rewritten(
    db: AsyncSession, store: StoreUnderTest, domain: uuid.UUID
) -> None:
    """A stamp that landed against a transaction that then rolled back leaves a
    marker with no record of it. The repair must recognise its own marker and
    say so — and must not restamp it, because the marker's date is evidence."""
    config = store.config
    await tasks.stamp_domain_marker(db, store.factory, domain, created_at=MADE_AT, config=config)
    first = await marker_on(store, domain)

    again = await tasks.stamp_domain_marker(
        db, store.factory, domain, created_at=NOW, config=config
    )

    assert again == "ours"
    assert await marker_on(store, domain) == first
    assert first is not None and first.written_at == MADE_AT


async def test_a_prefix_another_deployment_marked_is_left_alone_and_not_claimed(
    db: AsyncSession, store: StoreUnderTest, domain: uuid.UUID
) -> None:
    """Two databases can share one bucket — a restore, a blue/green pair, a
    staging stack pointed at production objects. Claiming a prefix that already
    names somebody else would hand the collector a licence to delete their live
    data, so the pass reports ``foreign`` and writes nothing.

    That verdict is also the only thing that ever tells an operator the two
    stacks are pointed at one bucket, so the line that says so — carrying both
    deployment ids and no byte of the prefix — is part of the contract.
    """
    config = store.config
    theirs = OwnerStamp(deployment_id=ANOTHER_DEPLOYMENT, written_at=NOW - timedelta(days=90))
    await write_marker(
        store.factory.admin(),
        ANOTHER_DEPLOYMENT,
        key=marker_key(str(domain)),
        now=NOW - timedelta(days=90),
    )

    with capture_logs() as said:
        stamped = await tasks.stamp_domain_marker(
            db, store.factory, domain, created_at=MADE_AT, config=config
        )

    assert stamped == "foreign"
    assert await marker_on(store, domain) == theirs
    reported = [line for line in said if line["event"] == "files.ownership.foreign_domain"]
    assert [(line["domain_id"], line["owner_deployment_id"]) for line in reported] == [
        (str(domain), ANOTHER_DEPLOYMENT)
    ]


def _foreign_count() -> float:
    """What the counter reads now. Absent until the first increment, which is
    the state a healthy deployment stays in."""
    value = REGISTRY.get_sample_value("files_domain_marker_foreign_total")
    return 0.0 if value is None else value


async def test_a_foreign_prefix_is_counted_once_and_a_domain_of_our_own_is_not(
    db: AsyncSession, store: StoreUnderTest, domain: uuid.UUID
) -> None:
    """The counter is how a fleet says "these two stacks share a bucket" without
    anybody reading a log, so it has to move exactly when that is true. One
    increment for the domain the pass found somebody else on, and none at all
    for the one it stamped itself — a counter that also climbed on our own
    domains would read as a conflict on every healthy deployment."""
    config = store.config
    other = uuid.uuid4()
    await write_marker(
        store.factory.admin(),
        ANOTHER_DEPLOYMENT,
        key=marker_key(str(domain)),
        now=NOW - timedelta(days=90),
    )
    before = _foreign_count()

    await tasks.stamp_domain_marker(db, store.factory, domain, created_at=MADE_AT, config=config)
    after_foreign = _foreign_count()
    await tasks.stamp_domain_marker(db, store.factory, other, created_at=MADE_AT, config=config)

    assert after_foreign == before + 1
    assert _foreign_count() == after_foreign


async def test_a_deployment_with_no_store_row_writes_nothing(
    db: AsyncSession, store: StoreUnderTest, domain: uuid.UUID, tmp_path: Path
) -> None:
    """The id in the marker IS this database's ``file_stores`` row for the
    bucket. Pointed at a bucket it has no row for, the pass has no name to
    write and must leave the prefix unmarked rather than invent one."""
    nowhere = _filesystem_store(tmp_path / "unknown-bucket")

    stamped = await tasks.stamp_domain_marker(
        db, nowhere.factory, domain, created_at=MADE_AT, config=nowhere.config
    )

    assert stamped == "unknown"
    assert await marker_on(nowhere, domain) is None


async def test_a_store_that_refuses_the_write_leaves_the_domain_owed(
    db: AsyncSession, store: StoreUnderTest, domain: uuid.UUID
) -> None:
    """One org's store being down is not the janitor pass's failure and not
    this domain's either: the next tick comes back for it."""
    config = store.config

    class RefusingFactory:
        def admin(self) -> object:
            raise OSError("the store is not answering")

    stamped = await tasks.stamp_domain_marker(
        db,
        RefusingFactory(),  # type: ignore[arg-type]
        domain,
        created_at=MADE_AT,
        config=config,
    )

    assert stamped == "unknown"
    assert await marker_on(store, domain) is None


# -- what a page of domains costs the database ------------------------------


class _CountingReads:
    """The deployment-id read, counted. It is a SELECT against ``file_stores``,
    so how many times a page of domains performs it is the whole subject."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.reads = 0

    async def __call__(self, session: object, config: object) -> str | None:
        self.reads += 1
        return self.answer


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("11111111-1111-4111-8111-111111111111", id="a deployment with a store row"),
        # The asymmetric case. `None` is also what "not read yet" looks like,
        # so a memo keyed on the value alone re-reads for every domain — and
        # this is precisely the deployment where every one of those reads is
        # wasted, because nothing can be settled without the row.
        pytest.param(None, id="a deployment with no store row"),
    ],
)
async def test_a_page_of_domains_reads_the_deployment_id_once(
    store: StoreUnderTest, answer: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One read per org per pass, whatever the answer was."""
    reads = _CountingReads(answer)
    monkeypatch.setattr(tasks, "deployment_id_for", reads)
    repair = tasks.DomainMarkerRepair(cast("Any", object()), store.factory, config=store.config)

    for _ in range(4):
        await repair(uuid.uuid4(), MADE_AT)

    assert reads.reads == 1


async def test_an_org_with_nothing_owed_does_not_read_it_at_all(
    store: StoreUnderTest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweeper builds one of these per org per pass and calls it only for
    the domains it actually has to ask about, so an org whose domains are all
    settled — every org, in the steady state — must cost nothing here."""
    reads = _CountingReads("11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(tasks, "deployment_id_for", reads)

    tasks.DomainMarkerRepair(cast("Any", object()), store.factory)

    assert reads.reads == 0
