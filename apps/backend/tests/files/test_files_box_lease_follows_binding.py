"""A box keeps a chat's or a workspace's folder only while it runs it.

A box beats to keep the lease on the folder it runs. The beat proves the box
is alive, not that the chat or the workspace is still meant to run there: once
it has moved to another box, the box it left may finish (its last push is the
only copy of its last turn) but may not stay. Its beat forces the lease rather
than extending it, so the lease lapses at the deadline it already had, however
long the old box keeps beating. Within that grace its hand-back still lands.

A workspace's folder follows the rule every door of the workspace reads: the
box that reported the workspace keeps it while it still holds it, a sandbox
kept up for a notebook kernel with no chat on the box included, and a box the
workspace's chats all moved off, or whose report was dropped, does not.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, FilesOrgFixture
from _live_holder import MockHolder
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import FileLease, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient, Response
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login
from tests.files._boxes import registered_box
from tests.test_machine_principal_routes import _box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


MINT = "/api/v1/machines/me/worker-credentials"


@pytest_asyncio.fixture(params=["agent-on-cli-token", "worker-credential", "machine-credential"])
async def box(
    request: pytest.FixtureRequest,
    platform_admin: OrgWithAdmin,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> AsyncIterator[tuple[AsyncClient, str]]:
    """The box, as each credential a box beats on: its operator's CLI token
    with the agent assertion it registered, the worker credential a platform
    box dedicated to the org mints for it, and the machine credential a pool
    box the org operates speaks on."""
    org = files_org.org
    if request.param == "agent-on-cli-token":
        token, machine_id = await registered_box(
            real_session, user_id=org.admin_id, email=org.admin_email, org_id=org.org_id
        )
        headers = {"Authorization": f"Bearer {token}", **agent_headers(machine_id)}
        async with app_client(headers=headers, names_org=False) as client:
            yield client, machine_id
        return
    if request.param == "worker-credential":
        made = await _box(platform_admin, tenancy="dedicated", served_org=org.org_id)
        machine_id = str(made.machine_id)
        # The chat of the org bound to the box that lets it mint a worker at all.
        await _anchor_chat(real_session, files_org, machine_id)
        async with app_client(
            headers={"Authorization": f"Bearer {made.raw}"}, names_org=False
        ) as root:
            minted = await root.post(MINT, json={"org_id": str(org.org_id)})
        assert minted.status_code == 201, minted.text
        raw = str(minted.json()["token"])
    else:
        made = await _box(org, tenancy="pool")
        machine_id, raw = str(made.machine_id), made.raw
    async with app_client(headers={"Authorization": f"Bearer {raw}"}, names_org=False) as client:
        yield client, machine_id


async def _anchor_chat(session: AsyncSession, files_org: FilesOrgFixture, machine: str) -> None:
    session.add(
        WorkspaceObject(
            org_team_id=files_org.org.org_id,
            logical_id=uuid.uuid4().hex,
            type="chat",
            title="Anchor",
            owner_user_id=files_org.org.admin_id,
            visibility_scope="private",
            spec={"machine_id": machine},
        )
    )
    await session.commit()


async def _bind(object_id: uuid.UUID | str, machine: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
            ),
            {"id": uuid.UUID(str(object_id)), "machine": machine},
        )
        await session.commit()


async def _member_chat(files_org: FilesOrgFixture) -> tuple[str, FileNode]:
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    try:
        made = await member.post(
            "/api/v1/chats", json={"title": "Pricing review", "clientId": secrets.token_hex(8)}
        )
    finally:
        await member.aclose()
    assert made.status_code == 201, made.text
    chat_id = str(made.json()["id"])
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == uuid.UUID(chat_id),
                    FileNode.subtype == "chat",
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        session.expunge(node)
    return chat_id, node


async def _beat(client: AsyncClient, holder: MockHolder) -> Response:
    """The beat a box sends: its fence on the request, as the SDK sends it."""
    return await client.post(
        f"{holder.item}/lease/heartbeat",
        json={"epoch": holder.epoch, "instanceId": holder.instance},
        headers=holder.fence,
    )


async def _release(client: AsyncClient, holder: MockHolder, session: AsyncSession) -> Response:
    """The hand-back a box sends, its fence on the request."""
    etag = (
        await session.execute(
            text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": holder.node_id}
        )
    ).scalar_one()
    return await client.post(
        f"{holder.item}/lease/release",
        json={"epoch": holder.epoch, "instanceId": holder.instance, "final": None},
        headers={**_idem(), **holder.fence, "If-Match": str(etag)},
    )


async def _expires_at(node_id: uuid.UUID) -> datetime:
    async with AsyncSessionLocal() as session:
        row = await session.get(FileLease, node_id)
        assert row is not None
        return row.expires_at


async def _deadline_passes(node_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE file_leases SET expires_at = now() - interval '1 second', "
                "grantable_after = now() - interval '1 second' WHERE node_id = :id"
            ),
            {"id": node_id},
        )
        await session.commit()


async def test_the_box_running_the_chat_keeps_its_folder(
    box: tuple[AsyncClient, str], files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    client, machine_id = box
    chat_id, node = await _member_chat(files_org)
    await _bind(chat_id, machine_id)
    holder = MockHolder(client, node.drive_id, node.id, machine=machine_id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    before = await _expires_at(node.id)

    beat = await _beat(client, holder)

    assert beat.status_code == 200, beat.text
    assert beat.json()["forced"] is False
    assert await _expires_at(node.id) > before


async def test_a_box_the_chat_moved_off_may_finish_but_not_stay(
    box: tuple[AsyncClient, str],
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """Moved to another box while this one keeps beating: the beat answers
    ``forced`` and the deadline stays where it was, and the hand-back the
    holder makes within it lands."""
    client, machine_id = box
    chat_id, node = await _member_chat(files_org)
    await _bind(chat_id, machine_id)
    holder = MockHolder(client, node.drive_id, node.id, machine=machine_id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    await _bind(chat_id, str(uuid.uuid4()))
    deadline = await _expires_at(node.id)

    beat = await _beat(client, holder)
    assert beat.status_code == 200, beat.text
    assert beat.json()["forced"] is True
    assert await _expires_at(node.id) == deadline
    again = await _beat(client, holder)
    assert again.status_code == 200, again.text
    assert await _expires_at(node.id) == deadline

    released = await _release(client, holder, real_session)
    assert released.status_code in (200, 204), released.text


async def test_past_the_grace_the_moved_box_is_fenced(
    box: tuple[AsyncClient, str], files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """A box that never hands back is cut off at the deadline its forced beat
    left standing: a late beat is not re-granted, and its next fenced write
    is refused."""
    client, machine_id = box
    chat_id, node = await _member_chat(files_org)
    await _bind(chat_id, machine_id)
    holder = MockHolder(client, node.drive_id, node.id, machine=machine_id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    await _bind(chat_id, str(uuid.uuid4()))
    await _deadline_passes(node.id)

    late = await _beat(client, holder)

    assert late.status_code in (404, 409), late.text
    written = await client.post(
        f"{BASE}/drives/{node.drive_id}/items/{node.id}/children",
        json={"name": "outputs", "kind": "folder"},
        headers={**_idem(), **holder.fence},
    )
    assert written.status_code in (404, 409), written.text


# ---------------------------------------------------------------------------
# A workspace's folder
# ---------------------------------------------------------------------------


async def _workspace(
    session: AsyncSession, files_org: FilesOrgFixture, *, reported: str | None
) -> tuple[uuid.UUID, FileNode]:
    """A native workspace of the admin's and its folder; ``reported`` is the
    box whose report the workspace carries (``""`` a report that was dropped,
    ``None`` no report ever)."""
    fx = FilesFixtures(session, files_org.org.org_id, files_org.org.admin_id)
    spec: dict[str, object] = {"layout": "native"}
    if reported is not None:
        spec |= {"binding_authority": "workspace", "machine_id": reported or None}
    row = WorkspaceObject(
        org_team_id=files_org.org.org_id,
        logical_id=uuid.uuid4().hex,
        type="workspace",
        title="Pricing",
        owner_user_id=files_org.org.admin_id,
        visibility_scope="private",
        spec=spec,
    )
    session.add(row)
    await session.commit()
    folder = await fx.node(
        b"Pricing.alkeraworkspace",
        kind="folder",
        subtype=WORKSPACE_TYPE,
        target_object_id=row.id,
        parent=await fx.home(),
    )
    return uuid.UUID(str(row.id)), folder


async def _workspace_chat(
    session: AsyncSession, files_org: FilesOrgFixture, workspace: uuid.UUID, machine: str
) -> uuid.UUID:
    """A live chat of the workspace, bound to ``machine``."""
    chat = WorkspaceObject(
        org_team_id=files_org.org.org_id,
        logical_id=uuid.uuid4().hex,
        type="chat",
        title="Member",
        owner_user_id=files_org.org.admin_id,
        visibility_scope="private",
        spec={"workspace_id": str(workspace), "machine_id": machine},
    )
    session.add(chat)
    await session.commit()
    return uuid.UUID(str(chat.id))


async def _delete(chat: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(WorkspaceObject).where(WorkspaceObject.id == chat).values(deleted_at=1)
        )
        await session.commit()


async def _report(workspace: uuid.UUID, machine: str) -> None:
    """The workspace's report naming ``machine`` (``""``: dropped by a move)."""
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, workspace)
        assert row is not None
        row.spec = {**(row.spec or {}), "machine_id": machine or None}
        await session.commit()


