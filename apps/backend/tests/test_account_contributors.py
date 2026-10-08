"""The account lifecycle reads its private parts from ``ACCOUNT_CONTRIBUTORS``.

With nothing registered (the open platform on its own) the plan carries no
domain's blockers and forfeits nothing, and an erasure completes with nothing
outside the database to erase and no stored object to delete. A contributor
registered on the point is asked for each of those, which is what makes the
empty case mean something: the lifecycle takes them from the point and from
nowhere else.

Each test swaps in a fresh point, so the product's billing contributor (which
the suite's app installs) is not on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alkera_core.account import archive_store
from alkera_core.account import contributors as contributors_module
from alkera_core.account.contributors import AccountContributor, ExternalErasure, OwnedContent
from alkera_core.account.plan import compute_plan
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.extensions import ExtensionPoint
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models import AccountDeletionRequest
from alkera_core.schemas.account import PlanBlocker
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401 - fixture
from tests.conftest import OrgWithAdmin
from tests.test_account_erasure import _run_due, _schedule, _seed

pytestmark = pytest.mark.asyncio


@pytest.fixture
def point(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[AccountContributor]:
    fresh: ExtensionPoint[AccountContributor] = ExtensionPoint("account.contributors")
    monkeypatch.setattr(contributors_module, "ACCOUNT_CONTRIBUTORS", fresh)
    return fresh


class _Recorder:
    """A domain that holds something of every kind the lifecycle asks about."""

    def __init__(self, org_id: uuid.UUID) -> None:
        self.org_id = org_id
        self.erased: list[ExternalErasure] = []

    async def blockers(self, db: AsyncSession, user_id: uuid.UUID) -> list[PlanBlocker]:
        return [PlanBlocker(code="legal_hold", org_id=self.org_id)]

    async def forfeited(self, db: AsyncSession, user_id: uuid.UUID) -> int:
        return 7_000

    async def external(self, job: ExternalErasure) -> Mapping[str, int]:
        self.erased.append(job)
        return {"recorder.erased": 1}

    def contributor(self) -> AccountContributor:
        return AccountContributor(
            name="recorder",
            blockers=self.blockers,
            forfeited_nanos=self.forfeited,
            external_erasure=self.external,
        )


async def _plan(user_id: uuid.UUID) -> tuple[list[tuple[str, uuid.UUID | None]], int]:
    async with AsyncSessionLocal() as session:
        plan = await compute_plan(session, user_id)
        await session.rollback()
    return [(b.code, b.org_id) for b in plan.blockers], plan.forfeited_credit_nanos


async def _certificate_rows(user_id: uuid.UUID) -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        request = await session.scalar(
            select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == user_id)
        )
    assert request is not None
    rows: dict[str, int] = (request.certificate or {})["rows"]
    return rows


# --- the plan -------------------------------------------------------------------------


async def test_with_nothing_registered_the_plan_has_no_domain_blocker_or_forfeit(
    point: ExtensionPoint[AccountContributor], org_admin: OrgWithAdmin
) -> None:
    assert await _plan(org_admin.admin_id) == ([], 0)


async def test_a_registered_contributor_s_blockers_and_forfeit_enter_the_plan(
    point: ExtensionPoint[AccountContributor], org_admin: OrgWithAdmin
) -> None:
    point.register(_Recorder(org_admin.org_id).contributor())
    assert await _plan(org_admin.admin_id) == ([("legal_hold", org_admin.org_id)], 7_000)


async def _item_counts(user_id: uuid.UUID) -> dict[uuid.UUID, tuple[int, int]]:
    async with AsyncSessionLocal() as session:
        plan = await compute_plan(session, user_id)
        await session.rollback()
    return {o.org_id: (o.shared_items, o.private_items) for o in plan.orgs}


async def test_a_contributor_s_owned_items_count_in_the_plan_only_when_registered(
    monkeypatch: pytest.MonkeyPatch, org_admin: OrgWithAdmin
) -> None:
    empty: ExtensionPoint[AccountContributor] = ExtensionPoint("account.contributors")
    monkeypatch.setattr(contributors_module, "ACCOUNT_CONTRIBUTORS", empty)
    alone = await _item_counts(org_admin.admin_id)

    async def owned(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> tuple[int, int]:
        return (2, 3) if org_id == org_admin.org_id else (0, 0)

    with_one: ExtensionPoint[AccountContributor] = ExtensionPoint("account.contributors")
    with_one.register(AccountContributor(name="recorder", owned_counts=owned))
    monkeypatch.setattr(contributors_module, "ACCOUNT_CONTRIBUTORS", with_one)
    shared, private = alone[org_admin.org_id]
    assert await _item_counts(org_admin.admin_id) == {
        **alone,
        org_admin.org_id: (shared + 2, private + 3),
    }


# --- the erasure ------------------------------------------------------------------------


async def test_with_nothing_registered_an_erasure_completes_with_nothing_external(
    point: ExtensionPoint[AccountContributor],
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
    files_on: None,  # noqa: F811
    multi_org: None,
) -> None:
    w = await _seed(org_admin, client)
    purge_after = await _schedule(client, w)
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "completed"
    rows = await _certificate_rows(w.u_id)
    assert "stripe_customers.deleted" not in rows
    assert "recorder.erased" not in rows


async def test_a_registered_contributor_erases_its_part_inside_the_erasure(
    point: ExtensionPoint[AccountContributor],
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
    files_on: None,  # noqa: F811
    multi_org: None,
) -> None:
    recorder = _Recorder(org_admin.org_id)
    # Registered without blockers: a blocker would hold the erasure.
    point.register(AccountContributor(name="recorder", external_erasure=recorder.external))
    w = await _seed(org_admin, client)
    purge_after = await _schedule(client, w)
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "completed"
    assert [job.user_id for job in recorder.erased] == [w.u_id]
    assert (await _certificate_rows(w.u_id))["recorder.erased"] == 1


async def _stored_object(store: FilesystemStore, tmp_path: Path) -> str:
    key = f"{archive_store.PREFIX}/{uuid.uuid4().hex}/held.zip"
    staged = tmp_path / "held.zip"
    staged.write_bytes(b"held for the person")
    await archive_store.upload(store, key, staged)
    return key


async def _archives_deleted(user_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        request = await session.scalar(
            select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == user_id)
        )
    assert request is not None
    deleted: int = (request.certificate or {})["archives_deleted"]
    return deleted


@pytest.mark.parametrize("registered", [False, True], ids=["nothing-registered", "registered"])
async def test_a_contributor_s_stored_objects_go_once_the_erasure_commits(
    registered: bool,
    point: ExtensionPoint[AccountContributor],
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
    files_on: None,  # noqa: F811
    multi_org: None,
    tmp_path: Path,
) -> None:
    key = await _stored_object(account_archives, tmp_path)
    named: list[uuid.UUID] = []

    async def held(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
        named.append(user_id)
        return [key]

    if registered:
        point.register(AccountContributor(name="holder", stored_objects=held))
    w = await _seed(org_admin, client)
    purge_after = await _schedule(client, w)
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "completed"
    if registered:
        assert named == [w.u_id]
        assert await account_archives.head(key) is None
        assert await _archives_deleted(w.u_id) == 1
    else:
        assert await account_archives.head(key) is not None
        assert await _archives_deleted(w.u_id) == 0


async def test_a_contributor_hands_over_and_erases_the_person_s_own_items_in_every_org(
    point: ExtensionPoint[AccountContributor],
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
    files_on: None,  # noqa: F811
    multi_org: None,
) -> None:
    """The org the person leaves hands their shared items to the admin; the
    org they were alone in closes with nobody to hand to. Once every org is
    handled the contributor erases whatever the person still owns anywhere."""
    owned: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    remaining: list[uuid.UUID] = []

    async def owned_content(job: OwnedContent) -> Mapping[str, int]:
        owned.append((job.org_id, job.recipient))
        return {"recorder.owned": 1}

    async def remaining_content(db: AsyncSession, user_id: uuid.UUID) -> None:
        remaining.append(user_id)

    point.register(
        AccountContributor(
            name="recorder", owned_content=owned_content, remaining_content=remaining_content
        )
    )
    w = await _seed(org_admin, client)
    purge_after = await _schedule(client, w)
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "completed"

    assert sorted(owned, key=lambda pair: str(pair[0])) == sorted(
        [(w.org_a, w.b_id), (w.org_s, None)], key=lambda pair: str(pair[0])
    )
    assert remaining == [w.u_id]
    assert (await _certificate_rows(w.u_id))["recorder.owned"] == 2
