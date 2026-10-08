"""The sweep that finds a queued operation nobody was ever told to run.

Against real Postgres, because the whole of it is one statement: the staleness
predicate, the attempt counter it keeps on the row, and the compare-and-swap
that lets a runner win a race with the sweep. A version of this written against
a fake repo would pass with the predicate inverted.

The gap it closes: a route's hand-off is best-effort inside a two-second
budget, so an orchestrator that is briefly unreachable — or a process killed
between the 202 and its background task — leaves a durable ``queued`` row with
nobody named to run it. The watchdog does not look at those rows; it only
judges ``running`` ones by their heartbeat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import ops
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, OperationId
from alkera_core.files.ops import RUNNER_NUDGED, RUNNER_PRESENT, Operations
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    """The wall clock, not the suite's FakeClock.

    ``created_at`` is stamped by Postgres with ``now()``, and the staleness
    predicate compares the two — a frozen test clock months from the server's
    would make every case here pass for the wrong reason.
    """
    return datetime.now(UTC)


def _ops(repo: FilesRepo, org: FilesOrg, clock: FakeClock) -> Operations:
    return Operations(
        repo,
        ActingContext(
            acting_principal=Principal(
                kind=PrincipalKind.USER,
                id=str(org.admin_id),
                org_id=org.org_team_id,
                credential=CredentialKind.JWT,
            )
        ),
        clock,
        None,
    )


async def _age(repo: FilesRepo, op_id: OperationId, by: timedelta) -> None:
    """Backdate the row's birth, which is what the staleness predicate reads."""
    await repo.session.execute(
        text(
            "UPDATE file_ops SET created_at = created_at - make_interval(secs => :secs) "
            "WHERE id = :id"
        ),
        {"secs": by.total_seconds(), "id": op_id},
    )
    await repo.session.commit()


async def _queued(operations: Operations, drive_id: DriveId, kind: str = "bulk") -> OperationId:
    return (await operations.start(kind, drive_id=drive_id)).id


async def _drive(files_factory: FilesFactory) -> DriveId:
    drive = await files_factory.drive()
    return DriveId(drive.id)


async def _progress(repo: FilesRepo, op_id: OperationId) -> dict[str, Any]:
    """The operation's raw progress document, straight out of Postgres."""
    row: Any = (
        await repo.session.execute(
            text("SELECT progress FROM file_ops WHERE id = :id"), {"id": op_id}
        )
    ).scalar_one()
    return dict(row or {})


async def _offer(
    operations: Operations, *, max_attempts: int = 3, limit: int = 50, present: bool = False
) -> tuple[tuple[Any, ...], Any]:
    """One whole tick: find the abandoned rows, then settle what the offer was
    worth. ``present`` stands for the orchestrator answering that a runner for
    the row already exists."""
    found = await operations.abandoned_queued(_now(), limit=limit)
    outcome = RUNNER_PRESENT if present else RUNNER_NUDGED
    settled = await operations.settle_recovery(
        {entry.id: outcome for entry in found}, max_attempts=max_attempts
    )
    return found, settled


async def test_an_operation_nobody_claimed_is_offered_to_a_runner_again(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The case the whole sweep exists for: a 202 whose hand-off was lost.

    The row is ``queued`` with no heartbeat, past the staleness threshold, and
    nothing else in the system will ever look at it again.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER + timedelta(seconds=1))

    found, settled = await _offer(operations)

    assert [(entry.id, entry.kind) for entry in found] == [(op_id, "bulk")]
    assert [(entry.id, entry.attempts) for entry in settled.rehanded] == [(op_id, 1)]
    assert settled.failed == ()
    assert (await operations.get(op_id)).state == "queued", "the sweep must not run it itself"


async def test_an_operation_still_inside_the_threshold_is_left_alone(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A row queued a moment ago belongs to the hand-off that just made it.

    Offering it again would double every operation the system queues, and the
    attempt count would be spent before the row was ever really abandoned.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER - timedelta(seconds=1))

    assert await operations.abandoned_queued(_now(), limit=50) == ()


async def test_finding_a_row_counts_nothing_against_it(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Being found is not being abandoned.

    From the database a row whose runner is merely waiting for a worker slot
    looks exactly like one nobody was ever told about, so the read that finds
    them writes nothing: what an offer was worth is only known once the
    orchestrator has answered.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER * 10)

    for _ in range(10):
        looked = await operations.abandoned_queued(_now(), limit=50)
        assert [entry.attempts for entry in looked] == [0]
    assert (await operations.get(op_id)).state == "queued"


async def test_a_slow_but_healthy_operation_is_never_failed_for_being_slow(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A runner that exists but has not reached a worker slot is not an
    abandonment, however long the queue is.

    This is the row a busy deployment produces by the hundred: the route's
    nudge landed, the workflow is waiting its turn, and the operation row is
    still ``queued`` because nothing has claimed it yet. Counting those looks
    against it would fail a perfectly healthy batch after a couple of minutes
    of load — the one outcome the recovery must never produce.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER * 10)

    for _ in range(12):
        _, settled = await _offer(operations, max_attempts=3, present=True)
        assert settled.failed == (), "a queue that is merely busy must not abandon a row"

    assert (await operations.get(op_id)).state == "queued"
    assert (await _progress(repo, op_id)).get(ops.RECOVERY_ATTEMPTS_KEY) == 0, (
        "a runner that exists clears the count"
    )


