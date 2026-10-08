"""Every pooled connection carries a lock timeout and a statement timeout.

The pool is shared by every tenant and a request holds a connection for its
whole life, so a statement parked behind someone else's row lock is a connection
nobody can use. These tests drive real connections against real Postgres: a
limit that was never sent, or was lost on the way back into the pool, shows up
as the server's own answer to ``SHOW`` — and as a lock wait that ends by itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from alkera_core.config import Settings, settings
from alkera_core.db import errors as db_errors
from alkera_core.db import session as db_session
from alkera_core.observability.errors import ErrorCode
from pydantic import ValidationError
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from structlog.testing import capture_logs

LIMITS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    database_lock_timeout_ms=300,
    database_statement_timeout_ms=1_000,
    database_background_lock_timeout_ms=7_000,
    database_background_statement_timeout_ms=60_000,
    database_idle_in_transaction_timeout_ms=20_000,
    database_background_idle_in_transaction_timeout_ms=90_000,
)


@pytest.fixture
async def one_connection_engine() -> AsyncIterator[AsyncEngine]:
    """An engine whose pool is ONE connection, so "the next checkout" is
    provably the connection the previous borrower handed back."""
    made = create_async_engine(settings.database_url, pool_size=1, max_overflow=0, pool_timeout=5)
    db_session.install_session_timeouts(
        made,
        limits=lambda: db_session.session_timeouts(LIMITS, db_session.active_timeout_profile()),
    )
    try:
        yield made
    finally:
        await made.dispose()


async def _shown(engine: AsyncEngine) -> tuple[str, str]:
    async with engine.connect() as conn:
        lock = (await conn.execute(text("SHOW lock_timeout"))).scalar_one()
        statement = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
    return str(lock), str(statement)


@pytest.mark.asyncio
async def test_a_pooled_connection_is_opened_with_the_request_limits(
    one_connection_engine: AsyncEngine,
) -> None:
    assert await _shown(one_connection_engine) == ("300ms", "1s")


@pytest.mark.asyncio
async def test_a_process_that_chose_the_background_profile_opens_with_those_limits(
    one_connection_engine: AsyncEngine,
) -> None:
    db_session.use_timeout_profile("background")
    try:
        assert await _shown(one_connection_engine) == ("7s", "1min")
    finally:
        db_session.use_timeout_profile("request")


async def _idle_limit(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str(
            (await conn.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar_one()
        )


@pytest.mark.asyncio
async def test_a_pooled_connection_opens_with_its_profiles_idle_transaction_limit(
    one_connection_engine: AsyncEngine,
) -> None:
    """A task that dies between two statements of a transaction leaves it
    open with its row locks; the server ends such a session once it has sat
    idle this long, so the locks cannot outlive the task by more."""
    assert await _idle_limit(one_connection_engine) == "20s"
    db_session.use_timeout_profile("background")
    try:
        await one_connection_engine.dispose()
        assert await _idle_limit(one_connection_engine) == "90s"
    finally:
        db_session.use_timeout_profile("request")


@pytest.mark.asyncio
async def test_a_transactions_own_override_does_not_outlive_it(
    one_connection_engine: AsyncEngine,
) -> None:
    async with one_connection_engine.connect() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '9s'"))
        assert (await conn.execute(text("SHOW lock_timeout"))).scalar_one() == "9s"
        await conn.commit()
    assert await _shown(one_connection_engine) == ("300ms", "1s")


@pytest.mark.asyncio
async def test_a_background_session_hands_back_a_connection_with_the_request_limits(
    one_connection_engine: AsyncEngine,
) -> None:
    async with db_session.background_session(source=one_connection_engine, s=LIMITS) as session:
        inside = (
            (await session.execute(text("SHOW lock_timeout"))).scalar_one(),
            (await session.execute(text("SHOW statement_timeout"))).scalar_one(),
        )
        # The limits hold across the session's own commits, which is the point
        # of pinning it to one connection.
        await session.commit()
        after_commit = (await session.execute(text("SHOW lock_timeout"))).scalar_one()
    assert inside == ("7s", "1min")
    assert after_commit == "7s"
    assert await _shown(one_connection_engine) == ("300ms", "1s")


@pytest.mark.asyncio
async def test_a_background_session_that_fails_still_hands_back_request_limits(
    one_connection_engine: AsyncEngine,
) -> None:
    with pytest.raises(DBAPIError):
        async with db_session.background_session(source=one_connection_engine, s=LIMITS) as session:
            await session.execute(text("SELECT 1 / 0"))
    assert await _shown(one_connection_engine) == ("300ms", "1s")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keep_cancelling",
    [
        pytest.param(False, id="one-cancel"),
        pytest.param(True, id="a-teardown-cancelling-what-survived-the-first"),
    ],
)
async def test_a_background_session_cancelled_mid_reset_keeps_its_connection_out_of_the_pool(
    one_connection_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    keep_cancelling: bool,
) -> None:
    """The reset is two round trips, and a cancellation can land between them.

    A shutdown or a dropped client cancels the task running the job, and the
    cancel travels through whatever the task is awaiting — including the
    ``RESET`` on the way back. A connection handed to the pool between the SET
    and its reset still carries the job's patience, and the request that
    borrows it next waits seven seconds on a lock instead of three hundred
    milliseconds, with nothing in the logs to say why.

    Once is the ordinary shape. The second case is a loop being torn down,
    which cancels again whatever survived the first round: every suspension the
    unwinding task reaches gets a cancel of its own, so a discard that suspends
    never finishes and the connection is pooled after all. The guarantee is
    unconditional, so the discard may not have a suspension in it.
    """
    real_rollback = AsyncConnection.rollback
    armed = asyncio.Event()
    resetting = asyncio.Event()

    async def trapped_rollback(self: AsyncConnection) -> None:
        if armed.is_set():
            armed.clear()
            resetting.set()
            # Never returns: the cancellation lands on this await, which is
            # where the reset's first round trip would have been.
            await asyncio.Event().wait()
        await real_rollback(self)

    monkeypatch.setattr(AsyncConnection, "rollback", trapped_rollback)

    async def job() -> None:
        async with db_session.background_session(source=one_connection_engine, s=LIMITS) as session:
            assert (await session.execute(text("SHOW lock_timeout"))).scalar_one() == "7s"
            armed.set()

    task = asyncio.create_task(job())
    await asyncio.wait_for(resetting.wait(), timeout=10)
    task.cancel()
    while keep_cancelling and not task.done():
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _shown(one_connection_engine) == ("300ms", "1s")


@pytest.mark.asyncio
async def test_a_statement_waiting_on_a_held_lock_gives_up_and_is_named(
    one_connection_engine: AsyncEngine,
) -> None:
    holder = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    key = 7_310_551
    try:
        async with holder.connect() as held:
            await held.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            with pytest.raises(DBAPIError) as raised:
                async with one_connection_engine.connect() as waiter:
                    await waiter.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            await held.rollback()
        busy = db_errors.contention(raised.value)
        assert busy is not None
        assert busy.code is ErrorCode.db_lock_timeout
        # The connection that gave up is usable again: the wait ended the
        # statement, not the backend.
        assert await _shown(one_connection_engine) == ("300ms", "1s")
    finally:
        await holder.dispose()


@pytest.mark.asyncio
async def test_a_statement_that_runs_too_long_is_cancelled_and_named(
    one_connection_engine: AsyncEngine,
) -> None:
    with pytest.raises(DBAPIError) as raised:
        async with one_connection_engine.connect() as conn:
            await conn.execute(text("SELECT pg_sleep(30)"))
    busy = db_errors.contention(raised.value)
    assert busy is not None
    assert busy.code is ErrorCode.db_statement_timeout


@pytest.mark.asyncio
async def test_an_ordinary_database_error_is_not_called_contention(
    one_connection_engine: AsyncEngine,
) -> None:
    """Telling a client to retry a bug hides the bug."""
    with pytest.raises(DBAPIError) as raised:
        async with one_connection_engine.connect() as conn:
            await conn.execute(text("SELECT 1 / 0"))
    assert db_errors.contention(raised.value) is None


def test_a_drained_pool_is_contention_and_asks_for_the_longest_wait() -> None:
    drained = db_errors.contention(PoolTimeoutError("QueuePool limit of size 20 overflow 10"))
    held = db_errors.contention(_WithStateError(db_errors.LOCK_NOT_AVAILABLE))
    assert drained is not None and held is not None
    assert drained.code is ErrorCode.db_pool_exhausted
    assert drained.retry_after_seconds > held.retry_after_seconds


class _WithStateError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        pytest.param("55P03", ErrorCode.db_lock_timeout, id="lock-not-available"),
        pytest.param("57014", ErrorCode.db_statement_timeout, id="query-canceled"),
        pytest.param("40001", None, id="serialization-failure-is-the-retry-ladders"),
        pytest.param("23505", None, id="unique-violation"),
        pytest.param("42601", None, id="syntax-error"),
    ],
)
def test_only_the_two_timeout_states_are_contention(
    sqlstate: str, expected: ErrorCode | None
) -> None:
    # Wrapped the way SQLAlchemy wraps a driver error: the state is on the cause.
    try:
        try:
            raise _WithStateError(sqlstate)
        except _WithStateError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        busy = db_errors.contention(outer)
    assert (busy.code if busy else None) is expected


def test_an_error_with_no_sqlstate_is_not_contention() -> None:
    assert db_errors.contention(RuntimeError("boom")) is None


@pytest.mark.parametrize(
    "sqlstate",
    [
        pytest.param("57P03", id="cannot-connect-now-the-system-is-starting-up"),
        pytest.param("57P01", id="admin-shutdown"),
        pytest.param("57P02", id="crash-shutdown"),
        pytest.param("08006", id="connection-failure"),
        pytest.param("08003", id="connection-does-not-exist"),
    ],
)
def test_a_database_that_is_away_is_unavailable(sqlstate: str) -> None:
    busy = db_errors.contention(_WithStateError(sqlstate))
    assert busy is not None
    assert busy.code is ErrorCode.db_unavailable


@pytest.mark.asyncio
async def test_a_refused_socket_is_unavailable_only_when_the_driver_raised_it() -> None:
    """The driver's refused connect carries no SQLSTATE; its frames name it.
    The same exception raised by anything else is not the database."""
    try:
        raise ConnectionRefusedError("refused")
    except ConnectionRefusedError as elsewhere:
        assert db_errors.contention(elsewhere) is None

    gone = create_async_engine(make_url(settings.database_url).set(port=1))
    try:
        with pytest.raises(OSError) as raised:
            async with gone.connect():
                pass
    finally:
        await gone.dispose()
    busy = db_errors.contention(raised.value)
    assert busy is not None
    assert busy.code is ErrorCode.db_unavailable


# ---- the pool-pressure line ------------------------------------------------


def test_pool_pressure_is_reported_once_per_interval_and_only_on_overflow() -> None:
    now = [100.0]
    reporter = db_session.PoolPressureReporter(interval_seconds=10.0, clock=lambda: now[0])
    with capture_logs() as logs:
        assert reporter.observe(checked_out=19, size=20, capacity=30) is False
        assert reporter.observe(checked_out=20, size=20, capacity=30) is True
        assert reporter.observe(checked_out=30, size=20, capacity=30) is False
        now[0] += 9.9
        assert reporter.observe(checked_out=30, size=20, capacity=30) is False
        now[0] += 0.2
        assert reporter.observe(checked_out=30, size=20, capacity=30) is True
    lines = [entry for entry in logs if entry["event"] == "db.pool.pressure"]
    assert [(line["checked_out"], line["saturated"]) for line in lines] == [
        (20, False),
        (30, True),
    ]


@pytest.mark.asyncio
async def test_a_checkout_that_fills_the_pool_is_what_reports_it() -> None:
    made = create_async_engine(settings.database_url, pool_size=1, max_overflow=1)
    seen: list[tuple[int, int, int]] = []

    class _Recorder(db_session.PoolPressureReporter):
        def observe(self, *, checked_out: int, size: int, capacity: int) -> bool:
            seen.append((checked_out, size, capacity))
            return False

    db_session.install_pool_pressure(made, _Recorder(interval_seconds=1.0))
    try:
        async with made.connect(), made.connect():
            pass
    finally:
        await made.dispose()
    assert seen == [(1, 1, 2), (2, 1, 2)]


# ---- settings bounds -------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["database_lock_timeout_ms", "database_background_lock_timeout_ms"],
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        pytest.param(-1, False, id="negative"),
        pytest.param(0, True, id="zero-switches-it-off"),
        pytest.param(99, False, id="shorter-than-ordinary-contention"),
        pytest.param(100, True, id="floor"),
        pytest.param(600_000, True, id="ceiling"),
        pytest.param(600_001, False, id="past-the-ceiling"),
    ],
)
def test_lock_timeout_bounds(field: str, value: int, accepted: bool) -> None:
    if accepted:
        assert getattr(Settings(_env_file=None, **{field: value}), field) == value  # type: ignore[arg-type]
    else:
        with pytest.raises(ValidationError, match="lock timeout"):
            Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["database_statement_timeout_ms", "database_background_statement_timeout_ms"],
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        pytest.param(-1, False, id="negative"),
        pytest.param(0, True, id="zero-switches-it-off"),
        pytest.param(999, False, id="under-a-second"),
        pytest.param(1_000, True, id="floor"),
        pytest.param(3_600_000, True, id="ceiling"),
        pytest.param(3_600_001, False, id="past-the-ceiling"),
    ],
)
def test_statement_timeout_bounds(field: str, value: int, accepted: bool) -> None:
    if accepted:
        assert getattr(Settings(_env_file=None, **{field: value}), field) == value  # type: ignore[arg-type]
    else:
        with pytest.raises(ValidationError, match="statement timeout"):
            Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        pytest.param(0.5, False, id="a-line-per-request-again"),
        pytest.param(1.0, True, id="floor"),
        pytest.param(3600.0, True, id="ceiling"),
        pytest.param(3600.5, False, id="silent-for-longer-than-an-outage"),
    ],
)
def test_pool_pressure_interval_bounds(value: float, accepted: bool) -> None:
    if accepted:
        made = Settings(_env_file=None, database_pool_pressure_log_interval_seconds=value)  # type: ignore[call-arg]
        assert made.database_pool_pressure_log_interval_seconds == value
    else:
        with pytest.raises(ValidationError, match="DATABASE_POOL_PRESSURE_LOG_INTERVAL_SECONDS"):
            Settings(_env_file=None, database_pool_pressure_log_interval_seconds=value)  # type: ignore[call-arg]


def test_a_request_gets_less_patience_than_a_job_by_default() -> None:
    """The shipped defaults are the decision: a caller is waiting on one and
    nobody is waiting on the other. Both finite."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    request = db_session.session_timeouts(s, "request")
    background = db_session.session_timeouts(s, "background")
    for name in ("lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout"):
        assert 0 < int(request[name]) < int(background[name])


@pytest.mark.parametrize(
    "field",
    [
        "database_idle_in_transaction_timeout_ms",
        "database_background_idle_in_transaction_timeout_ms",
    ],
)
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        pytest.param(-1, False, id="negative"),
        pytest.param(0, True, id="zero-switches-it-off"),
        pytest.param(9_999, False, id="shorter-than-a-slow-request"),
        pytest.param(10_000, True, id="floor"),
        pytest.param(86_400_000, True, id="ceiling"),
        pytest.param(86_400_001, False, id="past-the-ceiling"),
    ],
)
def test_idle_in_transaction_timeout_bounds(field: str, value: int, accepted: bool) -> None:
    if accepted:
        assert getattr(Settings(_env_file=None, **{field: value}), field) == value  # type: ignore[arg-type]
    else:
        with pytest.raises(ValidationError, match="idle-in-transaction timeout"):
            Settings(_env_file=None, **{field: value})  # type: ignore[arg-type]