@pytest.mark.parametrize(
    ("case", "keeps"),
    [
        pytest.param("notebook-only", True, id="reported-and-its-chats-gone-the-kernels-sandbox"),
        pytest.param("chat-here", True, id="reported-with-its-chat-here"),
        pytest.param("never-reported", True, id="an-older-box-that-never-reports"),
        pytest.param("chats-elsewhere", False, id="its-chats-moved-to-another-box"),
        pytest.param("report-dropped", False, id="its-chats-gone-and-its-report-dropped"),
        pytest.param("another-reported", False, id="its-chats-gone-and-another-box-reported"),
    ],
)
async def test_a_workspace_lease_is_kept_while_the_box_holds_the_workspace(
    box: tuple[AsyncClient, str],
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    case: str,
    keeps: bool,
) -> None:
    client, machine_id = box
    workspace, folder = await _workspace(
        real_session, files_org, reported=None if case == "never-reported" else machine_id
    )
    chat = await _workspace_chat(real_session, files_org, workspace, machine_id)
    holder = MockHolder(client, folder.drive_id, folder.id, machine=machine_id)
    taken = await holder.take(real_session, _idem, purpose="workspace")
    assert taken.status_code == 200, taken.text
    if case == "chats-elsewhere":
        await _bind(chat, str(uuid.uuid4()))
    if case in ("notebook-only", "report-dropped", "another-reported"):
        await _delete(chat)
    if case in ("report-dropped", "another-reported"):
        await _report(workspace, "" if case == "report-dropped" else str(uuid.uuid4()))
    deadline = await _expires_at(folder.id)

    beat = await _beat(client, holder)

    assert beat.status_code == 200, beat.text
    assert beat.json()["forced"] is (not keeps)
    if keeps:
        assert await _expires_at(folder.id) > deadline
    else:
        assert await _expires_at(folder.id) == deadline
