"""The Files workflows through a real Worker on the local dev server.

The workflows carry no logic of their own; what has to be proven on a real
server is the wiring nothing in-process can show — that each workflow reaches
the identically named activity, that the janitor's pinned clock survives the
round trip, and above all that the refusals declared non-retryable really do
end the run after ONE attempt, which is a property of the retry policy the
workflow attaches and of nothing the activity can assert about itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.files.errors import QuotaExceeded
from alkera_core.files.sweepers import JANITOR_ORDER, JanitorReport, SweepOutcome
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import WorkflowType
from sqlalchemy.exc import OperationalError
from temporalio.client import Client, WorkflowFailureError
from worker.activities import files as activities
from worker.tasks import files as tasks
from worker.workflows.files import FilesAclRewrite, FilesJanitor, FilesPromote

pytestmark = pytest.mark.temporal

ORG = uuid.UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Calls:
    """How many times the core actually ran, which is what "no retry" means."""

    def __init__(self) -> None:
        self.attempts = 0


async def test_the_promote_workflow_reaches_its_activity_and_returns_the_version(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id = str(uuid.uuid4())
    op_id = str(uuid.uuid4())
    seen: list[tuple[Any, ...]] = []

    async def _promote(op: Any, org: Any) -> str:
        seen.append((str(op), str(org)))
        return version_id

    monkeypatch.setattr(tasks, "promote", _promote)
    async with temporal_worker(
        workflows=[FilesPromote], activities=[activities.files_promote]
    ) as running:
        result = await temporal_client.execute_workflow(
            FilesPromote.run,
            args=[op_id, str(ORG)],
            id=f"files-promote-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert result == version_id
    assert seen == [(op_id, str(ORG))]


async def test_a_permanent_refusal_fails_the_run_after_exactly_one_attempt(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-quota upload is not going to fit on the sixth try either. The
    retry policy names ``QuotaExceeded`` non-retryable, so the core runs once."""
    calls = _Calls()

    async def _promote(*args: Any, **kwargs: Any) -> str:
        calls.attempts += 1
        raise QuotaExceeded(message="the drive is full")

    monkeypatch.setattr(tasks, "promote", _promote)
    async with temporal_worker(
        workflows=[FilesPromote], activities=[activities.files_promote]
    ) as running:
        with pytest.raises(WorkflowFailureError):
            await temporal_client.execute_workflow(
                FilesPromote.run,
                args=[str(uuid.uuid4()), str(ORG)],
                id=f"files-promote-quota-{uuid.uuid4()}",
                task_queue=running.task_queue,
            )

    assert calls.attempts == 1


async def test_a_transient_failure_is_retried_under_the_same_policy(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative twin of the case above: an error the policy does not name
    keeps its budget, so the second attempt is the one that succeeds — which is
    also what proves the first test's ``1`` is the policy at work and not a
    workflow that never retries anything."""
    calls = _Calls()
    version_id = str(uuid.uuid4())

    async def _promote(*args: Any, **kwargs: Any) -> str:
        calls.attempts += 1
        if calls.attempts == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))
        return version_id

    monkeypatch.setattr(tasks, "promote", _promote)
    async with temporal_worker(
        workflows=[FilesPromote], activities=[activities.files_promote]
    ) as running:
        result = await temporal_client.execute_workflow(
            FilesPromote.run,
            args=[str(uuid.uuid4()), str(ORG)],
            id=f"files-promote-transient-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert result == version_id
    assert calls.attempts == 2


def _report(at: datetime, swept: int) -> JanitorReport:
    return JanitorReport(
        at=at, outcomes=tuple(SweepOutcome(name=k.name, swept=swept) for k in JANITOR_ORDER)
    )


async def test_the_janitor_workflow_carries_its_pinned_clock_to_the_pass(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[datetime | None] = []

    async def _janitor(now: datetime | None = None, **kwargs: Any) -> tasks.JanitorPass:
        seen.append(now)
        return tasks.JanitorPass(
            at=now or NOW, orgs=(ORG,), reports=(_report(now or NOW, 2),), cursor=None
        )

    monkeypatch.setattr(tasks, "janitor", _janitor)
    async with temporal_worker(
        workflows=[FilesJanitor], activities=[activities.files_janitor]
    ) as running:
        swept = await temporal_client.execute_workflow(
            FilesJanitor.run,
            SweepInput(now=NOW),
            id=f"files-janitor-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert seen == [NOW]
    assert swept == 2 * len(JANITOR_ORDER)


async def test_the_acl_rewrite_workflow_reports_the_batch_it_fixed(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _rewrite(op: Any, org: Any) -> int:
        return 41

    monkeypatch.setattr(tasks, "acl_rewrite", _rewrite)
    async with temporal_worker(
        workflows=[FilesAclRewrite], activities=[activities.files_acl_rewrite]
    ) as running:
        fixed = await temporal_client.execute_workflow(
            FilesAclRewrite.run,
            args=[str(uuid.uuid4()), str(ORG)],
            id=f"files-acl-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert fixed == 41


def test_every_files_workflow_and_its_activity_share_one_name() -> None:
    pairs = [
        (FilesPromote, activities.files_promote, WorkflowType.FILES_PROMOTE),
        (FilesJanitor, activities.files_janitor, WorkflowType.FILES_JANITOR),
        (FilesAclRewrite, activities.files_acl_rewrite, WorkflowType.FILES_ACL_REWRITE),
    ]
    for cls, fn, workflow_type in pairs:
        assert cls.__temporal_workflow_definition.name == workflow_type.value  # type: ignore[attr-defined]
        assert fn.__temporal_activity_definition.name == workflow_type.value  # type: ignore[attr-defined]


async def test_a_fleet_larger_than_one_page_is_swept_across_activities(
    temporal_client: Client, temporal_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is the point: one activity sweeps a page and says where it
    stopped, and the workflow keeps going until a page runs short — so an
    install with more orgs than a single activity can hold is still swept
    whole, and no page ever repeats an org the last one covered."""
    fleet = sorted(uuid.UUID(int=n, version=4) for n in range(1, 6))
    page_size = 2
    handed: list[str | None] = []

    async def _janitor(
        now: datetime | None = None, *, after: uuid.UUID | None = None, **kwargs: Any
    ) -> tasks.JanitorPass:
        handed.append(str(after) if after is not None else None)
        remaining = [org for org in fleet if after is None or org > after]
        page = remaining[:page_size]
        return tasks.JanitorPass(
            at=now or NOW,
            orgs=tuple(page),
            reports=tuple(_report(now or NOW, 1) for _ in page),
            cursor=page[-1] if len(page) == page_size else None,
        )

    monkeypatch.setattr(tasks, "janitor", _janitor)
    async with temporal_worker(
        workflows=[FilesJanitor], activities=[activities.files_janitor]
    ) as running:
        swept = await temporal_client.execute_workflow(
            FilesJanitor.run,
            SweepInput(now=NOW),
            id=f"files-janitor-pages-{uuid.uuid4()}",
            task_queue=running.task_queue,
        )

    assert handed == [None, str(fleet[1]), str(fleet[3])], "each page resumed at the last org"
    assert swept == len(fleet) * len(JANITOR_ORDER), "every org in the fleet was swept once"
