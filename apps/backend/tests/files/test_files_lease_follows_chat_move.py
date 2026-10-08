"""A chat's folder lease follows the chat when placement moves it to another box.

A chat bound to a box that can no longer serve it — draining, gone quiet,
asleep, off the plane — is moved to a live box on its next message, and by the
next box that comes up. The box it moved off still holds the folder's lease.
Two things used to go wrong from that instant, both driven here through the
real routes with a real Postgres behind them:

* the departed box, if it was still alive and finishing, was a stranger to the
  folder the moment the binding moved: its item read, its lease heartbeat, its
  final push and its release all answered the opaque 404, so the lease it
  could not give back held the new box off for a whole TTL;
* a departed box that was dead — no heartbeat, asleep, released — was never
  going to release anything, and nothing did it for it.

Now the Files decider keeps a proven live-lease holder its machine rung on the
folder it holds, so a live departing box finishes and lets go; and the move
itself ends the lease of a box that cannot, in the same transaction that
rebinds the chat. Either way the new box's take is admitted at once.

Two box shapes, because they reach the drive differently: an org's own box on
its operator's session (an agent whose operator may own the chat), and a pool
box on its own machine credential (a member of no org, admitted to a chat's
folder only as its machine) — the shape the shared pool runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _files_kit import node_etag
from alkera_core.compute.machines import DRAINING as DRAINING_STATE
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.compute import DEDICATED_TENANCY, POOL_TENANCY, ComputeAllocation
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login
from tests.test_chat_machine_binding_seam import _browser, _daemon_headers, _heartbeat, _register

# compute_rows, not the fleet group: these tests bring live allocations onto the
# plane but run no fleet-wide pass (see COMPUTE_FLEET_GROUP in apps/backend/tests/conftest.py),
# and the shared group name belongs to the fleet modules' own directory.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("files_on"),
    pytest.mark.compute_rows,
]

PREFIX = "/api/v1/files"


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A beating box stays ``ready`` for the length of the case however slow
    the runner is; the cases about a box going quiet age its heartbeat past
    this window explicitly."""
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


@pytest.fixture(autouse=True)
async def _quiet_the_pool() -> None:
    """The pool is global: a pool box another test left live would be the
    answer for this test's org. Every platform box is retired first."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ComputeAllocation)
            .where(ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)))
            .where(ComputeAllocation.state != "released")
            .values(state="released", released_at=datetime.now(UTC))
        )
        await session.commit()


def _item(drive_id: str, node_id: str) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


class _Box:
    """One box driving the lease routes as the CLI's mount does.

    ``headers`` is how the box authenticates: an org box sends the device token
    it registered with plus the assertion naming its machine; a pool box sends
    its machine credential and nothing else.
    """

    def __init__(
        self,
        machine_id: str,
        headers: dict[str, str],
        idem: Callable[[], dict[str, str]],
    ) -> None:
        self.machine_id = machine_id
        self.headers = headers
        self.instance = f"{machine_id}:chat"
        self.epoch: int | None = None
        self._idem = idem

    @property
    def fence(self) -> dict[str, str]:
        assert self.epoch is not None, "the box has not taken the folder"
        return {"X-Alkera-Lease-Epoch": str(self.epoch), "X-Alkera-Lease-Instance": self.instance}

    def replaying(self, other: _Box) -> _Box:
        """This box, speaking another holder's fence."""
        twin = _Box(self.machine_id, self.headers, self._idem)
        twin.epoch, twin.instance = other.epoch, other.instance
        return twin

    async def acquire(self, session: AsyncSession, drive_id: str, node_id: str) -> Any:
        async with _browser() as client:
            response = await client.post(
                f"{_item(drive_id, node_id)}/lease",
                json={"instanceId": self.instance, "machineId": self.machine_id, "purpose": "chat"},
                headers={
                    **self.headers,
                    **self._idem(),
                    "If-Match": await node_etag(session, uuid.UUID(node_id)),
                },
            )
        if response.status_code == 200:
            self.epoch = int(response.json()["epoch"])
        return response

    async def read_item(self, drive_id: str, node_id: str, *, fenced: bool = True) -> Any:
        async with _browser() as client:
            return await client.get(
                _item(drive_id, node_id), headers={**self.headers, **(self.fence if fenced else {})}
            )

    async def heartbeat(self, drive_id: str, node_id: str, *, fenced: bool = True) -> Any:
        async with _browser() as client:
            return await client.post(
                f"{_item(drive_id, node_id)}/lease/heartbeat",
                json={"epoch": self.epoch, "instanceId": self.instance},
                headers={**self.headers, **(self.fence if fenced else {})},
            )

    async def release(self, session: AsyncSession, drive_id: str, node_id: str) -> Any:
        async with _browser() as client:
            return await client.post(
                f"{_item(drive_id, node_id)}/lease/release",
                json={"epoch": self.epoch, "instanceId": self.instance, "final": None},
                headers={
                    **self.headers,
                    **self.fence,
                    **self._idem(),
                    "If-Match": await node_etag(session, uuid.UUID(node_id)),
                },
            )


