"""Async SQLAlchemy engine + session factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Final, Literal

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

from alkera_core.config import Settings, settings
from alkera_core.db.tls import asyncpg_connect_args
from alkera_core.logging import get_logger
from alkera_core.observability import metrics

log = get_logger("alkera.db")

# Pool geometry is DECLARED, not inherited. SQLAlchemy's defaults (size 5 +
# overflow 10 = 15 connections, 30 s to wait for one) are an accident of the
# library, not a decision about this app: one uvicorn process serves every tenant
# on one loop, and `get_db` holds a connection for the WHOLE request -- so a
# handful of slow requests drains it. The defaults (20 + 10) suit that
# whole-request backend profile; a service with short-burst sessions (the
# gateway) overrides DATABASE_POOL_SIZE / DATABASE_POOL_MAX_OVERFLOW in its
# environment so the fleet's resident connections stay well inside the managed
# instance's budget.
POOL_SIZE = settings.database_pool_size
MAX_OVERFLOW = settings.database_pool_max_overflow
#: Wait for a free connection, then FAIL -- fast. A long queue turns a saturated
#: pool into a pile of requests each holding a uvicorn slot for half a minute,
#: which is how a transient stall becomes an outage. Shedding early is the
#: recoverable failure.
POOL_TIMEOUT_SECONDS = 10.0

#: Which pair of limits a process's connections are opened with. ``request`` is
#: a process whose statements run while a caller waits; ``background`` is one
#: whose statements nobody is waiting on (the worker).
TimeoutProfile = Literal["request", "background"]

_profile: TimeoutProfile = "request"


def session_timeouts(s: Settings, profile: TimeoutProfile) -> dict[str, str]:
    """The Postgres limits for ``profile``, as session defaults: how long a
    statement waits for a lock, how long it runs, and how long a transaction
    may sit idle before its session is ended.

    Bare integers are milliseconds to Postgres, and ``0`` is its own spelling of
    "no limit", so a setting of 0 needs no special case here.
    """
    if profile == "background":
        lock, statement, idle = (
            s.database_background_lock_timeout_ms,
            s.database_background_statement_timeout_ms,
            s.database_background_idle_in_transaction_timeout_ms,
        )
    else:
        lock, statement, idle = (
            s.database_lock_timeout_ms,
            s.database_statement_timeout_ms,
            s.database_idle_in_transaction_timeout_ms,
        )
    return {
        "lock_timeout": str(lock),
        "statement_timeout": str(statement),
        "idle_in_transaction_session_timeout": str(idle),
    }


def use_timeout_profile(profile: TimeoutProfile) -> None:
    """Choose the limits this PROCESS opens its connections with.

    Called once at boot, before the first connection exists: a connection keeps
    the limits it was opened with for as long as it lives in the pool.
    """
    global _profile
    _profile = profile


def active_timeout_profile() -> TimeoutProfile:
    """The limits this process opens its connections with (:func:`use_timeout_profile`)."""
    return _profile


def install_session_timeouts(
    target: AsyncEngine,
    *,
    limits: Callable[[], dict[str, str]],
) -> None:
    """Open every connection of ``target`` with ``limits()`` as session defaults.

    Sent in the startup packet rather than ``SET`` after connecting: a startup
    parameter is the session's *default*, so there is nothing to re-apply when a
    pooled connection is checked out again, a ``SET LOCAL`` a transaction makes
    reverts to it, and so does a ``RESET``. It reaches only this engine — the
    realtime LISTEN connection and Alembic each connect on their own, and an
    idle LISTEN runs no statement for a statement timeout to end.
    """

    @event.listens_for(target.sync_engine, "do_connect")
    def _stamp(_dialect: Any, _record: Any, _cargs: Any, cparams: dict[str, Any]) -> None:
        merged = dict(cparams.get("server_settings") or {})
        merged.update(limits())
        cparams["server_settings"] = merged


class PoolPressureReporter:
    """One log line per interval while the pool is running on its overflow.

    A pool at capacity is every request's problem at once, so reporting it per
    request would bury the log it is meant to be read in. The clock is injected
    so the throttle is testable without waiting it out.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval_seconds
        self._clock = clock
        self._last: float | None = None

    def observe(self, *, checked_out: int, size: int, capacity: int) -> bool:
        """Record the pool's level; ``True`` when this call wrote the log line."""
        metrics.record_db_pool(checked_out=checked_out, capacity=capacity)
        if checked_out < size:
            return False
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        log.warning(
            "db.pool.pressure",
            checked_out=checked_out,
            pool_size=size,
            capacity=capacity,
            saturated=checked_out >= capacity,
        )
        return True


