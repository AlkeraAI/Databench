"""Every copy door decides ``COPY`` on its source.

The policy has four refusals that exist only for a copy — a trashed source, a
sealed one, one under a folder marked "this goes no further", and one the
caller reads only through a grant that runs out — and ``…/duplicate`` decided
``COPY`` while the tracked ``…/copy`` and the batch route decided ``READ``. A
reader whose grant expires tomorrow could therefore ``/copy`` the folder into
their home and own a permanent copy the grant could no longer reach, and a
sealed or trashed source copied out through the doors that never asked.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, node_etag
from alkera_core.authz.policies.files import COPY_REFUSED
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import EventOutbox
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"]), str(response.json()["homeId"])


async def _folder(client: AsyncClient, drive: str, parent: str, name: str) -> str:
    made = await client.post(
        f"{BASE}/drives/{drive}/items/{parent}/children",
        json={"name": name, "kind": "folder"},
        headers=_idem(),
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


async def _decisions(org_id: uuid.UUID, node_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "file_node",
                EventOutbox.entity_id == node_id,
            )
            .order_by(EventOutbox.id)
        )
        return [dict(row.payload) for row in rows.scalars().all()]


async def _trashed_folder(
    files_client: AsyncClient, real_session: AsyncSession
) -> tuple[str, str, str]:
    """The admin's ``drafts``, trashed, and ``keep`` to copy it into."""
    drive, home = await _drive_and_home(files_client)
    drafts = await _folder(files_client, drive, home, "drafts")
    keep = await _folder(files_client, drive, home, "keep")
    binned = await files_client.delete(
        f"{BASE}/drives/{drive}/items/{drafts}",
        headers={**_idem(), "If-Match": await node_etag(real_session, uuid.UUID(drafts))},
    )
    assert binned.status_code == 200, binned.text
    return drive, drafts, keep


async def test_a_tracked_copy_of_a_trashed_source_is_refused(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    drive, drafts, keep = await _trashed_folder(files_client, real_session)

    copied = await files_client.post(
        f"{BASE}/drives/{drive}/items/{drafts}/copy", json={"parentId": keep}, headers=_idem()
    )

    assert copied.status_code == 403, copied.text
    assert copied.json()["code"] == "files.forbidden"
    last = (await _decisions(files_org.org.org_id, drafts))[-1]
    assert (last["action"], last["effect"], last["reason"]) == ("copy", "deny", "trashed")


async def test_a_batch_copy_of_a_trashed_source_is_refused(
    files_client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    drive, drafts, keep = await _trashed_folder(files_client, real_session)

    response = await files_client.post(
        f"{BASE}/drives/{drive}/bulk",
        json={"items": [{"id": "c", "op": "copy", "itemId": drafts, "parentId": keep}]},
        headers=_idem(),
    )

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["responses"]}
    assert rows["c"]["status"] == 403, rows
    last = (await _decisions(files_org.org.org_id, drafts))[-1]
    assert (last["action"], last["effect"], last["reason"]) == ("copy", "deny", "trashed")


async def test_a_read_on_loan_cannot_be_copied_out_through_any_door(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The member reads ``memo`` only through a grant that expires: the
    duplicate route already refused the copy with the copy code, and the
    tracked copy and the batch now answer the same."""
    drive, admin_home = await _drive_and_home(files_client)
    memo = await _folder(files_client, drive, admin_home, "memo")
    node = await fx.folder(uuid.UUID(memo))
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ctx,
            node,
            Principal(kind="user", id=files_org.member.id),
            ROLE_READER,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    await fx.repo.session.commit()
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    _, member_home = await _drive_and_home(member)

    readable = await member.get(f"{BASE}/drives/{drive}/items/{memo}")
    assert readable.status_code == 200, readable.text

    tracked = await member.post(
        f"{BASE}/drives/{drive}/items/{memo}/copy", json={"parentId": member_home}, headers=_idem()
    )
    assert tracked.status_code == 403, tracked.text
    assert tracked.json()["code"] == COPY_REFUSED

    batch = await member.post(
        f"{BASE}/drives/{drive}/bulk",
        json={"items": [{"id": "c", "op": "copy", "itemId": memo, "parentId": member_home}]},
        headers=_idem(),
    )
    assert batch.status_code == 200, batch.text
    rows = {row["id"]: row for row in batch.json()["responses"]}
    assert rows["c"]["status"] == 403, rows

    duplicated = await member.post(
        f"{BASE}/drives/{drive}/items/{memo}/duplicate", json={}, headers=_idem()
    )
    assert duplicated.status_code == 403, duplicated.text
    assert duplicated.json()["code"] == COPY_REFUSED

    reasons = {
        row["reason"]
        for row in await _decisions(files_org.org.org_id, memo)
        if row["effect"] == "deny"
    }
    assert reasons == {"conditional_grant"}
    await member.aclose()
