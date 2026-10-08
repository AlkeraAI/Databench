"""Basic health-endpoint tests. No DB needed for /live; /ready runs through the
REAL ``get_db`` dependency against local Postgres, because an overridden session
would prove the probe against a stand-in and not the wiring an orchestrator polls."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from alkera_core import readiness
from alkera_core.config import settings
from backend.api.routes.infra import health
from backend.services.files.store import set_store_factory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app


@pytest.mark.asyncio
async def test_live_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_reports_a_reachable_database(client: AsyncClient, monkeypatch):
    """The database half of readiness, and only that half.

    Files is pinned off so the answer is decided by the database alone. Left to
    the ambient dotenv this test reads whatever store the developer's machine
    happens to be running: green beside a live SeaweedFS, red on a CI runner that
    has no object store at all, and in neither case a statement about Postgres.
    The store's own branch is owned by apps/backend/tests/files/test_files_store_probe.py.
    """
    monkeypatch.setattr(settings, "files_enabled", False)

    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


@pytest.mark.asyncio
async def test_ready_stays_up_when_files_is_on_but_its_store_is_unconfigured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Files enabled with incomplete store settings must not take the instance
    out of rotation, and must not escape as a 500.

    Building the factory raises here — there is no bucket to build it from — so
    this process holds no handle to probe with. Readiness cannot judge a
    dependency it was never handed; the whole non-Files surface is healthy and
    stays in rotation, while the operator reads the misconfiguration in the log.
    """
    set_store_factory(None)  # nothing cached: the factory is built from settings
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "s3_compatible")
    monkeypatch.setattr(settings, "files_store_bucket", None)

    logged: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

    monkeypatch.setattr(health, "log", _Recorder())

    try:
        resp = await client.get("/health/ready")
    finally:
        set_store_factory(None)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert [event for event, _ in logged] == ["health.ready.files_store_unconfigured"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            OperationalError("SELECT 1", None, Exception("the socket is gone")), id="driver-refused"
        ),
        pytest.param(TimeoutError("the ping outlived its deadline"), id="ping-timed-out"),
    ],
)
async def test_ready_degrades_instead_of_erroring_when_the_ping_fails(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, failure: Exception
):
    """A dead database must read as 503 degraded, not 500. An orchestrator treats a
    500 as a broken service and pulls the pod; a 503 is the honest "not ready yet"
    that lets it keep polling while the database comes back.

    Scope: a mock that raises SYNCHRONOUSLY never opens a transaction, so SQLAlchemy
    has nothing to fail over and the dependency's commit-on-exit succeeds either way.
    That makes this case blind to the escaped-500 class entirely. The cancelled
    in-flight probe below is the one that reaches it."""

    async def _refuse(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(AsyncSession, "execute", _refuse)

    resp = await client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    # This probe is unauthenticated and internet-reachable, and a driver message
    # names the database host, port, user and statement. The reason belongs in
    # the logs; the body says only that the database is unreachable.
    assert body["detail"] == "database unreachable"
    assert str(failure) not in body["detail"]
    # Nothing claims the database is fine.
    assert body["db"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("driver_message", "secrets_in_it"),
    [
        pytest.param(
            "connect call failed ('10.0.12.34', 5432)",
            ["10.0.12.34", "5432"],
            id="refused-leaks-the-private-address",
        ),
        pytest.param(
            'connection to server at "10.0.12.34", port 5432 failed: FATAL:  password '
            'authentication failed for user "alkera_prod"',
            ["10.0.12.34", "5432", "alkera_prod"],
            id="bad-password-leaks-the-db-user",
        ),
        pytest.param(
            'database "alkera_prod_main" does not exist',
            ["alkera_prod_main"],
            id="bad-catalog-leaks-the-db-name",
        ),
    ],
)
async def test_ready_never_hands_the_driver_message_to_an_anonymous_caller(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    driver_message: str,
    secrets_in_it: list[str],
):
    """No credentials are needed to reach /health/ready and the load balancer
    routes to it on Host alone, so whatever the body says is public. A driver
    error names the datastore's VPC-internal address and the database user — an
    attacker who can push the database into failing would otherwise read both
    straight off the probe. The operator's answer moves to the log instead."""
    failure = OperationalError("SELECT 1", None, Exception(driver_message))

    async def _refuse(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(AsyncSession, "execute", _refuse)

    logged: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

    # The database check is the shared one, so its log line is the shared module's.
    monkeypatch.setattr(readiness, "log", _Recorder())

    resp = await client.get("/health/ready")

    assert resp.status_code == 503
    raw_body = resp.text
    for leaked in [*secrets_in_it, "SELECT 1", "sqlalche.me"]:
        assert leaked not in raw_body, f"{leaked!r} must not reach an unauthenticated caller"
    assert resp.json()["detail"] == "database unreachable"

    # ...but the operator still gets the driver's own words, off the wire.
    assert logged, "a failed readiness probe must leave a log line to diagnose from"
    event, payload = logged[0]
    assert event == "health.ready.failed"
    assert driver_message in payload["error"]


@pytest.mark.asyncio
async def test_ready_degrades_when_the_deadline_cancels_a_query_mid_flight(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """The real 500 class, and the only shape that reaches it: a probe that actually
    REACHES Postgres and is then cancelled by the readiness deadline. The connection
    is left mid-statement, so SQLAlchemy marks the session as needing a rollback and
    the dependency's commit-on-exit raises PendingRollbackError straight past the
    route's 503, and an orchestrator sees a 500 and pulls a pod whose database was
    merely slow. Driving the REAL query through the REAL get_db is what makes the
    transaction state real; a synchronously-raising mock cannot produce it."""
    original_execute = AsyncSession.execute

    async def _slow(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        return await original_execute(self, text("SELECT pg_sleep(10)"))

    monkeypatch.setattr(AsyncSession, "execute", _slow)
    monkeypatch.setattr(settings, "health_ready_timeout_seconds", 0.2)

    resp = await client.get("/health/ready")

    assert resp.status_code == 503, "a cancelled in-flight probe must not escape as a 500"
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] is None
    assert body["detail"]  # the reason is populated even when the cancel carries no message


@pytest.mark.asyncio
async def test_ready_waits_out_a_database_that_is_slow_rather_than_gone(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """A database that answers in two and a half seconds is SERVING, and the
    probe must say so.

    The deadline used to be two seconds, so a Postgres under load — a vacuum, a
    noisy neighbour, a connection storm — made every task in the fleet report
    itself unready at once and the load balancer drained the whole deployment
    over a database that was still answering. The default is now the balancer's
    own five-second health-check timeout, so the probe never gives up before the
    thing reading it does. The query is real: nothing here mocks the answer, so
    the wait is the wait an orchestrator would have seen."""
    original_execute = AsyncSession.execute

    async def _slow(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        return await original_execute(self, text("SELECT pg_sleep(2.5)"))

    monkeypatch.setattr(AsyncSession, "execute", _slow)
    monkeypatch.setattr(settings, "files_enabled", False)

    resp = await client.get("/health/ready")

    assert resp.status_code == 200, "a slow but serving database must not drain the task"
    assert resp.json()["db"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate_hangs", [False, True], ids=["rollback-hangs", "both-hang"])
async def test_ready_bounds_the_reset_of_a_blackholed_connection(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, invalidate_hangs: bool
):
    """The reset after a failed ping talks to the same database that just
    failed, so a blackholed server would hang the probe on the very connection
    it declared dead, exactly when an orchestrator most needs the 503. Both
    reset steps carry a deadline. The rollback times out and falls through to
    invalidate, and even an invalidate that hangs too still answers promptly.

    The session is mocked at the class level, so nothing here dirties a real
    transaction and the dependency's own commit-on-exit is never exercised
    against a broken connection. The mid-flight-cancel test above owns that
    half. A cancelled reset also abandons the pooled connection to the
    finalizer, which stays unproven here and is recorded as a follow-up."""

    async def _refuse(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise OperationalError("SELECT 1", None, Exception("the socket is gone"))

    async def _blackhole(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        # Finite, so an unbounded reset is a measured 30s failure, not a hung run.
        await asyncio.sleep(30)

    invalidated: list[bool] = []

    async def _record_invalidate(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        invalidated.append(True)

    monkeypatch.setattr(AsyncSession, "execute", _refuse)
    monkeypatch.setattr(AsyncSession, "rollback", _blackhole)
    monkeypatch.setattr(
        AsyncSession, "invalidate", _blackhole if invalidate_hangs else _record_invalidate
    )
    monkeypatch.setattr(readiness, "RESET_TIMEOUT_SECONDS", 0.1)

    started = time.monotonic()
    resp = await client.get("/health/ready")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "the reset must be bounded, never awaited to completion"
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
    if not invalidate_hangs:
        assert invalidated, "the timed-out rollback falls through to invalidate"


class _Clock:
    """The readiness latch's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_a_database_blip_after_a_ready_probe_does_not_pull_the_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """The staging outage of Sep 29 2026, at the probe: the database stalled for
    every task at once, both backends answered 503 for ninety seconds, and ECS
    stopped them into the same stalled database. A task that has been ready
    keeps answering 200 inside the grace window — its body saying ``degraded``
    — and is refused only once the failure has outlasted the window."""
    clock = _Clock()
    monkeypatch.setattr(health.probe_latch, "clock", clock)
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)
    monkeypatch.setattr(settings, "files_enabled", False)

    assert (await client.get("/health/ready")).status_code == 200

    async def _stalled(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("the database did not answer inside the deadline")

    monkeypatch.setattr(AsyncSession, "execute", _stalled)

    clock.now += 899.0
    graced = await client.get("/health/ready")
    assert graced.status_code == 200, "a shared-database stall replaced a task that was serving"
    assert graced.json() == {
        "status": "degraded",
        "db": None,
        "detail": "database unreachable",
        "byok": False,
    }

    clock.now += 1.0
    refused = await client.get("/health/ready")
    assert refused.status_code == 503, "a failure past the window must let ECS try a new task"
    assert refused.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_a_strict_window_answers_the_first_failure_with_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """``HEALTH_READY_GRACE_SECONDS=0`` restores the probe that follows the
    database second by second, for a deployment that wants it."""
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 0.0)
    monkeypatch.setattr(settings, "files_enabled", False)
    assert (await client.get("/health/ready")).status_code == 200

    async def _stalled(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("the database did not answer inside the deadline")

    monkeypatch.setattr(AsyncSession, "execute", _stalled)
    assert (await client.get("/health/ready")).status_code == 503


@pytest.mark.asyncio
async def test_openapi_schema_is_served():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert "paths" in resp.json()


@pytest.mark.asyncio
async def test_info_returns_build_metadata():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == "alkera-backend"
    assert isinstance(body["version"], str) and body["version"]
    assert body["env"] in {"local", "staging", "production"}
    # build_id is null until injected at deploy time.
    assert "build_id" in body


@pytest.mark.asyncio
async def test_ready_strict_is_503_when_db_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """The grace window is for the load balancer. An operator or a check that
    asks ``?strict=1`` is told the database is down the moment it is, even by a
    task that has been ready and is still inside its window."""
    clock = _Clock()
    monkeypatch.setattr(health.probe_latch, "clock", clock)
    monkeypatch.setattr(settings, "health_ready_grace_seconds", 900.0)
    monkeypatch.setattr(settings, "files_enabled", False)
    assert (await client.get("/health/ready?strict=1")).status_code == 200

    async def _stalled(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("the database did not answer inside the deadline")

    monkeypatch.setattr(AsyncSession, "execute", _stalled)
    clock.now += 1.0

    graced = await client.get("/health/ready")
    strict = await client.get("/health/ready?strict=1")

    assert graced.status_code == 200, "the default keeps its grace window"
    assert strict.status_code == 503
    assert (
        strict.json()
        == graced.json()
        == {
            "status": "degraded",
            "db": None,
            "detail": "database unreachable",
            "byok": False,
        }
    )
