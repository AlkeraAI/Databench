"""The chat-spare sweep: the async core against the real local Postgres, the
schedule pin that ties it to its minute cadence, and the activity registry.

A spare is a chat row flagged in its spec; the backend warms and claims it
through its routes (covered in ``apps/backend/tests/test_chat_spare.py``). What
the sweep owns is the reaping: a spare whose owner's page has stopped beating
past the idle cutoff goes, one that beat recently stays, and one older than the
maximum age goes whatever its heartbeat says.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, User, WorkspaceObject
from alkera_core.objects import chat_spares
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from sqlalchemy import select
from worker.activities.chat_spares import reap_chat_spares
from worker.schedules import SCHEDULES
from worker.tasks.chat_spares import run_reap_chat_spares
from worker.temporal.queues import activity_type_name, served_workflow_types
from worker.temporal.retry import policy_for


async def _spare(*, active_at: datetime | None, created_at: datetime) -> UUID:
    """A spare row written the way the backend writes one, with its clocks
    pinned so the sweep's judgement is the thing under test."""
    async with AsyncSessionLocal() as s:
        team = Team(name=f"spare-org-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"spare-{secrets.token_hex(6)}@alkera.dev",
            first_name="Spare",
            last_name="Owner",
        )
        s.add(user)
        await s.flush()
        row = WorkspaceObject(
            id=uuid4(),
            org_team_id=team.id,
            logical_id=uuid4().hex,
            type="chat",
            title="",
            spec={
                "spare": True,
                "spare_active_at": active_at.isoformat() if active_at else None,
                "machine_id": None,
                "machine_status": "none",
            },
            owner_user_id=user.id,
            visibility_scope="private",
            created_at=created_at,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _exists(chat_id: UUID) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(select(WorkspaceObject.id).where(WorkspaceObject.id == chat_id))
        return row.scalar_one_or_none() is not None


async def test_the_sweep_reaps_the_idle_and_the_old_and_keeps_the_beating() -> None:
    now = datetime.now(UTC)
    idle = await _spare(
        active_at=now - chat_spares.IDLE_CUTOFF - timedelta(seconds=1), created_at=now
    )
    beating = await _spare(active_at=now - timedelta(seconds=30), created_at=now)
    never_beat = await _spare(active_at=None, created_at=now - chat_spares.IDLE_CUTOFF)
    old = await _spare(active_at=now, created_at=now - chat_spares.MAX_AGE - timedelta(seconds=1))

    reaped = await run_reap_chat_spares(now)

    assert reaped >= 3
    assert not await _exists(idle), "no heartbeat past the cutoff"
    assert not await _exists(never_beat), "never stamped: judged from its creation"
    assert not await _exists(old), "past the maximum age however recently it beat"
    assert await _exists(beating), "its owner is still on the page"


async def test_the_sweep_never_touches_a_claimed_chat() -> None:
    """The row-level delete is guarded on the flag: a chat that was a spare
    and is now claimed — even one whose clocks would read stale — stays."""
    now = datetime.now(UTC)
    chat_id = await _spare(
        active_at=now - chat_spares.MAX_AGE, created_at=now - chat_spares.MAX_AGE
    )
    async with AsyncSessionLocal() as s:
        row = await s.get(WorkspaceObject, chat_id)
        assert row is not None
        await chat_spares.mark_claimed(s, row)
        await s.commit()
    await run_reap_chat_spares(now)
    assert await _exists(chat_id)


def test_the_sweep_is_a_minute_schedule_on_the_default_queue_with_no_retry() -> None:
    entry = next(e for e in SCHEDULES if e.workflow is WorkflowType.REAP_CHAT_SPARES)
    assert entry.every == timedelta(minutes=1)
    assert QUEUE_FOR[WorkflowType.REAP_CHAT_SPARES] is TaskQueue.DEFAULT
    assert policy_for(WorkflowType.REAP_CHAT_SPARES.value).retry.maximum_attempts == 1
    assert WorkflowType.REAP_CHAT_SPARES.value in served_workflow_types()
    assert activity_type_name(reap_chat_spares) == WorkflowType.REAP_CHAT_SPARES.value
