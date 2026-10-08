"""The live catalog refresh as a scheduled job.

The reconcile core is proved in the api-core suite; what is proved here is that
the refresh — which had no caller anywhere before — is actually WIRED: the core
walks the configured providers and skips the ones this deployment cannot act
through, the type is on the default queue the contract names, it retries like
other housekeeping, and the schedule carries it at the hourly cadence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alkera_core.compute.availability import SizeQuery
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from worker.schedules import SCHEDULES
from worker.tasks import compute as compute_tasks
from worker.temporal.retry import TRANSIENT_RETRY, policy_for

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class _CatalogFake:
    """A provider that only answers the two questions the refresh asks. An
    unconfigured one must never be asked for its catalog."""

    def __init__(self, *, configured: bool, entries: dict[str, dict[str, object]] | None) -> None:
        self._configured = configured
        self._entries = entries or {}

    def configured(self) -> bool:
        return self._configured

    async def catalog_entries(
        self, sizes: list[SizeQuery] | None = None
    ) -> dict[str, dict[str, object]]:
        assert self._configured, "an unconfigured provider must not be asked for its catalog"
        return self._entries


async def test_the_core_walks_configured_providers_and_skips_the_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip is the point: an unconfigured provider is never asked (its fake
    would fail the assertion if it were), and the pass still returns cleanly."""
    configured = _CatalogFake(configured=True, entries={})  # asked; nothing to reconcile
    unconfigured = _CatalogFake(configured=False, entries=None)  # must be skipped, not asked
    monkeypatch.setattr(
        compute_tasks,
        "reconcile_providers",
        lambda: [("runpod", configured), ("ec2", unconfigured)],
    )

    result = await compute_tasks.run_refresh_catalog(NOW)

    assert result == {"updated": 0, "marked_unavailable": 0, "reappeared": 0, "added": 0}


def test_the_catalog_refresh_is_on_the_default_queue() -> None:
    # It touches prices but never re-rates a running allocation (the meter bills
    # pinned prices), so it is not a money tick.
    assert QUEUE_FOR[WorkflowType.COMPUTE_CATALOG] is TaskQueue.DEFAULT


def test_the_catalog_refresh_retries_transiently() -> None:
    """Idempotent under its lock and an hour between runs: a transient failure is
    worth another attempt, not a lost hour of stale prices."""
    assert policy_for(WorkflowType.COMPUTE_CATALOG.value).retry is TRANSIENT_RETRY


def test_the_schedule_carries_the_catalog_refresh_hourly() -> None:
    entry = next(e for e in SCHEDULES if e.workflow is WorkflowType.COMPUTE_CATALOG)
    assert entry.cron == "15 * * * *"
