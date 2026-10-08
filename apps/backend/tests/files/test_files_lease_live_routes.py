"""The live plane through the real routes, driven by the holder's own double.

What a lease means here is a live computer holding the files: the box takes the
chat's folder, is told at what cadence to report, reports, drains what people
dropped into the chat while it ran, and settles what it applied. Every test
below drives that through ``httpx`` against the mounted app with a real
Postgres behind it — the same requests the mirror in the CLI sends — so what is
asserted is the status contract and the rows a client can actually observe.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from _live_holder import MockHolder
from alkera_core.authz.headers import agent_headers
from alkera_core.config import settings
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.models.event_outbox import EventOutbox
from asyncpg.exceptions import DeadlockDetectedError
from backend.services.org import storage_limits as storage_limit_service
from blake3 import blake3
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin
from tests.files._boxes import bind_chat as _bind_chat
from tests.files._boxes import registered_box as _registered_box

from alkera_core.db.session import AsyncSessionLocal  # isort: skip

PREFIX = "/api/v1/files"

pytestmark = pytest.mark.usefixtures("files_on")


# ---------------------------------------------------------------------------
# The world these tests run in
# ---------------------------------------------------------------------------


async def _room(fx: Any) -> Any:
    """A drive with the product's default ceilings — a drive is born with none,
    and a five-byte upload into one is a 507 for reasons nothing here is about."""
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    return drive


async def _awake_chat(fx: Any, session: AsyncSession, name: bytes) -> tuple[Any, Any]:
    """A chat folder as the bridge mints it — the object behind the node is what
    makes it a chat, never its name — holding its ``scratch/`` working directory."""
    # Under the actor's home, where the product files a chat: the org-admin
    # floor stops at a chat's folder, so a chat seeded at the drive root would
    # be one its own operator could not open.
    chat = await fx.node(name, kind="folder", parent=await fx.home())
    scratch = await fx.node(b"scratch", kind="folder", parent=chat)
    await session.execute(
        text("UPDATE file_nodes SET subtype = 'chat', target_object_id = :object WHERE id = :node"),
        {"object": uuid.uuid4(), "node": chat.id},
    )
    await session.commit()
    return chat, scratch


async def _drop(
    client: AsyncClient, parent_id: uuid.UUID, name: str, payload: bytes, idem: Any
) -> Any:
    """What the composer does: an unfenced upload aimed at the chat node."""
    opened = await client.post(
        f"{PREFIX}/uploads",
        json={"declaredSize": len(payload), "name": name, "parentId": str(parent_id)},
        headers=idem(),
    )
    if opened.status_code != 201:
        return opened
    upload_id = opened.json()["uploadId"]
    checksum = blake3(payload).digest().hex()
    part = await client.put(
        f"{PREFIX}/uploads/{upload_id}/parts/1",
        content=payload,
        headers={**idem(), "X-Part-Checksum": checksum},
    )
    assert part.status_code == 200, part.text
    return await client.post(
        f"{PREFIX}/uploads/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}]},
        headers=idem(),
    )


async def _landed(session: AsyncSession, drive_id: uuid.UUID, name: bytes) -> Any:
    return (
        await session.execute(
            text(
                "SELECT id, parent_id FROM file_nodes "
                "WHERE drive_id = :drive AND name = :name AND trashed_at IS NULL"
            ),
            {"drive": drive_id, "name": name},
        )
    ).one_or_none()


async def _denials(org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every DENY row this org filed about ``node_id``, oldest first."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.org_id == org_id,
                    EventOutbox.type == "authz.decision",
                    EventOutbox.entity == "file_node",
                    EventOutbox.entity_id == str(node_id),
                )
                .order_by(EventOutbox.id)
            )
        ).scalars()
    return [row.payload for row in rows if row.payload["effect"] == "deny"]


# ---------------------------------------------------------------------------
# The grant: what a holder is told about the plane it must run
# ---------------------------------------------------------------------------


async def test_a_chat_lease_is_granted_the_live_cadence_and_what_is_owed(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A holder learns how to run the plane from the grant, not from a constant
    compiled into it: every ceiling it must obey is served, and so is whether
    this folder takes drops at all."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)

    taken = await holder.take(real_session, idem, purpose="chat", live=True)
    assert taken.status_code == 200, taken.text
    live = taken.json()["live"]
    assert live == {
        "debounceMs": settings.files_live_debounce_ms,
        "batchEveryMs": settings.files_live_batch_ms,
        "maxBatchEntries": settings.files_live_max_batch_entries,
        "maxFileBytes": settings.files_live_max_file_bytes,
        "bandwidthBytesPerMinute": settings.files_live_bandwidth_bytes_per_minute,
        "maxPendingEntries": settings.files_live_max_pending_entries,
        "inbound": True,
        "metadataEveryMs": settings.files_live_metadata_every_ms,
        "metadataMaxEntries": settings.files_live_metadata_max_entries,
        "metadataGzipBytes": settings.files_live_metadata_gzip_bytes,
        "releaseDrainMs": settings.files_release_drain_seconds * 1000,
    }
    assert taken.json()["inboundPending"] == 0


async def test_the_beat_carries_what_a_person_dropped_while_the_box_ran(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The holder already sends a beat every few seconds. That beat is the poll:
    a drop that landed since the last one shows up as ``inboundPending``, so the
    box learns about it without a second request at its own cadence."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    assert (await holder.beat()).status_code == 200
    assert holder.grant["inboundPending"] == 0

    assert (await _drop(files_client, chat.id, "leads.csv", b"a,b\n", idem)).status_code == 202

    beat = await holder.beat()
    assert beat.status_code == 200, beat.text
    assert beat.json()["inboundPending"] == 1
    assert beat.json()["live"]["inbound"] is True

    landed = await _landed(real_session, drive.id, b"leads.csv")
    assert landed is not None
    settled = await holder.settle(landed.id, "applied")
    assert settled.status_code == 200, settled.text
    assert (await holder.beat()).json()["inboundPending"] == 0


async def test_a_version_restored_while_the_box_holds_the_file_is_owed_to_the_box(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A person restores an older version of a file in a folder a box is
    holding. The box's disk still has the bytes the restore replaced, so the
    restore is owed to it like any other write let in: the beat counts it and
    the drain names it. Unrecorded, the box never learned the head moved, its
    agent read the replaced text, and its next push filed that text over the
    restore as a newer version."""
    drive = await _room(fx)
    chat, scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    shared = await fx.node(b"shared.md", parent=scratch)
    first = await fx.version(shared, seq=1, content_hash="11" * 32, size_bytes=72)
    await fx.version(shared, seq=2, content_hash="22" * 32, size_bytes=68)
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    assert await holder.owed() == []

    etag = await real_session.scalar(
        text("SELECT etag FROM file_nodes WHERE id = :node"), {"node": shared.id}
    )
    restored = await files_client.post(
        f"{PREFIX}/drives/{drive.id}/items/{shared.id}/versions/{first.id}/restore",
        json={},
        headers={**idem(), "If-Match": str(etag)},
    )
    assert restored.status_code == 200, restored.text

    assert await holder.owed() == [(str(shared.id), "inbound")]
    assert (await holder.beat()).json()["inboundPending"] == 1
    assert (await holder.settle(shared.id, "applied")).status_code == 200
    assert await holder.owed() == []


