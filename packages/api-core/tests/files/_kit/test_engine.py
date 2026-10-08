"""The Files suite's engine: shared, and forgiving of one transient reset.

Two behaviours are pinned here. Sessions must come off ONE pooled engine —
the per-test engine cost a real connect per test, ~4,500 across a run on a
shared Postgres, which is what made the first flush of a test come back
``ConnectionResetError`` and take a whole module with it. And the first
statement of a test session must survive exactly one such reset — without
ever retrying a server that answered.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.engine import (
    connect_count,
    is_transient_connect_error,
    open_files_session,
)


async def test_repeated_sessions_reuse_the_pooled_connections(
    files_engine: AsyncEngine,
) -> None:
    """Ten sessions in a row must not cost ten connects.

    A per-test engine (pool_size=1, disposed each time) opened one backend per
    session; the pooled engine opens at most its pool's worth for the whole
    run, and here — one at a time — reuses a single one.
    """
    before = connect_count(files_engine)
    for _ in range(10):
        session = await open_files_session(files_engine)
        await session.execute(text("SELECT 1"))
        await session.rollback()
        await session.close()
    opened = connect_count(files_engine) - before
    assert opened <= 1, f"ten sequential sessions opened {opened} new connections"


async def test_the_session_a_test_gets_has_no_transaction_open_on_it(
    files_engine: AsyncEngine,
) -> None:
    """The connect probe must leave no transaction behind: the repo opens its
    own, and stamps the role and org GUC inside it."""
    session = await open_files_session(files_engine)
    try:
        assert not session.in_transaction()
        async with session.begin():
            await session.execute(text("SELECT 1"))
    finally:
        await session.close()


async def test_the_pool_pings_a_connection_before_handing_it_over(
    files_engine: AsyncEngine,
) -> None:
    """Reuse is only safe because a connection the server dropped between
    tests is caught on checkout rather than in the next test's first flush."""
    assert files_engine.pool._pre_ping is True  # type: ignore[attr-defined]


class _ResetOnce:
    """A session whose first statement dies in the socket, as a loaded
    Postgres resetting a half-open connect does."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.attempts = 0
        self.rollbacks = 0
        self.closed = False

    async def execute(self, _statement: Any) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise self.error

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tests.files._kit.engine.RETRY_BACKOFF_SECONDS", 0.0)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionResetError(104, "reset by peer"), id="reset"),
        pytest.param(ConnectionAbortedError("aborted"), id="aborted"),
        pytest.param(BrokenPipeError("broken pipe"), id="broken-pipe"),
        pytest.param(TimeoutError("connect timed out"), id="timeout"),
        pytest.param(
            RuntimeError("wrapper").with_traceback(None),
            id="wrapped",
        ),
    ],
)
async def test_a_transient_reset_on_the_first_statement_is_retried_once(
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: None,
    error: BaseException,
) -> None:
    if isinstance(error, RuntimeError):
        # SQLAlchemy hands the socket error over wrapped in a DBAPI error;
        # the cause chain is what must be inspected, not the outer type.
        error.__cause__ = ConnectionResetError(104, "reset by peer")
    made = _ResetOnce(error)
    monkeypatch.setattr("tests.files._kit.engine.AsyncSession", lambda **_kwargs: made)
    session = await open_files_session(object())  # type: ignore[arg-type]
    assert session is made
    assert made.attempts == 2, "the first statement must get exactly one retry"
    assert not made.closed


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("too many connections for role"), id="refused"),
        pytest.param(PermissionError("password authentication failed"), id="auth-failed"),
    ],
)
async def test_a_refusal_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    no_backoff: None,
    error: BaseException,
) -> None:
    """A server that answered is not a server to hammer: one attempt, and the
    refusal is raised as it is."""
    made = _ResetOnce(error)
    monkeypatch.setattr("tests.files._kit.engine.AsyncSession", lambda **_kwargs: made)
    with pytest.raises(type(error)):
        await open_files_session(object())  # type: ignore[arg-type]
    assert made.attempts == 1, "a refusal must not be retried"
    assert made.closed, "the session that will never be used must be closed"


async def test_a_second_reset_gives_up_rather_than_looping() -> None:
    """One retry, not a loop — a Postgres that keeps resetting must fail the
    test fast instead of stalling the run."""

    class _AlwaysResets(_ResetOnce):
        async def execute(self, _statement: Any) -> None:
            self.attempts += 1
            raise self.error

    made = _AlwaysResets(ConnectionResetError(104, "reset by peer"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("tests.files._kit.engine.RETRY_BACKOFF_SECONDS", 0.0)
        patch.setattr("tests.files._kit.engine.AsyncSession", lambda **_kwargs: made)
        with pytest.raises(ConnectionResetError):
            await open_files_session(object())  # type: ignore[arg-type]
    assert made.attempts == 2


def test_a_cycle_in_the_cause_chain_does_not_hang_the_classifier() -> None:
    """`__context__` can point back at an error already seen; the walk must
    terminate."""
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first
    assert is_transient_connect_error(first) is False


async def test_the_session_fixture_rides_the_shared_engine(
    files_engine: AsyncEngine, files_session: AsyncSession
) -> None:
    """The fixture every Files test uses must be bound to the session-scoped
    engine, not to one of its own."""
    assert files_session.bind is files_engine