async def test_a_runner_that_turns_up_again_resets_the_count_it_had_spent(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Only a run of CONSECUTIVE absences says the runner is really gone.

    A row that is lost, then told about, then lost again has not been abandoned
    three times running — and failing it as though it had would turn an
    intermittently unreachable orchestrator into lost customer work.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER * 10)

    await _offer(operations, max_attempts=2)
    await _offer(operations, max_attempts=2)
    assert (await _progress(repo, op_id))[ops.RECOVERY_ATTEMPTS_KEY] == 2

    await _offer(operations, max_attempts=2, present=True)
    assert (await _progress(repo, op_id))[ops.RECOVERY_ATTEMPTS_KEY] == 0

    _, settled = await _offer(operations, max_attempts=2)
    assert settled.failed == ()
    assert (await operations.get(op_id)).state == "queued"


async def test_an_operation_a_runner_already_claimed_is_not_offered_again(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The sweep and the runner race, and the runner's claim wins.

    ``queued → running`` is a compare-and-swap, so a row a runner took between
    the sweep's read and its write is no longer the sweep's business — its
    silence is the watchdog's.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER * 10)
    found = await operations.abandoned_queued(_now(), limit=50)
    assert [entry.id for entry in found] == [op_id]
    assert await operations._transition(op_id, "queued", "running", heartbeat=True)

    settled = await operations.settle_recovery({op_id: RUNNER_NUDGED}, max_attempts=1)

    assert settled.rehanded == ()
    assert settled.failed == ()
    assert (await operations.get(op_id)).state == "running"


@pytest.mark.parametrize("max_attempts", [1, 2, 3], ids=["one", "two", "three"])
async def test_an_operation_whose_runner_is_gone_every_time_is_failed_with_a_reason(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    max_attempts: int,
) -> None:
    """Offers are counted on the row, and they run out.

    A row re-offered every tick forever is a permanent load nobody reads, and a
    client polling ``queued 0/N`` is never told the work was abandoned. The
    count has to live on the row: the sweep is stateless and runs in whichever
    replica takes the tick.
    """
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER * 10)

    for attempt in range(1, max_attempts + 1):
        _, offered = await _offer(operations, max_attempts=max_attempts)
        assert [entry.attempts for entry in offered.rehanded] == [attempt]
        assert offered.failed == ()

    _, spent = await _offer(operations, max_attempts=max_attempts)

    assert spent.rehanded == ()
    assert spent.failed == (op_id,)
    abandoned = await operations.get(op_id)
    assert abandoned.state == "failed"
    assert [error["code"] for error in abandoned.errors] == [ops.RUNNER_LOST_CODE]
    assert str(max_attempts) in abandoned.errors[0]["message"]
    assert await operations.abandoned_queued(_now(), limit=50) == (), "a failed row is done"


async def test_the_offer_count_does_not_disturb_the_progress_a_client_reads(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The counter shares the ``progress`` document with what the client polls.

    Writing it must not flatten ``done`` / ``total`` — a batch that is later
    run would then report progress out of thin air.
    """
    operations = _ops(repo, files_org, clock)
    op = await operations.start("bulk", drive_id=await _drive(files_factory), total=42)
    await _age(repo, op.id, ops.QUEUED_STALE_AFTER * 10)

    await _offer(operations)

    after = await operations.get(op.id)
    assert (after.done, after.total) == (0, 42)
    assert (await _progress(repo, op.id))[ops.RECOVERY_ATTEMPTS_KEY] == 1


async def test_a_pass_takes_no_more_than_its_budget_oldest_first(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The sweep is a net for a rare lost hand-off, not a queue runner.

    A backlog is taken over several ticks rather than by one pass that runs
    long, and the rows that have waited longest go first — otherwise a steady
    trickle of newer abandoned rows could starve the oldest one forever.
    """
    operations = _ops(repo, files_org, clock)
    drive_id = await _drive(files_factory)
    by_age: list[OperationId] = []
    for minutes in (5, 4, 3, 2, 1):
        op_id = await _queued(operations, drive_id)
        await _age(repo, op_id, timedelta(minutes=minutes))
        by_age.append(op_id)

    first = await operations.abandoned_queued(_now(), limit=2)

    assert [entry.id for entry in first] == by_age[:2]


async def test_another_tenants_abandoned_operation_is_not_this_ones_to_recover(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Every statement in this class is org-scoped; the sweep is no exception."""
    operations = _ops(repo, files_org, clock)
    op_id = await _queued(operations, await _drive(files_factory))
    await _age(repo, op_id, ops.QUEUED_STALE_AFTER * 10)

    stranger = FilesRepo(repo.session, type(repo.scope)(org_team_id=files_org.admin_id))
    theirs = Operations(stranger, operations._ctx, clock, None)

    assert await theirs.abandoned_queued(_now(), limit=50) == ()
    assert (await theirs.settle_recovery({op_id: RUNNER_NUDGED}, max_attempts=1)).failed == ()
    assert (await operations.get(op_id)).state == "queued"
