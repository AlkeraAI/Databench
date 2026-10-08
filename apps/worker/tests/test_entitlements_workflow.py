"""The entitlements watchdog as a Temporal workflow: the real workflow and the
real activity through a real Worker.

The watchdog must self-skip when no grant is configured and otherwise re-emit
the status line at the level the grant's state demands; its types are the
historical task name on the default queue; a transient failure is attempted
exactly once (the policy table says no retry — this proves the workflow
attaches it); and its catalog entry is now created by the schedule reconciler
instead of being reported pending.

The states are driven by the grant's expiry relative to the real clock rather
than by freezing time: a frozen clock under a live Worker would stall the
event loop's own timers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core import entitlements as ent
from alkera_core.config import settings
from alkera_core.entitlements import Feature, generate_keypair, mint_entitlement_token
from alkera_core.temporal import TaskQueue, WorkflowType
from sqlalchemy.exc import OperationalError
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from worker.activities.entitlements import watchdog
from worker.schedules import SCHEDULES, ScheduleSyncReport, fingerprint, sync_schedules
from worker.tasks import entitlements as entitlements_tasks
from worker.temporal import queues
from worker.workflows.entitlements import EntitlementsWatchdog

pytestmark = pytest.mark.temporal

PRIV, PUB = generate_keypair()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.calls.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append(("warning", event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.calls.append(("error", event, kw))

    def status_levels(self) -> list[str]:
        return [level for level, event, _ in self.calls if event == "entitlements.status"]


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    ent.get_entitlements.cache_clear()
    monkeypatch.setattr(settings, "alkera_entitlements", None)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", None)
    monkeypatch.setattr(settings, "app_env", "local")
    yield
    ent.get_entitlements.cache_clear()


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(ent, "log", recorder)
    return recorder


def _entitled(monkeypatch: pytest.MonkeyPatch, *, expires_in_days: int) -> str:
    """A grant expiring ``expires_in_days`` from today (negative = already expired).
    Returns the ISO expiry date."""
    expires_on = datetime.now(UTC).date() + timedelta(days=expires_in_days)
    token = mint_entitlement_token(
        customer="acme-corp",
        features=Feature.BYOK,
        expires_on=expires_on,
        serial=1,
        signing_key_b64=PRIV,
    )
    monkeypatch.setattr(settings, "alkera_entitlements", token)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", PUB)
    ent.get_entitlements.cache_clear()
    return expires_on.isoformat()


async def _execute(temporal_worker: Any, temporal_client: Client) -> Any:
    async with temporal_worker(workflows=[EntitlementsWatchdog], activities=[watchdog]) as running:
        return await temporal_client.execute_workflow(
            EntitlementsWatchdog.run,
            id=f"entitlements.watchdog-{uuid4().hex[:8]}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )


async def test_the_watchdog_self_skips_when_no_entitlement_is_configured(
    temporal_worker: Any, temporal_client: Client, rec: _Recorder
) -> None:
    assert await _execute(temporal_worker, temporal_client) == {
        "skipped": "no entitlement configured"
    }
    assert rec.calls == []


@pytest.mark.parametrize(
    ("expires_in_days", "state", "level"),
    [
        pytest.param(365, "valid", "info", id="valid"),
        # Ten days past expiry sits inside the thirty-day grace window.
        pytest.param(-10, "grace", "warning", id="grace"),
        pytest.param(-60, "expired", "error", id="expired"),
    ],
)
async def test_the_watchdog_reports_the_grant_state_at_the_matching_log_level(
    temporal_worker: Any,
    temporal_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    expires_in_days: int,
    state: str,
    level: str,
) -> None:
    expires_on = _entitled(monkeypatch, expires_in_days=expires_in_days)
    result = await _execute(temporal_worker, temporal_client)
    assert result == {
        "state": state,
        "customer": "acme-corp",
        "features": ["byok"],
        "expires_on": expires_on,
    }
    assert rec.status_levels() == [level]


def test_the_types_are_the_historical_task_name_on_the_default_queue() -> None:
    assert queues.workflow_type_name(EntitlementsWatchdog) == "entitlements.watchdog"
    assert queues.activity_type_name(watchdog) == "entitlements.watchdog"
    for queue in TaskQueue:
        expect = queue is TaskQueue.DEFAULT
        assert (EntitlementsWatchdog in queues.WORKFLOWS_BY_QUEUE[queue]) is expect, queue
        assert (watchdog in queues.ACTIVITIES_BY_QUEUE[queue]) is expect, queue


async def test_a_transient_failure_is_attempted_exactly_once(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable failure class still gets one attempt: the log line is not
    worth a retry, and tomorrow's schedule re-emits it anyway."""
    attempts: list[int] = []

    def dropped() -> Any:
        attempts.append(len(attempts) + 1)
        raise OperationalError("SELECT 1", {}, Exception("connection dropped"))

    monkeypatch.setattr(entitlements_tasks, "get_entitlements", dropped)
    with pytest.raises(WorkflowFailureError) as excinfo:
        await _execute(temporal_worker, temporal_client)
    activity_error = excinfo.value.cause
    assert isinstance(activity_error, ActivityError)
    cause = activity_error.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "OperationalError"
    assert cause.non_retryable is False
    assert attempts == [1]


async def test_the_catalog_entry_is_created_now_that_the_workflow_is_served(
    temporal_client: Client,
) -> None:
    assert WorkflowType.ENTITLEMENTS_WATCHDOG.value in queues.served_workflow_types()
    [entry] = [e for e in SCHEDULES if e.workflow is WorkflowType.ENTITLEMENTS_WATCHDOG]
    assert entry.id == "entitlements-watchdog"
    owner = f"test-{uuid4().hex[:10]}"
    try:
        report = await sync_schedules(temporal_client, [entry], managed_by=owner)
        assert report == ScheduleSyncReport(created=(entry.id,))
        described = await temporal_client.get_schedule_handle(entry.id).describe()
        assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
    finally:
        await temporal_client.get_schedule_handle(entry.id).delete()
