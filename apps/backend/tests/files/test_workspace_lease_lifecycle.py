"""A box holding a workspace's folder, through the real routes.

The box that runs every chat of a workspace in one sandbox takes one
``workspace`` lease on the workspace's ``.alkeraworkspace`` folder, and each
chat's own lease on its records folder under ``.chats/`` nests beneath it.
What a person and a box can observe here:

* a person's write into the shared ``files/`` tree is taken while the box
  holds the folder, and a write into a chat's records is refused;
* a chat moved off a box that will never hand anything back takes the box's
  workspace lease with it (the chat's own lease ending never reaches the
  workspace's, which sits above it), and a box still serving keeps it;
* a chat's folder cannot be moved into another workspace, or out of its own;
* a workspace lease is its bound box's alone, a person's lease inside a
  workspace takes Full access, and forcing a box's lease off takes the owner
  or an org admin and a reason, each on record (``files.lease_kind``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from _files_kit import node_etag
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_MANAGER, ROLE_WRITER
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.models import EventOutbox, WorkspaceObject
from alkera_core.objects import chat_end
from backend.api.routes.files.leases import FORCE_REASON_RECORDED
from backend.services.chats import chat_service
from backend.services.compute import placement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login
from tests.files._boxes import registered_box

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


@dataclass(frozen=True)
class Held:
    """A workspace folder a box holds, with one chat of it awake."""

    drive_id: uuid.UUID
    workspace_id: uuid.UUID
    folder_id: uuid.UUID
    files_id: uuid.UUID
    chats_id: uuid.UUID
    chat_folder_id: uuid.UUID
    chat_id: uuid.UUID
    machine_id: str
    epoch: int
    box: AsyncClient


@pytest_asyncio.fixture
async def box(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> AsyncIterator[tuple[AsyncClient, str]]:
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        yield client, machine_id


async def _object(session: AsyncSession, fx: FilesFixtures, **columns: Any) -> uuid.UUID:
    row = WorkspaceObject(
        org_team_id=fx.org_team_id,
        logical_id=uuid.uuid4().hex,
        owner_user_id=fx.actor_id,
        visibility_scope="private",
        **columns,
    )
    session.add(row)
    await session.flush()
    return uuid.UUID(str(row.id))


async def _workspace_folder(
    fx: FilesFixtures, session: AsyncSession, *, name: bytes, machine: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A native workspace as the product lays it out: its folder holding
    ``files/`` and ``.chats/``, and one chat filed under ``.chats/`` bound to
    ``machine`` and naming the workspace."""
    workspace_id = await _object(
        session, fx, type="workspace", title=name.decode(), spec={"layout": "native"}
    )
    folder = await fx.node(
        name + b".alkeraworkspace",
        kind="folder",
        subtype=WORKSPACE_TYPE,
        target_object_id=workspace_id,
        parent=await fx.home(),
    )
    files = await fx.node(b"files", kind="folder", parent=folder)
    chats = await fx.node(b".chats", kind="folder", parent=folder)
    chat_id = await _object(
        session,
        fx,
        type="chat",
        title="Kickoff",
        spec={"machine_id": machine, "workspace_id": str(workspace_id), "mirror_state": "awake"},
    )
    chat_folder = await fx.node(
        b"Kickoff.alkerachat",
        kind="folder",
        subtype=CHAT_TYPE,
        target_object_id=chat_id,
        parent=chats,
    )
    await session.commit()
    return workspace_id, folder.id, files.id, chats.id, chat_folder.id, chat_id


async def _take(
    box: AsyncClient,
    session: AsyncSession,
    idem: Any,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    *,
    machine_id: str,
    instance: str,
    purpose: str,
) -> Any:
    return await box.post(
        f"{_item(drive_id, node_id)}/lease",
        json={"instanceId": instance, "machineId": machine_id, "purpose": purpose},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )


