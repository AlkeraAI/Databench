"""The identity security log's retention prune, against the real local Postgres.

Drives the async core and the activity directly; the workflow around them is
covered in ``test_identity_security_workflow.py``. Every assertion is by row
identity, never by the count: the prune deletes by predicate across the whole
table, and other tests on this worker's database write to it too.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import IdentitySecurityEvent, Team, User
from alkera_core.models.identity_security_event import PLATFORM_DECISION_EVENTS
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from freezegun import freeze_time
from sqlalchemy import select
from worker.activities.identity_security import prune_identity_security_events
from worker.schedules import SCHEDULES
from worker.tasks.identity_security import _prune_identity_security_events, retention_cutoff
from worker.temporal.queues import activity_type_name, served_workflow_types
from worker.temporal.retry import TRANSIENT_RETRY, policy_for

NOW = datetime(2027, 3, 4, 5, 6, 7, tzinfo=UTC)


async def _person() -> UUID:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"isec-org-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"isec-{secrets.token_hex(6)}@alkera.dev",
            first_name="Security",
            last_name="Log",
        )
        s.add(user)
        await s.commit()
        return user.id


async def _event(user_id: UUID, *, at: datetime, event: str = "auth.login_failed") -> UUID:
    async with AsyncSessionLocal() as s:
        row = IdentitySecurityEvent(id=uuid4(), user_id=user_id, event=event, created_at=at)
        s.add(row)
        await s.commit()
        return row.id


async def _alive(ids: list[UUID]) -> set[UUID]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(IdentitySecurityEvent.id).where(IdentitySecurityEvent.id.in_(ids))
        )
        return set(rows.scalars())


def _window() -> timedelta:
    return timedelta(days=settings.identity_security_event_retention_days)


async def test_rows_past_the_window_go_and_rows_inside_it_stay() -> None:
    user = await _person()
    cutoff = retention_cutoff(NOW)
    expired = await _event(user, at=cutoff - timedelta(seconds=1))
    boundary = await _event(user, at=cutoff)
    recent = await _event(user, at=NOW - timedelta(days=1))
    await _prune_identity_security_events(NOW)
    assert await _alive([expired, boundary, recent]) == {boundary, recent}


@pytest.mark.parametrize("event", sorted(PLATFORM_DECISION_EVENTS))
async def test_the_platforms_decisions_never_age_out(event: str) -> None:
    """Org reactivation reads the newest platform disable or enable as the
    platform's word; pruning it would read as "never disabled"."""
    user = await _person()
    decision = await _event(user, at=NOW - _window() - timedelta(days=400), event=event)
    other = await _event(user, at=NOW - _window() - timedelta(days=400))
    await _prune_identity_security_events(NOW)
    assert await _alive([decision, other]) == {decision}


async def test_a_backlog_larger_than_one_batch_is_drained_in_one_run() -> None:
    user = await _person()
    old = [await _event(user, at=NOW - _window() - timedelta(days=1, seconds=i)) for i in range(7)]
    keep = await _event(user, at=NOW)
    removed = await _prune_identity_security_events(NOW, batch=3)
    assert removed >= 7
    assert await _alive([*old, keep]) == {keep}


async def test_a_second_run_deletes_nothing_more() -> None:
    user = await _person()
    keep = await _event(user, at=NOW - timedelta(hours=1))
    await _event(user, at=NOW - _window() - timedelta(days=2))
    await _prune_identity_security_events(NOW)
    await _prune_identity_security_events(NOW)
    assert await _alive([keep]) == {keep}


async def test_a_batch_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="batch must be positive"):
        await _prune_identity_security_events(NOW, batch=0)


async def test_the_window_is_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    user = await _person()
    ten_days = await _event(user, at=NOW - timedelta(days=10))
    monkeypatch.setattr(settings, "identity_security_event_retention_days", 30)
    await _prune_identity_security_events(NOW)
    assert await _alive([ten_days]) == {ten_days}
    monkeypatch.setattr(settings, "identity_security_event_retention_days", 7)
    await _prune_identity_security_events(NOW)
    assert await _alive([ten_days]) == set()


async def test_the_activity_reads_the_wall_clock_across_the_window_edge() -> None:
    """Started with no input, the activity judges by the wall clock: a row is
    kept the second before it leaves the window and gone the second after."""
    written_at = datetime(2027, 1, 1, 12, 0, 0, tzinfo=UTC)
    user = await _person()
    row = await _event(user, at=written_at)
    with freeze_time(written_at + _window() - timedelta(seconds=1), real_asyncio=True) as clock:
        await prune_identity_security_events()
        assert await _alive([row]) == {row}
        clock.move_to(written_at + _window() + timedelta(seconds=1))
        await prune_identity_security_events()
        assert await _alive([row]) == set()


async def test_a_pinned_clock_wins_over_the_wall_clock() -> None:
    user = await _person()
    row = await _event(user, at=NOW - _window() - timedelta(minutes=1))
    await prune_identity_security_events(SweepInput(now=NOW - timedelta(hours=1)))
    assert await _alive([row]) == {row}
    await prune_identity_security_events(SweepInput(now=NOW))
    assert await _alive([row]) == set()


def test_it_runs_daily_on_the_default_queue_with_a_retrying_policy() -> None:
    entries = [e for e in SCHEDULES if e.workflow is WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS]
    assert [e.id for e in entries] == ["prune-identity-security-events"]
    assert entries[0].cron == "40 3 * * *"
    assert QUEUE_FOR[WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS] is TaskQueue.DEFAULT
    assert (
        activity_type_name(prune_identity_security_events)
        == WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value
    )
    assert WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value in served_workflow_types()
    policy = policy_for(WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value)
    assert policy.retry == TRANSIENT_RETRY
    assert policy.heartbeat_timeout == timedelta(minutes=2)
