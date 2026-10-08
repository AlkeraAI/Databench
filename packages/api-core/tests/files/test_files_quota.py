"""Quota holds: the Σ-holds invariant, the two-open race, the ceilings, freezing.

Every test here runs against real Postgres on this run's lane database, through
``FilesRepo.transaction()`` so the unprivileged role and the org GUC are in
force exactly as they are in production. Nothing is mocked: the concurrency test
uses two real connections and forces the interleaving with a checkpoint rather
than a sleep, and the expiry test moves the injected clock across the TTL
boundary rather than pinning a fixed ``now``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files import stats
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import Conflict, NotFound, QuotaExceeded
from alkera_core.files.ids import DriveId, NodeId, OrgScope, SessionId
from alkera_core.files.quota import FROZEN_CODE, OVER_QUOTA, QuotaService, UserCeiling
from alkera_core.files.repo import FilesRepo
from alkera_core.files.uploads import UploadCompletion
from alkera_core.models.files.history import FileDirStats, FileDirStatsDelta
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.uploads import FileUploadSession
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from hypothesis.strategies import integers
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.engine import close_hypothesis_runner, hypothesis_runner
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

TTL = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Rig:
    """A drive with known ceilings plus the service under test."""

    repo: FilesRepo
    quota: QuotaService
    drive: FileDrive
    org: FilesOrg
    clock: FakeClock

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive.id)


def _root(rig: Rig) -> NodeId:
    """The drive root, standing in for the changed node's parent.

    A head swap charges the folder the node lives in; in these rigs the node is
    a direct child of the root, so the root IS the parent — which also keeps the
    delta rows readable by :func:`_root_deltas`.
    """
    assert rig.drive.root_node_id is not None
    return NodeId(rig.drive.root_node_id)


def _ctx(org_id: uuid.UUID | None = None) -> ActingContext:
    """The acting context is carried, not consulted, by the quota decision."""
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(uuid.uuid4()),
            org_id=org_id or uuid.uuid4(),
        )
    )


async def _set_quota(
    session: AsyncSession, drive: FileDrive, *, quota_bytes: int, quota_nodes: int
) -> None:
    await session.execute(
        update(FileDrive.__table__)
        .where(FileDrive.id == drive.id)
        .values(quota_bytes=quota_bytes, quota_nodes=quota_nodes)
    )
    await session.commit()
    drive.quota_bytes = quota_bytes
    drive.quota_nodes = quota_nodes


async def _make_rig(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    quota_bytes: int = 1000,
    quota_nodes: int = 100,
    checkpoints: PausingCheckpoints | None = None,
) -> Rig:
    drive = await files_factory.drive()
    await _set_quota(files_session, drive, quota_bytes=quota_bytes, quota_nodes=quota_nodes)
    repo = FilesRepo(files_session, files_org.scope)
    kwargs = {"checkpoints": checkpoints} if checkpoints is not None else {}
    quota = QuotaService(repo, _ctx(), clock, None, **kwargs)  # type: ignore[arg-type]
    return Rig(repo=repo, quota=quota, drive=drive, org=files_org, clock=clock)


async def _open_session(
    repo: FilesRepo,
    drive: FileDrive,
    *,
    expires_at: datetime,
    declared_size: int = 0,
    state: str = "open",
    id: uuid.UUID | None = None,
) -> FileUploadSession:
    """One live upload session on the drive, its hold still at zero."""
    assert drive.root_node_id is not None
    row = FileUploadSession(
        id=id or uuid.uuid4(),
        org_team_id=drive.org_team_id,
        drive_id=drive.id,
        parent_id=drive.root_node_id,
        name=b"payload.bin",
        dedup_domain_id=drive.dedup_domain_id,
        state=state,
        declared_size=declared_size,
        expires_at=expires_at,
    )
    await repo.add(row)
    await repo.flush()
    return row


async def _sum_holds(session: AsyncSession, drive: FileDrive) -> tuple[int, int]:
    """Σ of the hold columns over *every* session row, live or not.

    Read outside the service, and deliberately unfiltered by state: the
    invariant is that a session which is no longer open holds nothing, so a
    filtered read would hide exactly the bug this checks for.
    """
    result = await session.execute(
        select(
            FileUploadSession.state,
            FileUploadSession.quota_hold_bytes,
            FileUploadSession.quota_hold_nodes,
        ).where(FileUploadSession.drive_id == drive.id)
    )
    total_bytes = 0
    total_nodes = 0
    for state, held_bytes, held_nodes in result.all():
        assert state in ("open", "uploading", "committing") or (held_bytes, held_nodes) == (0, 0), (
            f"a {state} session still holds {held_bytes}/{held_nodes}"
        )
        total_bytes += held_bytes
        total_nodes += held_nodes
    return total_bytes, total_nodes


async def _root_deltas(session: AsyncSession, drive: FileDrive) -> list[int]:
    """Every ``bytes_delta`` appended for the drive root, oldest first.

    Read straight off the append-only table rather than off the folded cache:
    the row is what the Layer 8 aggregator replays, so a fold with no row behind
    it would be a number nothing could rebuild.
    """
    rows = await session.execute(
        select(FileDirStatsDelta.bytes_delta)
        .where(FileDirStatsDelta.node_id == drive.root_node_id)
        .order_by(FileDirStatsDelta.id)
    )
    return [int(value) for value in rows.scalars().all()]


async def _set_used(session: AsyncSession, drive: FileDrive, *, bytes: int, files: int) -> None:
    """Seed the drive root's aggregate — what ``reserve`` reads as *used*."""
    assert drive.root_node_id is not None
    existing = await session.execute(
        update(FileDirStats.__table__)
        .where(FileDirStats.node_id == drive.root_node_id)
        .values(bytes=bytes, files=files)
    )
    if existing.rowcount == 0:
        session.add(
            FileDirStats(
                node_id=drive.root_node_id,
                org_team_id=drive.org_team_id,
                bytes=bytes,
                files=files,
                direct_children=0,
            )
        )
    await session.commit()