def pool_level(target: AsyncEngine) -> tuple[int, int, int]:
    """``(checked_out, size, capacity)`` of ``target``'s pool right now."""
    pool: Any = target.sync_engine.pool
    size = int(pool.size())
    overflow = int(getattr(pool, "_max_overflow", 0))
    return int(pool.checkedout()), size, size + max(overflow, 0)


def has_spare_connection(target: AsyncEngine) -> bool:
    """Whether ``target``'s pool can hand out another connection without
    waiting for one to come back.

    For work that would open a second connection while the caller holds one:
    with every connection held by callers doing the same, each would wait on
    the pool for the others, so such work runs on the caller's own connection
    instead. A pool that opens a connection per checkout, or has no overflow
    limit, always has one."""
    pool: Any = target.sync_engine.pool
    if not isinstance(pool, QueuePool):
        return True
    overflow = int(getattr(pool, "_max_overflow", 0))
    if overflow < 0:
        return True
    return int(pool.checkedout()) < int(pool.size()) + overflow


def install_pool_pressure(target: AsyncEngine, reporter: PoolPressureReporter) -> None:
    """Feed ``reporter`` the pool's level every time a connection leaves it."""

    @event.listens_for(target.sync_engine, "checkout")
    def _on_checkout(_conn: Any, _record: Any, _proxy: Any) -> None:
        checked_out, size, capacity = pool_level(target)
        reporter.observe(checked_out=checked_out, size=size, capacity=capacity)


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT_SECONDS,
    connect_args=asyncpg_connect_args(),
)
install_session_timeouts(
    engine, limits=lambda: session_timeouts(settings, active_timeout_profile())
)
pool_pressure = PoolPressureReporter(
    interval_seconds=settings.database_pool_pressure_log_interval_seconds
)
install_pool_pressure(engine, pool_pressure)


def report_pool_level() -> tuple[int, int, int]:
    """Report the request pool's level now, and return it.

    A checkout is what normally reports, and a pool that is completely stuck
    has no checkouts: the readiness probe calls this so a drained pool keeps
    saying so, once per interval, for as long as it lasts.
    """
    checked_out, size, capacity = pool_level(engine)
    pool_pressure.observe(checked_out=checked_out, size=size, capacity=capacity)
    return checked_out, size, capacity


AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)

# Readiness asks one question -- "can this process reach the database?" -- and
# the answer must not depend on how busy the tenants are. Probing through the
# request pool made a saturated pool read as a dead database: the probe queued
# behind the very requests that were stuck, the load balancer pulled the task,
# and the remaining tasks took its traffic and saturated in turn. One connection
# of its own, never lent to a request, and no overflow: a second concurrent
# probe waits for the first rather than opening a backend per probe.
health_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
    pool_size=1,
    max_overflow=0,
    pool_timeout=settings.health_ready_timeout_seconds,
    connect_args=asyncpg_connect_args(),
)
install_session_timeouts(health_engine, limits=lambda: session_timeouts(settings, "request"))

HealthSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=health_engine,
    expire_on_commit=False,
    autoflush=False,
)


