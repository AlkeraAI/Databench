"""A box reaches a folder because it runs it, never because a person shared it.

A grant names a person, a team or the whole org. A box speaks either on its
machine credential (whose org is the operator org, the customer's own when the
org runs its box) or on the worker credential that credential mints for one
org (whose org is that customer org). Both principals carry an org id, so a
grant to "everyone in the org" used to name the box as well: a box holding one
chat of the org could read, export, copy and lease every org-visible workspace,
every chat shared to the whole org, and every folder the org could see. What
admits a box is the binding of the chat or workspace to its machine, or the
live lease it holds; these tests pin both halves through the real routes, with
the decision row each refusal leaves behind.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, FilesFixtures, FilesOrgFixture, node_etag, refusal
from _live_holder import MockHolder
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_WRITER
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.models import EventOutbox, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client
from tests.files.test_files_content_routes import (  # noqa: F401 -- content_on is a fixture
    content_on,
)
from tests.test_machine_principal_routes import _box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
MINT = "/api/v1/machines/me/worker-credentials"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


def _client(raw: str) -> AsyncClient:
    return app_client(headers={"Authorization": f"Bearer {raw}"}, names_org=False)


@dataclass(frozen=True)
class Caller:
    """A box as one of its two credentials, and the machine it holds."""

    client: AsyncClient
    machine_id: str


async def _object(session: AsyncSession, fx: FilesFixtures, **columns: Any) -> uuid.UUID:
    row = WorkspaceObject(
        org_team_id=fx.org_team_id,
        logical_id=uuid.uuid4().hex,
        owner_user_id=fx.actor_id,
        **{"visibility_scope": "private", **columns},
    )
    session.add(row)
    await session.commit()
    return uuid.UUID(str(row.id))


async def _share_with_org(fx: FilesFixtures, org: OrgWithAdmin, node_id: uuid.UUID) -> None:
    """Can edit for everyone in the org, the widest share a member can make."""
    node = await fx.folder(node_id)
    ctx = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email="fixture@test")
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="org", id=org.org_id), ROLE_WRITER)
    await fx.repo.session.commit()


@dataclass(frozen=True)
class Workspace:
    drive_id: uuid.UUID
    folder_id: uuid.UUID
    file_id: uuid.UUID

    def item(self, node_id: uuid.UUID) -> str:
        return f"{BASE}/drives/{self.drive_id}/items/{node_id}"


async def _workspace(
    session: AsyncSession, fx: FilesFixtures, org: OrgWithAdmin, *, chat_machine: str | None
) -> Workspace:
    """An org-visible native workspace shared Can edit with the whole org, one
    file in its shared tree, and (when ``chat_machine`` is given) one chat of
    it bound to that machine."""
    workspace_id = await _object(
        session,
        fx,
        type="workspace",
        title="Forecast",
        spec={"layout": "native"},
        visibility_scope="org",
    )
    folder = await fx.node(
        b"Forecast.alkeraworkspace",
        kind="folder",
        subtype=WORKSPACE_TYPE,
        target_object_id=workspace_id,
        parent=await fx.shared(),
    )
    files = await fx.node(b"files", kind="folder", parent=folder)
    data = await fx.node(b"data.csv", parent=files)
    await fx.version(data)
    if chat_machine is not None:
        chats = await fx.node(b".chats", kind="folder", parent=folder)
        chat_id = await _object(
            session,
            fx,
            type="chat",
            title="Kickoff",
            spec={"machine_id": chat_machine, "workspace_id": str(workspace_id)},
            visibility_scope="private",
        )
        await fx.node(
            b"Kickoff.alkerachat",
            kind="folder",
            subtype=CHAT_TYPE,
            target_object_id=chat_id,
            parent=chats,
        )
    await _share_with_org(fx, org, folder.id)
    return Workspace(folder.drive_id, folder.id, data.id)


async def _chat_folder(
    session: AsyncSession, fx: FilesFixtures, org: OrgWithAdmin, *, machine: str | None
) -> tuple[uuid.UUID, uuid.UUID]:
    """A chat's folder shared Can edit with the whole org, bound to
    ``machine`` (or to none)."""
    spec: dict[str, str] = {} if machine is None else {"machine_id": machine}
    chat_id = await _object(
        session, fx, type="chat", title="Pricing", spec=spec, visibility_scope="private"
    )
    folder = await fx.node(
        b"Pricing.alkerachat",
        kind="folder",
        subtype=CHAT_TYPE,
        target_object_id=chat_id,
        parent=await fx.shared(),
    )
    await _share_with_org(fx, org, folder.id)
    return folder.drive_id, folder.id


async def _lease_held(node_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            text("SELECT 1 FROM file_leases WHERE node_id = :node AND released_at IS NULL"),
            {"node": node_id},
        )
        return row.first() is not None


async def _decisions(org_id: uuid.UUID, node_id: uuid.UUID) -> set[tuple[str, str, str]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox).where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "file_node",
                EventOutbox.entity_id == str(node_id),
            )
        )
        return {
            (r.payload["action"], r.payload["effect"], r.payload["reason"])
            for r in rows.scalars().all()
        }


@pytest_asyncio.fixture(params=["worker", "org-machine"])
async def caller(
    request: pytest.FixtureRequest,
    platform_admin: OrgWithAdmin,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    fx: FilesFixtures,
) -> AsyncIterator[Caller]:
    """The two ways a box carries the customer org as its own:

    * ``worker``: a platform box dedicated to the org, holding one chat of it,
      on the worker credential it minted for the org;
    * ``org-machine``: a pool box the org itself operates, on its machine
      credential.
    """
    org = files_org.org
    if request.param == "worker":
        made = await _box(platform_admin, tenancy="dedicated", served_org=org.org_id)
        machine = str(made.machine_id)
        # The one chat of the org bound to the box, which is what lets it mint
        # a worker for the org at all.
        await _object(real_session, fx, type="chat", title="Mine", spec={"machine_id": machine})
        async with _client(made.raw) as box:
            minted = await box.post(MINT, json={"org_id": str(org.org_id)})
        assert minted.status_code == 201, minted.text
        raw = str(minted.json()["token"])
    else:
        made = await _box(org, tenancy="pool")
        machine, raw = str(made.machine_id), made.raw
    async with _client(raw) as client:
        yield Caller(client, machine)


@pytest.mark.usefixtures("content_on")
async def test_a_box_reaches_nothing_of_an_org_shared_workspace_it_does_not_run(
    caller: Caller, files_org: FilesOrgFixture, real_session: AsyncSession, fx: FilesFixtures
) -> None:
    """An org-visible workspace no chat of which runs on the box, shared Can
    edit with everyone in the org: its folder, the file in its shared tree,
    that file's bytes and the folder's lease are each the opaque not-found,
    and the refusal says the box does not run it."""
    ws = await _workspace(real_session, fx, files_org.org, chat_machine=None)

    folder = await caller.client.get(ws.item(ws.folder_id))
    assert folder.status_code == 404, folder.text
    assert refusal(folder) == NOT_FOUND
    item = await caller.client.get(ws.item(ws.file_id))
    assert item.status_code == 404, item.text
    content = await caller.client.get(f"{ws.item(ws.file_id)}/content", follow_redirects=False)
    assert content.status_code == 404, content.text
    assert refusal(content) == NOT_FOUND
    # The batched lookup decides on the access alone; it names neither node.
    looked = await caller.client.post(
        f"{BASE}/drives/{ws.drive_id}/items/lookup",
        json={"ids": [str(ws.folder_id), str(ws.file_id)]},
    )
    assert looked.status_code == 200, looked.text
    assert looked.json()["value"] == []
    for purpose in ("workspace", "chat"):
        taken = await MockHolder(caller.client, ws.drive_id, ws.folder_id).take(
            real_session, _idem, purpose=purpose
        )
        assert taken.status_code == 404, taken.text
        assert refusal(taken) == NOT_FOUND

    org_id = files_org.org.org_id
    assert await _decisions(org_id, ws.folder_id) == {
        ("read", "deny", "machine_does_not_run"),
        ("lease", "deny", "machine_does_not_run"),
    }
    assert {row[2] for row in await _decisions(org_id, ws.file_id)} == {"machine_does_not_run"}


async def test_a_box_reaches_nothing_of_an_org_shared_chat_no_box_is_bound_to(
    caller: Caller, files_org: FilesOrgFixture, real_session: AsyncSession, fx: FilesFixtures
) -> None:
    """An unplaced chat shared Can edit with the whole org is not the first
    box's to take: its folder is the opaque not-found, read and lease alike."""
    drive_id, folder_id = await _chat_folder(real_session, fx, files_org.org, machine=None)
    item = f"{BASE}/drives/{drive_id}/items/{folder_id}"

    read = await caller.client.get(item)
    assert read.status_code == 404, read.text
    assert refusal(read) == NOT_FOUND
    taken = await MockHolder(caller.client, drive_id, folder_id).take(
        real_session, _idem, purpose="chat"
    )
    assert taken.status_code == 404, taken.text
    assert await _decisions(files_org.org.org_id, folder_id) == {
        ("read", "deny", "machine_does_not_run"),
        ("lease", "deny", "machine_does_not_run"),
    }


