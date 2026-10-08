"""The account lifecycle workflows through a real Worker on the dev server."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.account import archive_store
from alkera_core.account import requests as account_requests
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models import AccountDeletionRequest, Team, User
from temporalio.client import Client
from worker.activities.account import account_lifecycle_sweep
from worker.workflows.account import AccountLifecycleSweep

pytestmark = pytest.mark.temporal


async def _person() -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"acct-wf-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"acct-wf-{secrets.token_hex(6)}@alkera.dev",
            first_name="Flo",
            last_name="Wf",
        )
        s.add(user)
        await s.commit()
        return user.id


async def test_a_sweep_runs_through_temporal(
    temporal_worker: Any,
    temporal_client: Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_mail(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr("alkera_core.email.account._send", no_mail)
    archive_store.set_archive_store(
        FilesystemStore(tmp_path / "archives", clock=lambda: datetime.now(UTC))
    )
    try:
        user_id = await _person()
        async with AsyncSessionLocal() as s:
            request = await account_requests.schedule_deletion(
                s, user_id, requested_by=user_id, grace=timedelta(0)
            )
            await s.commit()
            request_id = request.id
        async with temporal_worker(
            workflows=[AccountLifecycleSweep], activities=[account_lifecycle_sweep]
        ) as running:
            report = await temporal_client.execute_workflow(
                AccountLifecycleSweep.run,
                id=f"account-sweep-{uuid.uuid4()}",
                task_queue=running.task_queue,
                execution_timeout=timedelta(seconds=60),
            )
        # The sweep takes every due request on the database, so another test's
        # leftover may complete beside this one; this person's is checked below.
        assert report["deletions_completed"] >= 1
        async with AsyncSessionLocal() as s:
            assert (await s.get(AccountDeletionRequest, request_id)).status == "completed"  # type: ignore[union-attr]
    finally:
        archive_store.set_archive_store(None)


async def test_the_reerase_workflow_erases_a_ledgered_identity_a_restore_brought_back(
    temporal_worker: Any,
    temporal_client: Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_core.account import ledger
    from worker.activities.account import account_reerase
    from worker.workflows.account import AccountReerase

    store = FilesystemStore(tmp_path / "ledger", clock=lambda: datetime.now(UTC))
    ledger.set_ledger_store(store)
    archive_store.set_archive_store(store)
    try:
        user_id = await _person()
        # The ledger says this identity was erased; the database (as restored)
        # still holds it.
        await ledger.record(
            store, user_id=user_id, request_id=uuid.uuid4(), erased_at=datetime.now(UTC)
        )
        async with temporal_worker(
            workflows=[AccountReerase], activities=[account_reerase]
        ) as running:
            report = await temporal_client.execute_workflow(
                AccountReerase.run,
                id=f"account-reerase-{uuid.uuid4()}",
                task_queue=running.task_queue,
                execution_timeout=timedelta(seconds=60),
            )
        assert report["resurrected"] >= 1 and report["failed"] == 0
        async with AsyncSessionLocal() as s:
            user = await s.get(User, user_id)
            assert user is not None and user.deleted_at is not None
    finally:
        ledger.set_ledger_store(None)
        archive_store.set_archive_store(None)
