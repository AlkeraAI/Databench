"""The serving process: one ``Worker`` per task queue, a liveness endpoint, and
an orderly stop.

`run_workers` is the whole of ``python -m worker run``. It connects (retrying
until the namespace answers — the self-hosted server registers its namespace
seconds after it starts serving, and a worker that races it must wait rather
than crash-loop), syncs the schedule catalog when it serves the default queue,
builds one Worker per queue, serves ``GET /health/live`` and runs until
SIGTERM / SIGINT. A worker whose run ends on its own (an error the SDK could
not recover from) is reported as ``failed`` and the endpoint answers 503 so the
orchestrator replaces the task; the other queues keep serving in the meantime.

The health endpoint is a few lines of raw HTTP/1.1 over ``asyncio.start_server``
on purpose: the worker image carries no web framework, and a probe needs none.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

from alkera_core.config import settings
from alkera_core.db import session as db_session
from alkera_core.entitlements import log_entitlements_status
from alkera_core.logging import configure_logging, get_logger
from alkera_core.observability.sentry import init_sentry
from alkera_core.temporal import (
    QUEUE_FOR,
    TaskQueue,
    WorkflowType,
    close_shared_client,
    connect_client,
    default_identity,
    drain_workflow_id,
    start_workflow_best_effort,
)
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.worker import Worker

from worker.files_bootstrap import FilesSettingsError, wire_files_jobs
from worker.schedules import sync_schedules
from worker.tasks.compute import ComputeProviderKeyMissingError, check_money_path_provider_key
from worker.temporal import queues
from worker.temporal.client import WORKER_COMPONENT
from worker.temporal.interceptors import ActivityRecorder, AlkeraWorkerInterceptor
from worker.temporal.sandbox import workflow_runner

log = get_logger(__name__)

GRACEFUL_SHUTDOWN = timedelta(seconds=110)
"""Inside the orchestrator's 120 s stop timeout: in-flight activities get this
long to finish before the process exits."""

QueueState = str  # "starting" | "running" | "stopped" | "failed"


class TemporalUnavailableError(RuntimeError):
    """The namespace did not answer within the boot connect budget."""


class NoWorkRegisteredError(LookupError):
    """A queue with no workflow and no activity registered cannot be served."""


# --- liveness ----------------------------------------------------------------


def liveness_body(states: Mapping[str, QueueState]) -> tuple[int, dict[str, Any]]:
    """``(status code, body)`` for ``/health/live``: 200 while every queue's
    worker is running, 503 once any has stopped or failed."""
    healthy = bool(states) and all(state == "running" for state in states.values())
    return (200 if healthy else 503), {
        "status": "ok" if healthy else "degraded",
        "queues": dict(states),
    }


class HealthServer:
    """``GET /health/live`` → 200 / 503 from ``liveness()``; anything else → 404."""

    def __init__(
        self, *, host: str, port: int, liveness: Callable[[], Mapping[str, QueueState]]
    ) -> None:
        self._host = host
        self._port = port
        self._liveness = liveness
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("health server is not running")
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # Drain the headers (bounded) so the client sees a clean response.
            for _ in range(100):
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1", "replace").split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            if method == "GET" and path.split("?", 1)[0] == "/health/live":
                code, body = liveness_body(self._liveness())
            else:
                code, body = 404, {"error": "not found"}
            payload = json.dumps(body).encode()
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[code]
            writer.write(
                (
                    f"HTTP/1.1 {code} {reason}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + payload
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()


# --- connect / build ---------------------------------------------------------


async def connect_with_retry(
    *,
    timeout_s: float,
    component: str = WORKER_COMPONENT,
    connect: Callable[..., Awaitable[Client]] = connect_client,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Client:
    """Connect and confirm the namespace answers, retrying with backoff for up
    to ``timeout_s``; raises ``TemporalUnavailableError`` past it."""
    started = clock()
    delay = 1.0
    attempt = 0
    while True:
        attempt += 1
        try:
            client = await connect(component=component)
            await client.service_client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=client.namespace)
            )
            return client
        except Exception as exc:
            elapsed = clock() - started
            remaining = timeout_s - elapsed
            if remaining <= 0:
                raise TemporalUnavailableError(
                    f"Temporal at {settings.temporal_address} (namespace "
                    f"{settings.temporal_namespace!r}) did not answer within {timeout_s:.0f}s: "
                    f"{exc}"
                ) from exc
            log.warning(
                "worker.temporal_connect_retry",
                attempt=attempt,
                error=str(exc) or type(exc).__name__,
                retry_in_s=min(delay, remaining),
            )
            await sleep(min(delay, remaining))
            delay = min(delay * 2, 10.0)


def build_worker(
    client: Client,
    queue: TaskQueue,
    *,
    recorder: ActivityRecorder | None = None,
    max_concurrent_activities: int | None = None,
) -> Worker:
    """One ``Worker`` serving exactly what the registries hold for ``queue``."""
    workflows = queues.WORKFLOWS_BY_QUEUE[queue]
    activities = queues.ACTIVITIES_BY_QUEUE[queue]
    if not workflows and not activities:
        raise NoWorkRegisteredError(
            f"no workflow or activity is registered for the {queue.value!r} queue; "
            "a worker serving it would poll forever"
        )
    return Worker(
        client,
        task_queue=queue.value,
        workflows=list(workflows),
        activities=list(activities),
        interceptors=[AlkeraWorkerInterceptor(recorder)],
        workflow_runner=workflow_runner(),
        max_concurrent_activities=(
            max_concurrent_activities or settings.alkera_worker_max_concurrent_activities
        ),
        identity=default_identity(f"worker-{queue.value}"),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN,
    )


def pool_budget(
    queues_served: int, concurrency: int, *, pool_size: int, max_overflow: int
) -> tuple[int, int]:
    """``(connections the workers may need, connections the pool can give)``.
    An advisory-locked activity holds two connections (the lock and its
    session), hence the factor of two."""
    return queues_served * concurrency * 2, pool_size + max_overflow


def _warn_if_pool_too_small(queues_served: int, concurrency: int) -> None:
    needed, available = pool_budget(
        queues_served,
        concurrency,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_max_overflow,
    )
    if needed > available:
        log.warning(
            "worker.db_pool_budget_exceeded",
            queues=queues_served,
            max_concurrent_activities=concurrency,
            connections_needed=needed,
            connections_available=available,
            hint="lower ALKERA_WORKER_MAX_CONCURRENT_ACTIVITIES or raise DATABASE_POOL_SIZE",
        )


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows has no loop-level signal handlers; a plain handler that
            # hops onto the loop is the portable fallback.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    return stop


# --- the process -------------------------------------------------------------


def _queues_with_work(requested: Sequence[TaskQueue]) -> tuple[TaskQueue, ...]:
    """The requested queues some job is registered for. A deployment without
    the product's job families has queues nothing serves (the open worker has
    no email job); those are logged and left unpolled rather than refused, so
    the default queue list boots every distribution. Refuses when none is left."""
    serving = tuple(
        queue
        for queue in requested
        if queues.WORKFLOWS_BY_QUEUE[queue] or queues.ACTIVITIES_BY_QUEUE[queue]
    )
    idle = [queue.value for queue in requested if queue not in serving]
    if idle:
        log.info("worker.queues_idle", queues=idle)
    if not serving:
        raise NoWorkRegisteredError(
            f"no workflow or activity is registered for any of {[q.value for q in requested]}; "
            "a worker serving them would poll forever"
        )
    return serving


async def run_workers(
    queues_to_serve: Sequence[TaskQueue],
    *,
    health_host: str,
    health_port: int,
    client: Client | None = None,
    stop: asyncio.Event | None = None,
    sync_schedules_on_boot: bool = True,
    worker_factory: Callable[[Client, TaskQueue], Worker] = build_worker,
    on_ready: Callable[[int], None] | None = None,
) -> None:
    """Serve ``queues_to_serve`` until ``stop`` is set (SIGTERM / SIGINT when no
    event is given). Raises the first worker failure after shutdown so the
    process exits non-zero; ``on_ready`` receives the bound health port."""
    if not queues_to_serve:
        raise ValueError("at least one task queue must be served")
    queues_to_serve = _queues_with_work(queues_to_serve)
    own_client = client is None
    if client is None:
        client = await connect_with_retry(timeout_s=settings.alkera_worker_connect_timeout_seconds)
    try:
        if TaskQueue.DEFAULT in queues_to_serve and sync_schedules_on_boot:
            await sync_schedules(client)
        if TaskQueue.DEFAULT in queues_to_serve:
            # A database restored from before an erasure brings the erased back;
            # the re-erasure checks the ledger on every boot and acts only on
            # what it finds. Best-effort: a refused start is logged, not fatal.
            await start_workflow_best_effort(
                client,
                WorkflowType.ACCOUNT_REERASE.value,
                id=drain_workflow_id(WorkflowType.ACCOUNT_REERASE),
                task_queue=QUEUE_FOR[WorkflowType.ACCOUNT_REERASE].value,
                args=(),
                signal=None,
                timeout_s=5.0,
                fail_log_event="account.reerase.boot_start_failed",
            )
        workers = {queue: worker_factory(client, queue) for queue in queues_to_serve}
        states: dict[str, QueueState] = {queue.value: "starting" for queue in queues_to_serve}
        _warn_if_pool_too_small(len(workers), settings.alkera_worker_max_concurrent_activities)

        health = HealthServer(host=health_host, port=health_port, liveness=lambda: dict(states))
        await health.start()
        if on_ready is not None:
            on_ready(health.port)
        loop = asyncio.get_running_loop()
        stop_event = stop if stop is not None else _install_signal_handlers(loop)

        async def run_one(queue: TaskQueue, worker: Worker) -> None:
            states[queue.value] = "running"
            try:
                await worker.run()
            except BaseException:
                states[queue.value] = "failed"
                log.exception("worker.queue_failed", queue=queue.value)
                raise
            states[queue.value] = "stopped"

        tasks = {queue: asyncio.create_task(run_one(queue, w)) for queue, w in workers.items()}
        log.info(
            "worker.serving",
            queues=[q.value for q in queues_to_serve],
            health_port=health.port,
            max_concurrent_activities=settings.alkera_worker_max_concurrent_activities,
        )
        stop_waiter = asyncio.create_task(stop_event.wait())
        try:
            # Serve until a stop is requested or every worker has ended on its own.
            # One worker ending early is reported as `failed` (the endpoint answers
            # 503 and the orchestrator replaces the task); the others keep serving
            # until then.
            alive: set[asyncio.Task[None]] = set(tasks.values())
            while alive and not stop_waiter.done():
                done, _ = await asyncio.wait(
                    {stop_waiter, *alive}, return_when=asyncio.FIRST_COMPLETED
                )
                alive -= done
            if stop_waiter.done():
                log.info("worker.stopping", queues=[q.value for q in queues_to_serve])
            await asyncio.gather(
                *(w.shutdown() for queue, w in workers.items() if not tasks[queue].done()),
                return_exceptions=True,
            )
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            stop_waiter.cancel()
            await health.stop()
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            raise failures[0]
    finally:
        if own_client:
            await close_shared_client()


def boot() -> None:
    """The side effects every long-lived worker process performs once."""
    configure_logging()
    init_sentry("worker")
    log_entitlements_status("worker")
    # Before the first connection is opened: a job's statements run with nobody
    # waiting on a response, so they get the background limits — long enough for
    # a batch over tens of thousands of rows, still finite.
    db_session.use_timeout_profile("background")
    # The pooled engine is right here: every activity runs on this one loop.
    log.info(
        "worker.db_pool",
        pool_size=db_session.POOL_SIZE,
        max_overflow=db_session.MAX_OVERFLOW,
        **db_session.session_timeouts(settings, "background"),
    )


def main_run(
    queues_to_serve: Sequence[TaskQueue],
    *,
    health_host: str,
    health_port: int,
    sync_schedules_on_boot: bool,
) -> int:
    """``python -m worker run``: boot, serve, exit 0 on an orderly stop."""
    boot()
    try:
        # A money path that cannot reach its provider bills nothing and cuts
        # nothing off, per row, forever. Refuse the queue rather than serve it
        # blind.
        check_money_path_provider_key(queues_to_serve)
    except ComputeProviderKeyMissingError as exc:
        log.error("worker.compute_provider_key_missing", error=str(exc))
        return 1
    try:
        # Files stays unwired when the deployment does not serve it; a store it
        # names but cannot address is refused here rather than on the first tick.
        wire_files_jobs()
    except FilesSettingsError as exc:
        log.error("worker.files_settings_invalid", error=str(exc))
        return 1
    try:
        asyncio.run(
            run_workers(
                queues_to_serve,
                health_host=health_host,
                health_port=health_port,
                sync_schedules_on_boot=sync_schedules_on_boot,
            )
        )
    except TemporalUnavailableError as exc:
        log.error("worker.temporal_unavailable", error=str(exc))
        return 1
    except Exception:
        log.exception("worker.exited_with_failure")
        return 1
    return 0