async def _lease_row(node_id: str) -> Any:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT holder_principal_id, epoch, released_at FROM file_leases "
                    "WHERE node_id = :n ORDER BY epoch DESC LIMIT 1"
                ),
                {"n": uuid.UUID(node_id)},
            )
        ).first()


async def _set_machine(machine_id: str, **columns: Any) -> None:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, uuid.UUID(machine_id))
        assert alloc is not None
        for name, value in columns.items():
            setattr(alloc, name, value)
        await session.commit()


async def _open_chat(org: OrgWithAdmin) -> dict[str, Any]:
    async with _browser() as browser:
        await login(browser, org.admin_email, org.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "moves with its folder"})
    assert created.status_code == 201, created.text
    chat: dict[str, Any] = created.json()
    assert chat["files_node_id"] and chat["files_drive_id"], "the chat has a folder to lease"
    return chat


async def _send(org: OrgWithAdmin, chat_id: str, text_: str) -> dict[str, Any]:
    async with _browser() as browser:
        await login(browser, org.admin_email, org.admin_password)
        sent = await browser.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": text_, "client_id": uuid.uuid4().hex}
        )
        assert sent.status_code == 201, sent.text
        reread = await browser.get(f"/api/v1/chats/{chat_id}")
    assert reread.status_code == 200, reread.text
    body: dict[str, Any] = reread.json()
    return body


async def _read(org: OrgWithAdmin, chat_id: str) -> dict[str, Any]:
    async with _browser() as browser:
        await login(browser, org.admin_email, org.admin_password)
        reread = await browser.get(f"/api/v1/chats/{chat_id}")
    assert reread.status_code == 200, reread.text
    body: dict[str, Any] = reread.json()
    return body


def _dead(machine_id: str, how: str) -> Any:
    """Make the box unable to answer: its heartbeat past the window, or asleep."""
    if how == "stopped-beating":
        return _set_machine(machine_id, last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=20))
    return _set_machine(machine_id, state="asleep")


# ---------------------------------------------------------------------------
# An org's own boxes, registered through the routes on the operator's session
# ---------------------------------------------------------------------------


async def _org_scene(
    session: AsyncSession, org: OrgWithAdmin, idem: Any, *, pod: str
) -> tuple[str, dict[str, Any], _Box]:
    """The org with compute, its first box up and beating, a chat opened on it
    and the box holding the chat's folder: ``(machine type code, chat, box)``."""
    machine_type = await make_machine_type(session)
    await make_grant(session, org_team_id=org.org_id, machine_type_id=machine_type.id, ceiling=2)
    first = await _register(org, machine_type.provider_type_id, pod)
    await _heartbeat(org, first)
    chat = await _open_chat(org)
    assert chat["machine_id"] == first
    box = _Box(first, await _daemon_headers(org, agent_id=first), idem)
    granted = await box.acquire(session, chat["files_drive_id"], chat["files_node_id"])
    assert granted.status_code == 200, granted.text
    return machine_type.provider_type_id, chat, box


async def _second_org_box(org: OrgWithAdmin, code: str, idem: Any, *, pod: str) -> _Box:
    second = await _register(org, code, pod)
    await _heartbeat(org, second)
    return _Box(second, await _daemon_headers(org, agent_id=second), idem)


