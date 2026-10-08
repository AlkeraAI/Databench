"""The workflow sandbox configuration the runner and the test harness share:
``pydantic_core`` is passed through alongside the SDK's own ``pydantic``
passthrough, so building a versioned shape inside a workflow never imports
during an activation. The behavioural pin — a sandboxed production workflow
that constructs pydantic models runs with no late-import warning — lives with
the first such workflow, in ``test_github_workflow.py``."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from alkera_core.temporal import TaskQueue
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions
from worker.temporal import queues as queues_mod
from worker.temporal.runner import build_worker
from worker.temporal.sandbox import PASSTHROUGH_MODULES, SANDBOX_RESTRICTIONS, workflow_runner


def test_pydantic_core_is_passed_through_on_top_of_the_sdk_defaults() -> None:
    assert PASSTHROUGH_MODULES == frozenset({"pydantic_core"})
    assert SANDBOX_RESTRICTIONS.passthrough_modules == (
        SandboxRestrictions.default.passthrough_modules | PASSTHROUGH_MODULES
    )
    # Nothing else about the default sandbox is relaxed.
    assert SANDBOX_RESTRICTIONS.passthrough_all_modules is False
    assert SANDBOX_RESTRICTIONS.invalid_modules == SandboxRestrictions.default.invalid_modules
    assert (
        SANDBOX_RESTRICTIONS.invalid_module_members
        == SandboxRestrictions.default.invalid_module_members
    )


def test_the_sdk_default_does_not_already_cover_pydantic_core() -> None:
    """Guard the guard: the day the SDK passes pydantic_core through itself,
    this override is dead weight and should go."""
    assert "pydantic" in SandboxRestrictions.default.passthrough_modules
    assert "pydantic_core" not in SandboxRestrictions.default.passthrough_modules


def test_workflow_runner_builds_a_sandboxed_runner_with_the_restrictions() -> None:
    runner = workflow_runner()
    assert isinstance(runner, SandboxedWorkflowRunner)
    assert runner.restrictions is SANDBOX_RESTRICTIONS
    assert workflow_runner() is not runner, "one runner per Worker, never shared"


@workflow.defn(name="stub.sandbox_noop", sandboxed=False)
class Noop:
    @workflow.run
    async def run(self) -> None:
        return None


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        queues_mod,
        "WORKFLOWS_BY_QUEUE",
        MappingProxyType({q: ((Noop,) if q is TaskQueue.SYNC else ()) for q in TaskQueue}),
    )
    monkeypatch.setattr(
        queues_mod, "ACTIVITIES_BY_QUEUE", MappingProxyType({q: () for q in TaskQueue})
    )


async def test_build_worker_installs_the_codebase_sandbox(
    registries: None, temporal_client: Client
) -> None:
    worker = build_worker(temporal_client, TaskQueue.SYNC)
    async with worker:
        installed = worker.config()["workflow_runner"]
    assert isinstance(installed, SandboxedWorkflowRunner)
    assert installed.restrictions is SANDBOX_RESTRICTIONS


async def test_the_test_harness_installs_the_same_sandbox(temporal_worker: Any) -> None:
    async with temporal_worker(workflows=[Noop]) as running:
        installed = running.worker.config()["workflow_runner"]
    assert isinstance(installed, SandboxedWorkflowRunner)
    assert installed.restrictions is SANDBOX_RESTRICTIONS
