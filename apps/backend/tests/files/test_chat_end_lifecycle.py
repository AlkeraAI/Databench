"""Every way a chat stops being served goes through one transition.

The box running a chat holds its folder's lease. Whatever ends the chat's
service — the box putting it to sleep, a person deleting it, the box going to
sleep or leaving the plane, a move to another box, the owner losing access, a
spare being reaped — must end that lease server-side, in the same transaction,
so the old holder is fenced out, and must leave the chat reading asleep with its
``end_seq`` moved, so the box drops it without pushing.

Each ending is driven through its real entry point (the route or the service a
production caller uses), and each is held to the same four facts:

* the lease row is RELEASED — stamped, not merely past its deadline;
* the old holder's heartbeat is refused, and so is its fenced write;
* its re-take is refused rather than re-granted;
* the chat reads asleep and says why.

The box in these tests is the org admin's own, on the admin's session with a
proven machine assertion — the shape of an org box on its operator's session,
whose writes the owner's own rung on the folder would otherwise keep admitting.
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
from alkera_core.compute.handoff import leave_placement
from alkera_core.files import objects_bridge
from alkera_core.files.objects_bridge import CHAT_TYPE
from alkera_core.models import ComputeAllocation, TeamRole, User
from alkera_core.objects import chat_end, chat_spares
from alkera_core.objects.chat_end import ChatEndReason
from backend.services.chats import chat_service
from backend.services.compute import placement, provisioning
from backend.services.identity import users as user_service
from backend.services.org import org_memberships as org_membership_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import make_member
from tests.files._boxes import bind_chat, registered_box

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


@dataclass(frozen=True)
class Running:
    """A chat whose folder the box holds, awake."""

    drive_id: uuid.UUID
    chat_id: uuid.UUID
    folder_id: uuid.UUID
    machine_id: str
    epoch: int
    instance: str
    box: AsyncClient

    @property
    def fence(self) -> dict[str, str]:
        return {"X-Alkera-Lease-Epoch": str(self.epoch), "X-Alkera-Lease-Instance": self.instance}


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


async def _take(running: Running, session: AsyncSession, idem: Any, *, retake: bool) -> Any:
    return await running.box.post(
        f"{_item(running.drive_id, running.folder_id)}/lease",
        json={
            "instanceId": running.instance,
            "machineId": running.machine_id,
            "purpose": "chat",
            "retake": retake,
        },
        headers={**idem(), "If-Match": await node_etag(session, running.folder_id)},
    )


async def _fenced_write(running: Running, idem: Any, name: str) -> Any:
    return await running.box.post(
        f"{_item(running.drive_id, running.folder_id)}/children",
        json={"name": name, "kind": "folder"},
        headers={**idem(), **running.fence},
    )


async def _beat(running: Running) -> Any:
    return await running.box.post(
        f"{_item(running.drive_id, running.folder_id)}/lease/heartbeat",
        json={"epoch": running.epoch, "instanceId": running.instance},
    )


@pytest_asyncio.fixture
async def running(
    fx: FilesFixtures, real_session: AsyncSession, box: tuple[AsyncClient, str], idem: Any
) -> Running:
    box_client, machine_id = box
    drive = await fx.drive()
    folder = await fx.node(
        b"Kickoff.alkerachat", kind="folder", subtype=CHAT_TYPE, parent=await fx.home()
    )
    await bind_chat(fx, real_session, folder, machine=machine_id)
    chat_id = (
        await real_session.execute(
            text("SELECT target_object_id FROM file_nodes WHERE id = :n"), {"n": folder.id}
        )
    ).scalar_one()
    await real_session.execute(
        text(
            'UPDATE workspace_objects SET spec = spec || \'{"mirror_state": "awake"}\'::jsonb '
            "WHERE id = :c"
        ),
        {"c": chat_id},
    )
    await real_session.commit()
    instance = f"{machine_id}:chat"
    chat = Running(
        drive_id=drive.id,
        chat_id=uuid.UUID(str(chat_id)),
        folder_id=folder.id,
        machine_id=machine_id,
        epoch=0,
        instance=instance,
        box=box_client,
    )
    granted = await _take(chat, real_session, idem, retake=False)
    assert granted.status_code == 200, granted.text
    chat = Running(**{**chat.__dict__, "epoch": int(granted.json()["epoch"])})
    # The control every ending is measured against: while the chat runs, the
    # holder beats and writes under its fence.
    assert (await _beat(chat)).status_code == 200
    wrote = await _fenced_write(chat, idem, "before")
    assert wrote.status_code in (200, 201), wrote.text
    return chat


async def _row(session: AsyncSession, running: Running) -> Any:
    row = (
        await session.execute(
            text(
                "SELECT l.released_at, l.reaped_at, l.expires_at > now() AS unexpired, "
                "o.spec->>'mirror_state' AS mirror_state, "
                "COALESCE((o.spec->>'end_seq')::int, 0) AS end_seq, "
                "o.spec->>'ended_reason' AS ended_reason "
                "FROM file_leases l, workspace_objects o "
                "WHERE l.node_id = :node AND o.id = :chat"
            ),
            {"node": running.folder_id, "chat": running.chat_id},
        )
    ).one()
    await session.commit()
    return row


async def _assert_ended(
    session: AsyncSession,
    running: Running,
    idem: Any,
    reason: ChatEndReason,
    *,
    asleep: bool | None = True,
    retake_refusal: str = "files.lease_ended",
    bound_elsewhere: bool = False,
) -> None:
    """``asleep`` None skips the session word (a move records none);
    ``bound_elsewhere`` is a box the chat was moved off, which the drive now
    refuses outright as a stranger to the chat before any lease is consulted."""
    row = await _row(session, running)
    assert row.released_at is not None, "the lease must be released, not left to lapse"
    assert row.reaped_at is None
    assert row.unexpired, "released on the spot, while its TTL still ran"
    assert (row.end_seq, row.ended_reason) == (1, reason.value)
    if asleep is not None:
        assert (row.mirror_state == "asleep") is asleep

    fenced = {(409, "files.lease_fenced")}
    if bound_elsewhere:
        fenced.add((403, "files.forbidden"))
    beat = await _beat(running)
    assert (beat.status_code, beat.json()["code"]) in fenced, beat.text
    late = await _fenced_write(running, idem, "after")
    assert (late.status_code, late.json()["code"]) in fenced | {(409, "files.trashed")}, late.text
    retake = await _take(running, session, idem, retake=True)
    refused = {(409, retake_refusal)} | ({(403, "files.forbidden")} if bound_elsewhere else set())
    assert (retake.status_code, retake.json()["code"]) in refused, retake.text


# ---------------------------------------------------------------------------
# The box's own sleep: the hand-back IS the transition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", [ChatEndReason.IDLE, ChatEndReason.EVICTED, ChatEndReason.DRAINED], ids=str
)
async def test_the_boxs_hand_back_with_its_reason_ends_the_chat(
    real_session: AsyncSession, running: Running, idem: Any, reason: ChatEndReason
) -> None:
    released = await running.box.post(
        f"{_item(running.drive_id, running.folder_id)}/lease/release",
        json={
            "epoch": running.epoch,
            "instanceId": running.instance,
            "final": [],
            "ending": reason.value,
        },
        headers={
            **idem(),
            **running.fence,
            "If-Match": await node_etag(real_session, running.folder_id),
        },
    )
    assert released.status_code in (200, 204), released.text

    await _assert_ended(real_session, running, idem, reason)


async def test_a_box_that_reports_the_chat_asleep_without_a_hand_back_ends_the_lease(
    real_session: AsyncSession, running: Running, idem: Any
) -> None:
    """The box lost the folder on its own disk, or its release never landed: the
    sleep report is the same transition, so the lease still goes."""
    reported = await running.box.put(
        f"/api/v1/chats/{running.chat_id}/publisher-state",
        json={"state": "asleep", "ending": "idle"},
    )
    assert reported.status_code == 200, reported.text

    await _assert_ended(real_session, running, idem, ChatEndReason.IDLE)


# ---------------------------------------------------------------------------
# Deletion: asleep first, then the trash — which never meets a live lease
# ---------------------------------------------------------------------------


async def test_deleting_an_awake_chat_ends_its_lease_before_the_trash_runs(
    files_client: AsyncClient,
    real_session: AsyncSession,
    running: Running,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted INSIDE the delete's transaction, at the moment the folder is
    trashed: the lease is already released there, so the trash's own guard has
    nothing to end."""
    seen: list[bool] = []
    tombstone = objects_bridge.tombstone_for_object

    async def watching(repo: Any, ctx: Any, object_id: uuid.UUID, **kwargs: Any) -> Any:
        live = (
            await repo.session.execute(
                text(
                    "SELECT released_at IS NULL AND reaped_at IS NULL AND expires_at > now() "
                    "FROM file_leases WHERE node_id = :node"
                ),
                {"node": running.folder_id},
            )
        ).scalar_one()
        seen.append(bool(live))
        return await tombstone(repo, ctx, object_id, **kwargs)

    monkeypatch.setattr(objects_bridge, "tombstone_for_object", watching)

    deleted = await files_client.delete(f"/api/v1/chats/{running.chat_id}")

    assert deleted.status_code == 204, deleted.text
    assert seen == [False], "the trash ran while the box still held the chat's folder"
    await _assert_ended(
        real_session, running, idem, ChatEndReason.DELETED, retake_refusal="files.trashed"
    )


