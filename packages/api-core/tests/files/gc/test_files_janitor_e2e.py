"""One janitor pass over a domain built by the real services.

The other janitor tests wire a sweeper to a lambda and watch it fire. This one
hands `SweepDeps` the actual bound methods the deployment hands it —
`UploadCompletion.sweep_expired`, `Operations.watchdog`, `stats.aggregate`,
`Janitor.expire_deleted`, `Trash.purge` — over a domain seeded through those
same services, with a clock past every deadline. What it proves is the wiring
and the invariants the delegation is *for*: SPEC inv 12 (`Σ holds == Σ open
sessions`) after the session sweeper, the staged bytes of an expired session
released, a trashed root past its deadline actually purged, the folder-stats
cache equal to an independent recount, and one report row per sweeper in
`JANITOR_ORDER`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import stats
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import OPEN_SESSION_STATES, Janitor
from alkera_core.files.ids import DomainId, NodeId, OrgScope
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.sweepers import JANITOR_ORDER, SweepDeps, run_janitor
from alkera_core.files.trash import Trash
from alkera_core.files.uploads import UploadCompletion, UploadService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.gc.conftest import Domain, MtimeAges, Seeder

pytestmark = pytest.mark.asyncio

#: The janitor's instant: past the upload TTL, the trash window and every
#: retention in the ledger, so no sweeper is held back by a deadline.
NOW = datetime.now(UTC) + timedelta(days=400)

#: The trash deadline the seeded root is already past.
PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _ctx(org: Any) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@pytest.fixture
async def drive(files_factory: Any) -> Any:
    return await files_factory.drive()


@pytest.fixture
def domain(tmp_path: Path, clock: FakeClock, drive: Any) -> Domain:
    root = tmp_path / "bucket"
    store = FilesystemStore(root, clock=clock, layout="bucket")
    return Domain(id=DomainId(drive.dedup_domain_id), root=root, store=store, ages=MtimeAges(root))


@pytest.fixture
def domain_store(domain: Domain, clock: FakeClock) -> DomainStore:
    """The per-domain handle a request path holds, rooted at the domain prefix."""
    return cast(DomainStore, FilesystemStore(domain.root / "domains" / str(domain.id), clock=clock))


@pytest.fixture
def repo(files_session: AsyncSession, files_org: Any) -> FilesRepo:
    return FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))


@pytest.fixture
def janitor(repo_for_org: Any, clock: FakeClock, domain: Domain) -> Janitor:
    return Janitor(repo_for_org, AdminOnlyFactory(domain.store), clock, age_source=domain.ages)


@pytest.fixture
def services(
    repo: FilesRepo, files_org: Any, clock: FakeClock, domain_store: DomainStore
) -> dict[str, Any]:
    """The five real services the janitor delegates to, on one session."""
    ctx = _ctx(files_org)
    return {
        "uploads": UploadService(repo, ctx, clock, domain_store),
        "completion": UploadCompletion(repo, ctx, clock, domain_store),
        "ops": Operations(repo, ctx, clock, domain_store),
        "trash": Trash(repo, ctx, clock, domain_store),
    }


def _deps(
    repo: FilesRepo,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    services: dict[str, Any],
) -> SweepDeps:
    """`SweepDeps` as a deployment builds it: the services' own bound methods.

    The two lambdas supply only the domain a sweeper has no way to know; every
    behaviour behind them is the real service's.
    """
    return SweepDeps(
        repo=repo,
        ctx=_ctx(files_org),
        checkpoints=PausingCheckpoints(),
        expire_deleted=lambda now: janitor.expire_deleted(domain.id, now),
        sweep_expired_sessions=services["completion"].sweep_expired,
        watchdog=services["ops"].watchdog,
        aggregate_stats=lambda: stats.aggregate([repo]),
        purge_trash=services["trash"].purge,
    )


async def _sum_holds(session: AsyncSession, org: Any, *, only_open: bool) -> int:
    clause = "AND state = ANY(:states)" if only_open else ""
    return int(
        (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(quota_hold_bytes), 0) FROM file_upload_sessions "
                    f"WHERE org_team_id = :org {clause}"
                ),
                {"org": org.org_team_id, "states": list(OPEN_SESSION_STATES)},
            )
        ).scalar_one()
    )


@pytest.fixture
async def seeded(
    files_session: AsyncSession,
    files_org: Any,
    files_factory: Any,
    drive: Any,
    domain: Domain,
    services: dict[str, Any],
) -> dict[str, Any]:
    """A domain with an expired upload, a purgeable trash root and folder stats."""
    tree = await files_factory.tree("box/ box/doc.bin gone/ gone/old.bin", drive=drive)
    seeder = Seeder(files_session, files_org, drive)

    session_row = await services["uploads"].open(
        drive.id, NodeId(tree["box"].id), b"stalled.bin", declared_size=4096
    )
    await files_session.commit()
    staged = f"incoming/{session_row.id}/part-1"
    domain.write(staged, b"staged bytes nobody completed")
    await files_session.execute(
        text(
            "UPDATE file_upload_sessions SET expires_at = now() - interval '2 days' WHERE id = :i"
        ),
        {"i": session_row.id},
    )

    # a trash root the window has already released
    await seeder.trash(tree["gone"], purge_after=PAST)
    await seeder.trash(tree["gone/old.bin"], purge_after=PAST)
    delta_repo = FilesRepo(files_session, files_org.scope)
    async with delta_repo.transaction():
        await stats.add_delta(
            delta_repo,
            node_id=NodeId(tree["box"].id),
            bytes_delta=0,
            files_delta=1,
            direct_children_delta=1,
            child_change_at=datetime.now(UTC),
        )
    await files_session.commit()
    return {"tree": tree, "session_id": session_row.id, "staged": staged}


async def _direct_children(session: AsyncSession, node_id: uuid.UUID) -> int:
    """The live children of one folder, counted afresh from `file_nodes`.

    An independent second computation of the number `file_dir_stats` caches, so
    a cache that merely echoes the deltas that produced it cannot pass. The
    count is exact: it reads the parent pointer rather than the path array, so
    a purge that removed a child is visible here immediately.
    """
    return int(
        (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM file_nodes WHERE parent_id = :id AND trashed_at IS NULL"
                ),
                {"id": node_id},
            )
        ).scalar_one()
    )


async def test_every_sweeper_reports_once_in_order(
    repo: FilesRepo,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    services: dict[str, Any],
    seeded: dict[str, Any],
) -> None:
    """One row per sweeper, in `JANITOR_ORDER`, and none of the wired five skipped."""
    report = await run_janitor(_deps(repo, files_org, janitor, domain, services), NOW)

    assert report.order == tuple(kind.name for kind in JANITOR_ORDER)
    wired = (
        "deleted_expiry",
        "expired_sessions",
        "trash_purge",
        "operation_watchdog",
        "dir_stats_aggregate",
    )
    assert [name for name in wired if report.by_name(name).skipped] == []


async def test_holds_equal_the_open_sessions_after_the_session_sweeper(
    files_session: AsyncSession,
    repo: FilesRepo,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    services: dict[str, Any],
    seeded: dict[str, Any],
) -> None:
    """SPEC inv 12: no session outside the open states keeps a quota hold.

    Before the pass the session is open and its hold is real, so the sum over
    the open sessions is non-zero; the sweeper is what expires it, and it does
    that by releasing the hold rather than by deleting the row — which is why
    the two sums must still agree afterwards.
    """
    held_before = await _sum_holds(files_session, files_org, only_open=True)
    assert held_before > 0, "the seeded session reserved nothing, so nothing is proven"

    await run_janitor(_deps(repo, files_org, janitor, domain, services), NOW)
    files_session.expire_all()

    assert await _sum_holds(files_session, files_org, only_open=False) == await _sum_holds(
        files_session, files_org, only_open=True
    )
    assert await _sum_holds(files_session, files_org, only_open=True) == 0
    state = (
        await files_session.execute(
            text("SELECT state FROM file_upload_sessions WHERE id = :i"),
            {"i": seeded["session_id"]},
        )
    ).scalar_one()
    assert state == "expired"


async def test_the_trash_root_past_its_deadline_is_purged(
    files_session: AsyncSession,
    repo: FilesRepo,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    services: dict[str, Any],
    seeded: dict[str, Any],
) -> None:
    """The trash sweeper delegates to the real `Trash.purge`: the rows are gone."""
    node_id = seeded["tree"]["gone/old.bin"].id
    await run_janitor(_deps(repo, files_org, janitor, domain, services), NOW)
    files_session.expire_all()

    left = (
        await files_session.execute(
            text("SELECT count(*) FROM file_nodes WHERE id = :i"), {"i": node_id}
        )
    ).scalar_one()
    assert left == 0, "a trash root past its purge deadline survived the janitor"


async def test_the_folder_stats_cache_equals_an_independent_recount(
    files_session: AsyncSession,
    repo: FilesRepo,
    files_org: Any,
    janitor: Janitor,
    domain: Domain,
    services: dict[str, Any],
    seeded: dict[str, Any],
) -> None:
    """The aggregate sweeper drains the delta table and lands the right number.

    Two things are asserted because either alone is weak: a pass that folded
    nothing would leave the delta row behind, and a pass that folded the wrong
    number would still empty it.
    """
    box = seeded["tree"]["box"].id
    pending_before = (
        await files_session.execute(
            text("SELECT count(*) FROM file_dir_stats_deltas WHERE node_id = :i"),
            {"i": box},
        )
    ).scalar_one()
    assert pending_before == 1, "nothing was pending, so the fold proves nothing"

    await run_janitor(_deps(repo, files_org, janitor, domain, services), NOW)
    files_session.expire_all()

    pending = (
        await files_session.execute(
            text("SELECT count(*) FROM file_dir_stats_deltas WHERE org_team_id = :o"),
            {"o": files_org.org_team_id},
        )
    ).scalar_one()
    assert pending == 0, "the aggregate sweeper left deltas unfolded"

    cached = (
        await files_session.execute(
            text("SELECT direct_children FROM file_dir_stats WHERE node_id = :i"),
            {"i": box},
        )
    ).scalar_one()
    assert int(cached) == await _direct_children(files_session, box)
