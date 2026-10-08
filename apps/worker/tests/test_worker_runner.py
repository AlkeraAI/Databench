"""The serving process: the liveness endpoint, the boot connect retry, worker
construction from the registries, and ``run_workers`` end to end on the dev server.

The queues served here are stubs on throwaway task-queue names; the runner code
is the production code, driven exactly as ``python -m worker run`` drives it
(minus the signal handlers, replaced by an explicit stop event).
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from types import MappingProxyType
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db import session as db_session
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.temporal import TaskQueue
from sqlalchemy import text
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker
from worker.health_probe import probe_health
from worker.temporal import queues as queues_mod
from worker.temporal import runner
from worker.temporal.interceptors import AlkeraWorkerInterceptor
from worker.temporal.runner import (
    GRACEFUL_SHUTDOWN,
    HealthServer,
    NoWorkRegisteredError,
    TemporalUnavailableError,
    build_worker,
    connect_with_retry,
    liveness_body,
    pool_budget,
    run_workers,
)


# Test stubs run unsandboxed (a test module is not sandbox-importable); the
# production registries are validated under the default sandbox below.
@workflow.defn(name="stub.runner_noop", sandboxed=False)
class Noop:
    @workflow.run
    async def run(self) -> str:
        return "ok"


@activity.defn(name="stub.db_ping")
async def db_ping() -> int:
    async with AsyncSessionLocal() as session:
        return int((await session.execute(text("SELECT 1"))).scalar_one())


@workflow.defn(name="stub.db_pings", sandboxed=False)
class DbPings:
    @workflow.run
    async def run(self, count: int) -> list[int]:
        out: list[int] = []
        for _ in range(count):
            out.append(
                await workflow.execute_activity(
                    db_ping, start_to_close_timeout=timedelta(seconds=10)
                )
            )
        return out


async def _probe(port: int, path: str = "/health/live") -> tuple[int, str]:
    if path == "/health/live":
        return await asyncio.to_thread(probe_health, "127.0.0.1", port)
    import urllib.error
    import urllib.request

    def _get() -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                return int(r.status), r.read().decode()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode()

    return await asyncio.to_thread(_get)


# --- liveness ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "code", "status"),
    [
        pytest.param({"money": "running"}, 200, "ok", id="one-running"),
        pytest.param({"money": "running", "sync": "running"}, 200, "ok", id="all-running"),
        pytest.param({"money": "running", "sync": "starting"}, 503, "degraded", id="starting"),
        pytest.param({"money": "running", "sync": "failed"}, 503, "degraded", id="one-failed"),
        pytest.param({"money": "stopped"}, 503, "degraded", id="stopped"),
        pytest.param({}, 503, "degraded", id="nothing-served"),
    ],
)
def test_liveness_body(states: dict[str, str], code: int, status: str) -> None:
    got_code, body = liveness_body(states)
    assert got_code == code
    assert body == {"status": status, "queues": states}


async def test_health_server_answers_live_and_nothing_else() -> None:
    states = {"money": "running"}
    server = HealthServer(host="127.0.0.1", port=0, liveness=lambda: states)
    await server.start()
    try:
        port = server.port
        assert port > 0
        code, body = await _probe(port)
        assert code == 200
        assert '"status": "ok"' in body and '"money": "running"' in body
        code, body = await _probe(port, "/health/live?verbose=1")
        assert code == 200
        code, _ = await _probe(port, "/nope")
        assert code == 404
        code, _ = await _probe(port, "/health/ready")
        assert code == 404
        states["money"] = "failed"
        code, body = await _probe(port)
        assert code == 503 and '"degraded"' in body
    finally:
        await server.stop()
    code, _ = await _probe(port)
    assert code == 0, "the port is closed after stop()"


async def test_health_server_port_is_unknown_before_start() -> None:
    server = HealthServer(host="127.0.0.1", port=0, liveness=dict)
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.port
    await server.stop()  # stopping a never-started server is a no-op


async def test_health_server_survives_a_garbage_request() -> None:
    server = HealthServer(host="127.0.0.1", port=0, liveness=lambda: {"q": "running"})
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        assert response.startswith(b"HTTP/1.1 404")
        code, _ = await _probe(server.port)
        assert code == 200, "still serving after the bad request"
    finally:
        await server.stop()


def test_probe_health_reports_a_refused_connection_as_zero() -> None:
    code, body = probe_health("127.0.0.1", 1, timeout_s=1)
    assert code == 0
    assert body


# --- connect with retry ----------------------------------------------------------


class _FakeService:
    def __init__(self, fail_describe: int) -> None:
        self.fail_describe = fail_describe
        self.describes = 0

    async def describe_namespace(self, request: Any) -> None:
        self.describes += 1
        if self.describes <= self.fail_describe:
            raise RuntimeError("namespace not registered yet")


class _FakeClient:
    def __init__(self, fail_describe: int = 0) -> None:
        self.namespace = "default"
        self.workflow_service = _FakeService(fail_describe)
        self.service_client = self


def _fake_connect(*, fail_connect: int = 0, fail_describe: int = 0) -> tuple[Any, list[str]]:
    calls: list[str] = []
    client = _FakeClient(fail_describe)

    async def connect(*, component: str) -> Any:
        calls.append(component)
        if len(calls) <= fail_connect:
            raise ConnectionRefusedError("refused")
        return client

    return connect, calls


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_connect_retries_a_refused_connection_with_doubling_backoff() -> None:
    connect, calls = _fake_connect(fail_connect=3)
    clock = _Clock()
    client = await connect_with_retry(
        timeout_s=120, component="worker-test", connect=connect, sleep=clock.sleep, clock=clock
    )
    assert isinstance(client, _FakeClient)
    assert calls == ["worker-test"] * 4
    assert clock.sleeps == [1.0, 2.0, 4.0]


async def test_connect_retries_until_the_namespace_answers() -> None:
    """A connected frontend whose namespace is not registered yet is not ready."""
    connect, _calls = _fake_connect(fail_describe=2)
    clock = _Clock()
    client = await connect_with_retry(
        timeout_s=120, connect=connect, sleep=clock.sleep, clock=clock
    )
    assert client.workflow_service.describes == 3
    assert clock.sleeps == [1.0, 2.0]


async def test_connect_gives_up_after_the_budget_and_never_oversleeps_it() -> None:
    connect, calls = _fake_connect(fail_connect=100)
    clock = _Clock()
    with pytest.raises(TemporalUnavailableError, match="did not answer within 12s"):
        await connect_with_retry(timeout_s=12, connect=connect, sleep=clock.sleep, clock=clock)
    # 1 + 2 + 4 = 7, then the remaining 5 (not 8), then no time left.
    assert clock.sleeps == [1.0, 2.0, 4.0, 5.0]
    assert clock.now == 12.0
    assert len(calls) == 5


async def test_connect_backoff_is_capped_at_ten_seconds() -> None:
    connect, _ = _fake_connect(fail_connect=6)
    clock = _Clock()
    await connect_with_retry(timeout_s=600, connect=connect, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


async def test_connect_succeeds_first_time_without_sleeping() -> None:
    connect, calls = _fake_connect()
    clock = _Clock()
    await connect_with_retry(timeout_s=1, connect=connect, sleep=clock.sleep, clock=clock)
    assert calls == ["worker"] and clock.sleeps == []


# --- build_worker ------------------------------------------------------------------


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        queues_mod,
        "WORKFLOWS_BY_QUEUE",
        MappingProxyType({q: ((Noop, DbPings) if q is TaskQueue.SYNC else ()) for q in TaskQueue}),
    )
    monkeypatch.setattr(
        queues_mod,
        "ACTIVITIES_BY_QUEUE",
        MappingProxyType({q: ((db_ping,) if q is TaskQueue.SYNC else ()) for q in TaskQueue}),
    )


# A built Worker holds its (namespace, task queue) slot in the SDK core until it is
# shut down, so each test runs the worker for the block and lets the exit release it.


async def test_build_worker_serves_exactly_the_registered_work(
    registries: None, temporal_client: Client
) -> None:
    worker = build_worker(temporal_client, TaskQueue.SYNC)
    async with worker:
        config = worker.config()
    assert config["task_queue"] == "sync"
    assert config["workflows"] == [Noop, DbPings]
    assert config["activities"] == [db_ping]
    assert config["max_concurrent_activities"] == settings.alkera_worker_max_concurrent_activities
    assert config["graceful_shutdown_timeout"] == GRACEFUL_SHUTDOWN == timedelta(seconds=110)
    assert config["identity"].startswith("worker-sync@")
    assert any(isinstance(i, AlkeraWorkerInterceptor) for i in config["interceptors"])


async def test_build_worker_honours_an_explicit_concurrency(
    registries: None, temporal_client: Client
) -> None:
    worker = build_worker(temporal_client, TaskQueue.SYNC, max_concurrent_activities=1)
    async with worker:
        assert worker.config()["max_concurrent_activities"] == 1


def test_build_worker_refuses_a_queue_with_nothing_registered(
    registries: None, temporal_client: Client
) -> None:
    with pytest.raises(NoWorkRegisteredError, match="'email'"):
        build_worker(temporal_client, TaskQueue.EMAIL)


def test_the_real_registries_only_hold_known_types() -> None:
    """Whatever the family modules register today lands on the queue the contract
    assigns; a workflow class with an unknown type name cannot be registered."""
    for queue, classes in queues_mod.WORKFLOWS_BY_QUEUE.items():
        for cls in classes:
            name = queues_mod.workflow_type_name(cls)
            assert queues_mod.QUEUE_FOR[name] is queue
    for queue, fns in queues_mod.ACTIVITIES_BY_QUEUE.items():
        for fn in fns:
            assert queues_mod.queue_for_activity(queues_mod.activity_type_name(fn)) is queue
    assert set(queues_mod.WORKFLOWS_BY_QUEUE) == set(TaskQueue)
    assert set(queues_mod.ACTIVITIES_BY_QUEUE) == set(TaskQueue)


async def test_every_registered_workflow_validates_under_the_default_sandbox(
    temporal_client: Client,
) -> None:
    """``build_worker`` prepares each workflow class in the SDK's sandbox, which
    re-imports the class's module in isolation: a production workflow module that
    forgets ``workflow.unsafe.imports_passed_through()`` around its app imports
    fails right here instead of at the first poll in production. (Vacuous until a
    job family registers its workflows; then it is the guard.)"""
    for queue in TaskQueue:
        if queues_mod.WORKFLOWS_BY_QUEUE[queue] or queues_mod.ACTIVITIES_BY_QUEUE[queue]:
            worker = build_worker(temporal_client, queue)
            async with worker:
                pass


def test_queue_for_activity_routes_the_email_dispatch_and_refuses_strangers() -> None:
    assert queues_mod.queue_for_activity("billing.dispatch_emails") is TaskQueue.EMAIL
    assert queues_mod.queue_for_activity("billing.expire_grants") is TaskQueue.MONEY
    with pytest.raises(LookupError, match="not a known workflow type"):
        queues_mod.queue_for_activity("billing.mystery")


def test_type_name_helpers_reject_undecorated_objects() -> None:
    assert queues_mod.workflow_type_name(Noop) == "stub.runner_noop"
    assert queues_mod.activity_type_name(db_ping) == "stub.db_ping"
    with pytest.raises(TypeError):
        queues_mod.workflow_type_name(dict)
    with pytest.raises(TypeError):
        queues_mod.activity_type_name(print)


# --- pool budget -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("queues", "concurrency", "pool", "overflow", "needed", "available"),
    [
        pytest.param(4, 3, 20, 10, 24, 30, id="dev-default-fits"),
        pytest.param(1, 1, 2, 1, 2, 3, id="saas-per-queue-fits"),
        pytest.param(4, 4, 20, 10, 32, 30, id="over"),
    ],
)
def test_pool_budget_counts_two_connections_per_activity(
    queues: int, concurrency: int, pool: int, overflow: int, needed: int, available: int
) -> None:
    assert pool_budget(queues, concurrency, pool_size=pool, max_overflow=overflow) == (
        needed,
        available,
    )


class _LogRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _rec(self, level: str) -> Any:
        def _log(event: str, **kw: Any) -> None:
            self.calls.append((level, event, kw))

        return _log

    def __getattr__(self, name: str) -> Any:
        if name in {"info", "warning", "error", "exception", "debug"}:
            return self._rec(name)
        raise AttributeError(name)


@pytest.fixture
def log_rec(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    rec = _LogRecorder()
    monkeypatch.setattr(runner, "log", rec)
    return rec


def test_boot_warns_when_the_pool_cannot_feed_the_workers(
    monkeypatch: pytest.MonkeyPatch, log_rec: _LogRecorder
) -> None:
    monkeypatch.setattr(settings, "database_pool_size", 2)
    monkeypatch.setattr(settings, "database_pool_max_overflow", 1)
    runner._warn_if_pool_too_small(1, 1)
    assert log_rec.calls == []
    runner._warn_if_pool_too_small(1, 2)
    [(level, event, fields)] = log_rec.calls
    assert (level, event) == ("warning", "worker.db_pool_budget_exceeded")
    assert (fields["connections_needed"], fields["connections_available"]) == (4, 3)


async def test_boot_leaves_the_process_opening_connections_with_a_jobs_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody waits on a job's statements, so they get the wider pair.

    The profile is what every connection this process opens is stamped with and
    a connection keeps its stamp for life, so it has to be chosen before the
    first one exists — which is the only reason boot says anything about the
    database at all. Asked of Postgres through the real pooled engine: a boot
    that stopped choosing would hand a batch over tens of thousands of rows the
    patience meant for a caller waiting on a response.

    Logging and Sentry are the process-global side effects of the same call and
    are stubbed; they have coverage of their own.
    """
    monkeypatch.setattr(runner, "configure_logging", lambda: None)
    monkeypatch.setattr(runner, "init_sentry", lambda component: None)
    monkeypatch.setattr(runner, "log_entitlements_status", lambda component: None)

    runner.boot()
    try:
        # The pool may hold connections opened before the choice; the stamp
        # rides the startup packet, so the proof is a connection opened after.
        await db_session.engine.dispose()
        async with db_session.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT name, setting FROM pg_settings "
                    "WHERE name IN ('lock_timeout', 'statement_timeout')"
                )
            )
            carried = {str(name): int(value) for name, value in rows}
    finally:
        db_session.use_timeout_profile("request")
        await db_session.engine.dispose()

    assert carried == {
        "lock_timeout": settings.database_background_lock_timeout_ms,
        "statement_timeout": settings.database_background_statement_timeout_ms,
    }