@pytest.mark.parametrize(
    ("purpose", "asked", "inbound"),
    [
        pytest.param("mount", {}, False, id="a-mount-asks-for-nothing"),
        pytest.param("mount", {"inbound": True}, True, id="a-mount-asks-for-drops"),
        pytest.param("mount", {"live": True}, True, id="a-mount-runs-the-plane"),
        pytest.param("chat", {}, True, id="a-chat-takes-drops-by-what-it-is"),
    ],
)
async def test_what_the_folder_does_with_a_write_that_is_not_the_holders(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    purpose: str,
    asked: dict[str, bool],
    inbound: bool,
) -> None:
    """Either flag turns the plane on — a folder that keeps drops nobody drains
    and a holder that drains a folder keeping none are the same mistake told
    from opposite ends. A holder that asks for neither gets what its purpose
    means: a chat's lease takes drops, a plain mount does not."""
    drive = await _room(fx)
    folder = await fx.node(b"project", kind="folder")
    if purpose == "chat":
        folder, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, folder.id)

    taken = await holder.take(real_session, idem, purpose=purpose, **asked)
    assert taken.status_code == 200, taken.text
    assert taken.json()["live"]["inbound"] is inbound

    stored = (
        await real_session.execute(
            text("SELECT accepts_inbound FROM file_leases WHERE node_id = :node"),
            {"node": folder.id},
        )
    ).scalar_one()
    assert stored is inbound


