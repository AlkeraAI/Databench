"""The account lifecycle cores the worker runs, driven directly.

A person with an org of their own asks for their account's deletion; the sweep
erases the account once its grace window has ended, across the boundary on a
frozen clock.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.account import archive_store
from alkera_core.account import requests as account_requests
from alkera_core.account.dispositions import tombstone_email
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models import Team, TeamMembership, TeamRole, User
from freezegun import freeze_time
from worker.tasks import account as account_tasks

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path: Path) -> Iterator[FilesystemStore]:
    built = FilesystemStore(tmp_path / "archives", clock=lambda: datetime.now(UTC))
    archive_store.set_archive_store(built)
    yield built
    archive_store.set_archive_store(None)


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    async def capture(message: Any, *, log_event: str, to: str, **_: object) -> bool:
        sent.append(log_event)
        return True

    monkeypatch.setattr("alkera_core.email.account._send", capture)
    return sent


async def _person() -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"acct-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"acct-{secrets.token_hex(6)}@alkera.dev",
            first_name="Ada",
            last_name="Worker",
        )
        s.add(user)
        await s.flush()
        s.add(TeamMembership(user_id=user.id, team_id=team.id, role=TeamRole.ADMIN))
        await s.commit()
        return user.id


async def test_the_sweep_core_erases_only_after_the_grace_window(
    store: FilesystemStore, mail: list[str]
) -> None:
    user_id = await _person()
    start = datetime.now(UTC)
    async with AsyncSessionLocal() as s:
        request = await account_requests.schedule_deletion(
            s, user_id, requested_by=user_id, now=start
        )
        await s.commit()
        purge_after = request.purge_after
    assert purge_after == start + timedelta(days=14)

    with freeze_time(purge_after - timedelta(minutes=1), real_asyncio=True):
        early = await account_tasks._lifecycle_sweep()
    assert not any(k.startswith("deletions_") for k in early)
    with freeze_time(purge_after + timedelta(minutes=1), real_asyncio=True):
        late = await account_tasks._lifecycle_sweep()
    assert late.get("deletions_completed", 0) >= 1
    async with AsyncSessionLocal() as s:
        user = await s.get(User, user_id)
        assert user is not None and user.email == tombstone_email(user_id)
        assert user.deleted_at is not None
    assert "email.account_deletion_completed.sent" in mail