# --------------------------------------------------------------------------
# Ceilings and boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("used", "wanted", "accepted"),
    [
        pytest.param(0, 1000, True, id="exactly-the-quota-is-accepted"),
        pytest.param(0, 1001, False, id="one-byte-past-the-quota-is-refused"),
        pytest.param(999, 1, True, id="the-last-free-byte-is-accepted"),
        pytest.param(999, 2, False, id="one-past-the-last-free-byte-is-refused"),
        pytest.param(1000, 0, True, id="a-zero-byte-reserve-on-a-full-drive-is-accepted"),
        pytest.param(1000, 1, False, id="a-full-drive-refuses-one-byte"),
    ],
)
async def test_the_byte_ceiling_is_refused_at_the_first_byte_past_it(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    used: int,
    wanted: int,
    accepted: bool,
) -> None:
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    await _set_used(files_session, rig.drive, bytes=used, files=0)
    async with rig.repo.transaction():
        if accepted:
            hold = await rig.quota.reserve(
                rig.drive_id, bytes=wanted, nodes=0, session_id=SessionId(uuid.uuid4())
            )
            assert hold.bytes == wanted
        else:
            with pytest.raises(QuotaExceeded) as raised:
                await rig.quota.reserve(
                    rig.drive_id, bytes=wanted, nodes=0, session_id=SessionId(uuid.uuid4())
                )
            assert raised.value.kind == "bytes"


@pytest.mark.parametrize(
    ("used_nodes", "wanted_nodes", "accepted"),
    [
        pytest.param(99, 1, True, id="the-hundredth-node-is-accepted"),
        pytest.param(100, 1, False, id="the-hundred-and-first-node-is-refused"),
        pytest.param(0, 100, True, id="a-hundred-nodes-at-once-is-accepted"),
        pytest.param(0, 101, False, id="a-hundred-and-one-nodes-at-once-is-refused"),
    ],
)
async def test_the_node_ceiling_is_refused_at_the_hundred_and_first_node(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    used_nodes: int,
    wanted_nodes: int,
    accepted: bool,
) -> None:
    """``quota_nodes`` is 100 here, so 100 fits and 101 does not."""
    rig = await _make_rig(files_session, files_factory, files_org, clock, quota_nodes=100)
    await _set_used(files_session, rig.drive, bytes=0, files=used_nodes)
    async with rig.repo.transaction():
        if accepted:
            hold = await rig.quota.reserve(
                rig.drive_id, bytes=0, nodes=wanted_nodes, session_id=SessionId(uuid.uuid4())
            )
            assert hold.nodes == wanted_nodes
        else:
            with pytest.raises(QuotaExceeded) as raised:
                await rig.quota.reserve(
                    rig.drive_id, bytes=0, nodes=wanted_nodes, session_id=SessionId(uuid.uuid4())
                )
            assert raised.value.kind == "nodes"