@pytest_asyncio.fixture
async def held(
    fx: FilesFixtures, real_session: AsyncSession, box: tuple[AsyncClient, str], idem: Any
) -> Held:
    box_client, machine_id = box
    drive = await fx.drive()
    workspace_id, folder_id, files_id, chats_id, chat_folder_id, chat_id = await _workspace_folder(
        fx, real_session, name=b"Pricing", machine=machine_id
    )
    granted = await _take(
        box_client,
        real_session,
        idem,
        drive.id,
        folder_id,
        machine_id=machine_id,
        instance=f"{machine_id}:ws:{workspace_id}",
        purpose="workspace",
    )
    assert granted.status_code == 200, granted.text
    # The chat's own lease on its records nests under the box's workspace lease.
    nested = await _take(
        box_client,
        real_session,
        idem,
        drive.id,
        chat_folder_id,
        machine_id=machine_id,
        instance=f"{machine_id}:{chat_id}",
        purpose="chat",
    )
    assert nested.status_code == 200, nested.text
    return Held(
        drive_id=drive.id,
        workspace_id=workspace_id,
        folder_id=folder_id,
        files_id=files_id,
        chats_id=chats_id,
        chat_folder_id=chat_folder_id,
        chat_id=chat_id,
        machine_id=machine_id,
        epoch=int(granted.json()["epoch"]),
        box=box_client,
    )


async def _lease_row(session: AsyncSession, node_id: uuid.UUID) -> Any:
    row = (
        await session.execute(
            text("SELECT released_at, purpose FROM file_leases WHERE node_id = :node"),
            {"node": node_id},
        )
    ).one()
    await session.commit()
    return row


