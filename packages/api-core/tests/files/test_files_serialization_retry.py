"""`40001` / `40P01` are replayed; everything else is not; the effect happens once.

The failures are *scheduled* into a real `AsyncSession` — the session still
talks to the lane's Postgres, it just refuses one statement it was told to
refuse, with the driver's own `psycopg.errors.SerializationFailure` /
`DeadlockDetected` wrapped the way SQLAlchemy wraps it. No mock returns an
error and no test asserts a call count: the proof is the number of `file_history`
rows the node ends up with after a ladder of replays.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files import history
from alkera_core.files.clock import FakeClock
from alkera_core.files.db_retry import (
    RETRYABLE_SQLSTATES,
    RetryBudget,
    is_retryable,
    sqlstate_of,
    with_db_retries,
)
from alkera_core.files.idempotency import StoredResponse, idempotent
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileHistory
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: The two verdicts a replay may answer, and their driver classes.
SERIALIZATION_FAILURE = psycopg.errors.SerializationFailure
DEADLOCK_DETECTED = psycopg.errors.DeadlockDetected
#: A refusal that is a bug, not a collision: replaying it repeats an effect.
UNIQUE_VIOLATION = psycopg.errors.UniqueViolation


@dataclass
class DbFault:
    """One scheduled refusal: the `n`th statement whose SQL contains `match`."""

    match: str
    error: type[psycopg.Error]
    occurrence: int = 1
    every_time: bool = False
    fired: int = 0

    def should_fire(self, seen: int) -> bool:
        if self.every_time:
            return True
        return seen == self.occurrence


class FaultySession(AsyncSession):
    """A real session that refuses the statements a schedule names.

    Modelled on `FaultyStore`: the driver underneath is genuine, the fault is a
    plan made before the run rather than a stubbed return value, and the refusal
    is the driver's own exception wrapped as SQLAlchemy wraps it — so the code
    under test cannot tell it from Postgres refusing for real.
    """

    def __init__(self, *args: Any, faults: list[DbFault], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._faults = faults
        self._seen: dict[str, int] = {}

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        sql = str(statement)
        for fault in self._faults:
            if fault.match not in sql:
                continue
            self._seen[fault.match] = self._seen.get(fault.match, 0) + 1
            if fault.should_fire(self._seen[fault.match]):
                fault.fired += 1
                orig = fault.error(f"scheduled {fault.error.__name__} on {fault.match!r}")
                raise DBAPIError(sql, {}, orig)
        return await super().execute(statement, *args, **kwargs)


@dataclass
class SleepLog:
    """The injected sleeper: records the ladder instead of waiting it out."""

    delays: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass(frozen=True, slots=True)
class RetryRig:
    repo: FilesRepo
    ctx: ActingContext
    node_id: NodeId
    faults: list[DbFault]
    sleeps: SleepLog
    clock: FakeClock
    reader: AsyncSession

    async def history_rows(self) -> int:
        """How many history rows the node actually carries, read on a clean session."""
        rows = await self.reader.execute(
            sa.select(sa.func.count())
            .select_from(FileHistory)
            .where(FileHistory.node_id == uuid.UUID(str(self.node_id)))
        )
        return int(rows.scalar_one())


@pytest.fixture
async def retry_rig(
    files_session: AsyncSession,
    files_engine: AsyncEngine,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
) -> AsyncIterator[RetryRig]:
    """The faulted session, plus ``files_session`` as the clean reader.

    The faults live in the session wrapper, not in the connection, so the
    session binds to the suite's engine like any other.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("papers/ papers/a.bin", drive=drive)
    node_id = NodeId(tree["papers/a.bin"].id)

    faults: list[DbFault] = []
    session = FaultySession(bind=files_engine, expire_on_commit=False, faults=faults)
    ctx = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(files_org.admin_id), org_id=files_org.org_team_id
        )
    )
    try:
        yield RetryRig(
            repo=FilesRepo(session, files_org.scope),
            ctx=ctx,
            node_id=node_id,
            faults=faults,
            sleeps=SleepLog(),
            clock=clock,
            reader=files_session,
        )
    finally:
        await session.close()


def _call(rig: RetryRig, *, key: str) -> Callable[[], Any]:
    """One idempotent mutation, then a statement the schedule may refuse.

    The aftermath statement stands for everything a service does after its unit
    of work commits. A refusal there is the interesting one: the effect is
    already durable, so a naive retry would double it and only the idempotency
    key stops that.
    """

    async def effect() -> StoredResponse:
        await history.record(
            rig.repo,
            rig.ctx,
            node_id=rig.node_id,
            kind="attrs",
            before=None,
            after={"etag": 1},
        )
        return StoredResponse(status=200, body=b'{"ok":true}')

    async def call() -> StoredResponse:
        stored = await idempotent(
            rig.repo,
            rig.ctx,
            route="files.test.retry",
            key=key,
            request_hash=b"\x01" * 32,
            run=effect,
        )
        async with rig.repo.transaction():
            await rig.repo.session.execute(sa.text("SELECT 1 /* aftermath */"))
        return stored

    return call


