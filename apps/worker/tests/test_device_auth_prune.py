"""The device-authorization prune against the real local Postgres.

Drives the async core directly, like ``test_auth_prune``; the Temporal
activity and workflow around it are covered in ``test_auth_workflow.py``. The
catalog pin here is the one line that ties this job to its daily schedule.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.auth import hash_lookup_token
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import DeviceAuthorization, DeviceAuthStatus
from alkera_core.temporal import TaskQueue, WorkflowType
from sqlalchemy import select
from worker.activities.auth import prune_expired_device_codes
from worker.schedules import SCHEDULES
from worker.tasks.auth import run_prune_expired_device_codes
from worker.temporal.queues import activity_type_name, served_workflow_types


async def _seed() -> tuple[str, str]:
    """Create one expired + one live device authorization. Returns their hashes."""
    async with AsyncSessionLocal() as s:
        now = datetime.now(UTC)
        expired_hash = hash_lookup_token(f"expired-{secrets.token_hex(8)}")
        live_hash = hash_lookup_token(f"live-{secrets.token_hex(8)}")
        s.add(
            DeviceAuthorization(
                device_code_hash=expired_hash,
                user_code=f"EX{secrets.token_hex(2).upper()}-CODE",
                client_id="alkera-cli",
                interval_seconds=1,
                status=DeviceAuthStatus.PENDING,
                expires_at=now - timedelta(minutes=1),
            )
        )
        s.add(
            DeviceAuthorization(
                device_code_hash=live_hash,
                user_code=f"LV{secrets.token_hex(2).upper()}-CODE",
                client_id="alkera-cli",
                interval_seconds=1,
                status=DeviceAuthStatus.PENDING,
                expires_at=now + timedelta(minutes=10),
            )
        )
        await s.commit()
        return expired_hash, live_hash


async def _hash_exists(device_code_hash: str) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(
            select(DeviceAuthorization.id).where(
                DeviceAuthorization.device_code_hash == device_code_hash
            )
        )
        return row.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_prune_removes_expired_keeps_live() -> None:
    expired_hash, live_hash = await _seed()

    removed = await run_prune_expired_device_codes()

    assert removed >= 1
    assert await _hash_exists(expired_hash) is False
    assert await _hash_exists(live_hash) is True


def test_the_device_code_prune_is_served_and_scheduled_daily() -> None:
    """The activity keeps the historical task name, some worker serves its
    workflow, and the catalog runs it at 03:15 UTC on the default queue."""
    assert activity_type_name(prune_expired_device_codes) == "auth.prune_expired_device_codes"
    assert WorkflowType.PRUNE_EXPIRED_DEVICE_CODES.value in served_workflow_types()
    entry = next(e for e in SCHEDULES if e.id == "prune-expired-device-codes")
    assert entry.workflow is WorkflowType.PRUNE_EXPIRED_DEVICE_CODES
    assert entry.spec_text == "15 3 * * *"
    assert entry.queue is TaskQueue.DEFAULT