async def test_a_person_drops_into_the_shared_tree_but_not_into_a_chats_records(
    held: Held, files_client: AsyncClient, idem: Any
) -> None:
    taken = await files_client.post(
        f"{_item(held.drive_id, held.files_id)}/children",
        json={"name": "brief", "kind": "folder"},
        headers=idem(),
    )
    assert taken.status_code in (200, 201), taken.text

    refused = await files_client.post(
        f"{_item(held.drive_id, held.chat_folder_id)}/children",
        json={"name": "forged", "kind": "folder"},
        headers=idem(),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"


async def _move_off(session: AsyncSession, held: Held, *, departed_state: str) -> bool:
    await session.execute(
        text(
            "UPDATE compute_allocations SET state = :state, last_heartbeat_at = now() WHERE id = :m"
        ),
        {"state": departed_state, "m": held.machine_id},
    )
    await session.commit()
    chat = await chat_end.lock_chat(session, held.chat_id)
    assert chat is not None
    moved = await chat_service.rebind_machine(
        session, chat=chat, machine_id=str(uuid.uuid4()), machine_status="ready"
    )
    ended = await placement.hand_over_folder(session, chat=moved, departed=held.machine_id)
    await session.commit()
    return ended


async def test_a_move_off_a_box_that_cannot_hand_back_ends_its_workspace_lease(
    real_session: AsyncSession, held: Held
) -> None:
    assert await _move_off(real_session, held, departed_state="asleep")

    workspace = await _lease_row(real_session, held.folder_id)
    assert workspace.purpose == "workspace"
    assert workspace.released_at is not None
    assert (await _lease_row(real_session, held.chat_folder_id)).released_at is not None


async def test_a_move_off_a_box_still_serving_leaves_it_the_workspace_lease(
    real_session: AsyncSession, held: Held
) -> None:
    """A draining box hands the folder back itself: its last push is the only
    copy of what its chats last did, so nothing it holds is ended for it."""
    assert not await _move_off(real_session, held, departed_state="ready")

    assert (await _lease_row(real_session, held.folder_id)).released_at is None


async def _patch_parent(
    client: AsyncClient,
    session: AsyncSession,
    idem: Any,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    parent_id: uuid.UUID,
) -> Any:
    return await client.patch(
        _item(drive_id, node_id),
        json={"parentId": str(parent_id)},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )


async def test_a_chat_cannot_be_moved_into_another_workspace(
    fx: FilesFixtures,
    real_session: AsyncSession,
    files_client: AsyncClient,
    idem: Any,
) -> None:
    drive = await fx.drive()
    machine = str(uuid.uuid4())
    _, _, _, _, chat_folder, _ = await _workspace_folder(
        fx, real_session, name=b"Alpha", machine=machine
    )
    _, _, _, other_chats, _, _ = await _workspace_folder(
        fx, real_session, name=b"Beta", machine=machine
    )

    refused = await _patch_parent(
        files_client, real_session, idem, drive.id, chat_folder, other_chats
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.chat_workspace_move"


async def test_a_chat_of_one_cannot_be_moved_into_a_workspace_and_a_plain_folder_can(
    fx: FilesFixtures,
    real_session: AsyncSession,
    files_client: AsyncClient,
    idem: Any,
) -> None:
    drive = await fx.drive()
    _, _, files_id, chats_id, _, _ = await _workspace_folder(
        fx, real_session, name=b"Gamma", machine=str(uuid.uuid4())
    )
    home = await fx.home()
    loose_chat_id = await _object(real_session, fx, type="chat", title="Loose", spec={})
    loose = await fx.node(
        b"Loose.alkerachat",
        kind="folder",
        subtype=CHAT_TYPE,
        target_object_id=loose_chat_id,
        parent=home,
    )
    plain = await fx.node(b"notes", kind="folder", parent=home)
    await real_session.commit()

    refused = await _patch_parent(files_client, real_session, idem, drive.id, loose.id, chats_id)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.chat_workspace_move"

    moved = await _patch_parent(files_client, real_session, idem, drive.id, plain.id, files_id)
    assert moved.status_code == 200, moved.text


# -- who may take, and force off, which kind of lease ------------------------------

LEASE_KIND = "file_lease"


async def _lease_decisions(org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
    """The ``files.lease_kind`` decision rows for one folder, oldest first."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == LEASE_KIND,
                EventOutbox.entity_id == str(node_id),
            )
            .order_by(EventOutbox.id)
        )
        return [row.payload for row in rows.scalars().all()]


def _verdicts(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(row["effect"], row["reason"]) for row in rows]


async def test_the_bound_box_takes_its_workspace_on_record(
    held: Held, files_org: FilesOrgFixture
) -> None:
    rows = await _lease_decisions(files_org.org.org_id, held.folder_id)
    assert ("allow", "bound_box_takes_workspace") in _verdicts(rows)
    allowed = next(row for row in rows if row["reason"] == "bound_box_takes_workspace")
    assert allowed["attrs"]["purpose"] == "workspace"
    assert allowed["attrs"]["runs_here"] is True


async def test_a_person_cannot_take_a_workspace_lease_whatever_their_rung(
    fx: FilesFixtures,
    real_session: AsyncSession,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The org admin who owns the workspace still is not its box."""
    drive = await fx.drive()
    _, folder_id, *_ = await _workspace_folder(
        fx, real_session, name=b"Owned", machine=str(uuid.uuid4())
    )
    refused = await _take(
        files_client,
        real_session,
        idem,
        drive.id,
        folder_id,
        machine_id="laptop",
        instance="laptop:1",
        purpose="workspace",
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.lease_kind_refused"
    assert _verdicts(await _lease_decisions(files_org.org.org_id, folder_id)) == [
        ("deny", "workspace_lease_needs_its_box")
    ]


async def test_a_box_a_workspace_is_not_bound_to_cannot_reach_it(
    fx: FilesFixtures,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    idem: Any,
) -> None:
    """The workspace runs on another box. This box's operator owns the folder
    (it is in their home), which lets the box read it as them; it does not
    make the box the workspace's machine, so the lease is refused as the
    wrong machine's, on record."""
    box_client, machine_id = box
    drive = await fx.drive()
    _, folder_id, *_ = await _workspace_folder(
        fx, real_session, name=b"Elsewhere", machine=str(uuid.uuid4())
    )
    refused = await _take(
        box_client,
        real_session,
        idem,
        drive.id,
        folder_id,
        machine_id=machine_id,
        instance=f"{machine_id}:ws",
        purpose="workspace",
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    async with AsyncSessionLocal() as session:
        reasons = (
            await session.execute(
                select(EventOutbox.payload["reason"].astext).where(
                    EventOutbox.type == "authz.decision",
                    EventOutbox.entity == "file_node",
                    EventOutbox.entity_id == str(folder_id),
                )
            )
        ).scalars()
        assert "machine_mismatch" in set(reasons)


async def _member_client(
    fx: FilesFixtures, files_org: FilesOrgFixture, node_id: uuid.UUID, role: str
) -> AsyncClient:
    node = await fx.folder(node_id)
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, node, Principal(kind="user", id=files_org.member.id), role)
    await fx.repo.session.commit()
    member = app_client()
    return await login(member, files_org.member.email, files_org.member_password)


_EDIT_REFUSED = (403, ("deny", "workspace_lease_needs_full_access"))
_NOT_A_BOX = (403, ("deny", "box_purpose_needs_its_box"))
_ALLOWED = (200, ("allow", "ladder_decides_lease"))


@pytest.mark.parametrize(
    ("role", "purpose", "outcome"),
    [
        pytest.param(ROLE_WRITER, "mount", _EDIT_REFUSED, id="can-edit-mount"),
        pytest.param(ROLE_WRITER, "box", _EDIT_REFUSED, id="can-edit-box"),
        pytest.param(ROLE_WRITER, "share", _EDIT_REFUSED, id="can-edit-share"),
        pytest.param(ROLE_WRITER, "chat", _NOT_A_BOX, id="can-edit-chat"),
        pytest.param(ROLE_MANAGER, "mount", _ALLOWED, id="full-access-mount"),
        pytest.param(ROLE_MANAGER, "chat", _NOT_A_BOX, id="full-access-chat"),
    ],
)
async def test_a_collaborator_who_may_only_edit_cannot_park_a_workspace(
    fx: FilesFixtures,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    idem: Any,
    role: str,
    purpose: str,
    outcome: tuple[int, tuple[str, str]],
) -> None:
    """A lease anywhere in a workspace's tree stops its box from taking it
    when its chats wake: a person's own lease takes the rung that may also
    force a lease off, and a box's purpose (``chat``) is never a person's,
    whatever their rung."""
    drive = await fx.drive()
    _, folder_id, files_id, *_ = await _workspace_folder(
        fx, real_session, name=b"Shared", machine=str(uuid.uuid4())
    )
    member = await _member_client(fx, files_org, folder_id, role)
    async with member:
        answer = await _take(
            member,
            real_session,
            idem,
            drive.id,
            files_id,
            machine_id="laptop",
            instance="laptop:1",
            purpose=purpose,
        )
    status_code, verdict = outcome
    assert answer.status_code == status_code, answer.text
    assert _verdicts(await _lease_decisions(files_org.org.org_id, files_id))[-1] == verdict


async def _force(
    client: AsyncClient,
    session: AsyncSession,
    idem: Any,
    held: Held,
    body: dict[str, Any] | None,
) -> Any:
    return await client.post(
        f"{_item(held.drive_id, held.folder_id)}/lease/force-release",
        json=body,
        headers={**idem(), "If-Match": await node_etag(session, held.folder_id)},
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(None, id="no-body"),
        pytest.param({}, id="no-reason"),
        pytest.param({"reason": "   "}, id="a-blank-reason"),
    ],
)
async def test_forcing_a_boxs_workspace_lease_off_without_a_reason_says_what_is_missing(
    held: Held,
    real_session: AsyncSession,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    idem: Any,
    body: dict[str, Any] | None,
) -> None:
    """The owner may force the box off, and has not said why: that is a
    request missing what it needs (422, with words), not a bare refusal of
    who they are. Nothing is forced and nothing is decided."""
    before = await _lease_decisions(files_org.org.org_id, held.folder_id)

    refused = await _force(files_client, real_session, idem, held, body)

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "files.force_reason_required"
    assert "why" in refused.json()["message"]
    assert await _lease_decisions(files_org.org.org_id, held.folder_id) == before
    assert (await _lease_row(real_session, held.folder_id)).released_at is None


async def test_forcing_a_boxs_workspace_lease_off_records_why(
    held: Held,
    real_session: AsyncSession,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    idem: Any,
) -> None:
    """The reason itself goes on the decision row, capped, so whoever reads
    the record later knows why a turn in flight was cut off."""
    long_reason = "the box is wedged on a deploy; " + "x" * 600

    forced = await _force(files_client, real_session, idem, held, {"reason": long_reason[:500]})

    assert forced.status_code == 200, forced.text
    decision = (await _lease_decisions(files_org.org.org_id, held.folder_id))[-1]
    assert (decision["effect"], decision["reason"]) == ("allow", "owner_forces_box_lease")
    recorded = decision["attrs"]["reason"]
    assert recorded.startswith("the box is wedged on a deploy")
    assert len(recorded) == FORCE_REASON_RECORDED
