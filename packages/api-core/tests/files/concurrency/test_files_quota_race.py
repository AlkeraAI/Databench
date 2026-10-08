"""Two concurrent reserves against one drive — exactly one 507, never a double spend.

Lives beside the other interleaving tests because it needs the `sessions(n)`
fixture: two real backends on two real connections, so the race under test is
Postgres arbitrating a row lock rather than SQLAlchemy serializing statements on
one connection. The order is forced with a checkpoint; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import QuotaExceeded
from alkera_core.files.ids import DriveId, SessionId
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files.test_files_quota import TTL, _open_session, _sum_holds


def _ctx() -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(uuid.uuid4()), org_id=uuid.uuid4()
        )
    )


async def test_two_concurrent_reserves_that_together_exceed_the_quota_leave_one_507(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[AsyncSession]]],
    run_interleaved: Callable[..., Awaitable[list[object]]],
) -> None:
    """Two backends, one drive row, 600 + 600 against a 1000-byte quota.

    A is paused *after* it has taken the drive lock and before it decides, so B
    reaches the same lock and blocks on it in Postgres. The forced order is what
    makes the double-spend the failing outcome: without ``FOR UPDATE`` both
    would read a zero hold and both would be granted.
    """
    drive = await files_factory.drive()
    await files_session.execute(
        update(FileDrive.__table__)
        .where(FileDrive.id == drive.id)
        .values(quota_bytes=1000, quota_nodes=100)
    )
    await files_session.commit()
    drive_id = DriveId(drive.id)
    two = await sessions(2)
    checkpoints = PausingCheckpoints()
    checkpoints.pause("quota.after_lock")
    gate = asyncio.Barrier(2)

    async def attempt(session: AsyncSession, size: int) -> str:
        repo = FilesRepo(session, files_org.scope)
        service = QuotaService(repo, _ctx(), clock, None, checkpoints=checkpoints)
        session_id = SessionId(uuid.uuid4())
        async with repo.transaction():
            # Both backends are provably inside their transaction before either
            # asks for the drive lock, so the drive row is what decides the race
            # and not who started first. The lock order is drive → parent, so
            # the session row (whose FK share-locks the parent) is written after
            # the reservation, never before it.
            await gate.wait()
            try:
                await service.reserve(drive_id, bytes=size, nodes=1, session_id=session_id)
            except QuotaExceeded as exceeded:
                raise AssertionError(f"refused:{exceeded.kind}") from None
            row = await _open_session(repo, drive, expires_at=clock.now() + TTL, id=session_id)
            row.declared_size = size
            row.quota_hold_bytes = size
            row.quota_hold_nodes = 1
            await repo.flush()
        return "granted"

    async def winner() -> str:
        return await attempt(two[0], 600)

    async def loser() -> str:
        return await attempt(two[1], 600)

    async def referee() -> None:
        # One backend holds the drive row and is parked past the lock; the
        # other is queued on that row inside Postgres. Releasing lets the
        # holder commit, and the queued one then reads the hold it must lose to.
        await checkpoints.wait_paused("quota.after_lock")
        assert checkpoints.reached.count("quota.after_lock") == 1, (
            "both backends passed the drive lock at once — the reserve is not serialized"
        )
        checkpoints.release("quota.after_lock")

    outcomes = await run_interleaved(winner(), loser(), referee())
    granted = [outcome for outcome in outcomes if outcome == "granted"]
    refused = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, AssertionError) and str(outcome) == "refused:bytes"
    ]
    assert len(granted) == 1, outcomes
    assert len(refused) == 1, outcomes
    assert await _sum_holds(files_session, drive) == (600, 1)