async def test_reaping_a_spare_ends_its_lease_and_takes_its_folder_at_once(
    real_session: AsyncSession, running: Running
) -> None:
    """A spare nobody claimed is a deleted chat: its lease ends first, so the
    purge beside it takes the folder now instead of on a later sweep."""
    await real_session.execute(
        text(
            "UPDATE workspace_objects SET spec = spec || '{\"spare\": true}'::jsonb WHERE id = :c"
        ),
        {"c": running.chat_id},
    )
    await real_session.commit()
    chat = await chat_end.lock_chat(real_session, running.chat_id)
    assert chat is not None

    await chat_spares.reap(real_session, chat)
    await real_session.commit()

    gone = (
        await real_session.execute(
            text("SELECT count(*) FROM file_nodes WHERE id = :n"), {"n": running.folder_id}
        )
    ).scalar_one()
    assert gone == 0, "the reaped spare's folder was left for a later sweep"


# ---------------------------------------------------------------------------
# The box goes away, or the chat does
# ---------------------------------------------------------------------------


async def test_putting_the_box_to_sleep_ends_every_chat_it_held(
    real_session: AsyncSession, files_org: FilesOrgFixture, running: Running, idem: Any
) -> None:
    await real_session.execute(
        text("UPDATE compute_allocations SET state = 'ready' WHERE id = :m"),
        {"m": running.machine_id},
    )
    await real_session.commit()
    alloc = await real_session.get(ComputeAllocation, uuid.UUID(running.machine_id))
    caller = await real_session.get(User, files_org.org.admin_id)
    assert alloc is not None and caller is not None

    await provisioning.sleep(real_session, alloc, caller=caller)
    await real_session.commit()

    await _assert_ended(real_session, running, idem, ChatEndReason.BOX_ASLEEP)


