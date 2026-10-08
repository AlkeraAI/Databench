"""One beat for every lease a box holds, through the real route.

A box holds one folder lease per chat, and a beat per lease every few seconds
was most of what an idle box asked the API. The batched route keeps them all
in one call; what it must never do is decide a lease differently from the
per-lease route an older daemon still uses. Every verdict below is therefore
checked against the per-lease answer for the same lease as well as against the
lease row.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from _files_kit import node_etag
from alkera_core.db.session import AsyncSessionLocal
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login

PREFIX = "/api/v1/files"

pytestmark = pytest.mark.usefixtures("files_on")


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


async def _acquire(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    idem: Any,
    session: AsyncSession,
    *,
    instance: str,
) -> dict[str, Any]:
    granted = await client.post(
        f"{_item(drive_id, node_id)}/lease",
        json={"instanceId": instance, "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )
    assert granted.status_code == 200, granted.text
    body: dict[str, Any] = granted.json()
    return body


async def _expires(session: AsyncSession, node_id: uuid.UUID) -> Any:
    row = (
        await session.execute(
            text("SELECT expires_at FROM file_leases WHERE node_id = :n AND released_at IS NULL"),
            {"n": node_id},
        )
    ).first()
    return None if row is None else row.expires_at


async def _age(session: AsyncSession, node_id: uuid.UUID) -> None:
    """Pull the lease's expiry back, so a renewal is visible as it moving on."""
    await session.execute(
        text(
            "UPDATE file_leases SET expires_at = now() + interval '5 seconds' "
            "WHERE node_id = :n AND released_at IS NULL"
        ),
        {"n": node_id},
    )
    await session.commit()


