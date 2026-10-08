"""The engines the Files test suite runs on, and how long each one lives.

Every Files test needs a real session on a real database. Building a fresh
``create_async_engine`` per test made that one TCP connect, one Postgres
backend fork and one authentication round-trip *per test* — about 4,500 of
them across a full run, on a Postgres shared with every other lane. Under
load that is where the suite broke: the very first flush of a test came back
``ConnectionResetError`` and the whole module errored out, which reads like
test-order coupling and is not.

So the engine is built once per test session (once per xdist worker, since a
worker is its own process with its own database) and every test borrows a
connection from its small pool. Isolation is unchanged: a test still gets its
own ``AsyncSession`` on its own connection, still seeds its own org and drive,
and the Files role and org GUC are still stamped per transaction by
``FilesRepo.transaction()`` with ``SET LOCAL`` — which Postgres unwinds at the
end of that transaction, so a pooled connection carries nothing into the next
test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from alkera_core.config import settings
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

#: Room for the handful of sessions a single test opens at once (a second
#: tenant, a concurrent writer) and nothing more — the point of the exercise is
#: to stop hoarding backends on a Postgres shared with every other lane.
POOL_SIZE = 5
MAX_OVERFLOW = 2
POOL_TIMEOUT_SECONDS = 10.0

#: A connect that died in the socket, not one the server turned away. A reset
#: peer, a broken pipe, an aborted handshake or a connect that never landed are
#: all "the box was busy, try once more". Anything else — a refused port, a bad
#: password, a missing database, ``too many connections`` — is an answer, and
#: retrying an answer only doubles the load that produced it.
TRANSIENT_CONNECT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
)
#: Long enough for a saturated server to finish forking the backends already
#: queued ahead of us, short enough that a genuinely dead server fails fast.
RETRY_BACKOFF_SECONDS = 0.5

_CONNECT_COUNT = "_alkera_files_connects"


def make_files_engine() -> AsyncEngine:
    """The one engine the Files suite runs on.

    ``pool_pre_ping`` is what makes reuse safe: a pooled connection the server
    dropped between tests is detected on checkout and replaced, instead of
    failing the next test's first statement.
    """
    engine = create_async_engine(
        settings.database_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
    )
    setattr(engine.sync_engine, _CONNECT_COUNT, 0)

    @event.listens_for(engine.sync_engine, "connect")
    def _count(_dbapi_connection: object, _record: object) -> None:
        sync: Engine = engine.sync_engine
        setattr(sync, _CONNECT_COUNT, getattr(sync, _CONNECT_COUNT) + 1)

    return engine


def connect_count(engine: AsyncEngine) -> int:
    """How many real connections this engine has opened since it was built.

    The measure the fix is about: it used to climb by one per test.
    """
    return int(getattr(engine.sync_engine, _CONNECT_COUNT))


def is_transient_connect_error(error: BaseException) -> bool:
    """Whether ``error`` (or anything it wraps) is a socket-level reset rather
    than the server declining to serve us."""
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, TRANSIENT_CONNECT_ERRORS):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


async def open_files_session(engine: AsyncEngine) -> AsyncSession:
    """A session bound to ``engine``, with its connection already proven.

    The first statement of a test session is the one that pays for the connect,
    and it is the one that was failing. It gets exactly one retry, and only
    when the failure was a transient reset; a refusal is raised as it is. The
    probe transaction is rolled back so the test still sees a pristine session
    with no transaction of ours open on it.
    """
    session = AsyncSession(bind=engine, expire_on_commit=False)
    for attempt in (1, 2):
        try:
            await session.execute(text("SELECT 1"))
        except Exception as error:
            await session.rollback()
            if attempt == 2 or not is_transient_connect_error(error):
                await session.close()
                raise
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        else:
            await session.rollback()
            return session
    raise AssertionError("unreachable")


#: The pool behind the Hypothesis runner below. One example holds one session at
#: a time and the examples run one after another, so a single connection would
#: serve them all; the spare slots are for an example that died before its
#: teardown gave its connection back, so the next example takes a fresh one. A
#: run that leaks every time exhausts them and fails on the checkout timeout,
#: which is the loud answer — a pool with unlimited overflow would hide the leak.
HYPOTHESIS_POOL_SIZE = 4
HYPOTHESIS_MAX_OVERFLOW = 0
HYPOTHESIS_POOL_TIMEOUT_SECONDS = 30.0

#: How long a pooled connection may go unused before it is thrown away and
#: reopened. This pool does NOT pre-ping, unlike the suite's: a session here
#: gives its connection back at the end of every transaction and takes one again
#: for the next, which is five thousand checkouts in a single state-machine run,
#: so a ping on each one would cost more round-trips than the connects it saves.
#: Age is the cheap proxy — a timestamp compared on checkout, no I/O — and it is
#: enough, because the only way a connection here sits idle at all is the gap
#: between one Hypothesis module and the next.
HYPOTHESIS_POOL_RECYCLE_SECONDS = 300

T = TypeVar("T")


class HypothesisRunner:
    """One event loop and one engine for every Hypothesis example in this process.

    A Hypothesis test body is synchronous, so a suite that drives async services
    from one needs a loop of its own. Building that loop per example — an
    ``asyncio.run(...)`` in the test, or ``new_event_loop()`` in a state
    machine's ``__init__`` — forces a fresh engine per example as well: an
    asyncpg connection belongs to the loop that opened it, so an engine built on
    a loop that is about to be closed cannot be handed to the next one. Every
    example therefore paid for a TCP connect, a forked Postgres backend and an
    authentication round-trip, which across the Files property and state-machine
    modules came to about a hundred and sixty of them per run — on Windows, where
    a backend is a ``CreateProcess`` rather than a ``fork``, that is most of what
    those modules spend their time on.

    Keeping the loop and the engine for the whole process turns an example into a
    pool checkout. Nothing else about an example changes: it still opens its own
    session, still seeds its own tenant and drive, and still closes the session
    when it is done, so one example can no more see another's identity map or
    open transaction than before.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._engine = create_async_engine(
            settings.database_url,
            pool_size=HYPOTHESIS_POOL_SIZE,
            max_overflow=HYPOTHESIS_MAX_OVERFLOW,
            pool_timeout=HYPOTHESIS_POOL_TIMEOUT_SECONDS,
            pool_recycle=HYPOTHESIS_POOL_RECYCLE_SECONDS,
        )

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The loop every example's work runs on."""
        return self._loop

    @property
    def engine(self) -> AsyncEngine:
        """The engine every example's session binds to."""
        return self._engine

    def run(self, coroutine: Awaitable[T]) -> T:
        """Drive ``coroutine`` to completion on the shared loop."""
        return self._loop.run_until_complete(coroutine)

    def close(self) -> None:
        """Give the pool's connections and the loop back."""
        self._loop.run_until_complete(self._engine.dispose())
        self._loop.close()


_runner: HypothesisRunner | None = None


def hypothesis_runner() -> HypothesisRunner:
    """The process-wide runner, built on first use.

    Built lazily rather than in a fixture because the callers are Hypothesis
    state machines, whose ``__init__`` runs per example with no fixture in
    reach. Under xdist a process is one worker, already pointed at its own
    database by the repo-root conftest.
    """
    global _runner
    if _runner is None:
        _runner = HypothesisRunner()
    return _runner


def close_hypothesis_runner() -> None:
    """Close the runner if one was built. Idempotent, so every suite that uses it
    can register this without caring which of them ran first."""
    global _runner
    if _runner is not None:
        runner, _runner = _runner, None
        runner.close()
