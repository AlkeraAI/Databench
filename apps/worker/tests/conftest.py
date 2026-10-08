"""Worker-suite fixtures: a Worker factory on the session's Temporal dev server.

The dev server itself (``temporal_env`` / ``temporal_client``) lives in the
repo-root conftest so every suite shares it; this file adds only what a worker
test needs on top — a running ``Worker`` on a queue of its own, always carrying
the production interceptor.

The suite runs whatever composition is installed when it starts: the open
worker in the open tree, the product where a distribution's test layer (see
the root conftest) installed its extensions first.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.compute.container import make_container_provider
from alkera_core.config import settings
from temporalio.client import Client
from temporalio.worker import Worker
from worker.temporal.interceptors import ActivityRecorder, AlkeraWorkerInterceptor
from worker.temporal.sandbox import workflow_runner


@dataclass(frozen=True, slots=True)
class RunningWorker:
    """What the factory yields: the queue to start workflows on, and the worker."""

    task_queue: str
    worker: Worker


WorkerFactory = Callable[..., Any]


@pytest.fixture(autouse=True)
def _no_real_registered_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconcile lists every registered provider that says it is configured,
    and the meter's boot gate passes when any catalog provider is; in a test
    process on a developer's Mac that includes the real ``localdev`` provider,
    which shells out to Docker and reads as configured under ``APP_ENV=local``.
    Each worker test gets an unconfigured placeholder for every kind but RunPod
    and EC2 (whose settings the tests set), and a test about one of those
    providers substitutes its own fake over this."""
    from alkera_core.compute.provider import EC2, RUNPOD
    from worker.tasks import compute as compute_tasks

    real = compute_tasks.provider_for_kind

    def _provider_for_kind(kind: str, config: Any) -> Any:
        if kind in (RUNPOD, EC2):
            return real(kind, config)
        return make_container_provider(settings)

    monkeypatch.setattr(compute_tasks, "provider_for_kind", _provider_for_kind)
    monkeypatch.setattr(
        compute_tasks, "make_kind_provider", lambda kind: make_container_provider(settings)
    )


@pytest.fixture
def temporal_worker(temporal_client: Client, request: pytest.FixtureRequest) -> WorkerFactory:
    """``async with temporal_worker(workflows=[...], activities=[...]) as running:``

    Runs a ``Worker`` for the block on a queue unique to this test (so parallel
    tests on the shared dev server never pick up each other's tasks), with the
    production ``AlkeraWorkerInterceptor`` and workflow sandbox installed
    exactly as the runner does. ``recorder`` plugs an ``ActivityRecorder`` in;
    ``queue`` overrides the name.
    """

    @asynccontextmanager
    async def _run(
        *,
        workflows: Sequence[type] = (),
        activities: Sequence[Callable[..., Any]] = (),
        queue: str | None = None,
        recorder: ActivityRecorder | None = None,
        max_concurrent_activities: int | None = None,
    ) -> AsyncIterator[RunningWorker]:
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", request.node.name)[:40]
        task_queue = queue or f"test-{slug}-{uuid4().hex[:8]}"
        worker = Worker(
            temporal_client,
            task_queue=task_queue,
            workflows=list(workflows),
            activities=list(activities),
            interceptors=[AlkeraWorkerInterceptor(recorder)],
            workflow_runner=workflow_runner(),
            max_concurrent_activities=max_concurrent_activities,
        )
        async with worker:
            yield RunningWorker(task_queue=task_queue, worker=worker)

    return _run