async def _batch(
    client: AsyncClient, drive_id: uuid.UUID, leases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    answer = await client.post(
        f"{PREFIX}/drives/{drive_id}/leases/heartbeat", json={"leases": leases}
    )
    assert answer.status_code == 200, answer.text
    verdicts: list[dict[str, Any]] = answer.json()["leases"]
    return verdicts


async def test_every_verdict_in_one_call_matches_the_per_lease_answer(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """Three leases, three verdicts, one request: the held lease is renewed
    with a grant, the one another holder took is superseded, and the node that
    does not exist is gone — and the per-lease route says 200 / 409 / 404 to
    the very same three."""
    drive = await fx.drive()
    kept = await fx.node(b"kept", kind="folder")
    taken = await fx.node(b"taken", kind="folder")
    drive_id, kept_id, taken_id = drive.id, kept.id, taken.id
    kept_grant = await _acquire(
        files_client, drive_id, kept_id, idem, real_session, instance="box:a"
    )
    stale = await _acquire(files_client, drive_id, taken_id, idem, real_session, instance="box:b")
    released = await files_client.post(
        f"{_item(drive_id, taken_id)}/lease/release",
        json={"epoch": stale["epoch"], "instanceId": "box:b", "final": None},
        headers={**idem(), "If-Match": await node_etag(real_session, taken_id)},
    )
    assert released.status_code == 200, released.text
    await _acquire(files_client, drive_id, taken_id, idem, real_session, instance="other:b")
    await _age(real_session, kept_id)
    before = await _expires(real_session, kept_id)
    missing = uuid.uuid4()

    asked = [
        {"nodeId": str(kept_id), "epoch": kept_grant["epoch"], "instanceId": "box:a"},
        {"nodeId": str(taken_id), "epoch": stale["epoch"], "instanceId": "box:b"},
        {"nodeId": str(missing), "epoch": 1, "instanceId": "box:c"},
    ]
    verdicts = await _batch(files_client, drive_id, asked)

    assert [(v["nodeId"], v["verdict"]) for v in verdicts] == [
        (str(kept_id), "renewed"),
        (str(taken_id), "superseded"),
        (str(missing), "gone"),
    ]
    grant = verdicts[0]["grant"]
    assert grant["epoch"] == kept_grant["epoch"]
    assert grant["heartbeatEvery"] == kept_grant["heartbeatEvery"]
    assert "inboundPending" in grant
    assert verdicts[1]["grant"] is None and verdicts[2]["grant"] is None
    after = await _expires(real_session, kept_id)
    assert after is not None and before is not None and after > before, (
        "a renewed verdict must have moved the lease's expiry"
    )

    per_lease = [
        (
            await files_client.post(
                f"{_item(drive_id, uuid.UUID(entry['nodeId']))}/lease/heartbeat",
                json={"epoch": entry["epoch"], "instanceId": entry["instanceId"]},
            )
        ).status_code
        for entry in asked
    ]
    assert per_lease == [200, 409, 404]


async def test_a_superseded_lease_does_not_cost_its_neighbours_their_beat(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The refused lease sits between two held ones; both still renew."""
    drive = await fx.drive()
    first = await fx.node(b"first", kind="folder")
    second = await fx.node(b"second", kind="folder")
    a = await _acquire(files_client, drive.id, first.id, idem, real_session, instance="box:1")
    b = await _acquire(files_client, drive.id, second.id, idem, real_session, instance="box:2")
    drive_id, first_id, second_id = drive.id, first.id, second.id
    await _age(real_session, second_id)
    before = await _expires(real_session, second_id)

    verdicts = await _batch(
        files_client,
        drive_id,
        [
            {"nodeId": str(first_id), "epoch": a["epoch"], "instanceId": "box:1"},
            {"nodeId": str(first_id), "epoch": a["epoch"] + 7, "instanceId": "box:1"},
            {"nodeId": str(second_id), "epoch": b["epoch"], "instanceId": "box:2"},
        ],
    )

    assert [v["verdict"] for v in verdicts] == ["renewed", "superseded", "renewed"]
    after = await _expires(real_session, second_id)
    assert after is not None and before is not None and after > before


async def test_another_orgs_drive_is_the_opaque_404(files_client: AsyncClient, fx: Any) -> None:
    """A drive that is not the caller's is refused before any lease is read,
    and a body the caller got wrong does not change that answer."""
    for body in ({"leases": []}, {"leases": "not a list"}):
        answer = await files_client.post(
            f"{PREFIX}/drives/{uuid.uuid4()}/leases/heartbeat", json=body
        )
        assert answer.status_code == 404, answer.text


async def test_the_batch_needs_no_idempotency_key(files_client: AsyncClient, fx: Any) -> None:
    """A beat is state-based: replaying it is the point, so the batched beat is
    exempt from the key every other lease POST needs, exactly as the per-lease
    beat is. An empty batch is an empty answer."""
    drive = await fx.drive()
    assert await _batch(files_client, drive.id, []) == []


async def _last_sync(session: AsyncSession, node_id: uuid.UUID) -> Any:
    row = (
        await session.execute(
            text("SELECT last_sync_at FROM file_leases WHERE node_id = :n AND released_at IS NULL"),
            {"n": node_id},
        )
    ).first()
    return None if row is None else row.last_sync_at


@pytest.mark.parametrize(
    ("synced", "stamped"),
    [
        pytest.param(True, True, id="a-running-plane-is-stamped-synced"),
        pytest.param(False, False, id="a-beat-alone-says-nothing-about-the-plane"),
    ],
)
async def test_a_beat_that_says_its_plane_runs_stamps_the_lease_synced(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    synced: bool,
    stamped: bool,
) -> None:
    """The word an empty live batch per folder used to carry, on the beat the
    box sends anyway — for a lease that runs the plane."""
    drive = await fx.drive()
    folder = await fx.node(b"live", kind="folder")
    drive_id, folder_id = drive.id, folder.id
    granted = await files_client.post(
        f"{_item(drive_id, folder_id)}/lease",
        json={"instanceId": "box:live", "machineId": "machine-a", "purpose": "mount", "live": True},
        headers={**idem(), "If-Match": await node_etag(real_session, folder_id)},
    )
    assert granted.status_code == 200, granted.text
    assert await _last_sync(real_session, folder_id) is None

    verdicts = await _batch(
        files_client,
        drive_id,
        [
            {
                "nodeId": str(folder_id),
                "epoch": granted.json()["epoch"],
                "instanceId": "box:live",
                "synced": synced,
            }
        ],
    )

    assert [v["verdict"] for v in verdicts] == ["renewed"]
    assert (await _last_sync(real_session, folder_id) is not None) is stamped


async def test_a_synced_beat_lands_while_the_holders_own_push_holds_the_folder(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The deadlock the box hit: a push under the lease holds the leased folder
    (every writer's lease gate) while the batched beat for that folder arrives
    ``synced``. The beat must hold the lease row alone — a second statement
    stamping the row re-checks its key to the folder and queues behind the
    push, which is then queued behind the beat — so it lands, renewed and
    stamped, while the folder is still held by another backend."""
    drive = await fx.drive()
    folder = await fx.node(b"pushed", kind="folder")
    drive_id, folder_id = drive.id, folder.id
    granted = await files_client.post(
        f"{_item(drive_id, folder_id)}/lease",
        json={"instanceId": "box:push", "machineId": "machine-a", "purpose": "chat", "live": True},
        headers={**idem(), "If-Match": await node_etag(real_session, folder_id)},
    )
    assert granted.status_code == 200, granted.text
    await _age(real_session, folder_id)
    before = await _expires(real_session, folder_id)

    async with AsyncSessionLocal() as push:
        await push.execute(
            text("SELECT id FROM file_nodes WHERE id = :id FOR UPDATE"), {"id": folder_id}
        )
        try:
            verdicts = await asyncio.wait_for(
                _batch(
                    files_client,
                    drive_id,
                    [
                        {
                            "nodeId": str(folder_id),
                            "epoch": granted.json()["epoch"],
                            "instanceId": "box:push",
                            "synced": True,
                        }
                    ],
                ),
                10,
            )
        finally:
            await push.rollback()

    assert [v["verdict"] for v in verdicts] == ["renewed"]
    assert await _last_sync(real_session, folder_id) is not None
    after = await _expires(real_session, folder_id)
    assert after is not None and before is not None and after > before


async def _share(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    idem: Any,
    session: AsyncSession,
) -> None:
    """Give ``user_id`` ``role`` on the node; a second share moves the grant."""
    shared = await client.post(
        f"{_item(drive_id, node_id)}/permissions",
        json={"principal": {"kind": "user", "id": str(user_id)}, "role": role},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )
    assert shared.status_code == 201, shared.text


async def test_a_lease_the_holder_may_no_longer_take_is_gone_and_its_neighbours_renew(
    client: AsyncClient,
    files_client: AsyncClient,
    files_org: Any,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A member holds three folders; the middle one is then shared down to Can
    view, so they still read it but may no longer lease it -- the per-lease
    beat's 403. In the batch that lease alone is gone, the ones around it still
    renew, and the answer is one verdict per lease rather than a 403 that
    leaves every lease after the refused one to lapse."""
    drive = await fx.drive()
    shared = await fx.shared()
    folders = [await fx.node(name, kind="folder", parent=shared) for name in (b"a", b"b", b"c")]
    drive_id = drive.id
    first, middle, last = (folder.id for folder in folders)
    member_id = files_org.member.id
    for node_id in (first, middle, last):
        await _share(files_client, drive_id, node_id, member_id, "writer", idem, real_session)

    member = await login(client, files_org.member.email, files_org.member_password)
    grants = {
        node_id: await _acquire(member, drive_id, node_id, idem, real_session, instance="box:m")
        for node_id in (first, middle, last)
    }
    admin = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    await _share(admin, drive_id, middle, member_id, "reader", idem, real_session)
    for node_id in (first, last):
        await _age(real_session, node_id)
    before = {node_id: await _expires(real_session, node_id) for node_id in (first, last)}

    member = await login(client, files_org.member.email, files_org.member_password)
    per_lease = await member.post(
        f"{_item(drive_id, middle)}/lease/heartbeat",
        json={"epoch": grants[middle]["epoch"], "instanceId": "box:m"},
    )
    assert per_lease.status_code == 403, per_lease.text
    assert (await member.get(_item(drive_id, middle))).status_code == 200, (
        "the member must still read the folder, or this is the opaque 404 case"
    )

    verdicts = await _batch(
        member,
        drive_id,
        [
            {"nodeId": str(node_id), "epoch": grants[node_id]["epoch"], "instanceId": "box:m"}
            for node_id in (first, middle, last)
        ],
    )

    assert [(v["nodeId"], v["verdict"]) for v in verdicts] == [
        (str(first), "renewed"),
        (str(middle), "gone"),
        (str(last), "renewed"),
    ]
    assert verdicts[1]["grant"] is None
    assert verdicts[0]["grant"]["epoch"] == grants[first]["epoch"]
    assert verdicts[2]["grant"]["epoch"] == grants[last]["epoch"]
    for node_id in (first, last):
        after = await _expires(real_session, node_id)
        assert after is not None and before[node_id] is not None
        assert after > before[node_id], "a renewed verdict must have moved the lease's expiry"
