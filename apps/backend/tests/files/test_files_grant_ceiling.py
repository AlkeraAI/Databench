"""A share is granted up to the sharer's own rung, and withdrawn up to it too.

``SHARE`` is on the ``manager`` rung. Left at that alone, a person holding
"Full access" could hand out — to a colleague, to the whole org, or to
themselves — the ``owner`` rung they do not hold, and with it the purge and
the hold; and could withdraw the owner's own direct grant. The ceiling is the
caller's effective rung on the node: the ladder decides it, the policy refuses
it visibly (a manager can already read the node, so there is nothing to hide),
and the row says why.

The org-admin floor is the case that matters most: every org admin is floored
at ``manager`` on every node of the drive, and without a ceiling that floor is a
one-request ladder to ``owner`` on any member's file.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, node_etag
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_MANAGER, ROLE_OWNER, ROLE_READER, ROLE_WRITER
from alkera_core.files.ids import NodeId
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
EXCEEDS = "files.grant_exceeds_rung"


def _url(node: FileNode) -> str:
    return f"{BASE}/drives/{node.drive_id}/items/{node.id}/permissions"


def _fixture_ctx(files_org: FilesOrgFixture) -> ActingContext:
    return ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )


async def _mutate(
    session: AsyncSession, node: FileNode, idem: Callable[[], dict[str, str]]
) -> dict[str, str]:
    return {**idem(), "If-Match": await node_etag(session, node.id)}


async def _grant(
    fx: FilesFixtures, files_org: FilesOrgFixture, node: FileNode, user_id: uuid.UUID, role: str
) -> uuid.UUID:
    async with fx.repo.transaction():
        share = await acl.grant(
            fx.repo, _fixture_ctx(files_org), node, Principal(kind="user", id=user_id), role
        )
    await fx.repo.session.commit()
    return uuid.UUID(str(share.id))


async def _live_grants(fx: FilesFixtures, node: FileNode) -> set[tuple[str, uuid.UUID, str]]:
    async with fx.repo.transaction():
        rows = await fx.repo.shares_of(NodeId(node.id))
    return {
        (row.principal_kind, row.principal_id, row.role) for row in rows if row.revoked_at is None
    }


async def _last_decision(org_id: uuid.UUID, node_id: uuid.UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "file_node",
                EventOutbox.entity_id == str(node_id),
            )
            .order_by(EventOutbox.id)
        )
        return dict(rows.scalars().all()[-1].payload)


async def _manager(
    fx: FilesFixtures, files_org: FilesOrgFixture, node: FileNode, client: AsyncClient
) -> AsyncClient:
    """The member, holding "Full access" on ``node`` and signed in."""
    await _grant(fx, files_org, node, files_org.member.id, ROLE_MANAGER)
    return await login(client, files_org.member.email, files_org.member_password)


@pytest.mark.parametrize("principal", ["self", "colleague", "org"])
async def test_full_access_may_not_grant_a_rung_above_its_own(
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    principal: str,
) -> None:
    node = await fx.node(b"budget.xlsx")
    manager = await _manager(fx, files_org, node, client)
    before = await _live_grants(fx, node)
    if principal == "self":
        target = {"kind": "user", "id": str(files_org.member.id)}
    elif principal == "colleague":
        colleague, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
        target = {"kind": "user", "id": str(colleague.id)}
    else:
        target = {"kind": "org", "id": str(files_org.org.org_id)}

    refused = await manager.post(
        _url(node),
        json={"principal": target, "role": ROLE_OWNER},
        headers=await _mutate(real_session, node, idem),
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == EXCEEDS
    assert await _live_grants(fx, node) == before, "the refused grant landed anyway"
    decision = await _last_decision(files_org.org.org_id, node.id)
    assert (decision["action"], decision["effect"], decision["reason"]) == (
        "share",
        "deny",
        "grant_exceeds_rung",
    )


@pytest.mark.parametrize("role", [ROLE_MANAGER, ROLE_WRITER, ROLE_READER])
async def test_full_access_grants_up_to_its_own_rung(
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    role: str,
) -> None:
    """The ceiling is inclusive: a manager hands out ``manager`` and anything
    below it, so sharing "as much as I have" still works."""
    node = await fx.node(b"budget.xlsx")
    manager = await _manager(fx, files_org, node, client)
    colleague, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)

    made = await manager.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(colleague.id)}, "role": role},
        headers=await _mutate(real_session, node, idem),
    )

    assert made.status_code == 201, made.text
    assert ("user", colleague.id, role) in await _live_grants(fx, node)


async def test_full_access_may_not_withdraw_a_grant_above_its_own(
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Revoking rides ``SHARE`` too, so the same ceiling applies: a manager
    cannot take the owner's direct grant away, and can take a reader's."""
    node = await fx.node(b"budget.xlsx")
    colleague, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    reader, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    owner_share = await _grant(fx, files_org, node, colleague.id, ROLE_OWNER)
    reader_share = await _grant(fx, files_org, node, reader.id, ROLE_READER)
    manager = await _manager(fx, files_org, node, client)

    refused = await manager.delete(
        f"{_url(node)}/{owner_share}", headers=await _mutate(real_session, node, idem)
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == EXCEEDS
    assert ("user", colleague.id, ROLE_OWNER) in await _live_grants(fx, node)
    decision = await _last_decision(files_org.org.org_id, node.id)
    assert (decision["action"], decision["effect"], decision["reason"]) == (
        "share",
        "deny",
        "grant_exceeds_rung",
    )

    withdrawn = await manager.delete(
        f"{_url(node)}/{reader_share}", headers=await _mutate(real_session, node, idem)
    )
    assert withdrawn.status_code == 204, withdrawn.text
    assert ("user", reader.id, ROLE_READER) not in await _live_grants(fx, node)


async def test_the_org_admin_floor_is_not_a_ladder_to_owner(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """An org admin holds ``manager`` on a member's folder by the floor alone.
    Granting themselves ``owner`` there is the same refusal a manager gets."""
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    drives = await member.get(f"{BASE}/drives")
    assert drives.status_code == 200, drives.text
    drive_id, home_id = str(drives.json()["id"]), str(drives.json()["homeId"])
    made = await member.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": "expenses", "kind": "folder"},
        headers={"Idempotency-Key": secrets.token_hex(8)},
    )
    assert made.status_code == 201, made.text
    await member.aclose()
    node = await fx.folder(uuid.UUID(made.json()["id"]))

    refused = await files_client.post(
        _url(node),
        json={"principal": {"kind": "user", "id": str(files_org.org.admin_id)}, "role": ROLE_OWNER},
        headers=await _mutate(real_session, node, idem),
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == EXCEEDS
    assert await _live_grants(fx, node) == set()
