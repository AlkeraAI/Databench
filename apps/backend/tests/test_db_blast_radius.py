"""One tenant's stuck statement must not become every tenant's outage.

The request pool is shared, so the blast radius of a blocked statement is
decided by three things, each pinned here against real Postgres:

* a request waiting on a held lock gives up inside the lock timeout, answers a
  coded, retryable ``503`` and hands its connection back — so the pool recovers
  the moment the requests stop arriving, not when the holder finally commits;
* a drained pool is itself a coded, retryable ``503``, never an opaque ``500``;
* readiness is asked on a connection of its own, so a drained request pool does
  not read as a dead database and get the task pulled by the load balancer.

The holder is always a second real connection with an open transaction; nothing
here sleeps to arrange an interleaving.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from alkera_core.config import Settings, settings
from alkera_core.db import session as db_session
from alkera_core.observability.asgi import install_exception_handlers
from asyncpg.exceptions import CannotConnectNowError
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from tests._suite_app import app as fastapi_app

pytestmark = pytest.mark.asyncio

LIMITS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    database_lock_timeout_ms=300,
    database_statement_timeout_ms=1_000,
)

#: No request in this module may take longer than this. It is a failure
#: deadline, not a wait: with the limits in force every answer arrives in well
#: under a second, and without them the blocked request never answers at all.
DEADLINE_SECONDS = 15.0

LOCK_KEY = 8_450_113


@pytest.fixture
async def small_pool() -> AsyncIterator[AsyncEngine]:
    """A request pool of exactly two connections carrying the limits above."""
    made = create_async_engine(
        settings.database_url, pool_size=2, max_overflow=0, pool_timeout=0.3, pool_pre_ping=True
    )
    db_session.install_session_timeouts(
        made, limits=lambda: db_session.session_timeouts(LIMITS, "request")
    )
    try:
        yield made
    finally:
        await made.dispose()


def _probe_app(pool: AsyncEngine) -> FastAPI:
    """The real error handlers and the real unit-of-work dependency shape, over
    routes that do nothing but take a lock, run long, or fail."""
    factory = async_sessionmaker(bind=pool, expire_on_commit=False, autoflush=False)

    async def db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    probe = FastAPI()
    install_exception_handlers(probe)

    @probe.post("/locked")
    async def locked(session: AsyncSession = Depends(db, scope="function")) -> dict[str, bool]:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        return {"ok": True}

    @probe.post("/slow")
    async def slow(session: AsyncSession = Depends(db, scope="function")) -> dict[str, bool]:
        await session.execute(text("SELECT pg_sleep(30)"))
        return {"ok": True}

    @probe.post("/broken")
    async def broken(session: AsyncSession = Depends(db, scope="function")) -> dict[str, bool]:
        await session.execute(text("SELECT 1 / 0"))
        return {"ok": True}

    return probe


def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_requests_blocked_on_a_held_lock_fail_fast_and_the_pool_recovers(
    small_pool: AsyncEngine,
) -> None:
    holder = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    try:
        async with holder.connect() as held, _client(_probe_app(small_pool)) as client:
            await held.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})

            # As many blocked requests as the pool has connections: without a
            # lock timeout these two ARE the outage.
            blocked = await asyncio.wait_for(
                asyncio.gather(client.post("/locked"), client.post("/locked")),
                timeout=DEADLINE_SECONDS,
            )
            for resp in blocked:
                assert resp.status_code == 503
                body = resp.json()["error"]
                assert body["code"] == "db_lock_timeout"
                assert body["details"] == {"retryable": True}
                assert resp.headers["retry-after"] == "1"

            # The holder is STILL holding. Every connection is back regardless.
            assert db_session.pool_level(small_pool)[0] == 0

            await held.rollback()
            after = await asyncio.wait_for(client.post("/locked"), timeout=DEADLINE_SECONDS)
            assert after.status_code == 200
    finally:
        await holder.dispose()


async def test_a_statement_past_its_time_limit_is_a_coded_retryable_503(
    small_pool: AsyncEngine,
) -> None:
    async with _client(_probe_app(small_pool)) as client:
        resp = await asyncio.wait_for(client.post("/slow"), timeout=DEADLINE_SECONDS)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "db_statement_timeout"
    assert "retry-after" in resp.headers
    assert db_session.pool_level(small_pool)[0] == 0


async def test_an_ordinary_database_error_is_still_an_opaque_500(
    small_pool: AsyncEngine,
) -> None:
    """The negative: only contention is retryable. A bug stays a bug, and the
    driver's words — which name the statement — never reach the caller."""
    async with _client(_probe_app(small_pool)) as client:
        resp = await client.post("/broken")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "retry-after" not in resp.headers
    assert "division" not in resp.text