async def test_the_box_leaving_the_plane_ends_every_chat_it_held(
    real_session: AsyncSession, running: Running, idem: Any
) -> None:
    alloc = await real_session.get(ComputeAllocation, uuid.UUID(running.machine_id))
    assert alloc is not None

    await leave_placement(real_session, alloc, actor=None)
    await real_session.commit()

    await _assert_ended(real_session, running, idem, ChatEndReason.BOX_LOST)


async def _move_off(session: AsyncSession, running: Running, *, departed_state: str) -> bool:
    await session.execute(
        text(
            "UPDATE compute_allocations SET state = :state, last_heartbeat_at = now() WHERE id = :m"
        ),
        {"state": departed_state, "m": running.machine_id},
    )
    await session.commit()
    chat = await chat_end.lock_chat(session, running.chat_id)
    assert chat is not None
    # The move as placement makes it: the row is rebound first, then the
    # departed box's service ends.
    moved = await chat_service.rebind_machine(
        session, chat=chat, machine_id=str(uuid.uuid4()), machine_status="ready"
    )
    ended = await placement.hand_over_folder(session, chat=moved, departed=running.machine_id)
    await session.commit()
    return ended


async def test_a_move_off_a_box_that_cannot_hand_back_ends_its_lease(
    real_session: AsyncSession, running: Running, idem: Any
) -> None:
    assert await _move_off(real_session, running, departed_state="asleep")

    await _assert_ended(
        real_session, running, idem, ChatEndReason.MOVED, asleep=False, bound_elsewhere=True
    )