async def test_a_draining_org_box_hands_back_the_folder_of_a_chat_that_moved_off_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, idem: Any
) -> None:
    """The box is alive and mid hand-back when the chat's next message moves
    the chat to the org's other box. Its item read, its heartbeat and its
    release keep landing as the holder's, with its fence — and the box the
    chat is now bound to still cannot beat that lease by replaying it. The new
    box's own take is refused while the lease stands and admitted the moment
    the release commits."""
    code, chat, box_a = await _org_scene(real_session, org_admin, idem, pod="pod-move-a")
    drive_id, node_id = chat["files_drive_id"], chat["files_node_id"]
    box_b = await _second_org_box(org_admin, code, idem, pod="pod-move-b")
    await _set_machine(box_a.machine_id, state=DRAINING_STATE)

    moved = await _send(org_admin, chat["id"], "are you still there?")
    assert moved["machine_id"] == box_b.machine_id

    row = await _lease_row(node_id)
    assert row is not None and row.released_at is None, "a beating holder keeps its lease"
    read = await box_a.read_item(drive_id, node_id)
    assert read.status_code == 200, read.text
    assert read.json()["trashed"] is False, "the folder is where it was; only the chat moved"
    assert (await box_a.heartbeat(drive_id, node_id)).status_code == 200

    unfenced = await box_a.heartbeat(drive_id, node_id, fenced=False)
    assert unfenced.status_code == 403, unfenced.text
    replayed = await box_b.replaying(box_a).heartbeat(drive_id, node_id)
    assert replayed.status_code == 409, replayed.text
    assert replayed.json()["code"] == "files.lease_fenced"
    busy = await box_b.acquire(real_session, drive_id, node_id)
    assert busy.status_code == 409, busy.text

    released = await box_a.release(real_session, drive_id, node_id)
    assert released.status_code == 200, released.text

    taken = await box_b.acquire(real_session, drive_id, node_id)
    assert taken.status_code == 200, taken.text
    assert taken.json()["epoch"] > box_a.epoch


async def test_a_move_off_an_org_box_that_stopped_beating_releases_its_lease_at_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin, idem: Any
) -> None:
    """A box that has stopped heartbeating will never hand the folder back.
    The message that moves the chat ends that lease in the same transaction,
    so the new box's take is admitted at once rather than after the TTL."""
    code, chat, box_a = await _org_scene(real_session, org_admin, idem, pod="pod-move-c")
    drive_id, node_id = chat["files_drive_id"], chat["files_node_id"]
    box_b = await _second_org_box(org_admin, code, idem, pod="pod-move-d")
    await _dead(box_a.machine_id, "stopped-beating")

    moved = await _send(org_admin, chat["id"], "hello?")
    assert moved["machine_id"] == box_b.machine_id

    row = await _lease_row(node_id)
    assert row is not None and str(row.holder_principal_id) == box_a.machine_id
    assert row.released_at is not None, "the move ended the departed box's lease"
    taken = await box_b.acquire(real_session, drive_id, node_id)
    assert taken.status_code == 200, taken.text
    assert taken.json()["epoch"] > box_a.epoch


async def test_a_chat_that_stays_put_keeps_its_boxs_lease(
    real_session: AsyncSession, org_admin: OrgWithAdmin, idem: Any
) -> None:
    """The asymmetric case: a message on a chat whose box still answers moves
    nothing and ends no lease — the box is mid-turn on that very folder."""
    _code, chat, box_a = await _org_scene(real_session, org_admin, idem, pod="pod-move-e")
    drive_id, node_id = chat["files_drive_id"], chat["files_node_id"]

    stayed = await _send(org_admin, chat["id"], "carry on")
    assert stayed["machine_id"] == box_a.machine_id

    row = await _lease_row(node_id)
    assert row is not None and row.released_at is None
    assert (await box_a.heartbeat(drive_id, node_id)).status_code == 200


async def test_the_box_that_comes_up_finds_a_dead_boxs_folder_free(
    real_session: AsyncSession, org_admin: OrgWithAdmin, idem: Any
) -> None:
    """The other placement moment: a box registering and beating takes every
    stranded chat of the org. A chat stranded on a released box whose lease
    was still live is moved AND has that lease ended, so the box that came up
    takes the folder on its first try."""
    code, chat, box_a = await _org_scene(real_session, org_admin, idem, pod="pod-move-f")
    drive_id, node_id = chat["files_drive_id"], chat["files_node_id"]
    await _set_machine(box_a.machine_id, state="released", released_at=datetime.now(UTC))

    box_b = await _second_org_box(org_admin, code, idem, pod="pod-move-g")

    assert (await _read(org_admin, chat["id"]))["machine_id"] == box_b.machine_id
    row = await _lease_row(node_id)
    assert row is not None and row.released_at is not None
    taken = await box_b.acquire(real_session, drive_id, node_id)
    assert taken.status_code == 200, taken.text