# A refused request's decision row has to be committed while the request's own
# transaction is still open, so it needs a connection of its own -- and taking
# that second connection from the request pool is the two-per-request shape
# that drains it: every refused request holding one and waiting for another.
# Ending the request's transaction first to free its connection instead threw
# away the caller's session state: a listing that refused one row mid-loop had
# every row it had already loaded expired under it. So the records have a small
# pool of their own, never lent to a request. A write is one INSERT and a
# commit; a pool this size absorbs bursts of them, and one that cannot get a
# connection in time is logged and the refusal stands.
DECISION_POOL_SIZE: Final = 2
DECISION_POOL_MAX_OVERFLOW: Final = 3

decision_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
    pool_size=DECISION_POOL_SIZE,
    max_overflow=DECISION_POOL_MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT_SECONDS,
    connect_args=asyncpg_connect_args(),
)
install_session_timeouts(decision_engine, limits=lambda: session_timeouts(settings, _profile))

DecisionSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=decision_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_health_db() -> AsyncIterator[AsyncSession]:
    """The readiness probe's session: its own connection, never the tenants'."""
    async with HealthSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields a session per request, scoped as a unit
    of work. Commits on a clean exit; rolls back on any exception. Service
    functions should `flush()` (so identity columns populate) and let this
    dependency drive the commit boundary.

    Inject via ``Depends(get_db, scope="function")``. Function scope runs this
    teardown before the response is sent, so the commit is durable before any
    client can read the response and act on it. The default request scope
    commits after delivery, which opens a read-after-write window (a caller
    that immediately acts on a mutation's response finds the row missing).
    The dependency cache keys on scope, so a stray bare injection does not
    share the scoped session; it opens a second, independently committed one
    beside it, splitting the request across two transactions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def background_session(
    *, source: AsyncEngine | None = None, s: Settings | None = None
) -> AsyncIterator[AsyncSession]:
    """A session for long work run inside a ``request``-profile process.

    A Files operation run inline after its response is the same work the worker
    does, in a process whose connections carry a caller's limits. The session is
    pinned to ONE connection for its whole life — a plain session hands its
    connection back at every commit, and the next transaction may be given a
    different one — and that connection carries the background limits until it
    is returned, when they are reset to the defaults it was opened with. A
    connection whose reset cannot be confirmed is discarded, never pooled: the
    next borrower would be a request running with a job's patience.
    """
    limits = session_timeouts(s or settings, "background")
    async with (source or engine).connect() as conn:
        for name, value in limits.items():
            await conn.execute(text(f"SET {name} = {int(value)}"))
        # A SET inside a transaction that rolls back is rolled back with it.
        await conn.commit()
        try:
            async with AsyncSession(bind=conn, expire_on_commit=False, autoflush=False) as session:
                yield session
        finally:
            reset = False
            try:
                await conn.rollback()
                for name in limits:
                    await conn.execute(text(f"RESET {name}"))
                await conn.commit()
                reset = True
            except Exception as error:
                # Not re-raised: the body's own failure is the one worth
                # raising, and the connection is discarded below either way.
                log.warning(
                    "db.background_session.reset_failed",
                    error=str(error),
                    error_type=type(error).__name__,
                )
            finally:
                if not reset:
                    # Reached by a cancellation landing between the RESET and
                    # its COMMIT as well as by a failed reset — a connection
                    # still carrying a job's patience must not be pooled, and a
                    # cancelled task runs no `except` clause of its own.
                    #
                    # Through the sync facade, NOT `await conn.invalidate()`:
                    # that suspends inside greenlet_spawn, so a task cancelled
                    # a second time (a loop tearing down cancels what survived
                    # the first) loses the discard there and the connection
                    # goes back to the pool after all. This call terminates the
                    # socket and returns the pool slot with nothing attached,
                    # and has no await for a cancellation to land on.
                    discardable = conn.sync_connection
                    if discardable is not None:
                        discardable.invalidate()