async def test_a_move_off_a_box_still_serving_leaves_it_the_lease_to_finish(
    real_session: AsyncSession, running: Running, idem: Any
) -> None:
    """The asymmetric half of a move: a draining box hands the folder back
    itself — its last push is the only copy of its last turn — so its lease
    is spared and the box is not told to stop."""
    assert not await _move_off(real_session, running, departed_state="ready")

    row = await _row(real_session, running)
    assert row.released_at is None
    assert row.end_seq == 0
    # Its fenced push still lands: the lease it finishes under is its own.
    finishing = await _fenced_write(running, idem, "finishing")
    assert finishing.status_code in (200, 201), finishing.text


@pytest.mark.parametrize("how", ["platform-disabled", "membership-deactivated"])
async def test_the_owner_losing_access_ends_their_chats(
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    running: Running,
    idem: Any,
    how: str,
) -> None:
    owner = await real_session.get(User, files_org.org.admin_id)
    assert owner is not None
    if how == "platform-disabled":
        await user_service.set_active(real_session, owner, False)
    else:
        # The org's own offboarding, on the membership. Another admin keeps the
        # org administrable, which the deactivation insists on.
        await make_member(real_session, org_id=files_org.org.org_id, role=TeamRole.ADMIN)
        membership = await org_membership_service.get(
            real_session, user_id=owner.id, org_team_id=files_org.org.org_id
        )
        assert membership is not None
        await org_membership_service.deactivate(real_session, membership, actor=None)
    await real_session.commit()
    row = await _row(real_session, running)
    assert row.released_at is not None
    assert (row.mirror_state, row.ended_reason) == ("asleep", "access_removed")


# ---------------------------------------------------------------------------
# The transition itself, for every registered reason
# ---------------------------------------------------------------------------


async def test_every_reason_has_a_registered_ending() -> None:
    assert set(chat_end.registered()) == set(ChatEndReason)


async def test_an_unregistered_reason_is_refused() -> None:
    with pytest.raises(ValueError, match="no chat ending"):
        chat_end.ending_for("vanished")


async def test_only_a_move_may_spare_a_serving_holder(
    real_session: AsyncSession, running: Running
) -> None:
    with pytest.raises(ValueError, match="ends every lease"):
        await chat_end.end_chat(
            real_session,
            running.chat_id,
            ChatEndReason.IDLE,
            actor=None,
            spare_serving_holder=uuid.UUID(running.machine_id),
        )
    await real_session.rollback()


@pytest.mark.parametrize("reason", list(ChatEndReason), ids=str)
async def test_the_transition_ends_the_lease_and_fences_the_holder_for_every_reason(
    real_session: AsyncSession, running: Running, idem: Any, reason: ChatEndReason
) -> None:
    ended = await chat_end.end_chat(real_session, running.chat_id, reason, actor=None)
    await real_session.commit()

    assert ended.released == (running.folder_id,)
    await _assert_ended(
        real_session,
        running,
        idem,
        reason,
        asleep=True if chat_end.ending_for(reason).records_asleep else None,
    )


async def test_ending_an_ended_chat_changes_nothing(
    real_session: AsyncSession, running: Running
) -> None:
    await chat_end.end_chat(real_session, running.chat_id, ChatEndReason.IDLE, actor=None)
    await real_session.commit()

    again = await chat_end.end_chat(real_session, running.chat_id, ChatEndReason.IDLE, actor=None)
    await real_session.commit()

    assert (again.released, again.changed) == ((), False)
    assert (await _row(real_session, running)).end_seq == 1