# ---- the retry predicate ------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(SERIALIZATION_FAILURE, "40001", id="serialization-failure"),
        pytest.param(DEADLOCK_DETECTED, "40P01", id="deadlock-detected"),
        pytest.param(UNIQUE_VIOLATION, "23505", id="unique-violation"),
        pytest.param(psycopg.errors.CheckViolation, "23514", id="check-violation"),
        pytest.param(psycopg.errors.LockNotAvailable, "55P03", id="lock-not-available"),
    ],
)
async def test_the_sqlstate_is_read_through_the_wrapper(
    error: type[psycopg.Error], expected: str
) -> None:
    wrapped = DBAPIError("SELECT 1", {}, error("boom"))
    assert sqlstate_of(wrapped) == expected
    assert is_retryable(wrapped) is (expected in RETRYABLE_SQLSTATES)


async def test_a_failure_with_no_sqlstate_is_never_retryable() -> None:
    assert sqlstate_of(ValueError("not a database at all")) is None
    assert is_retryable(ValueError("not a database at all")) is False


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        pytest.param(0, None, id="zero-attempts-refused"),
        pytest.param(1, 1, id="one-attempt-never-retries"),
        pytest.param(2, 2, id="two-attempts"),
    ],
)
async def test_a_budget_must_allow_at_least_one_attempt(
    attempts: int, expected: int | None
) -> None:
    if expected is None:
        with pytest.raises(ValueError, match="at least one attempt"):
            RetryBudget(attempts=attempts)
    else:
        assert RetryBudget(attempts=attempts).attempts == expected


# ---- exactly-once through idempotency -----------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SERIALIZATION_FAILURE, id="40001"),
        pytest.param(DEADLOCK_DETECTED, id="40P01"),
    ],
)
async def test_a_replay_after_the_key_committed_leaves_exactly_one_effect(
    retry_rig: RetryRig, error: type[psycopg.Error]
) -> None:
    """The first attempt commits the effect and then fails; the replay adds none."""
    retry_rig.faults.append(DbFault(match="aftermath", error=error))

    stored = await with_db_retries(
        _call(retry_rig, key="k-after"),
        budget=RetryBudget(attempts=3),
        clock=retry_rig.clock,
        sleep=retry_rig.sleeps,
    )

    assert stored == StoredResponse(status=200, body=b'{"ok":true}')
    assert await retry_rig.history_rows() == 1
    assert retry_rig.sleeps.delays == [pytest.approx(0.05)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SERIALIZATION_FAILURE, id="40001"),
        pytest.param(DEADLOCK_DETECTED, id="40P01"),
    ],
)
async def test_a_replay_before_the_key_committed_leaves_exactly_one_effect(
    retry_rig: RetryRig, error: type[psycopg.Error]
) -> None:
    """A failure inside the unit of work rolls the effect back, so the replay writes it."""
    retry_rig.faults.append(DbFault(match="file_idempotency_keys", error=error, occurrence=2))

    stored = await with_db_retries(
        _call(retry_rig, key="k-before"),
        budget=RetryBudget(attempts=3),
        clock=retry_rig.clock,
        sleep=retry_rig.sleeps,
    )

    assert stored.status == 200
    assert await retry_rig.history_rows() == 1


async def test_a_non_retryable_failure_is_raised_at_once_and_writes_nothing(
    retry_rig: RetryRig,
) -> None:
    """The negative twin: 23505 is a bug, so it never sleeps and never replays."""
    fault = DbFault(match="file_idempotency_keys", error=UNIQUE_VIOLATION, every_time=True)
    retry_rig.faults.append(fault)

    with pytest.raises(DBAPIError) as raised:
        await with_db_retries(
            _call(retry_rig, key="k-fatal"),
            budget=RetryBudget(attempts=5),
            clock=retry_rig.clock,
            sleep=retry_rig.sleeps,
        )

    assert sqlstate_of(raised.value) == "23505"
    assert retry_rig.sleeps.delays == []
    assert fault.fired == 1
    assert await retry_rig.history_rows() == 0


async def test_a_budget_that_runs_out_raises_the_last_failure_and_keeps_one_effect(
    retry_rig: RetryRig,
) -> None:
    """Every attempt collides: the ladder is bounded and the effect is still one."""
    retry_rig.faults.append(
        DbFault(match="aftermath", error=SERIALIZATION_FAILURE, every_time=True)
    )

    with pytest.raises(DBAPIError) as raised:
        await with_db_retries(
            _call(retry_rig, key="k-exhausted"),
            budget=RetryBudget(attempts=3),
            clock=retry_rig.clock,
            sleep=retry_rig.sleeps,
        )

    assert sqlstate_of(raised.value) == "40001"
    assert retry_rig.sleeps.delays == [pytest.approx(0.05), pytest.approx(0.1)]
    assert await retry_rig.history_rows() == 1


async def test_the_window_stops_the_ladder_before_it_crosses_the_deadline(
    retry_rig: RetryRig,
) -> None:
    """A caller behind a timeout is not held past it, even with attempts left."""
    retry_rig.faults.append(DbFault(match="aftermath", error=DEADLOCK_DETECTED, every_time=True))

    with pytest.raises(DBAPIError):
        await with_db_retries(
            _call(retry_rig, key="k-window"),
            budget=RetryBudget(attempts=9, base_delay=1.0, window=timedelta(seconds=0.5)),
            clock=retry_rig.clock,
            sleep=retry_rig.sleeps,
        )

    assert retry_rig.sleeps.delays == []
    assert await retry_rig.history_rows() == 1