async def test_a_drained_pool_is_a_coded_retryable_503(small_pool: AsyncEngine) -> None:
    async with small_pool.connect(), small_pool.connect():
        async with _client(_probe_app(small_pool)) as client:
            resp = await asyncio.wait_for(client.post("/locked"), timeout=DEADLINE_SECONDS)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "db_pool_exhausted"
    assert resp.headers["retry-after"] == "5"
    assert "QueuePool" not in resp.text


async def test_readiness_answers_while_the_request_pool_is_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real app. The tenants' session factory is pointed at a pool
    of one connection and that connection is held, which is what a drained pool
    is; a route that needs the database is refused, and readiness — which must
    not share that fate — still answers."""
    monkeypatch.setattr(settings, "files_enabled", False)
    drained = create_async_engine(
        settings.database_url, pool_size=1, max_overflow=0, pool_timeout=0.3
    )
    db_session.AsyncSessionLocal.configure(bind=drained)
    try:
        async with drained.connect(), _client(fastapi_app) as client:
            tenant = await asyncio.wait_for(
                client.post(
                    "/api/v1/auth/login",
                    json={"email": "nobody@example.com", "password": "not-a-real-password"},
                ),
                timeout=DEADLINE_SECONDS,
            )
            ready = await asyncio.wait_for(client.get("/health/ready"), timeout=DEADLINE_SECONDS)
            live = await client.get("/health/live")
    finally:
        db_session.AsyncSessionLocal.configure(bind=db_session.engine)
        await drained.dispose()

    assert tenant.status_code == 503
    assert tenant.json()["error"]["code"] == "db_pool_exhausted"
    assert ready.status_code == 200
    assert ready.json()["db"] == "ok"
    assert live.status_code == 200


# ---- the database away: restarting, failing over, not listening yet ---------


def _away_app() -> FastAPI:
    """The real handlers over routes that meet a database that is not there."""
    probe = FastAPI()
    install_exception_handlers(probe)
    # Port 1 refuses: nothing a test machine runs listens there.
    gone_url = make_url(settings.database_url).set(port=1)

    @probe.get("/refused")
    async def refused() -> dict[str, bool]:
        gone = create_async_engine(gone_url, pool_size=1, max_overflow=0)
        try:
            async with gone.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await gone.dispose()
        return {"ok": True}

    @probe.get("/starting-up")
    async def starting_up() -> dict[str, bool]:
        raise CannotConnectNowError("the database system is starting up")

    @probe.get("/other-socket")
    async def other_socket() -> dict[str, bool]:
        raise ConnectionRefusedError("an SMTP relay, not the database")

    return probe


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/refused", id="nothing-listening-on-the-database-port"),
        pytest.param("/starting-up", id="the-database-system-is-starting-up"),
    ],
)
async def test_a_database_that_is_away_is_a_retryable_503(path: str) -> None:
    """While Postgres restarts, a request answers what a client can act on:
    retry after a moment. It used to be an opaque 500 for the whole restart."""
    async with _client(_away_app()) as client:
        resp = await asyncio.wait_for(client.get(path), timeout=DEADLINE_SECONDS)
    assert resp.status_code == 503, resp.text
    body = resp.json()["error"]
    assert body["code"] == "db_unavailable"
    assert body["details"] == {"retryable": True}
    assert resp.headers["retry-after"] == "5"


async def test_a_socket_error_outside_the_database_stays_a_500() -> None:
    """Only the database's own connection is called unavailable: any other
    refused socket is a failure the request should not be told to retry."""
    async with _client(_away_app()) as client:
        resp = await client.get("/other-socket")
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"
