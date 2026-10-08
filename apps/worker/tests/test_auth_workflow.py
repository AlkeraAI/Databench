"""The auth prunes as Temporal workflows: the real workflow and the real
activity through a real Worker, against the test database.

Each prune must remove exactly the expired rows and spare the live ones; the
workflow and activity types are the historical task names on the default
queue; a transient failure is attempted exactly once (the policy table says no
retry — this proves the workflow attaches it); and the catalog entries are
created by the schedule reconciler instead of being reported pending.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.auth import hash_lookup_token
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    AuthToken,
    DeviceAuthorization,
    DeviceAuthStatus,
    LoginLockout,
    Team,
    TokenType,
    User,
)
from alkera_core.temporal import TaskQueue, WorkflowType
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from worker.activities import auth as auth_activities
from worker.schedules import SCHEDULES, ScheduleSyncReport, fingerprint, sync_schedules
from worker.tasks import auth as auth_tasks
from worker.temporal import queues
from worker.workflows.auth import (
    PruneExpiredDeviceCodes,
    PruneExpiredTokens,
    PruneLoginLockouts,
)

pytestmark = pytest.mark.temporal

FAMILY = [
    pytest.param(
        PruneExpiredTokens,
        auth_activities.prune_expired_tokens,
        WorkflowType.PRUNE_EXPIRED_TOKENS,
        id="tokens",
    ),
    pytest.param(
        PruneExpiredDeviceCodes,
        auth_activities.prune_expired_device_codes,
        WorkflowType.PRUNE_EXPIRED_DEVICE_CODES,
        id="device-codes",
    ),
    pytest.param(
        PruneLoginLockouts,
        auth_activities.prune_login_lockouts,
        WorkflowType.PRUNE_LOGIN_LOCKOUTS,
        id="login-lockouts",
    ),
]
FAMILY_TYPES = {
    WorkflowType.PRUNE_EXPIRED_TOKENS,
    WorkflowType.PRUNE_EXPIRED_DEVICE_CODES,
    WorkflowType.PRUNE_LOGIN_LOCKOUTS,
}


# --- seeds -------------------------------------------------------------------


async def _seed_tokens() -> tuple[str, str]:
    """A user with one expired and one live token. Returns their jtis."""
    async with AsyncSessionLocal() as s:
        team = Team(name=f"prune-wf-org-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"prune-wf-{secrets.token_hex(6)}@alkera.dev",
            first_name="Prune",
            last_name="Workflow",
        )
        s.add(user)
        await s.flush()
        now = datetime.now(UTC)
        expired_jti, live_jti = uuid4().hex, uuid4().hex
        s.add(
            AuthToken(
                jti=expired_jti,
                user_id=user.id,
                token_type=TokenType.CLI,
                issued_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )
        s.add(
            AuthToken(
                jti=live_jti,
                user_id=user.id,
                token_type=TokenType.CLI,
                issued_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        await s.commit()
        return expired_jti, live_jti


async def _jti_exists(jti: str) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(select(AuthToken.jti).where(AuthToken.jti == jti))
        return row.scalar_one_or_none() is not None


async def _seed_device_codes() -> tuple[str, str]:
    """One expired and one live device authorization. Returns their hashes."""
    async with AsyncSessionLocal() as s:
        now = datetime.now(UTC)
        expired_hash = hash_lookup_token(f"expired-wf-{secrets.token_hex(8)}")
        live_hash = hash_lookup_token(f"live-wf-{secrets.token_hex(8)}")
        for code_hash, prefix, expires_at in (
            (expired_hash, "EW", now - timedelta(minutes=1)),
            (live_hash, "LW", now + timedelta(minutes=10)),
        ):
            s.add(
                DeviceAuthorization(
                    device_code_hash=code_hash,
                    user_code=f"{prefix}{secrets.token_hex(2).upper()}-CODE",
                    client_id="alkera-cli",
                    interval_seconds=1,
                    status=DeviceAuthStatus.PENDING,
                    expires_at=expires_at,
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


async def _seed_lockouts() -> tuple[str, str]:
    """One spent counter and one still inside its cool-off. Returns their digests.

    Stamped relative to the window the prune will read, so the row that stays is
    kept by its LOCK rather than by its age — the distinction the retention
    predicate exists to make.
    """
    now = datetime.now(UTC)
    window = timedelta(seconds=settings.auth_lockout_window_seconds)
    spent, still_locked = secrets.token_hex(32), secrets.token_hex(32)
    async with AsyncSessionLocal() as s:
        s.add(
            LoginLockout(
                identifier_digest=spent,
                failed_count=3,
                last_failed_at=now - window - timedelta(minutes=5),
                locked_until=now - timedelta(minutes=1),
            )
        )
        s.add(
            LoginLockout(
                identifier_digest=still_locked,
                failed_count=3,
                last_failed_at=now - window - timedelta(minutes=5),
                locked_until=now + timedelta(hours=1),
            )
        )
        await s.commit()
    return spent, still_locked


async def _digest_exists(digest: str) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(
            select(LoginLockout.identifier_digest).where(LoginLockout.identifier_digest == digest)
        )
        return row.scalar_one_or_none() is not None


# --- driving the real workflow ---------------------------------------------------


async def _execute(
    temporal_worker: Any, temporal_client: Client, workflow_cls: type, activity_fn: Any
) -> Any:
    async with temporal_worker(workflows=[workflow_cls], activities=[activity_fn]) as running:
        return await temporal_client.execute_workflow(
            workflow_cls.run,
            id=f"{queues.workflow_type_name(workflow_cls)}-{uuid4().hex[:8]}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )


async def test_the_token_prune_removes_expired_and_keeps_live(
    temporal_worker: Any, temporal_client: Client
) -> None:
    expired_jti, live_jti = await _seed_tokens()
    removed = await _execute(
        temporal_worker, temporal_client, PruneExpiredTokens, auth_activities.prune_expired_tokens
    )
    # The COUNT is the whole database's, not this test's: every prune here deletes
    # by predicate across the table, and `test_auth_prune.py` and
    # `test_auth_revocation.py` prune the same one on the same per-worker database.
    # So the number says nothing this test means and couples it to what else has
    # run; what it means is the two rows below. Only the shape is pinned here,
    # because that is what travels back through the workflow.
    assert isinstance(removed, int)
    assert await _jti_exists(expired_jti) is False
    assert await _jti_exists(live_jti) is True


async def test_the_device_code_prune_removes_expired_and_keeps_live(
    temporal_worker: Any, temporal_client: Client
) -> None:
    expired_hash, live_hash = await _seed_device_codes()
    removed = await _execute(
        temporal_worker,
        temporal_client,
        PruneExpiredDeviceCodes,
        auth_activities.prune_expired_device_codes,
    )
    # The COUNT is the whole database's, not this test's: every prune here deletes
    # by predicate across the table, and `test_auth_prune.py` and
    # `test_auth_revocation.py` prune the same one on the same per-worker database.
    # So the number says nothing this test means and couples it to what else has
    # run; what it means is the two rows below. Only the shape is pinned here,
    # because that is what travels back through the workflow.
    assert isinstance(removed, int)
    assert await _hash_exists(expired_hash) is False
    assert await _hash_exists(live_hash) is True


async def test_the_lockout_prune_removes_spent_counters_and_keeps_the_locked(
    temporal_worker: Any, temporal_client: Client
) -> None:
    """Through the real workflow and the real activity: a counter past both its
    window and its cool-off goes, one still inside its cool-off stays.

    The row that stays is the whole point — the `users` half of this pair is
    never pruned, so releasing this one early is the account-existence oracle
    the table was added to close.
    """
    spent, still_locked = await _seed_lockouts()
    removed = await _execute(
        temporal_worker,
        temporal_client,
        PruneLoginLockouts,
        auth_activities.prune_login_lockouts,
    )
    # The COUNT is the whole database's, not this test's: every prune here deletes
    # by predicate across the table, and `test_auth_prune.py` and
    # `test_auth_revocation.py` prune the same one on the same per-worker database.
    # So the number says nothing this test means and couples it to what else has
    # run; what it means is the two rows below. Only the shape is pinned here,
    # because that is what travels back through the workflow.
    assert isinstance(removed, int)
    assert await _digest_exists(spent) is False
    assert await _digest_exists(still_locked) is True


@pytest.mark.parametrize(("workflow_cls", "activity_fn", "workflow_type"), FAMILY)
def test_the_types_are_the_historical_task_names_on_the_default_queue(
    workflow_cls: type, activity_fn: Any, workflow_type: WorkflowType
) -> None:
    assert queues.workflow_type_name(workflow_cls) == workflow_type.value
    assert queues.activity_type_name(activity_fn) == workflow_type.value
    for queue in TaskQueue:
        expect = queue is TaskQueue.DEFAULT
        assert (workflow_cls in queues.WORKFLOWS_BY_QUEUE[queue]) is expect, queue
        assert (activity_fn in queues.ACTIVITIES_BY_QUEUE[queue]) is expect, queue


@pytest.mark.parametrize(("workflow_cls", "activity_fn", "workflow_type"), FAMILY)
async def test_a_transient_failure_is_attempted_exactly_once(
    temporal_worker: Any,
    temporal_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    workflow_cls: type,
    activity_fn: Any,
    workflow_type: WorkflowType,
) -> None:
    """A dropped connection is the retryable class — and the prune still runs
    once, because the policy the workflow attaches allows a single attempt.
    Tomorrow's schedule is the recovery, not a retry storm on a hurt database."""
    opened: list[str] = []

    def dropped_session() -> Any:
        opened.append(workflow_type.value)
        raise OperationalError("SELECT 1", {}, Exception("connection dropped"))

    monkeypatch.setattr(auth_tasks, "AsyncSessionLocal", dropped_session)
    with pytest.raises(WorkflowFailureError) as excinfo:
        await _execute(temporal_worker, temporal_client, workflow_cls, activity_fn)
    activity_error = excinfo.value.cause
    assert isinstance(activity_error, ActivityError)
    cause = activity_error.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "OperationalError"
    # Left retryable by the interceptor — only the policy stopped a second attempt.
    assert cause.non_retryable is False
    assert opened == [workflow_type.value]


async def test_the_catalog_entries_are_created_now_that_the_workflows_are_served(
    temporal_client: Client,
) -> None:
    """Before this family landed the reconciler reported both prunes pending; a
    default-queue worker booting now creates them under its ownership memo."""
    assert {t.value for t in FAMILY_TYPES} <= queues.served_workflow_types()
    entries = [e for e in SCHEDULES if e.workflow in FAMILY_TYPES]
    assert sorted(e.id for e in entries) == [
        "prune-expired-device-codes",
        "prune-expired-tokens",
        "prune-login-lockouts",
    ]
    owner = f"test-{uuid4().hex[:10]}"
    try:
        report = await sync_schedules(temporal_client, entries, managed_by=owner)
        assert report == ScheduleSyncReport(created=tuple(e.id for e in entries))
        for entry in entries:
            described = await temporal_client.get_schedule_handle(entry.id).describe()
            assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
    finally:
        for entry in entries:
            await temporal_client.get_schedule_handle(entry.id).delete()