async def test_an_open_holds_room_against_a_later_reserve(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A live hold is spent room: the second reserve sees it and is refused.

    The negative twin of the ceiling tests — here *used* is zero, so a service
    that read only ``file_dir_stats`` would happily double-spend.
    """
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    async with rig.repo.transaction():
        first = await _open_session(rig.repo, rig.drive, expires_at=clock.now() + TTL)
        first.declared_size = 900
        await rig.quota.reserve_on_session(first)
        with pytest.raises(QuotaExceeded) as raised:
            await rig.quota.reserve(
                rig.drive_id, bytes=200, nodes=0, session_id=SessionId(uuid.uuid4())
            )
        assert raised.value.kind == "bytes"
        assert (await rig.quota.usage(rig.drive_id)).held_bytes == 900


async def test_the_reserving_session_does_not_count_against_itself(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Re-reserving on a session replaces its hold rather than stacking it."""
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    async with rig.repo.transaction():
        row = await _open_session(rig.repo, rig.drive, expires_at=clock.now() + TTL)
        row.declared_size = 600
        await rig.quota.reserve_on_session(row)
        row.declared_size = 700
        await rig.quota.reserve_on_session(row)
        assert (await rig.quota.usage(rig.drive_id)).held_bytes == 700


# --------------------------------------------------------------------------
# Release, reconcile, expiry
# --------------------------------------------------------------------------


async def test_release_frees_the_room_for_the_next_reserve(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    async with rig.repo.transaction():
        row = await _open_session(rig.repo, rig.drive, expires_at=clock.now() + TTL)
        row.declared_size = 1000
        await rig.quota.reserve_on_session(row)
        await rig.quota.release(SessionId(row.id))
        hold = await rig.quota.reserve(
            rig.drive_id, bytes=1000, nodes=0, session_id=SessionId(uuid.uuid4())
        )
        assert hold.bytes == 1000
    assert await _sum_holds(files_session, rig.drive) == (0, 0)


async def test_reconcile_turns_a_hold_into_committed_usage(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The total never dips: the hold goes to zero as the bytes land as used."""
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    async with rig.repo.transaction():
        row = await _open_session(rig.repo, rig.drive, expires_at=clock.now() + TTL)
        row.declared_size = 800
        await rig.quota.reserve_on_session(row)
        await rig.quota.reconcile(SessionId(row.id), actual_bytes=650, actual_nodes=1)
        after = await rig.quota.usage(rig.drive_id)
    assert (after.bytes, after.nodes) == (650, 1)
    assert (after.held_bytes, after.held_nodes) == (0, 0)
    assert await _sum_holds(files_session, rig.drive) == (0, 0)


async def test_the_sweep_releases_the_hold_of_a_session_past_its_ttl(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The production sweep is driven across the TTL boundary, not at a fixed
    ``now``.

    Before the boundary the hold is still spent room; one second past it the
    sweep frees it. A stale session that merely *passed* its deadline still
    holds — only the state flip releases, which is what keeps Σholds honest.
    """
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    sweeper = UploadCompletion(rig.repo, _ctx(), clock, None)  # type: ignore[arg-type]
    async with rig.repo.transaction():
        row = await _open_session(rig.repo, rig.drive, expires_at=EPOCH + TTL)
        row.declared_size = 1000
        await rig.quota.reserve_on_session(row)

    clock.advance(TTL - timedelta(seconds=1))
    assert await sweeper.sweep_expired(clock.now()) == 0
    async with rig.repo.transaction():
        assert (await rig.quota.usage(rig.drive_id)).held_bytes == 1000

    clock.advance(timedelta(seconds=2))
    assert await sweeper.sweep_expired(clock.now()) == 1
    async with rig.repo.transaction():
        usage = await rig.quota.usage(rig.drive_id)
        assert usage.held_bytes == 0
        hold = await rig.quota.reserve(
            rig.drive_id, bytes=1000, nodes=0, session_id=SessionId(uuid.uuid4())
        )
        assert hold.bytes == 1000
    assert await _sum_holds(files_session, rig.drive) == (0, 0)


@pytest.mark.parametrize(
    ("state", "expired"),
    [
        pytest.param("open", True, id="an-open-session-expires"),
        pytest.param("uploading", True, id="an-uploading-session-expires"),
        # A commit in flight is the re-drive's to settle, never the idle sweep's:
        # expiring it would free room its version is about to occupy.
        pytest.param("committing", False, id="a-committing-session-keeps-its-hold"),
    ],
)
async def test_the_sweep_expires_only_sessions_still_accepting_parts(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    state: str,
    expired: bool,
) -> None:
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    sweeper = UploadCompletion(rig.repo, _ctx(), clock, None)  # type: ignore[arg-type]
    async with rig.repo.transaction():
        row = await _open_session(rig.repo, rig.drive, expires_at=EPOCH + TTL, state=state)
        row.declared_size = 1000
        await rig.quota.reserve_on_session(row)
        session_id = row.id

    clock.advance(TTL + timedelta(seconds=1))
    await sweeper.sweep_expired(clock.now())

    async with rig.repo.transaction():
        swept = (
            await rig.repo.execute_scoped(
                select(FileUploadSession.state, FileUploadSession.quota_hold_bytes).where(
                    FileUploadSession.id == session_id
                )
            )
        ).one()
    assert (str(swept[0]), int(swept[1])) == (("expired", 0) if expired else (state, 1000))


# --------------------------------------------------------------------------
# Freezing
# --------------------------------------------------------------------------


async def test_over_quota_freezes_the_drive_and_a_reserve_on_it_is_refused(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    await _set_used(files_session, rig.drive, bytes=1200, files=0)
    async with rig.repo.transaction():
        assert await rig.quota.freeze_if_over(rig.drive_id) is True
    async with rig.repo.transaction():
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason == OVER_QUOTA
        with pytest.raises(Conflict) as raised:
            await rig.quota.reserve(
                rig.drive_id, bytes=0, nodes=0, session_id=SessionId(uuid.uuid4())
            )
        assert raised.value.code == FROZEN_CODE


async def test_a_drive_inside_its_quota_is_not_frozen(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The negative twin: at exactly the ceiling the drive stays writable."""
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    await _set_used(files_session, rig.drive, bytes=1000, files=100)
    async with rig.repo.transaction():
        assert await rig.quota.freeze_if_over(rig.drive_id) is False
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason is None


async def test_dropping_below_the_limit_thaws_the_drive(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    await _set_used(files_session, rig.drive, bytes=1200, files=0)
    async with rig.repo.transaction():
        assert await rig.quota.freeze_if_over(rig.drive_id) is True
        assert await rig.quota.thaw_if_under(rig.drive_id) is False
    await _set_used(files_session, rig.drive, bytes=900, files=0)
    async with rig.repo.transaction():
        assert await rig.quota.thaw_if_under(rig.drive_id) is True
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason is None
        assert (
            await rig.quota.reserve(
                rig.drive_id, bytes=100, nodes=0, session_id=SessionId(uuid.uuid4())
            )
        ).bytes == 100


@pytest.mark.parametrize(
    ("used", "delta", "expected_cache", "expected_reason"),
    [
        pytest.param(900, 200, 1100, OVER_QUOTA, id="a-bigger-head-crosses-the-ceiling"),
        pytest.param(900, 100, 1000, None, id="landing-exactly-on-it-is-not-over-it"),
        pytest.param(900, -400, 500, None, id="a-smaller-head-stays-under"),
    ],
)
async def test_settle_head_swap_counts_the_swap_and_decides_the_freeze(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    used: int,
    delta: int,
    expected_cache: int,
    expected_reason: str | None,
) -> None:
    """A restore and a resolve republish stored bytes with no hold behind them.

    Nothing on the upload path accounts for that, so this is the one seam: the
    delta row and the cache fold land together with the freeze decision, and the
    cache the quota check reads has to end up at the swapped-to size.
    """
    rig = await _make_rig(files_session, files_factory, files_org, clock, quota_bytes=1000)
    await _set_used(files_session, rig.drive, bytes=used, files=0)

    async with rig.repo.transaction():
        await rig.quota.settle_head_swap(rig.drive_id, _root(rig), bytes_delta=delta)

    assert await _root_deltas(files_session, rig.drive) == [delta]
    async with rig.repo.transaction():
        assert (await rig.quota.usage(rig.drive_id)).bytes == expected_cache
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason == expected_reason


async def test_settle_head_swap_thaws_a_drive_a_smaller_head_brought_back_under(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The seam runs both ways: promoting a smaller version is not a one-way freeze."""
    rig = await _make_rig(files_session, files_factory, files_org, clock, quota_bytes=1000)
    await _set_used(files_session, rig.drive, bytes=1200, files=0)
    async with rig.repo.transaction():
        assert await rig.quota.freeze_if_over(rig.drive_id) is True

    async with rig.repo.transaction():
        await rig.quota.settle_head_swap(rig.drive_id, _root(rig), bytes_delta=-400)

    async with rig.repo.transaction():
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason is None
        assert (await rig.quota.usage(rig.drive_id)).bytes == 800


async def test_settle_head_swap_of_the_same_size_appends_nothing(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Promoting a version the same size as the head moves no bytes to record."""
    rig = await _make_rig(files_session, files_factory, files_org, clock, quota_bytes=1000)
    await _set_used(files_session, rig.drive, bytes=900, files=0)

    async with rig.repo.transaction():
        await rig.quota.settle_head_swap(rig.drive_id, _root(rig), bytes_delta=0)

    assert await _root_deltas(files_session, rig.drive) == []
    async with rig.repo.transaction():
        assert (await rig.quota.usage(rig.drive_id)).bytes == 900


async def test_a_teardown_freeze_survives_a_quota_thaw(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Only ``over_quota`` is cleared — a teardown is not a quota decision."""
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    await files_session.execute(
        update(FileDrive.__table__)
        .where(FileDrive.id == rig.drive.id)
        .values(frozen_reason="teardown")
    )
    await files_session.commit()
    async with rig.repo.transaction():
        assert await rig.quota.thaw_if_under(rig.drive_id) is False
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason == "teardown"


async def test_a_missing_drive_is_not_found(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    async with rig.repo.transaction():
        with pytest.raises(NotFound):
            await rig.quota.usage(DriveId(uuid.uuid4()))
        with pytest.raises(NotFound):
            await rig.quota.reserve(
                DriveId(uuid.uuid4()), bytes=1, nodes=1, session_id=SessionId(uuid.uuid4())
            )


# --------------------------------------------------------------------------
# The read budget
# --------------------------------------------------------------------------


async def test_usage_reads_every_aggregate_in_one_statement_and_never_walks_the_tree(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Folded aggregate + unfolded deltas + open holds, in ONE statement.

    The pin was "two indexed reads and ``file_nodes`` is never named". Now
    *used* is the folded drive-root row PLUS the delta rows the aggregator has
    not folded yet, so the statement must name ``file_dir_stats_deltas`` and it
    must reach ``file_nodes`` to say which drive a delta belongs to. The budget
    is unchanged and the thing being pinned is unchanged: ONE statement, and the
    node reference is a ``drive_id`` lookup, never a walk of the tree — no
    ``path_ids`` containment and no recursion, which is what fails if anyone
    reimplements *used* as a subtree scan.
    """
    rig = await _make_rig(files_session, files_factory, files_org, clock)
    tree = " ".join(f"f{index}.txt" for index in range(40))
    await files_factory.tree(tree, drive=rig.drive)
    emitted: list[str] = []

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        emitted.append(statement)

    from sqlalchemy import event

    engine = files_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with rig.repo.transaction():
            emitted.clear()
            await rig.quota.usage(rig.drive_id)
            statements = [
                sql
                for sql in emitted
                if "SET LOCAL" not in sql and "set_config" not in sql and "COMMIT" not in sql
            ]
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(statements) == 1, statements
    only = statements[0]
    assert "file_dir_stats" in only
    assert "file_dir_stats_deltas" in only, "used must include the unfolded remainder"
    assert "file_upload_sessions" in only
    assert "drive_id" in only, "the node reference is the drive lookup"
    assert "path_ids" not in only, "usage must not walk the tree"
    assert "RECURSIVE" not in only.upper(), "usage must not walk the tree"


# --------------------------------------------------------------------------
# The stateful model: Σholds == Σ open sessions, used + held <= quota
# --------------------------------------------------------------------------


class QuotaModel(RuleBasedStateMachine):
    """A number for used and a set of open holds, driven against Postgres.

    The model is independent arithmetic: it never calls the service to compute
    what it expects. After every rule both invariants are recomputed from the
    rows themselves, so a hold that leaked, double-counted or outlived its
    session fails here rather than in production.
    """

    QUOTA_BYTES = 1000
    QUOTA_NODES = 100

    def __init__(self) -> None:
        super().__init__()
        # The loop and the engine are the process's, not this example's: an
        # asyncpg connection belongs to the loop that opened it, so a machine
        # that built its own loop would have to build an engine to match and
        # throw both away after twelve steps. See ``HypothesisRunner``.
        self.runner = hypothesis_runner()
        self.loop = self.runner.loop
        self.open: dict[uuid.UUID, tuple[int, int]] = {}
        self.expires: dict[uuid.UUID, datetime] = {}
        self.used_bytes = 0
        self.used_nodes = 0
        self.clock = FakeClock(now=EPOCH)

    @initialize()
    def start(self) -> None:
        self.loop.run_until_complete(self._start())

    async def _start(self) -> None:
        self.session = AsyncSession(bind=self.runner.engine, expire_on_commit=False)
        org_id = uuid.uuid4()
        await self.session.execute(
            text("INSERT INTO teams (id, name, created_at) VALUES (:id, :name, now())"),
            {"id": org_id, "name": f"quota-model-{org_id.hex[:8]}"},
        )
        await self.session.commit()
        self.org = FilesOrg(org_team_id=org_id, admin_id=uuid.uuid4(), member_id=uuid.uuid4())
        factory = FilesFactory(self.session, self.org)
        self.drive = await factory.drive()
        await _set_quota(
            self.session, self.drive, quota_bytes=self.QUOTA_BYTES, quota_nodes=self.QUOTA_NODES
        )
        self.repo = FilesRepo(self.session, OrgScope(org_team_id=org_id))
        self.quota = QuotaService(self.repo, _ctx(), self.clock, None)
        # The production sweep: these sessions stage no parts, so it never
        # reaches the store.
        self.sweeper = UploadCompletion(self.repo, _ctx(), self.clock, None)  # type: ignore[arg-type]

    @rule(size=integers(min_value=0, max_value=400), nodes=integers(min_value=0, max_value=3))
    def open_session(self, size: int, nodes: int) -> None:
        free_bytes = self.QUOTA_BYTES - self.used_bytes - sum(h[0] for h in self.open.values())
        free_nodes = self.QUOTA_NODES - self.used_nodes - sum(h[1] for h in self.open.values())
        fits = size <= free_bytes and nodes <= free_nodes
        result = self.loop.run_until_complete(self._open(size, nodes))
        if fits:
            assert result is not None, f"{size}/{nodes} fits in {free_bytes}/{free_nodes}"
            self.open[result] = (size, nodes)
            self.expires[result] = self.clock.now() + TTL
        else:
            assert result is None, f"{size}/{nodes} does not fit in {free_bytes}/{free_nodes}"

    async def _open(self, size: int, nodes: int) -> uuid.UUID | None:
        async with self.repo.transaction():
            row = await _open_session(self.repo, self.drive, expires_at=self.clock.now() + TTL)
            try:
                await self.quota.reserve(
                    DriveId(self.drive.id),
                    bytes=size,
                    nodes=nodes,
                    session_id=SessionId(row.id),
                )
            except QuotaExceeded:
                await self.quota.release(SessionId(row.id))
                await self.repo.session.execute(
                    update(FileUploadSession.__table__)
                    .where(FileUploadSession.id == row.id)
                    .values(state="aborted")
                )
                return None
            await self.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == row.id)
                .values(quota_hold_bytes=size, quota_hold_nodes=nodes)
            )
            return row.id

    @precondition(lambda self: bool(self.open))
    @rule(pick=integers(min_value=0))
    def abort_session(self, pick: int) -> None:
        chosen = sorted(self.open)[pick % len(self.open)]
        self.loop.run_until_complete(self._abort(chosen))
        del self.open[chosen]
        self.expires.pop(chosen, None)

    async def _abort(self, session_id: uuid.UUID) -> None:
        async with self.repo.transaction():
            await self.quota.release(SessionId(session_id))
            await self.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == session_id)
                .values(state="aborted")
            )

    @precondition(lambda self: bool(self.open))
    @rule(pick=integers(min_value=0), shrink=integers(min_value=0, max_value=100))
    def complete_session(self, pick: int, shrink: int) -> None:
        chosen = sorted(self.open)[pick % len(self.open)]
        held_bytes, held_nodes = self.open[chosen]
        actual_bytes = max(0, held_bytes - shrink)
        self.loop.run_until_complete(self._complete(chosen, actual_bytes, held_nodes))
        del self.open[chosen]
        self.expires.pop(chosen, None)
        self.used_bytes += actual_bytes
        self.used_nodes += held_nodes

    async def _complete(self, session_id: uuid.UUID, actual_bytes: int, actual_nodes: int) -> None:
        async with self.repo.transaction():
            await self.quota.reconcile(
                SessionId(session_id), actual_bytes=actual_bytes, actual_nodes=actual_nodes
            )
            await self.repo.session.execute(
                update(FileUploadSession.__table__)
                .where(FileUploadSession.id == session_id)
                .values(state="done")
            )

    @rule(minutes=integers(min_value=1, max_value=45))
    def pass_time(self, minutes: int) -> None:
        """Move the clock, then sweep — the only way a hold expires."""
        self.clock.advance(timedelta(minutes=minutes))
        now = self.clock.now()
        expired = {sid for sid, deadline in self.expires.items() if deadline <= now}
        swept = self.loop.run_until_complete(self._sweep(now))
        assert swept == len(expired), f"swept {swept}, model expected {len(expired)}"
        for sid in expired:
            del self.open[sid]
            del self.expires[sid]

    async def _sweep(self, now: datetime) -> int:
        return await self.sweeper.sweep_expired(now)

    @invariant()
    def holds_equal_open_sessions(self) -> None:
        rows = self.loop.run_until_complete(self._read_holds())
        assert rows == {sid: hold for sid, hold in self.open.items()}, (
            f"rows {rows} vs model {self.open}"
        )

    @invariant()
    def used_plus_held_stays_within_quota(self) -> None:
        usage = self.loop.run_until_complete(self._read_usage())
        assert (usage.bytes, usage.nodes) == (self.used_bytes, self.used_nodes)
        assert usage.held_bytes == sum(h[0] for h in self.open.values())
        assert usage.held_nodes == sum(h[1] for h in self.open.values())
        assert usage.bytes + usage.held_bytes <= usage.quota_bytes
        assert usage.nodes + usage.held_nodes <= usage.quota_nodes

    async def _read_holds(self) -> dict[uuid.UUID, tuple[int, int]]:
        result = await self.session.execute(
            select(
                FileUploadSession.id,
                FileUploadSession.state,
                FileUploadSession.quota_hold_bytes,
                FileUploadSession.quota_hold_nodes,
            ).where(FileUploadSession.drive_id == self.drive.id)
        )
        live: dict[uuid.UUID, tuple[int, int]] = {}
        for sid, state, held_bytes, held_nodes in result.all():
            if state in ("open", "uploading", "committing"):
                live[sid] = (held_bytes, held_nodes)
            else:
                assert (held_bytes, held_nodes) == (0, 0), f"{state} session still holds"
        return live

    async def _read_usage(self) -> object:
        async with self.repo.transaction():
            return await self.quota.usage(DriveId(self.drive.id))

    def teardown(self) -> None:
        """Give this example's connection back; the loop serves the next one."""
        self.loop.run_until_complete(self.session.close())


@pytest.fixture(scope="session", autouse=True)
def _close_the_shared_runner() -> Iterator[None]:
    """Close the loop and engine the machine above shares, once, at the end.

    The machine builds its rig from ``__init__``, where no fixture is in reach,
    so the runner is a process-wide singleton rather than a fixture value; this
    is what gives it a finaliser. Closing it is idempotent, so the other suites
    that share it register the same thing without coordinating.
    """
    yield
    close_hypothesis_runner()


TestQuotaModel = QuotaModel.TestCase
TestQuotaModel.settings = hypothesis_settings(  # type: ignore[attr-defined]
    max_examples=8,
    stateful_step_count=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# --------------------------------------------------------------------------
# used is the folded root plus the unfolded remainder
# --------------------------------------------------------------------------


async def test_an_unfolded_delta_counts_against_the_ceiling_before_the_job_runs(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A commit is over the ceiling the moment its delta is appended.

    The write path charges the changed node's parent, never the root, so the
    folded root row still says 400 here. If *used* were that row alone, a burst
    could commit past the ceiling and only be caught when the aggregator ran.
    """
    rig = await _make_rig(files_session, files_factory, files_org, clock, quota_bytes=1000)
    await _set_used(files_session, rig.drive, bytes=400, files=0)
    folder = (await files_factory.tree("papers/", drive=rig.drive))["papers"]

    async with rig.repo.transaction():
        await stats.add_delta(
            rig.repo,
            node_id=NodeId(folder.id),
            bytes_delta=700,
            files_delta=1,
            direct_children_delta=1,
            child_change_at=clock.now(),
        )

    async with rig.repo.transaction():
        seen = await rig.quota.usage(rig.drive_id)
        assert seen.bytes == 1100, "used must add the deltas the job has not folded"
        assert seen.over_quota is True
        assert await rig.quota.freeze_if_over(rig.drive_id) is True

    folded = (
        await files_session.execute(
            select(FileDirStats.bytes).where(FileDirStats.node_id == rig.drive.root_node_id)
        )
    ).scalar_one()
    assert int(folded) == 400, "the write path never charges the root directly"


async def test_a_drive_frozen_over_the_line_thaws_after_a_purge_below_it(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """F-282/F-299: the byte-freeing side of the decision is real, not deferred.

    The freeze here is the one a commit takes (``freeze_if_over`` after the
    bytes land), not a promote's — a promote's bytes go through the content
    close, which is where the freeze already is. Taking the bytes back out then
    has to lift it in the same transaction, without waiting for the aggregator.
    """
    rig = await _make_rig(files_session, files_factory, files_org, clock, quota_bytes=1000)
    folder = (await files_factory.tree("papers/", drive=rig.drive))["papers"]

    async with rig.repo.transaction():
        await stats.add_delta(
            rig.repo,
            node_id=NodeId(folder.id),
            bytes_delta=1400,
            files_delta=2,
            direct_children_delta=2,
            child_change_at=clock.now(),
        )
        assert await rig.quota.freeze_if_over(rig.drive_id) is True

    async with rig.repo.transaction():
        assert await rig.quota.thaw_if_under(rig.drive_id) is False, "still over the line"
        # What a purge of those nodes appends: the same numbers, negated.
        await stats.add_delta(
            rig.repo,
            node_id=NodeId(folder.id),
            bytes_delta=-600,
            files_delta=-1,
            direct_children_delta=-1,
            child_change_at=clock.now(),
        )
        assert await rig.quota.thaw_if_under(rig.drive_id) is True

    async with rig.repo.transaction():
        drive = await rig.repo.drive(rig.drive_id)
        assert drive is not None
        assert drive.frozen_reason is None
        assert (await rig.quota.usage(rig.drive_id)).bytes == 800


# --------------------------------------------------------------------------- #
# The refusal a person's own ceiling answers names the limit and what is left
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ceiling", "own", "wanted", "message"),
    [
        pytest.param(
            UserCeiling(limit_bytes=1_000_000, scope="org", team_id=uuid.uuid4(), team_name="L3"),
            300_000,
            2_000_000,
            "This file needs 2 MB; your L3 storage limit is 1 MB and 700 KB is left.",
            id="a-team-allowance-with-room-left",
        ),
        pytest.param(
            UserCeiling(limit_bytes=1_000_000, scope="org"),
            300_000,
            2_000_000,
            "This file needs 2 MB; your storage limit is 1 MB and 700 KB is left.",
            id="an-org-wide-cap-names-no-team",
        ),
        pytest.param(
            UserCeiling(
                limit_bytes=1_000_000,
                scope="team",
                team_id=uuid.uuid4(),
                team_name="Data",
                scope_path="1.2",
            ),
            1_000_000,
            10,
            "This file needs 10 B; your Data storage limit is 1 MB and nothing is left.",
            id="at-the-ceiling",
        ),
        pytest.param(
            UserCeiling(limit_bytes=1_000_000, scope="org", team_name="Data"),
            1_200_000,
            0,
            "Your Data storage limit is 1 MB and nothing is left.",
            id="a-write-that-adds-no-bytes-past-the-ceiling",
        ),
    ],
)
def test_the_user_ceiling_refusal_names_the_limit_the_team_and_what_is_left(
    ceiling: UserCeiling, own: int, wanted: int, message: str
) -> None:
    refusal = ceiling.refusal(own=own, wanted=wanted)
    assert refusal.status == 507
    assert refusal.code == "files.user_quota_bytes"
    assert refusal.message == message
    assert "out of storage" not in refusal.message
    assert refusal.detail == {
        "kind": "bytes",
        "scope": ceiling.scope,
        "limit_bytes": ceiling.limit_bytes,
        "used_bytes": own,
        "remaining_bytes": max(0, ceiling.limit_bytes - own),
        "needed_bytes": wanted,
        "team_id": None if ceiling.team_id is None else str(ceiling.team_id),
        "team_name": ceiling.team_name,
    }