# ---------------------------------------------------------------------------
# Pool boxes on their own machine credential — the shared pool's shape
# ---------------------------------------------------------------------------


async def _pool_box(session: AsyncSession, org: OrgWithAdmin, idem: Any, *, name: str) -> _Box:
    """A ready, beating gVisor pool box held by a live machine credential, the
    way ``/machines/claim`` leaves one; it speaks with that credential alone."""
    machine_type = await make_machine_type(session)
    now = datetime.now(UTC)
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        origin="registered",
        name=name,
        tenancy=POOL_TENANCY,
        sandbox="gvisor",
        state="ready",
        provider_machine_id=f"pod-{name}",
        created_at=now,
        ready_at=now,
        last_metered_at=now,
        last_heartbeat_at=now,
        capacity=6,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=1,
    )
    session.add(alloc)
    await session.commit()
    credential = await hold_with_credential(session, alloc)
    return _Box(str(alloc.id), {"Authorization": f"Bearer {credential}"}, idem)


async def _pool_scene(
    session: AsyncSession, org: OrgWithAdmin, idem: Any, *, name: str
) -> tuple[dict[str, Any], _Box]:
    """One pool box up, a chat placed on it, the box holding the chat's folder."""
    box = await _pool_box(session, org, idem, name=name)
    chat = await _open_chat(org)
    assert chat["machine_id"] == box.machine_id
    granted = await box.acquire(session, chat["files_drive_id"], chat["files_node_id"])
    assert granted.status_code == 200, granted.text
    return chat, box


async def test_a_draining_pool_box_hands_back_the_folder_of_a_chat_that_moved_off_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, idem: Any
) -> None:
    """A pool box is a member of no org: nothing but the chat's binding, or
    the folder's lease, admits it to the folder. Once the chat has moved, the
    binding is gone and the lease is all it has — so its fenced reads and its
    release land, and the same requests without the fence are the opaque 404
    a stranger gets."""
    chat, box_a = await _pool_scene(real_session, org_admin, idem, name="pool-a")
    drive_id, node_id = chat["files_drive_id"], chat["files_node_id"]
    box_b = await _pool_box(real_session, org_admin, idem, name="pool-b")
    await _set_machine(box_a.machine_id, state=DRAINING_STATE)

    moved = await _send(org_admin, chat["id"], "are you still there?")
    assert moved["machine_id"] == box_b.machine_id

    read = await box_a.read_item(drive_id, node_id)
    assert read.status_code == 200, read.text
    assert read.json()["trashed"] is False
    assert (await box_a.read_item(drive_id, node_id, fenced=False)).status_code == 404, (
        "without its fence the departed box proves nothing and is nobody here"
    )
    assert (await box_a.heartbeat(drive_id, node_id)).status_code == 200
    assert (await box_b.acquire(real_session, drive_id, node_id)).status_code == 409

    released = await box_a.release(real_session, drive_id, node_id)
    assert released.status_code == 200, released.text
    assert (await box_a.read_item(drive_id, node_id)).status_code == 404, (
        "the rung went back with the lease"
    )
    taken = await box_b.acquire(real_session, drive_id, node_id)
    assert taken.status_code == 200, taken.text


@pytest.mark.parametrize("how", ["stopped-beating", "asleep"])
async def test_a_move_off_a_pool_box_that_cannot_answer_releases_its_lease_at_once(
    real_session: AsyncSession, org_admin: OrgWithAdmin, idem: Any, how: str
) -> None:
    """A pool box is interchangeable: one that stopped beating or was put to
    sleep has its chat moved to a pool box that is up, and the lease it will
    never release goes with the move."""
    chat, box_a = await _pool_scene(real_session, org_admin, idem, name=f"pool-{how}-a")
    drive_id, node_id = chat["files_drive_id"], chat["files_node_id"]
    box_b = await _pool_box(real_session, org_admin, idem, name=f"pool-{how}-b")
    await _dead(box_a.machine_id, how)

    moved = await _send(org_admin, chat["id"], "hello?")
    assert moved["machine_id"] == box_b.machine_id

    row = await _lease_row(node_id)
    assert row is not None and str(row.holder_principal_id) == box_a.machine_id
    assert row.released_at is not None
    taken = await box_b.acquire(real_session, drive_id, node_id)
    assert taken.status_code == 200, taken.text
    assert taken.json()["epoch"] > box_a.epoch