# ---------------------------------------------------------------------------
# The report route
# ---------------------------------------------------------------------------


async def test_a_live_report_needs_no_idempotency_key(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A report is state-based and lands at the plane's cadence: a key per
    report would be a key per file per debounce window, and a report refused for
    a missing one would leave the drop the person made owed forever."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    file_node = await fx.node(b"draft.md", parent=chat)

    reported = await files_client.post(
        f"{holder.item}/lease/live",
        json={"entries": [{"nodeId": str(file_node.id), "state": "uploading"}]},
        headers=holder.fence,
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["liveSeq"] > 0


async def test_a_report_lands_while_the_holders_own_tree_report_holds_the_folder(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The deadlock CI hit: the box's tree report holds the leased folder
    (every writer's gate) and has not yet taken the lease row, and the box's
    ``applied`` for a drop it just drained arrives on the live route. The route
    must hold the lease row alone — a second statement on that row re-checks its
    key to the folder and queues behind the report, which is queued behind the
    batch — so it answers, with the row settled and the lease stamped, while the
    folder is still held by another backend."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    file_node = await fx.node(b"draft.md", parent=chat)
    first = await holder.report([{"nodeId": str(file_node.id), "state": "writing"}])
    assert first.status_code == 200, first.text

    async with AsyncSessionLocal() as tree_report:
        await tree_report.execute(
            text("SELECT id FROM file_nodes WHERE id = :id FOR UPDATE"), {"id": chat.id}
        )
        try:
            settled = await asyncio.wait_for(holder.settle(file_node.id, "applied"), 10)
        finally:
            await tree_report.rollback()

    assert settled.status_code == 200, settled.text
    assert settled.json() == {"liveSeq": 2, "pending": 0}
    lease = (
        await real_session.execute(
            text(
                "SELECT live_seq, last_sync_at, "
                "(SELECT count(*) FROM file_lease_live_entries WHERE lease_node_id = :node) "
                "AS in_flight FROM file_leases WHERE node_id = :node"
            ),
            {"node": chat.id},
        )
    ).one()
    assert (lease.live_seq, lease.in_flight) == (2, 0)
    assert lease.last_sync_at is not None


@pytest.mark.parametrize(
    "fence",
    [
        pytest.param("none", id="no-fence-at-all"),
        pytest.param("stale", id="a-superseded-epoch"),
        pytest.param("other-instance", id="another-instance"),
    ],
)
async def test_a_report_from_anyone_but_the_live_holder_is_fenced(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    fence: str,
) -> None:
    """A holder that has been superseded must stop writing, and a caller that
    never held the folder was never on the plane. Both are told so, and neither
    moves the sequence the live holder reads."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    file_node = await fx.node(b"draft.md", parent=chat)
    headers = {
        "none": {},
        "stale": holder.stale_fence(),
        "other-instance": {
            "X-Alkera-Lease-Epoch": str(holder.epoch),
            "X-Alkera-Lease-Instance": "mirror-2",
        },
    }[fence]

    refused = await holder.report(
        [{"nodeId": str(file_node.id), "state": "uploading"}], headers=headers
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_fenced"
    assert (
        await real_session.execute(
            text("SELECT count(*) FROM file_lease_live_entries WHERE lease_node_id = :node"),
            {"node": chat.id},
        )
    ).scalar_one() == 0


async def test_a_batch_naming_a_node_in_another_chat_is_refused_whole(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A fence confines a holder to the folder it holds. A batch that reaches
    outside it is refused entirely rather than trimmed — a partially applied
    batch would leave the holder believing it had reported something it had
    not — and the node it did name in its own folder records nothing."""
    drive = await _room(fx)
    mine, _mine_scratch = await _awake_chat(fx, real_session, b"Mine.alkerachat")
    theirs, _their_scratch = await _awake_chat(fx, real_session, b"Theirs.alkerachat")
    inside = await fx.node(b"ours.md", parent=mine)
    outside = await fx.node(b"theirs.md", parent=theirs)
    holder = MockHolder(files_client, drive.id, mine.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200

    refused = await holder.report(
        [
            {"nodeId": str(inside.id), "state": "uploading"},
            {"nodeId": str(outside.id), "state": "uploading"},
        ]
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_mismatch"
    assert (
        await real_session.execute(
            text("SELECT count(*) FROM file_lease_live_entries WHERE lease_node_id = :node"),
            {"node": mine.id},
        )
    ).scalar_one() == 0


# ---------------------------------------------------------------------------
# The assertion is not the proof
# ---------------------------------------------------------------------------


async def test_a_member_asserting_the_public_machine_id_is_refused_the_plane(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
) -> None:
    """The machine a chat runs on is named on the chat itself, and the two agent
    headers are a claim anyone may put on their own session. A member sending
    them — the org admin here, who may read everything — is refused the live
    plane, and the row says why: not a missing rung, but a missing proof."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    file_node = await fx.node(b"draft.md", parent=chat)

    as_agent = await files_client.post(
        f"{holder.item}/lease/live",
        json={"entries": [{"nodeId": str(file_node.id), "state": "uploading"}]},
        headers={**holder.fence, **agent_headers("box-7")},
    )
    assert as_agent.status_code == 403, as_agent.text

    denials = await _denials(files_org.org.org_id, chat.id)
    assert denials, "a refused live report files no decision row"
    assert denials[-1]["reason"] == "machine_unverified"
    assert denials[-1]["action"] == "snapshot"  # the verb the live plane decides
    assert denials[-1]["attrs"]["is_agent"] is True
    assert denials[-1]["attrs"]["agent_machine_verified"] is False
    assert denials[-1]["as_not_found"] is False

    # The very same request on the same session, without the claim, is allowed:
    # what was refused is the assertion, never the human's own reach.
    assert (await holder.report([{"nodeId": str(file_node.id), "state": "uploading"}])).status_code


# ---------------------------------------------------------------------------
# Proven is not enough: a box holds only the chat it is bound to
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def box(real_session: AsyncSession, files_org: Any) -> AsyncIterator[tuple[AsyncClient, str]]:
    """A provisioned box of this org, and the machine id it speaks as.

    The credential is the box's own device token, registered against the
    machine row — the shape :func:`verify_machine_assertion` admits — so every
    request this client makes is a PROVEN machine rather than a member sending
    two headers. Its operator is the org's admin, and the chats below are the
    admin's own, filed in their home: what the tests below refuse is therefore
    never the human's reach. A member's chat is the box's only through the
    chat's binding (``test_files_box_runs_its_chat.py``).
    """
    token, machine_id = await _registered_box(
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


async def test_a_box_may_not_take_the_folder_of_a_chat_another_box_runs(
    box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
) -> None:
    """The cross-chat fence. A box registers once and speaks for every chat it
    serves on that one credential, so proving it is a machine says nothing
    about WHICH conversation is its own — and an agent under prompt injection
    in one chat could otherwise take a colleague's sleeping chat over, write it
    and read it back through the seal. The chat's own binding settles it, and
    the row names the reason: the wrong machine, not an unproven one."""
    client, machine_id = box
    drive = await _room(fx)
    theirs, _ = await _awake_chat(fx, real_session, b"Theirs.alkerachat")
    await _bind_chat(fx, real_session, theirs, machine=str(uuid.uuid4()))

    holder = MockHolder(client, drive.id, theirs.id, machine=machine_id)
    refused = await holder.take(real_session, idem, purpose="chat", live=True)

    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "files.forbidden"
    denials = await _denials(files_org.org.org_id, theirs.id)
    assert denials, "a refused take files no decision row"
    assert denials[-1]["reason"] == "machine_mismatch"
    assert denials[-1]["action"] == "lease"
    assert denials[-1]["attrs"]["agent_machine_verified"] is True
    assert denials[-1]["attrs"]["chat_bound_elsewhere"] is True
    assert denials[-1]["as_not_found"] is False
    assert (
        await real_session.execute(
            text("SELECT count(*) FROM file_leases WHERE node_id = :node"), {"node": theirs.id}
        )
    ).scalar_one() == 0


async def test_a_box_takes_its_own_chat_and_one_no_box_has_claimed(
    box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The positive control, and the shape the fence must not break.

    Its own chat is what the box is for. A chat bound to no machine at all is
    the state every conversation sits in before a box takes it — placed, named,
    claimed by nobody — and it keeps exactly the behaviour it had before the
    binding was checked, or no box could ever pick a chat up.
    """
    client, machine_id = box
    drive = await _room(fx)
    mine, _ = await _awake_chat(fx, real_session, b"Mine.alkerachat")
    await _bind_chat(fx, real_session, mine, machine=machine_id)
    unclaimed, _ = await _awake_chat(fx, real_session, b"Unclaimed.alkerachat")
    await _bind_chat(fx, real_session, unclaimed, machine=None)

    own = await MockHolder(client, drive.id, mine.id, machine=machine_id).take(
        real_session, idem, purpose="chat", live=True
    )
    assert own.status_code == 200, own.text

    free = await MockHolder(
        client, drive.id, unclaimed.id, instance="mirror-2", machine=machine_id
    ).take(real_session, idem, purpose="chat", live=True)
    assert free.status_code == 200, free.text


async def test_a_box_may_not_snapshot_a_chat_another_box_runs(
    box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
) -> None:
    """Holding one chat's fence is not a pass for another chat's plane.

    The live report is the box's word about a folder, and a box that has
    legitimately taken its own chat still reaches the route with a valid fence
    in hand. What it may report on is decided per folder, by the same binding
    the take was decided by.
    """
    client, machine_id = box
    drive = await _room(fx)
    mine, _ = await _awake_chat(fx, real_session, b"Mine.alkerachat")
    await _bind_chat(fx, real_session, mine, machine=machine_id)
    theirs, their_scratch = await _awake_chat(fx, real_session, b"Theirs.alkerachat")
    await _bind_chat(fx, real_session, theirs, machine=str(uuid.uuid4()))

    own = MockHolder(client, drive.id, mine.id, machine=machine_id)
    assert (await own.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    their_file = await fx.node(b"notes.md", parent=their_scratch)

    trespass = await client.post(
        f"{PREFIX}/drives/{drive.id}/items/{theirs.id}/lease/live",
        json={"entries": [{"nodeId": str(their_file.id), "state": "uploading"}]},
        headers=own.fence,
    )

    assert trespass.status_code == 403, trespass.text
    denials = await _denials(files_org.org.org_id, theirs.id)
    assert denials, "a refused live report files no decision row"
    assert denials[-1]["reason"] == "machine_mismatch"
    assert denials[-1]["action"] == "snapshot"
    assert (
        await real_session.execute(
            text("SELECT count(*) FROM file_lease_live_entries WHERE lease_node_id = :node"),
            {"node": theirs.id},
        )
    ).scalar_one() == 0


async def test_a_box_may_not_write_inside_a_chat_another_box_runs(
    box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    files_org: Any,
) -> None:
    """The bytes half. Taking the folder over is the loud way in; writing a
    file inside it without ever holding it is the quiet one, and both are the
    machine's to do only on the chat it runs."""
    client, _machine_id = box
    drive = await _room(fx)
    theirs, their_scratch = await _awake_chat(fx, real_session, b"Theirs.alkerachat")
    await _bind_chat(fx, real_session, theirs, machine=str(uuid.uuid4()))
    their_file = await fx.node(b"notes.md", parent=their_scratch)
    payload = b"rewritten by somebody else's box\n"

    refused = await client.put(
        f"{PREFIX}/drives/{drive.id}/items/{their_file.id}/content",
        content=payload,
        headers={
            **idem(),
            "If-Match": f'"{their_file.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )

    assert refused.status_code == 403, refused.text
    denials = await _denials(files_org.org.org_id, their_file.id)
    assert denials, "a refused write files no decision row"
    assert denials[-1]["reason"] == "machine_mismatch"
    assert denials[-1]["action"] == "write"
    assert (
        await real_session.execute(
            text("SELECT head_version_id FROM file_nodes WHERE id = :node"),
            {"node": their_file.id},
        )
    ).scalar_one() is None


# ---------------------------------------------------------------------------
# What the plane does when the org runs out of room
# ---------------------------------------------------------------------------


async def _no_room(org_id: uuid.UUID) -> None:
    """Take the org's remaining bytes away, the way a busy org uses them up.

    Through the override the resolver actually reads: the drive row is only the
    fallback when an org has none, so moving the row would cap a number a write
    on a plan never consults.
    """
    async with AsyncSessionLocal() as session:
        await storage_limit_service.set_org_override(session, org_id, limit_bytes=8, by=None)
        await session.commit()


async def _entry_state(session: AsyncSession, lease_node_id: uuid.UUID, node_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "SELECT state FROM file_lease_live_entries "
                "WHERE lease_node_id = :lease AND node_id = :node"
            ),
            {"lease": lease_node_id, "node": node_id},
        )
    ).scalar_one_or_none()


async def test_a_batch_past_the_ceiling_the_grant_names_is_refused_before_a_row_moves(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch ceiling the grant hands the holder is the one the drive keeps.

    A batch is one statement per entry inside a single transaction holding the
    lease row, so a list nobody bounds is a transaction nobody bounds. The
    holder is told it sent too many — 422, before a row moves — rather than
    having them applied, and the number it is refused on is the number the
    grant told it to chunk at.
    """
    monkeypatch.setattr(settings, "files_live_max_batch_entries", 2)
    drive = await _room(fx)
    chat, scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    assert holder.grant["live"]["maxBatchEntries"] == 2

    made = [await fx.node(f"report-{n}.csv".encode(), parent=scratch) for n in range(3)]
    refused = await holder.report([{"nodeId": str(node.id), "state": "writing"} for node in made])
    assert refused.status_code == 422, refused.text
    assert await _entry_state(real_session, chat.id, made[0].id) is None

    # At the ceiling it lands, so the refusal is the ceiling and not the shape.
    accepted = await holder.report(
        [{"nodeId": str(node.id), "state": "writing"} for node in made[:2]]
    )
    assert accepted.status_code == 200, accepted.text
    assert await _entry_state(real_session, chat.id, made[0].id) == "writing"


async def test_one_node_named_many_times_in_a_batch_costs_what_naming_it_once_costs(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A holder that named the same file twice said two things about it and
    means the later one.

    Writing both would be the same row written twice — one statement per
    repeat, inside the transaction holding the lease — so a batch at the entry
    ceiling could be a single file repeated and still cost the drive the whole
    batch. The repeats collapse to the last word before anything is written,
    which is what makes the entry ceiling a ceiling on work.
    """
    from _oracle import probe as count_probe
    from alkera_core.db.session import engine

    drive = await _room(fx)
    chat, scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    node = await fx.node(b"report.csv", parent=scratch)

    once = await count_probe(
        files_client,
        engine.sync_engine,
        "POST",
        f"{holder.item}/lease/live",
        json={"entries": [{"nodeId": str(node.id), "state": "writing"}]},
        headers=holder.fence,
    )
    assert once.status == 200

    repeated = await count_probe(
        files_client,
        engine.sync_engine,
        "POST",
        f"{holder.item}/lease/live",
        json={
            "entries": [{"nodeId": str(node.id), "state": "uploading"}] * 60
            + [{"nodeId": str(node.id), "state": "on_box"}]
        },
        headers=holder.fence,
    )
    assert repeated.status == 200, repeated.body
    assert repeated.statements == once.statements, (
        f"one node named 61 times cost {repeated.statements} statements, "
        f"naming it once cost {once.statements}"
    )
    # The last word is what stands, and the node is in flight once.
    assert await _entry_state(real_session, chat.id, node.id) == "on_box"
    assert await _rows_for(real_session, chat.id, node.id) == 1


async def _rows_for(session: AsyncSession, lease_node_id: uuid.UUID, node_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM file_lease_live_entries "
                    "WHERE lease_node_id = :lease AND node_id = :node"
                ),
                {"lease": lease_node_id, "node": node_id},
            )
        ).scalar_one()
    )


async def test_a_live_upload_past_the_ceiling_is_refused_and_the_file_stays_on_the_box(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    org_admin: OrgWithAdmin,
) -> None:
    """The plane is bounded by the drive, and so is everything else a box sends.

    A lease used to exempt every write it fenced. That was a mount's occasional
    checkpoint; under the live plane it is a write every few hundred
    milliseconds, so the exemption became a way for one awake chat to spend past
    the org's ceiling for as long as it stayed awake. What the box is told
    instead is the truth — there is no room — and it says so on the plane: the
    file stays on its disk and the entry reads ``deferred``, which is what a
    reader of the folder sees until room is made.
    """
    drive = await _room(fx)
    chat, scratch = await _awake_chat(fx, real_session, b"Kickoff.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200

    # A file the drive already knows about, which the agent has just rewritten:
    # the box says it is sending it before it sends a byte.
    made = await fx.node(b"report.csv", parent=scratch)
    node_id = made.id
    reported = await holder.report([{"nodeId": str(node_id), "state": "uploading"}])
    assert reported.status_code == 200, reported.text

    await _no_room(org_admin.org_id)

    refused = await holder.push(real_session, idem, node_id, b"x" * 4096)
    assert refused.status_code == 507, refused.text
    assert refused.json()["code"] == "files.quota_bytes"

    deferred = await holder.report([{"nodeId": str(node_id), "state": "deferred"}])
    assert deferred.status_code == 200, deferred.text
    assert await _entry_state(real_session, chat.id, node_id) == "deferred"

    # ...and a box that calls the same push its hand-back is told the same
    # thing. A drive cannot tell a holder that means it from one that says it
    # on every write, so the claim buys no room and the file stays deferred:
    # the way out is room on the drive, not a header.
    claimed = await holder.push(real_session, idem, node_id, b"x" * 4096, final=True)
    assert claimed.status_code == 507, claimed.text
    assert claimed.json()["code"] == "files.quota_bytes"
    assert await _entry_state(real_session, chat.id, node_id) == "deferred"


def _deadlock_once(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """Make ``LiveEntriesService.<name>`` lose one deadlock, the way Postgres
    hands a waiter on the lease row the victim's verdict: the driver's own
    ``40P01``, wrapped as SQLAlchemy wraps it. Answers the call count."""
    real = getattr(LiveEntriesService, name)
    calls: list[int] = []

    async def first_one_loses(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise DBAPIError(
                "SELECT ... FROM file_leases FOR SHARE",
                {},
                DeadlockDetectedError("deadlock detected"),
            )
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(LiveEntriesService, name, first_one_loses)
    return calls


async def test_a_drain_that_loses_a_deadlock_is_replayed_not_a_500(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain fences on the lease row every heartbeat, report and tree batch
    of the holder locks, so it can be the waiter a deadlock's detector picks.
    The transaction was rolled back whole: the route runs it again and answers
    what is owed, where it used to answer 500."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Deadlock.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    assert (await _drop(files_client, chat.id, "owed.csv", b"a,b\n", idem)).status_code == 202
    landed = await _landed(real_session, drive.id, b"owed.csv")
    assert landed is not None

    calls = _deadlock_once(monkeypatch, "inbound_for_holder")
    drained = await holder.drain()
    assert drained.status_code == 200, drained.text
    assert [entry["nodeId"] for entry in drained.json()["entries"]] == [str(landed.id)]
    assert len(calls) == 2


async def test_a_report_that_loses_a_deadlock_is_replayed_not_a_500(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session, b"Report.alkerachat")
    holder = MockHolder(files_client, drive.id, chat.id)
    assert (await holder.take(real_session, idem, purpose="chat", live=True)).status_code == 200
    assert (await _drop(files_client, chat.id, "owed.csv", b"a,b\n", idem)).status_code == 202
    landed = await _landed(real_session, drive.id, b"owed.csv")
    assert landed is not None

    calls = _deadlock_once(monkeypatch, "upsert")
    settled = await holder.settle(landed.id, "applied")
    assert settled.status_code == 200, settled.text
    assert settled.json()["pending"] == 0
    assert len(calls) == 2