async def test_a_box_still_runs_the_chat_bound_to_it(
    caller: Caller, files_org: FilesOrgFixture, real_session: AsyncSession, fx: FilesFixtures
) -> None:
    """The same org share on a chat bound to the box changes nothing: the box
    reads and leases it on its own rung, on record as the box's."""
    drive_id, folder_id = await _chat_folder(
        real_session, fx, files_org.org, machine=caller.machine_id
    )
    item = f"{BASE}/drives/{drive_id}/items/{folder_id}"

    read = await caller.client.get(item)
    assert read.status_code == 200, read.text
    assert read.json()["capabilities"]["can_share"] is False
    taken = await MockHolder(caller.client, drive_id, folder_id).take(
        real_session, _idem, purpose="chat"
    )
    assert taken.status_code == 200, taken.text
    assert {row[2] for row in await _decisions(files_org.org.org_id, folder_id)} == {
        "machine_holds_chat"
    }


async def test_a_box_still_runs_the_workspace_a_chat_bound_to_it_belongs_to(
    caller: Caller, files_org: FilesOrgFixture, real_session: AsyncSession, fx: FilesFixtures
) -> None:
    """A workspace one of whose chats is bound to the box is the box's: it
    reads the shared tree and takes the workspace lease."""
    ws = await _workspace(real_session, fx, files_org.org, chat_machine=caller.machine_id)

    item = await caller.client.get(ws.item(ws.file_id))
    assert item.status_code == 200, item.text
    holder = MockHolder(caller.client, ws.drive_id, ws.folder_id)
    taken = await holder.take(real_session, _idem, purpose="workspace")
    assert taken.status_code == 200, taken.text
    assert {row[2] for row in await _decisions(files_org.org.org_id, ws.folder_id)} == {
        "machine_holds_workspace"
    }


