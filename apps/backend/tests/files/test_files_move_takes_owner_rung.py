"""A move may not hand the mover a rung on the node they did not hold.

A move re-derives the subtree's inherited grants from its new parent. Decided
as ``WRITE`` on both ends alone, that let any "Can edit" holder drag a
colleague's folder into their own home: the home's default made the mover
``owner`` of it (purge, hold, share), and everyone whose access came from the
old folder — the colleague who owned it included — lost it. So when the mover's
rung on the destination is above their rung on the source, the move is decided
again as ``DELETE`` on the source — the owner's verb — and refused on record;
a move between folders where the mover holds the same rung stays the ordinary
write it always was, and the batch route decides an item exactly as the single
one does.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, node_etag
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_MANAGER, ROLE_WRITER
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import select, text
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


async def _parent_of(node_id: str) -> str:
    async with AsyncSessionLocal() as session:
        return str(
            (
                await session.execute(
                    text("SELECT parent_id FROM file_nodes WHERE id = :id"), {"id": node_id}
                )
            ).scalar_one()
        )


async def _last_decision(org_id: uuid.UUID, node_id: str) -> dict[str, Any]:
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
        return dict(rows.scalars().all()[-1].payload)


async def _shared_with_member(
    files_client: AsyncClient, fx: FilesFixtures, files_org: FilesOrgFixture, role: str
) -> tuple[str, str, FileNode, AsyncClient, str]:
    """The admin's folder ``plan``, shared with the member at ``role``; the
    member signed in and their own home id to aim a move at."""
    drive, admin_home = await _drive_and_home(files_client)
    plan = await _folder(files_client, drive, admin_home, "plan")
    node = await fx.folder(uuid.UUID(plan))
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="user", id=files_org.member.id), role)
    await fx.repo.session.commit()
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    _, member_home = await _drive_and_home(member)
    return drive, plan, node, member, member_home


@pytest.mark.parametrize("role", [ROLE_WRITER, ROLE_MANAGER])
async def test_a_sharee_cannot_move_a_folder_into_their_own_home(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    role: str,
) -> None:
    drive, plan, node, member, member_home = await _shared_with_member(
        files_client, fx, files_org, role
    )
    parent_before = await _parent_of(plan)

    moved = await member.patch(
        f"{BASE}/drives/{drive}/items/{plan}",
        json={"parentId": member_home},
        headers={**_idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert moved.status_code == 403, moved.text
    assert moved.json()["code"] == "files.forbidden"
    assert await _parent_of(plan) == parent_before
    decision = await _last_decision(files_org.org.org_id, plan)
    assert (decision["action"], decision["effect"], decision["reason"]) == (
        "delete",
        "deny",
        "action_not_allowed",
    )
    # The owner still has it, exactly where it was.
    still = await files_client.get(f"{BASE}/drives/{drive}/items/{plan}")
    assert still.status_code == 200, still.text
    assert still.json()["parentId"] == parent_before
    await member.aclose()


async def test_a_writer_still_renames_and_writes_in_place(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    drive, plan, node, member, _ = await _shared_with_member(
        files_client, fx, files_org, ROLE_WRITER
    )

    renamed = await member.patch(
        f"{BASE}/drives/{drive}/items/{plan}",
        json={"name": "plan-v2"},
        headers={**_idem(), "If-Match": await node_etag(real_session, node.id)},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "plan-v2"

    inside = await _folder(member, drive, plan, "notes")
    assert inside
    await member.aclose()


async def test_a_writer_moves_between_two_folders_they_may_edit(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The control for the rule's shape: the rung does not rise, so the move
    is the ordinary write — reorganising a shared area still works for the
    people who edit in it."""
    drive, plan, node, member, _ = await _shared_with_member(
        files_client, fx, files_org, ROLE_WRITER
    )
    _, admin_home = await _drive_and_home(files_client)
    archive = await _folder(files_client, drive, admin_home, "archive")
    archive_node = await fx.folder(uuid.UUID(archive))
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo, ctx, archive_node, Principal(kind="user", id=files_org.member.id), ROLE_WRITER
        )
    await fx.repo.session.commit()

    moved = await member.patch(
        f"{BASE}/drives/{drive}/items/{plan}",
        json={"parentId": archive},
        headers={**_idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert moved.status_code == 200, moved.text
    assert await _parent_of(plan) == archive
    await member.aclose()


async def test_the_owner_moves_it(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    drive, admin_home = await _drive_and_home(files_client)
    plan = await _folder(files_client, drive, admin_home, "plan")
    archive = await _folder(files_client, drive, admin_home, "archive")

    moved = await files_client.patch(
        f"{BASE}/drives/{drive}/items/{plan}",
        json={"parentId": archive},
        headers={**_idem(), "If-Match": await node_etag(real_session, uuid.UUID(plan))},
    )

    assert moved.status_code == 200, moved.text
    assert await _parent_of(plan) == archive


async def test_the_batch_route_decides_a_move_at_the_same_rung(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    drive, plan, node, member, member_home = await _shared_with_member(
        files_client, fx, files_org, ROLE_WRITER
    )
    parent_before = await _parent_of(plan)

    response = await member.post(
        f"{BASE}/drives/{drive}/bulk",
        json={
            "items": [
                {
                    "id": "m",
                    "op": "move",
                    "itemId": plan,
                    "parentId": member_home,
                    "ifMatch": await node_etag(real_session, node.id),
                }
            ]
        },
        headers=_idem(),
    )

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["responses"]}
    assert rows["m"]["status"] == 403, rows
    assert await _parent_of(plan) == parent_before
    await member.aclose()


# -- the restore door: a trashed tree landing in a folder of the caller's choosing


async def _trash(client: AsyncClient, drive: str, node_id: str, etag: str) -> str:
    """Trash ``node_id`` through the route and return its TRASH op id — the
    handle a restore names — read off the trash listing, not the operation."""
    binned = await client.delete(
        f"{BASE}/drives/{drive}/items/{node_id}", headers={**_idem(), "If-Match": etag}
    )
    assert binned.status_code == 200, binned.text
    async with AsyncSessionLocal() as session:
        return str(
            (
                await session.execute(
                    text("SELECT id FROM file_trash_ops WHERE root_node_id = :root"),
                    {"root": node_id},
                )
            ).scalar_one()
        )


async def _trashed_at(node_id: str) -> Any:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                text("SELECT trashed_at FROM file_nodes WHERE id = :id"), {"id": node_id}
            )
        ).scalar_one()


async def test_a_writer_cannot_restore_a_colleagues_folder_into_their_own_home(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The same escalation through the third door: a writer may trash the
    folder and may restore it, and a restore that names a parent is a move —
    the rung rule runs on it exactly as on the move."""
    drive, plan, node, member, member_home = await _shared_with_member(
        files_client, fx, files_org, ROLE_WRITER
    )
    op_id = await _trash(member, drive, plan, await node_etag(real_session, node.id))

    refused = await member.post(
        f"{BASE}/drives/{drive}/trash/{op_id}/restore",
        json={"parent_id": member_home},
        headers={**_idem(), "If-Match": "0"},
    )

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    assert await _trashed_at(plan) is not None, "the refused restore moved nothing"
    decision = await _last_decision(files_org.org.org_id, plan)
    assert (decision["action"], decision["effect"], decision["reason"]) == (
        "delete",
        "deny",
        "action_not_allowed",
    )

    # The owner brings it back where it was.
    restored = await files_client.post(
        f"{BASE}/drives/{drive}/trash/{op_id}/restore",
        json={},
        headers={**_idem(), "If-Match": "0"},
    )
    assert restored.status_code == 200, restored.text
    assert await _trashed_at(plan) is None
    await member.aclose()


async def test_a_writer_restores_to_the_original_place(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """No parent named, no rung raised: the restore is the writer's as before."""
    drive, plan, node, member, _ = await _shared_with_member(
        files_client, fx, files_org, ROLE_WRITER
    )
    parent_before = await _parent_of(plan)
    op_id = await _trash(member, drive, plan, await node_etag(real_session, node.id))

    restored = await member.post(
        f"{BASE}/drives/{drive}/trash/{op_id}/restore",
        json={},
        headers={**_idem(), "If-Match": "0"},
    )

    assert restored.status_code == 200, restored.text
    assert await _trashed_at(plan) is None
    assert await _parent_of(plan) == parent_before
    await member.aclose()


async def test_the_owner_restores_into_a_folder_of_their_choosing(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    drive, admin_home = await _drive_and_home(files_client)
    plan = await _folder(files_client, drive, admin_home, "plan")
    archive = await _folder(files_client, drive, admin_home, "archive")
    op_id = await _trash(files_client, drive, plan, await node_etag(real_session, uuid.UUID(plan)))

    restored = await files_client.post(
        f"{BASE}/drives/{drive}/trash/{op_id}/restore",
        json={"parent_id": archive},
        headers={**_idem(), "If-Match": "0"},
    )

    assert restored.status_code == 200, restored.text
    assert await _trashed_at(plan) is None
    assert await _parent_of(plan) == archive