# --- run_workers on the dev server --------------------------------------------------


def _stub_factory(prefix: str, *, fail: frozenset[TaskQueue] = frozenset()) -> Any:
    def factory(client: Client, queue: TaskQueue) -> Any:
        if queue in fail:
            return _FailingWorker()
        return Worker(
            client,
            task_queue=f"{prefix}-{queue.value}",
            workflows=[Noop, DbPings],
            activities=[db_ping],
            interceptors=[AlkeraWorkerInterceptor()],
        )

    return factory


class _FailingWorker:
    """A worker whose run ends on its own — what an unrecoverable SDK error looks like."""

    async def run(self) -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("poller died")

    async def shutdown(self) -> None:
        return None


@pytest.mark.temporal
async def test_run_workers_serves_every_queue_and_stops_on_the_event(
    temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    prefix = f"runner-{request.node.name}"
    task = asyncio.create_task(
        run_workers(
            [TaskQueue.SYNC, TaskQueue.DEFAULT],
            health_host="127.0.0.1",
            health_port=0,
            client=temporal_client,
            stop=stop,
            sync_schedules_on_boot=False,
            worker_factory=_stub_factory(prefix),
            on_ready=ready.set_result,
        )
    )
    port = await asyncio.wait_for(ready, timeout=30)
    # Both queues are polling: a workflow started on each completes.
    for queue in ("sync", "default"):
        result = await temporal_client.execute_workflow(
            Noop.run,
            id=f"{prefix}-{queue}-noop",
            task_queue=f"{prefix}-{queue}",
            execution_timeout=timedelta(seconds=30),
        )
        assert result == "ok"
    code, body = await _probe(port)
    assert code == 200
    assert '"sync": "running"' in body and '"default": "running"' in body
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=15)
    assert time.monotonic() - started < 5.0
    code, _ = await _probe(port)
    assert code == 0, "the health port closed with the process"