async def test_a_box_holding_the_lease_keeps_its_rung_after_the_binding_moves(
    caller: Caller, files_org: FilesOrgFixture, real_session: AsyncSession, fx: FilesFixtures
) -> None:
    """The departing holder: the chat moved off the box, which still holds the
    folder's live lease and so may finish (read under its fence) and let go.
    Without the fence the same box is a stranger to the folder."""
    drive_id, folder_id = await _chat_folder(
        real_session, fx, files_org.org, machine=caller.machine_id
    )
    holder = MockHolder(caller.client, drive_id, folder_id)
    taken = await holder.take(real_session, _idem, purpose="chat")
    assert taken.status_code == 200, taken.text
    node = await real_session.get(FileNode, folder_id)
    assert node is not None and node.target_object_id is not None
    chat = await real_session.get(WorkspaceObject, node.target_object_id)
    assert chat is not None
    chat.spec = {}
    await real_session.commit()

    item = f"{BASE}/drives/{drive_id}/items/{folder_id}"
    fenced = await caller.client.get(item, headers=holder.fence)
    assert fenced.status_code == 200, fenced.text
    bare = await caller.client.get(item)
    assert bare.status_code == 404, bare.text
    # The hand-back carries the fence, as the box's SDK sends it.
    released = await caller.client.post(
        f"{item}/lease/release",
        json={"epoch": holder.epoch, "instanceId": holder.instance, "final": None},
        headers={
            **_idem(),
            **holder.fence,
            "If-Match": await node_etag(real_session, folder_id),
        },
    )
    assert released.status_code == 200, released.text
    assert await _lease_held(folder_id) is False


async def test_a_box_holding_a_workspace_lease_with_no_chat_on_it_keeps_the_tree(
    caller: Caller, files_org: FilesOrgFixture, real_session: AsyncSession, fx: FilesFixtures
) -> None:
    """A notebook-only workspace: the box took the workspace lease, and then no
    chat of the workspace is bound to it any more. The live lease alone keeps
    the box's rung on the shared tree under its fence; without the fence the
    same box reads nothing of it."""
    ws = await _workspace(real_session, fx, files_org.org, chat_machine=caller.machine_id)
    holder = MockHolder(caller.client, ws.drive_id, ws.folder_id)
    taken = await holder.take(real_session, _idem, purpose="workspace")
    assert taken.status_code == 200, taken.text
    folder = await real_session.get(FileNode, ws.folder_id)
    assert folder is not None and folder.target_object_id is not None
    await real_session.execute(
        text(
            "UPDATE workspace_objects SET spec = spec - 'machine_id' "
            "WHERE type = 'chat' AND spec->>'workspace_id' = :ws"
        ),
        {"ws": str(folder.target_object_id)},
    )
    await real_session.commit()

    fenced = await caller.client.get(ws.item(ws.file_id), headers=holder.fence)
    assert fenced.status_code == 200, fenced.text
    bare = await caller.client.get(ws.item(ws.file_id))
    assert bare.status_code == 404, bare.text