@pytest.mark.temporal
async def test_a_queue_whose_worker_dies_degrades_liveness_but_the_rest_keep_serving(
    temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    prefix = f"runner-{request.node.name}"
    task = asyncio.create_task(
        run_workers(
            [TaskQueue.SYNC, TaskQueue.EMAIL],
            health_host="127.0.0.1",
            health_port=0,
            client=temporal_client,
            stop=stop,
            sync_schedules_on_boot=False,
            worker_factory=_stub_factory(prefix, fail=frozenset({TaskQueue.EMAIL})),
            on_ready=ready.set_result,
        )
    )
    port = await asyncio.wait_for(ready, timeout=30)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        code, body = await _probe(port)
        if code == 503:
            break
        await asyncio.sleep(0.05)
    assert code == 503
    assert '"email": "failed"' in body and '"sync": "running"' in body
    assert not task.done(), "the process keeps serving the healthy queue"
    assert (
        await temporal_client.execute_workflow(
            Noop.run,
            id=f"{prefix}-still-serving",
            task_queue=f"{prefix}-sync",
            execution_timeout=timedelta(seconds=30),
        )
        == "ok"
    )
    stop.set()
    with pytest.raises(RuntimeError, match="poller died"):
        await asyncio.wait_for(task, timeout=15)


@pytest.mark.temporal
async def test_run_workers_exits_with_the_failure_when_every_worker_has_died(
    temporal_client: Client,
) -> None:
    stop = asyncio.Event()
    with pytest.raises(RuntimeError, match="poller died"):
        await asyncio.wait_for(
            run_workers(
                [TaskQueue.EMAIL],
                health_host="127.0.0.1",
                health_port=0,
                client=temporal_client,
                stop=stop,
                sync_schedules_on_boot=False,
                worker_factory=_stub_factory("x", fail=frozenset({TaskQueue.EMAIL})),
            ),
            timeout=15,
        )


@pytest.mark.temporal
async def test_run_workers_syncs_schedules_only_when_serving_the_default_queue(
    temporal_client: Client, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    synced: list[Any] = []

    async def fake_sync(client: Any) -> None:
        synced.append(client)

    monkeypatch.setattr(runner, "sync_schedules", fake_sync)
    prefix = f"runner-{request.node.name}"
    for served, expected in (([TaskQueue.SYNC], 0), ([TaskQueue.DEFAULT, TaskQueue.SYNC], 1)):
        synced.clear()
        stop = asyncio.Event()
        ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            run_workers(
                served,
                health_host="127.0.0.1",
                health_port=0,
                client=temporal_client,
                stop=stop,
                worker_factory=_stub_factory(f"{prefix}-{len(served)}"),
                on_ready=ready.set_result,
            )
        )
        await asyncio.wait_for(ready, timeout=30)
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        assert len(synced) == expected
        assert all(c is temporal_client for c in synced)


@pytest.mark.temporal
async def test_run_workers_skips_the_schedule_sync_when_told_to(
    temporal_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_sync(client: Any) -> None:
        raise AssertionError("must not sync")

    monkeypatch.setattr(runner, "sync_schedules", fake_sync)
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        run_workers(
            [TaskQueue.DEFAULT],
            health_host="127.0.0.1",
            health_port=0,
            client=temporal_client,
            stop=stop,
            sync_schedules_on_boot=False,
            worker_factory=_stub_factory("nosync"),
            on_ready=ready.set_result,
        )
    )
    await asyncio.wait_for(ready, timeout=30)
    stop.set()
    await asyncio.wait_for(task, timeout=15)


async def test_run_workers_refuses_an_empty_queue_list(temporal_client: Client) -> None:
    with pytest.raises(ValueError, match="at least one task queue"):
        await run_workers([], health_host="127.0.0.1", health_port=0, client=temporal_client)


@pytest.mark.temporal
async def test_run_workers_leaves_a_queue_with_nothing_registered_unpolled(
    registries: None, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    """A worker built without the product's job families still boots on the
    default queue list: the queue nothing serves is skipped, the rest serve."""
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    built: list[TaskQueue] = []
    factory = _stub_factory(f"runner-{request.node.name}")

    def recording_factory(client: Client, queue: TaskQueue) -> Any:
        built.append(queue)
        return factory(client, queue)

    task = asyncio.create_task(
        run_workers(
            [TaskQueue.EMAIL, TaskQueue.SYNC],
            health_host="127.0.0.1",
            health_port=0,
            client=temporal_client,
            stop=stop,
            sync_schedules_on_boot=False,
            worker_factory=recording_factory,
            on_ready=ready.set_result,
        )
    )
    port = await asyncio.wait_for(ready, timeout=30)
    code, body = await _probe(port)
    stop.set()
    await asyncio.wait_for(task, timeout=15)
    assert built == [TaskQueue.SYNC]
    assert code == 200
    assert '"sync": "running"' in body and '"email"' not in body


async def test_run_workers_refuses_when_no_requested_queue_has_work(
    registries: None, temporal_client: Client
) -> None:
    with pytest.raises(NoWorkRegisteredError, match="'email'"):
        await run_workers(
            [TaskQueue.EMAIL], health_host="127.0.0.1", health_port=0, client=temporal_client
        )


@pytest.mark.temporal
async def test_activities_share_the_pooled_engine_on_the_worker_loop(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    """Every activity runs on the one process loop, so the pooled engine (not a
    per-task NullPool) is correct: repeated DB activities in one worker reuse it."""
    async with temporal_worker(workflows=[DbPings], activities=[db_ping]) as running:
        result = await temporal_client.execute_workflow(
            DbPings.run,
            3,
            id=f"db-pings-{request.node.name}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == [1, 1, 1]


@pytest.mark.temporal
async def test_run_workers_starts_the_reerasure_on_boot_only_when_serving_the_default_queue(
    temporal_client: Client, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A database restored from before an erasure must not wait for anyone to
    remember the runbook: every boot of a default-queue worker starts the
    re-erasure, which checks the ledger and acts only on what it finds."""
    started: list[tuple[str, str]] = []

    async def fake_start(client: Any, workflow: str, **kwargs: Any) -> bool:
        started.append((workflow, kwargs["id"]))
        return True

    async def fake_sync(client: Any) -> None:
        return None

    monkeypatch.setattr(runner, "start_workflow_best_effort", fake_start)
    monkeypatch.setattr(runner, "sync_schedules", fake_sync)
    prefix = f"runner-{request.node.name}"
    for served, expected in (
        ([TaskQueue.SYNC], []),
        ([TaskQueue.DEFAULT], [("account.reerase", "account.reerase")]),
    ):
        started.clear()
        stop = asyncio.Event()
        ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            run_workers(
                served,
                health_host="127.0.0.1",
                health_port=0,
                client=temporal_client,
                stop=stop,
                worker_factory=_stub_factory(f"{prefix}-{served[0].value}"),
                on_ready=ready.set_result,
            )
        )
        await asyncio.wait_for(ready, timeout=30)
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        assert started == expected
