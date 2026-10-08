"""The lease family through the real routes.

Every test here drives ``httpx`` against the mounted app with a real Postgres
behind it, so what is asserted is the status contract a client actually sees —
never a service return value the route might not be rendering.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, node_etag, refusal
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from backend.api.routes.files.leases import HEARTBEATS_PER_TTL
from backend.services.org import storage_limits as storage_limit_service
from blake3 import blake3
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login
from tests.files._boxes import bind_chat, registered_box

PREFIX = "/api/v1/files"

pytestmark = pytest.mark.usefixtures("files_on")


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


async def _folder(fx: Any) -> Any:
    return await fx.node(b"project", kind="folder")


async def _precondition(session: AsyncSession, node_id: uuid.UUID) -> dict[str, str]:
    """The folder's current etag, as the ``If-Match`` every lease POST needs.

    A lease route fences on the epoch rather than on the counter, so it never
    reads this value — but the resource a lease names is the folder, and the
    precondition is required on every mutation, so the folder's etag is what a
    caller sends. Read back from the database each time because the call
    before it may have moved the counter.
    """
    return {"If-Match": await node_etag(session, node_id)}


async def _acquire(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    idem: Any,
    session: AsyncSession,
    *,
    instance: str = "instance-a",
    machine: str = "machine-a",
    etag_of: uuid.UUID | None = None,
) -> Any:
    return await client.post(
        f"{_item(drive_id, node_id)}/lease",
        json={"instanceId": instance, "machineId": machine, "purpose": "mount"},
        headers={**idem(), **await _precondition(session, etag_of or node_id)},
    )


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


async def test_acquire_heartbeat_release_round_trip(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A mount's whole life through the routes, asserted on the lease row: the
    grant exists, the beat keeps it, and the release ends it."""
    folder = await _folder(fx)
    drive = await fx.drive()

    granted = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert granted.status_code == 200, granted.text
    grant = granted.json()
    assert grant["epoch"] > 0
    # Several beats inside one TTL, so a single lost beat never costs the lease.
    assert grant["heartbeatEvery"] == min(
        settings.files_lease_heartbeat_seconds,
        settings.files_lease_ttl_seconds / HEARTBEATS_PER_TTL,
    )

    beat = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": grant["epoch"], "instanceId": "instance-a"},
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["epoch"] == grant["epoch"]

    released = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": grant["epoch"], "instanceId": "instance-a", "final": None},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert released.status_code == 200, released.text

    row = (
        await real_session.execute(
            text("SELECT released_at FROM file_leases WHERE node_id = :n"),
            {"n": folder.id},
        )
    ).first()
    assert row is not None and row.released_at is not None

    # Re-grantable the instant the release commits.
    again = await _acquire(
        files_client, drive.id, folder.id, idem, real_session, instance="instance-b"
    )
    assert again.status_code == 200, again.text
    assert again.json()["epoch"] > grant["epoch"]


async def _seconds_left(session: AsyncSession, node_id: uuid.UUID) -> float:
    """How long the lease on ``node_id`` still has, by the database's own clock."""
    row = (
        await session.execute(
            text(
                "SELECT extract(epoch FROM (expires_at - now())) AS left "
                "FROM file_leases WHERE node_id = :n AND released_at IS NULL"
            ),
            {"n": node_id},
        )
    ).first()
    assert row is not None, "no live lease on that node"
    return float(row.left)


@pytest.mark.parametrize("ttl", [90, 1800])
async def test_the_deployment_decides_how_long_a_grant_lives(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
    ttl: int,
) -> None:
    """The window a silent holder is allowed is a setting, not a constant in
    the build — and the beat renews to the same value.

    How long a box may go quiet is a property of the deployment: a fleet whose
    boxes push large trees between beats, or a caller driving the mount library
    with no beat loop at all, needs a wider window than the default. A beat
    that renewed to the constant instead would shorten the very lease it was
    sent to extend.
    """
    monkeypatch.setattr(settings, "files_lease_ttl_seconds", ttl)
    # The cadence a holder is handed is the lower of the deployment's own
    # figure and the TTL-derived ceiling; park the figure out of reach so this
    # case reads the ceiling, which is the half that tracks the TTL.
    monkeypatch.setattr(settings, "files_lease_heartbeat_seconds", 10_000)
    folder = await _folder(fx)
    drive = await fx.drive()

    granted = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert granted.status_code == 200, granted.text
    assert granted.json()["heartbeatEvery"] == pytest.approx(ttl / 4)
    assert await _seconds_left(real_session, folder.id) > ttl * 0.9

    monkeypatch.setattr(settings, "files_lease_ttl_seconds", 2 * ttl)
    beat = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": granted.json()["epoch"], "instanceId": "instance-a"},
    )

    assert beat.status_code == 200, beat.text
    assert beat.json()["heartbeatEvery"] == pytest.approx(2 * ttl / 4)
    assert await _seconds_left(real_session, folder.id) > ttl * 1.9


async def test_a_holder_that_names_its_own_ttl_still_gets_that_one(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting is the default, not a floor: a holder that knows it will be
    back in a moment asks for a short grant and gets it, so a crash does not
    park the folder for the deployment's whole window."""
    monkeypatch.setattr(settings, "files_lease_ttl_seconds", 1800)
    folder = await _folder(fx)
    drive = await fx.drive()

    granted = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={
            "instanceId": "instance-a",
            "machineId": "machine-a",
            "purpose": "mount",
            "ttl": 30,
        },
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )

    assert granted.status_code == 200, granted.text
    assert await _seconds_left(real_session, folder.id) < 60


async def test_the_configured_ttl_decides_the_deadline_and_the_cadence_it_serves(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``files_lease_ttl_seconds`` reaches the route, not just the library.

    The grant's deadline and the beat rate it tells the holder to keep are the
    same number, so a deployment that lengthens the lease lengthens both — a
    holder cannot be served a cadence the row it beats disagrees with.
    """
    monkeypatch.setattr(settings, "files_lease_ttl_seconds", 240)
    # The cadence a holder is handed is the lower of the deployment's own
    # figure and the TTL-derived ceiling; park the figure out of reach so this
    # case reads the ceiling, which is the half that tracks the TTL.
    monkeypatch.setattr(settings, "files_lease_heartbeat_seconds", 10_000)
    folder = await _folder(fx)
    drive = await fx.drive()

    granted = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert granted.status_code == 200, granted.text
    assert granted.json()["heartbeatEvery"] == 240 / HEARTBEATS_PER_TTL

    row = (
        await real_session.execute(
            text("SELECT acquired_at, expires_at FROM file_leases WHERE node_id = :n"),
            {"n": folder.id},
        )
    ).first()
    assert row is not None
    assert row.expires_at - row.acquired_at == timedelta(seconds=240)


async def _went_quiet(session: AsyncSession, node_id: uuid.UUID, *, ago: float) -> None:
    """Rewind the lease on ``node_id`` so its holder stopped beating ``ago``
    seconds back.

    Every stamp moves by the same interval, which is the row wall time actually
    leaves behind when a holder simply goes away: the grant is older than the
    deadline and the deadline is in the past. Dragging the deadline back on its
    own would be the shape a force makes, which means something else.
    """
    await session.execute(
        text(
            "UPDATE file_leases SET "
            "acquired_at = acquired_at - (expires_at - now() + make_interval(secs => :ago)), "
            "heartbeat_at = heartbeat_at - (expires_at - now() + make_interval(secs => :ago)), "
            "grantable_after = "
            "grantable_after - (expires_at - now() + make_interval(secs => :ago)), "
            "expires_at = expires_at - (expires_at - now() + make_interval(secs => :ago)) "
            "WHERE node_id = :node"
        ),
        {"node": node_id, "ago": ago},
    )
    await session.commit()


@pytest.mark.parametrize(
    ("delay", "quiet_for", "expected"),
    [
        pytest.param(60, 30, 409, id="inside-the-configured-delay"),
        pytest.param(60, 90, 200, id="past-the-configured-delay"),
        pytest.param(5, 30, 200, id="a-shorter-delay-hands-the-folder-over-sooner"),
    ],
)
async def test_the_configured_grant_delay_is_what_the_route_serves(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
    delay: int,
    quiet_for: int,
    expected: int,
) -> None:
    """``files_lease_grant_delay_seconds`` is how long a lapsed folder stays off
    the next asker, and the serving path has to read it: a deployment that gives
    a slow holder a minute to come back must not have the route hand its folder
    away in five seconds.
    """
    monkeypatch.setattr(settings, "files_lease_grant_delay_seconds", delay)
    folder = await _folder(fx)
    drive = await fx.drive()
    granted = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert granted.status_code == 200, granted.text

    await _went_quiet(real_session, folder.id, ago=quiet_for)

    # A second instance of the same holder is not the resume, so it waits out
    # the delay exactly as a stranger does.
    taken = await _acquire(
        files_client, drive.id, folder.id, idem, real_session, instance="instance-b"
    )
    assert taken.status_code == expected, taken.text
    if expected == 409:
        assert taken.json()["code"] == "files.leased"


@pytest.mark.parametrize(
    ("knob", "expected"),
    [
        pytest.param(5, 5.0, id="a-tighter-cadence-than-the-ttl-allows-is-honoured"),
        pytest.param(600, 450.0, id="a-looser-one-is-clamped-to-the-ttl-derived-ceiling"),
    ],
)
async def test_the_deployment_may_tighten_the_beat_but_never_loosen_it(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
    knob: int,
    expected: float,
) -> None:
    """``files_lease_heartbeat_seconds`` is the cadence a holder is told to
    keep — but only downwards.

    A fleet that wants a dead box noticed sooner than a quarter of the window
    says so and is obeyed. One that asks for a beat rarer than the TTL allows
    would be fenced between beats, so the TTL-derived figure wins instead: the
    grant always promises several chances inside one window.
    """
    monkeypatch.setattr(settings, "files_lease_ttl_seconds", 1800)
    monkeypatch.setattr(settings, "files_lease_heartbeat_seconds", knob)
    folder = await _folder(fx)
    drive = await fx.drive()

    granted = await _acquire(files_client, drive.id, folder.id, idem, real_session)

    assert granted.status_code == 200, granted.text
    assert granted.json()["heartbeatEvery"] == pytest.approx(expected)
    assert granted.json()["heartbeatEvery"] <= 1800 / 4


# ---------------------------------------------------------------------------
# Overlap: the 409 and who it names
# ---------------------------------------------------------------------------


async def test_overlap_names_the_holder_when_the_caller_can_read_it(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A second machine racing for a folder its owner can read is told who has
    it, so the client can render "already mounted on machine-a"."""
    folder = await _folder(fx)
    drive = await fx.drive()
    first = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert first.status_code == 200, first.text

    clash = await _acquire(
        files_client,
        drive.id,
        folder.id,
        idem,
        real_session,
        instance="instance-b",
        machine="machine-b",
    )
    assert clash.status_code == 409, clash.text
    body = clash.json()
    assert body["code"] == "files.leased"
    # The whole point of the refusal: the client can say who has it, on which
    # machine, since when. Without these the 409 is a dead end.
    detail = body["detail"]
    assert detail["machine"] == "machine-a"
    assert detail["holder"] and detail["since"]


async def test_the_same_instance_resumes_its_own_mount_through_the_routes(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A mount that was killed re-acquires immediately and keeps its epoch, so
    the client that comes back is the same writer it was before rather than one
    waiting out a TTL nobody is using."""
    folder = await _folder(fx)
    drive = await fx.drive()
    first = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    resumed = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["epoch"] == first["epoch"]

    pushed = await files_client.post(
        f"{_item(drive.id, folder.id)}/snapshots",
        json={"changes": []},
        headers={
            **idem(),
            **await _precondition(real_session, folder.id),
            "X-Alkera-Lease-Epoch": str(first["epoch"]),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )
    assert pushed.status_code == 200, pushed.text


async def test_a_caller_who_cannot_read_the_folder_gets_the_opaque_404(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The three not-yours classes are one answer: a same-org member with no
    grant on the folder, and a node that does not exist at all, are byte-
    identical 404s — so a lease attempt is never a way to learn a folder is
    there, let alone that somebody has it mounted."""
    folder = await _folder(fx)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    other = await login(client, files_org.member.email, files_org.member_password)
    # Both send the same precondition — the folder's etag — so the two answers
    # differ in nothing but the node id in the path.
    unreadable = await _acquire(
        other, drive.id, folder.id, idem, real_session, instance="instance-b"
    )
    nonexistent = await _acquire(
        other,
        drive.id,
        uuid.uuid4(),
        idem,
        real_session,
        instance="instance-b",
        etag_of=folder.id,
    )

    assert unreadable.status_code == nonexistent.status_code == 404
    assert refusal(unreadable) == refusal(nonexistent) == NOT_FOUND


# ---------------------------------------------------------------------------
# Fencing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("epoch_shift", "instance", "expected", "code"),
    [
        pytest.param(0, "instance-a", 200, None, id="the-holders-epoch-lands"),
        pytest.param(
            -1, "instance-a", 409, "files.lease_fenced", id="a-superseded-epoch-is-fenced"
        ),
        pytest.param(0, "instance-z", 409, "files.lease_fenced", id="another-instance-is-fenced"),
    ],
)
async def test_snapshot_is_fenced_by_the_epoch_headers(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    epoch_shift: int,
    instance: str,
    expected: int,
    code: str | None,
) -> None:
    """The three fenced-write cases, one parametrized test: the holder's epoch
    lands, a superseded epoch and a different instance are both refused."""
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    pushed = await files_client.post(
        f"{_item(drive.id, folder.id)}/snapshots",
        json={"changes": []},
        headers={
            **idem(),
            **await _precondition(real_session, folder.id),
            "X-Alkera-Lease-Epoch": str(grant["epoch"] + epoch_shift),
            "X-Alkera-Lease-Instance": instance,
        },
    )
    assert pushed.status_code == expected, pushed.text
    if code is not None:
        assert pushed.json()["code"] == code


async def test_a_writer_replaying_the_holders_fence_is_refused_and_writes_nothing(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The fence names a lease; it does not prove a holder, so it is checked
    against one.

    Both halves of the pair are reachable by anyone who can see the folder —
    a lease facet names the machine, a refusal names the holder, and the epoch
    is a small counter that starts at one — so a colleague who may write the
    folder could otherwise replay them and write as whoever has it mounted.
    The holder's own create under that pair lands; the replay is refused with
    the same ``files.lease_fenced`` a superseded holder gets, so it tells a
    forger nothing a stale epoch would not, and the folder keeps exactly the
    one child the holder made.
    """
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    fence = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }
    landed = await files_client.post(
        f"{_item(drive.id, folder.id)}/children",
        json={"name": "mine", "kind": "folder"},
        headers={**idem(), **fence},
    )
    assert landed.status_code == 201, landed.text

    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            folder,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    colleague = await login(client, files_org.member.email, files_org.member_password)

    forged = await colleague.post(
        f"{_item(drive.id, folder.id)}/children",
        json={"name": "forged", "kind": "folder"},
        headers={**idem(), **fence},
    )

    assert forged.status_code == 409, forged.text
    assert forged.json()["code"] == "files.lease_fenced"
    names = (
        (
            await real_session.execute(
                text("SELECT name FROM file_nodes WHERE parent_id = :p"), {"p": folder.id}
            )
        )
        .scalars()
        .all()
    )
    assert [bytes(name) for name in names] == [b"mine"]


async def test_a_writer_asserting_the_holders_own_principal_id_writes_nothing(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The half of the fence that says WHO is asking is derived, not asserted.

    The agent assertion is two headers any member may put on their own session,
    and the holder's principal id is served — it is the ``holder`` of every
    ``files.leased`` refusal and the ``holder`` of the lease facet on the
    folder and everything under it. So the attack is to take the pair AND spell
    the holder's own id as the agent id: a caller whose asserted identity is
    taken at its word would then equal the row and write as the holder.

    It writes nothing. An unverified assertion carries no holdership at all, so
    the replay is refused with the same ``files.lease_fenced`` a superseded
    holder gets and the folder keeps exactly the one child its holder made.
    """
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    fence = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }
    landed = await files_client.post(
        f"{_item(drive.id, folder.id)}/children",
        json={"name": "mine", "kind": "folder"},
        headers={**idem(), **fence},
    )
    assert landed.status_code == 201, landed.text

    # Exactly what the product hands out: the holder as a 409 detail names it.
    held = await real_session.execute(
        text("SELECT holder_principal_id FROM file_leases WHERE node_id = :n"), {"n": folder.id}
    )
    holder = held.scalar_one()

    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            folder,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    colleague = await login(client, files_org.member.email, files_org.member_password)

    forged = await colleague.post(
        f"{_item(drive.id, folder.id)}/children",
        json={"name": "forged", "kind": "folder"},
        headers={**idem(), **fence, **agent_headers(str(holder))},
    )

    assert forged.status_code == 409, forged.text
    assert forged.json()["code"] == "files.lease_fenced"
    names = (
        (
            await real_session.execute(
                text("SELECT name FROM file_nodes WHERE parent_id = :p"), {"p": folder.id}
            )
        )
        .scalars()
        .all()
    )
    assert [bytes(name) for name in names] == [b"mine"]


async def _proven_box(
    session: AsyncSession, files_org: Any
) -> AsyncIterator[tuple[AsyncClient, str]]:
    """A client that IS a machine, and the id it speaks as.

    Its credential is a box's own device token registered against the machine
    row, so ``verify_machine_assertion`` passes. Its operator is the org admin,
    who may read every chat in the org — so nothing the tests below refuse is
    the human's own reach being refused.
    """
    token, machine_id = await registered_box(
        session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as box:
        yield box, machine_id


@pytest_asyncio.fixture
async def proven_box(
    real_session: AsyncSession, files_org: Any
) -> AsyncIterator[tuple[AsyncClient, str]]:
    async for pair in _proven_box(real_session, files_org):
        yield pair


async def test_a_second_box_replaying_a_holders_fence_holds_none_of_its_leases(
    files_client: AsyncClient,
    proven_box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The read side of the fence answers the holder, not the pair — and not
    the machine id either, which is published on every chat the box serves.

    The nodes a request "holds" are what tell the decider it is standing inside
    its own mount, and a chat folder is ``NO_DOWNLOAD`` for everyone but the box
    running it — so a second caller that replays the pair and is counted as the
    holder would be handed the bytes of somebody else's conversation. The
    replay is spelled the way an attacker would spell it: the HOLDER'S OWN
    machine id, which is served on ``LeaseFacet.machine`` and on every chat row
    that box serves, on a credential that is not the box's. It reads the row
    the way any other member does: ``canDownload`` false.
    """
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT

    box, machine_id = proven_box
    drive = await fx.drive()
    folder = await fx.node(
        b"Kickoff.alkerachat", kind="folder", flags=NO_DOWNLOAD_BIT, parent=await fx.home()
    )
    await real_session.commit()
    granted = await box.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={
            "instanceId": f"{machine_id}:chat-a",
            "machineId": machine_id,
            "purpose": "chat",
        },
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text
    fence = {
        "X-Alkera-Lease-Epoch": str(granted.json()["epoch"]),
        "X-Alkera-Lease-Instance": f"{machine_id}:chat-a",
    }
    # The holder itself, for the same read: the row it is served is what the
    # replays below are measured against.
    held = await box.get(f"{_item(drive.id, folder.id)}", headers=fence)
    assert held.status_code == 200, held.text
    assert _can_download(held.json()) is True

    # Two impostors on the org admin's own session: one inventing an id, and
    # one presenting the id the product publishes for the real holder. The
    # second is the only one that could ever have worked.
    for label, asserted in (("an-invented-id", str(uuid.uuid4())), ("the-holders-own", machine_id)):
        async with app_client(
            cookies=files_client.cookies, headers=agent_headers(asserted)
        ) as impostor:
            replayed = await impostor.get(f"{_item(drive.id, folder.id)}", headers=fence)
        assert replayed.status_code == 200, (label, replayed.text)
        assert _can_download(replayed.json()) is False, label


@pytest_asyncio.fixture
async def box_wearing_a_members_uuid(
    real_session: AsyncSession, files_org: Any
) -> AsyncIterator[tuple[AsyncClient, str]]:
    """A proven box whose machine id IS the org member's user id.

    A user row and a compute allocation are UUIDs drawn from two different
    tables, and nothing keeps one value out of both. Every holder in the rest of
    the suite is a uuid4 from one of them, so the collision the fence's kind half
    exists for is unreachable from any other fixture — and a fence that matched
    the uuid alone would look exactly as correct. This builds it on purpose.
    """
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
        machine_id=files_org.member.id,
    )
    assert machine_id == str(files_org.member.id)
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as box:
        yield box, machine_id


async def _writer(fx: Any, files_org: Any, folder: Any) -> None:
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            folder,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )


async def test_a_person_sharing_a_boxs_uuid_holds_none_of_that_boxs_lease(
    box_wearing_a_members_uuid: tuple[AsyncClient, str],
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The holder is a PAIR — which id space, and which id — and this is the
    case that separates the two halves.

    The member's user id and the box's machine id are the same uuid here, so the
    id half of the fence matches for both of them and only the kind tells them
    apart. The member may edit the folder, so every one of these requests
    reaches the fence rather than the ladder: the write, the beat that would keep
    somebody else's lease alive, the release that would end it without the
    manager's rung, and the read that decides whether this caller is standing
    inside its own mount. All four answer the caller, not the uuid.
    """
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT

    box, machine_id = box_wearing_a_members_uuid
    drive = await fx.drive()
    folder = await fx.node(
        b"Kickoff.alkerachat", kind="folder", flags=NO_DOWNLOAD_BIT, parent=await fx.home()
    )
    await real_session.commit()
    instance = f"{machine_id}:chat-a"
    granted = await box.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": instance, "machineId": machine_id, "purpose": "chat"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text
    epoch = granted.json()["epoch"]
    fence = {"X-Alkera-Lease-Epoch": str(epoch), "X-Alkera-Lease-Instance": instance}
    kind = (
        await real_session.execute(
            text("SELECT holder_kind FROM file_leases WHERE node_id = :n"), {"n": folder.id}
        )
    ).scalar_one()
    assert kind == "machine", "a proven box's lease is recorded as a person's"

    await _writer(fx, files_org, folder)
    colleague = await _as_colleague(files_org)
    try:
        wrote = await colleague.post(
            f"{_item(drive.id, folder.id)}/children",
            json={"name": "forged", "kind": "folder"},
            headers={**idem(), **fence},
        )
        assert wrote.status_code == 409, wrote.text
        assert wrote.json()["code"] == "files.lease_fenced"

        beat = await colleague.post(
            f"{_item(drive.id, folder.id)}/lease/heartbeat",
            json={"epoch": epoch, "instanceId": instance},
        )
        assert beat.status_code == 409, beat.text
        assert beat.json()["code"] == "files.lease_fenced"

        ended = await colleague.post(
            f"{_item(drive.id, folder.id)}/lease/release",
            json={"epoch": epoch, "instanceId": instance, "final": None},
            headers={**idem(), **await _precondition(real_session, folder.id)},
        )
        assert ended.status_code == 409, ended.text
        assert ended.json()["code"] == "files.lease_fenced"

        # The resume: acquiring under the holder's own instance is how a mount
        # that was killed takes its lease back without waiting out the TTL, so
        # it is the one path that hands a live lease to a matching caller.
        resumed = await _acquire(
            colleague,
            drive.id,
            folder.id,
            idem,
            real_session,
            instance=instance,
            machine=machine_id,
        )
        assert resumed.status_code == 409, resumed.text
        assert resumed.json()["code"] == "files.leased"

        # The read side of the same question: what a caller holds is what tells
        # the decider it is inside its own mount, and this folder is sealed to
        # everyone else.
        seen = await colleague.get(f"{_item(drive.id, folder.id)}", headers=fence)
        assert seen.status_code == 200, seen.text
        assert _can_download(seen.json()) is False

        # …and it is not one of their mounts either.
        listed = await colleague.get(f"{PREFIX}/drives/{drive.id}/leases?mine=true")
        assert listed.status_code == 200, listed.text
        assert [row["nodeId"] for row in listed.json()] == []
    finally:
        await colleague.aclose()

    still = await _live_lease(real_session, folder.id)
    assert still.released_at is None, "a person ended a box's lease by wearing its uuid"
    assert still.epoch == epoch
    names = (
        (
            await real_session.execute(
                text("SELECT name FROM file_nodes WHERE parent_id = :p"), {"p": folder.id}
            )
        )
        .scalars()
        .all()
    )
    assert names == []
    # …and the box, whose lease it is, is refused none of it.
    mine = await box.get(f"{_item(drive.id, folder.id)}", headers=fence)
    assert mine.status_code == 200 and _can_download(mine.json()) is True


async def test_a_box_sharing_a_persons_uuid_holds_none_of_that_persons_lease(
    files_client: AsyncClient,
    box_wearing_a_members_uuid: tuple[AsyncClient, str],
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The mirror. The person holds, and the box wearing their uuid is fenced
    out of the mount — so the pair is compared, not merely carried.

    The folder is sealed, because the read side of the fence only ever answers
    an agent: what a caller holds is what tells the decider it is standing
    inside its own mount, and a person is never inside one. So the box is the
    only caller that can show the read half refusing a matching uuid.
    """
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT

    box, machine_id = box_wearing_a_members_uuid
    drive = await fx.drive()
    folder = await fx.node(
        b"Retro.alkerachat", kind="folder", flags=NO_DOWNLOAD_BIT, parent=await fx.home()
    )
    await real_session.commit()
    await _writer(fx, files_org, folder)
    colleague = await _as_colleague(files_org)
    try:
        granted = await _acquire(colleague, drive.id, folder.id, idem, real_session)
        assert granted.status_code == 200, granted.text
        epoch = granted.json()["epoch"]
        held = (
            await real_session.execute(
                text("SELECT holder_kind, holder_principal_id FROM file_leases WHERE node_id = :n"),
                {"n": folder.id},
            )
        ).one()
        assert held.holder_kind == "user"
        assert str(held.holder_principal_id) == machine_id, "the fixture's collision is gone"

        fence = {"X-Alkera-Lease-Epoch": str(epoch), "X-Alkera-Lease-Instance": "instance-a"}
        wrote = await box.post(
            f"{_item(drive.id, folder.id)}/children",
            json={"name": "forged", "kind": "folder"},
            headers={**idem(), **fence},
        )
        assert wrote.status_code == 409, wrote.text
        assert wrote.json()["code"] == "files.lease_fenced"

        beat = await box.post(
            f"{_item(drive.id, folder.id)}/lease/heartbeat",
            json={"epoch": epoch, "instanceId": "instance-a"},
        )
        assert beat.status_code == 409, beat.text
        assert beat.json()["code"] == "files.lease_fenced"

        # The read side: a box standing inside a mount is handed the bytes of a
        # sealed folder, and this box is standing inside nothing.
        seen = await box.get(f"{_item(drive.id, folder.id)}", headers=fence)
        assert seen.status_code == 200, seen.text
        assert _can_download(seen.json()) is False

        # The holder's own beat, on the same row, still works: what was refused
        # is the caller, never the verb.
        ours = await colleague.post(
            f"{_item(drive.id, folder.id)}/lease/heartbeat",
            json={"epoch": epoch, "instanceId": "instance-a"},
        )
        assert ours.status_code == 200, ours.text
    finally:
        await colleague.aclose()


async def test_a_box_wears_the_machine_the_server_proved_and_a_person_their_own_label(
    files_client: AsyncClient,
    proven_box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """``machineId`` is a claim from a box and a fact from a person.

    The label is read back onto every chat row, onto the lease facet of the
    folder and everything under it, and onto the ``files.leased`` refusal — so a
    box that could name itself anything would put another machine's badge on the
    folder it is holding. A box's label is therefore replaced by the machine the
    server proved it to be. A person's is not touched: the name on their own
    laptop is their word about their own device, and nothing decides on it.
    """
    box, machine_id = proven_box
    drive = await fx.drive()
    theirs = await fx.node(b"Kickoff.alkerachat", kind="folder", parent=await fx.home())
    mine = await fx.node(b"project", kind="folder")
    await real_session.commit()

    badged = await box.post(
        f"{_item(drive.id, theirs.id)}/lease",
        json={
            "instanceId": f"{machine_id}:chat-a",
            "machineId": "someone-elses-box",
            "purpose": "chat",
        },
        headers={**idem(), **await _precondition(real_session, theirs.id)},
    )
    assert badged.status_code == 200, badged.text

    laptop = await _acquire(
        files_client, drive.id, mine.id, idem, real_session, machine="MacBook Pro"
    )
    assert laptop.status_code == 200, laptop.text

    labels = {
        str(row.node_id): row.machine_id
        for row in await real_session.execute(
            text("SELECT node_id, machine_id FROM file_leases WHERE node_id IN (:a, :b)"),
            {"a": theirs.id, "b": mine.id},
        )
    }
    assert labels[str(theirs.id)] == machine_id, (
        "a box put a machine id it typed on the folder it is holding"
    )
    assert labels[str(mine.id)] == "MacBook Pro"

    # What a reader is shown, which is where the badge does its damage.
    shown = await files_client.get(f"{_item(drive.id, theirs.id)}")
    assert shown.status_code == 200, shown.text
    assert shown.json()["lease"]["machine"] == machine_id
    own = await files_client.get(f"{_item(drive.id, mine.id)}")
    assert own.status_code == 200, own.text
    assert own.json()["lease"]["machine"] == "MacBook Pro"


async def test_a_snapshot_advances_last_sync_at(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """``last_sync_at`` moves only when a batch actually lands, which is what
    makes "last synced 12 s ago" true rather than optimistic."""
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    before = (
        await real_session.execute(
            text("SELECT last_sync_at FROM file_leases WHERE node_id = :n"), {"n": folder.id}
        )
    ).first()
    assert before is not None and before.last_sync_at is None

    pushed = await files_client.post(
        f"{_item(drive.id, folder.id)}/snapshots",
        json={"changes": []},
        headers={
            **idem(),
            **await _precondition(real_session, folder.id),
            "X-Alkera-Lease-Epoch": str(grant["epoch"]),
            "X-Alkera-Lease-Instance": "instance-a",
        },
    )
    assert pushed.status_code == 200, pushed.text
    after = (
        await real_session.execute(
            text("SELECT last_sync_at FROM file_leases WHERE node_id = :n"), {"n": folder.id}
        )
    ).first()
    assert after is not None and after.last_sync_at is not None


async def test_release_with_a_final_snapshot_is_one_transaction(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The final batch and the release commit together: after the call the lease
    is gone AND its last batch is visible, never one without the other."""
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    released = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={
            "epoch": grant["epoch"],
            "instanceId": "instance-a",
            "final": [{"op": "put", "path": "/a.txt"}],
        },
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert released.status_code == 200, released.text
    row = (
        await real_session.execute(
            text("SELECT released_at, last_sync_at FROM file_leases WHERE node_id = :n"),
            {"n": folder.id},
        )
    ).first()
    assert row is not None
    assert row.released_at is not None
    assert row.last_sync_at is not None


async def test_release_at_a_superseded_epoch_is_fenced(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A fenced holder cannot release the lease out from under its successor."""
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    fenced = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": grant["epoch"] - 1, "instanceId": "instance-a", "final": None},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert fenced.status_code == 409
    assert fenced.json()["code"] == "files.lease_fenced"


async def _as_colleague(files_org: Any) -> AsyncClient:
    """A SECOND logged-in client for the org's member.

    ``files_client`` and ``client`` are one httpx client with one cookie jar, so
    logging the member in on ``client`` replaces the admin's session. A test
    that needs both callers alive at once — the holder and the writer trying to
    take its lease away — needs its own client for the second one.
    """
    colleague = app_client()
    return await login(colleague, files_org.member.email, files_org.member_password)


async def _live_lease(session: AsyncSession, node_id: uuid.UUID) -> Any:
    """The lease row as it stands: released or not, and at which epoch."""
    return (
        await session.execute(
            text(
                "SELECT epoch, released_at, expires_at > now() AS live "
                "FROM file_leases WHERE node_id = :n"
            ),
            {"n": node_id},
        )
    ).one()


async def test_a_writer_cannot_end_or_keep_a_lease_that_is_not_theirs(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Handing a folder back and keeping it alive are the holder's own verbs.

    Both routes decide on ``LEASE``, which the ladder hands to the writer rung,
    and both were matched on the epoch and the instance alone — values a
    colleague can read off a refusal and off the lease facet. So a colleague
    with "Can edit" could end a live lease outright: no grace, no manager rung,
    and the folder re-grantable in the same breath. Taking a folder off a live
    holder is ``force-release``, which is a manager's verb and leaves the
    holder one TTL to finish.

    Both are refused with the same ``files.lease_fenced`` a superseded holder
    gets, the row is untouched, and the holder's own beat and release still
    work.
    """
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    epoch = grant["epoch"]

    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            folder,
            Principal(kind="user", id=files_org.member.id),
            "writer",
        )
    colleague = await _as_colleague(files_org)

    beat = await colleague.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": epoch, "instanceId": "instance-a"},
    )
    assert beat.status_code == 409, beat.text
    assert beat.json()["code"] == "files.lease_fenced"

    ended = await colleague.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": epoch, "instanceId": "instance-a", "final": None},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert ended.status_code == 409, ended.text
    assert ended.json()["code"] == "files.lease_fenced"

    still = await _live_lease(real_session, folder.id)
    assert still.released_at is None, "a writer's release ended somebody else's lease"
    assert still.live is True
    assert still.epoch == epoch

    # The holder's own beat and release, on the same row, still work — what was
    # refused is the caller, never the verb.
    mine = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": epoch, "instanceId": "instance-a"},
    )
    assert mine.status_code == 200, mine.text
    handed_back = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": epoch, "instanceId": "instance-a", "final": None},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert handed_back.status_code == 200, handed_back.text
    assert (await _live_lease(real_session, folder.id)).released_at is not None
    await colleague.aclose()


async def test_a_manager_still_takes_a_folder_back_and_the_holder_keeps_its_grace(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The take-back path the product designed, unchanged by the fence.

    ``request-release`` asks and touches nothing; ``force-release`` is the
    manager's verb, and it does not cut the holder off mid-write — the lease
    stays live for one more TTL and the next beat reports ``forced``, which is
    how a well-behaved client learns to wrap up. None of that is fenced on the
    caller, because none of it claims to BE the holder.
    """
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    epoch = grant["epoch"]

    asked = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/request-release",
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert asked.status_code == 200, asked.text
    assert asked.json()["epoch"] == epoch
    assert (await _live_lease(real_session, folder.id)).released_at is None

    forced = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/force-release",
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] is True

    # The holder learns it on its next beat and still has the folder.
    beat = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": epoch, "instanceId": "instance-a"},
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["forced"] is True
    assert (await _live_lease(real_session, folder.id)).live is True


# ---------------------------------------------------------------------------
# The idempotency asymmetry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "payload", "expected"),
    [
        pytest.param(
            "/lease/heartbeat",
            {"epoch": 0, "instanceId": "instance-a"},
            409,
            id="heartbeat-needs-no-key",
        ),
        pytest.param(
            "/lease",
            {"instanceId": "i", "machineId": "m"},
            428,
            id="acquire-without-a-key-is-428",
        ),
        pytest.param(
            "/lease/release",
            {"epoch": 1, "instanceId": "i"},
            428,
            id="release-without-a-key-is-428",
        ),
        pytest.param("/lease/request-release", {}, 428, id="request-release-without-a-key-is-428"),
        pytest.param("/lease/force-release", {}, 428, id="force-release-without-a-key-is-428"),
        pytest.param("/snapshots", {"changes": []}, 428, id="snapshot-without-a-key-is-428"),
    ],
)
async def test_only_the_heartbeat_is_exempt_from_the_idempotency_key(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    suffix: str,
    payload: dict[str, Any],
    expected: int,
) -> None:
    """The heartbeat is state-based and needs no key; every other lease POST is
    a 428 without one. The heartbeat's 409 proves it got past the dependency and
    reached the library, which a 428 never would.

    The precondition is sent on every case so the 428 can only be the missing
    key: without it the other precondition would answer 428 too and the test
    would pass with the idempotency dependency deleted."""
    folder = await _folder(fx)
    drive = await fx.drive()
    answered = await files_client.post(
        f"{_item(drive.id, folder.id)}{suffix}",
        json=payload,
        headers=await _precondition(real_session, folder.id),
    )
    assert answered.status_code == expected, answered.text
    if expected == 428:
        assert answered.json()["code"] == "files.idempotency_key_required", answered.text


# ---------------------------------------------------------------------------
# Listing my mounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("?mine=true", 200, id="mine-true-lists"),
        pytest.param("", 422, id="the-unfiltered-listing-is-refused"),
        pytest.param("?mine=false", 422, id="mine-false-is-refused"),
    ],
)
async def test_mine_is_required_on_the_lease_listing(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    query: str,
    expected: int,
) -> None:
    """A drive-wide listing would have to be filtered per row by readability, so
    day one answers only the question it can answer safely."""
    folder = await _folder(fx)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    listed = await files_client.get(f"{PREFIX}/drives/{drive.id}/leases{query}")
    assert listed.status_code == expected, listed.text
    if expected == 200:
        rows = listed.json()
        assert [row["nodeId"] for row in rows] == [str(folder.id)]


async def test_the_listing_shows_only_my_own_mounts(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Another member's mount is not mine, so it is not on my list."""
    folder = await _folder(fx)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    other = await login(client, files_org.member.email, files_org.member_password)
    listed = await other.get(f"{PREFIX}/drives/{drive.id}/leases?mine=true")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


# ---------------------------------------------------------------------------
# Force
# ---------------------------------------------------------------------------


async def test_force_release_gives_the_holder_a_grace_it_learns_about(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A force does not cut the holder off: its next beat succeeds and carries
    ``forced``, which is how a well-behaved client knows to wrap up."""
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    assert grant["forced"] is False

    forced = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/force-release",
        json={},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] is True

    beat = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": grant["epoch"], "instanceId": "instance-a"},
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["forced"] is True


async def test_the_grace_a_force_leaves_is_the_deployments_own_window(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A force hands out the same TTL acquire and heartbeat do.

    The route promises the holder learns it was forced from its next beat. On a
    deployment that widens the window the holder was told to beat every quarter
    of it — so a grace cut from the build's compiled-in minute would fence it
    hundreds of seconds before that beat was due, and the ``forced`` flag it
    was promised would never reach it.
    """
    monkeypatch.setattr(settings, "files_lease_ttl_seconds", 1800)
    monkeypatch.setattr(settings, "files_lease_heartbeat_seconds", 10_000)
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    forced = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/force-release",
        json={},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )

    assert forced.status_code == 200, forced.text
    # The grace on the row, by the database's own clock: a whole window, not a
    # minute. Anything shorter than the beat cadence is a holder cut off before
    # it could hear about the force.
    grace = await _seconds_left(real_session, folder.id)
    assert grace > 1800 * 0.9
    assert grace > grant["heartbeatEvery"]
    # And the grant the manager reads back describes that same window.
    assert forced.json()["heartbeatEvery"] == pytest.approx(grant["heartbeatEvery"])


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------


async def test_every_lease_route_is_dark_when_files_are_disabled(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    idem: Any,
) -> None:
    """Production ships the surface but not the behaviour: with the flag off the
    routes answer the same opaque 404 as a node that is not there."""
    folder = await _folder(fx)
    drive = await fx.drive()
    monkeypatch.setattr(settings, "files_enabled", False)
    dark = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert dark.status_code == 404
    assert refusal(dark) == NOT_FOUND


# ---------------------------------------------------------------------------
# A file created under a live lease
# ---------------------------------------------------------------------------


async def _room(fx: Any) -> Any:
    """The drive with the product's default ceilings, so a five-byte upload is
    not a 507 — a drive is created with no room at all."""
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    return drive


async def _upload(
    client: AsyncClient,
    parent_id: uuid.UUID,
    name: str,
    payload: bytes,
    idem: Any,
    *,
    fence: dict[str, str],
) -> Any:
    """Open, send the single part and complete — the path a *new* file takes.

    An edit reaches the server as a content PUT on a node that already exists;
    a file that is not there yet can only arrive through an upload session, so
    this is the write path a mount uses for every file it creates.
    """
    opened = await client.post(
        f"{PREFIX}/uploads",
        json={"declaredSize": len(payload), "name": name, "parentId": str(parent_id)},
        headers={**idem(), **fence},
    )
    if opened.status_code != 201:
        return opened
    upload_id = opened.json()["uploadId"]
    checksum = blake3(payload).digest().hex()
    part = await client.put(
        f"{PREFIX}/uploads/{upload_id}/parts/1",
        content=payload,
        headers={**idem(), "X-Part-Checksum": checksum, **fence},
    )
    assert part.status_code == 200, part.text
    return await client.post(
        f"{PREFIX}/uploads/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}]},
        headers={**idem(), **fence},
    )


async def test_a_file_created_under_the_holders_lease_has_its_bytes(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The holder's own new file is readable afterwards.

    A mount creates files as well as editing them, and a create that lands a
    node with no head version is silent data loss: the file appears in the
    listing and every read of it answers 404.
    """
    drive = await _room(fx)
    folder = await _folder(fx)
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    held = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }

    finished = await _upload(files_client, folder.id, "fresh.txt", b"hello", idem, fence=held)
    assert finished.status_code == 202, finished.text

    listed = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert listed.status_code == 200, listed.text
    made = [row for row in listed.json()["value"] if row["name"] == "fresh.txt"]
    assert made, listed.text

    served = await files_client.get(
        f"{_item(drive.id, uuid.UUID(made[0]['id']))}/content", follow_redirects=False
    )
    assert served.status_code == 302, served.text


#: What a holder used to put on the push that went with its release, and what
#: nothing on the server reads any more. Kept in the tests because a client in
#: the field still sends it, and what it must buy is nothing.
_HANDING_BACK = {"X-Alkera-Lease-Final": "1"}


async def _capped(fx: Any, org_admin: OrgWithAdmin, *, limit_bytes: int) -> Any:
    """The drive with room for nodes, and an org ceiling almost out of bytes.

    The ceiling is the org override rather than the drive row because that is
    what a deployment actually binds a write with: the resolver reads the
    override first and only falls back to the row when there is none, so a test
    that moved the row alone would be capping a number nothing reads.
    """
    drive = await fx.drive()
    drive.quota_nodes = settings.files_quota_default_nodes
    await fx._session.commit()
    async with AsyncSessionLocal() as session:
        await storage_limit_service.set_org_override(
            session, org_admin.org_id, limit_bytes=limit_bytes, by=None
        )
        await session.commit()
    return drive


async def _put(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    payload: bytes,
    idem: Any,
    session: AsyncSession,
    *,
    fence: dict[str, str],
) -> Any:
    """An edit: the whole body onto a node that already exists."""
    return await client.put(
        f"{_item(drive_id, node_id)}/content",
        content=payload,
        headers={
            **idem(),
            **fence,
            "If-Match": await node_etag(session, node_id),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )


async def _bytes_on_the_drive(session: AsyncSession, folder_id: uuid.UUID, name: str) -> int | None:
    """What the drive holds for one child, or ``None`` when there is no child.

    Read as a column rather than through the listing because the question is
    what LANDED: a refused write must leave the file at the bytes it had, and a
    refused create must leave no file behind at all.
    """
    return (
        await session.execute(
            text(
                "SELECT size FROM file_nodes "
                "WHERE parent_id = :parent AND name = :name AND trashed_at IS NULL"
            ),
            {"parent": folder_id, "name": name.encode()},
        )
    ).scalar_one_or_none()


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("edit", id="an-edit-of-a-file-the-folder-already-has"),
        pytest.param("create", id="a-file-the-push-is-adding"),
    ],
)
async def test_no_push_a_holder_can_make_lands_past_the_drives_byte_ceiling(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    org_admin: OrgWithAdmin,
    shape: str,
) -> None:
    """The holder's ordinary write is bounded by the drive; its hand-back is not.

    A lease used to exempt every write it fenced, which was harmless while a
    mount wrote at its checkpoints and became a standing bypass the moment the
    same fence carried a live plane reporting every few hundred milliseconds:
    an org could then write past its own ceiling for as long as a box stayed
    awake. Nor is a holder's last push: which push is the last is something
    only the box knows, so a marker saying so is a holder's own word, and a
    holder whose word bought the exemption would never be refused again.
    """
    drive = await _capped(fx, org_admin, limit_bytes=8)
    folder = await _folder(fx)
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    held = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }
    payload = b"x" * 64
    name = "out.txt" if shape == "edit" else "fresh.txt"
    edited = await fx.node(name.encode(), parent=folder) if shape == "edit" else None

    async def write(fence: dict[str, str]) -> Any:
        if edited is not None:
            return await _put(
                files_client, drive.id, edited.id, payload, idem, real_session, fence=fence
            )
        return await _upload(files_client, folder.id, name, payload, idem, fence=fence)

    refused = await write(held)
    assert refused.status_code == 507, refused.text
    assert refused.json()["code"] == "files.quota_bytes"
    # Nothing of the refused write is on the drive: an edit left the file at the
    # bytes it had, and a create left no file at all.
    assert await _bytes_on_the_drive(real_session, folder.id, name) == (
        0 if shape == "edit" else None
    )

    # ...and the same write calling itself the hand-back is refused identically.
    claimed = await write({**held, **_HANDING_BACK})
    assert claimed.status_code == 507, claimed.text
    assert claimed.json()["code"] == "files.quota_bytes"
    assert await _bytes_on_the_drive(real_session, folder.id, name) == (
        0 if shape == "edit" else None
    )


async def test_a_hand_back_marker_buys_an_unfenced_caller_nothing_either(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    org_admin: OrgWithAdmin,
) -> None:
    """A caller who is not even a holder is refused on the ceiling, not on the
    header: the marker is no longer a thing the server reads, so a write
    carrying it is exactly the write without it."""
    drive = await _capped(fx, org_admin, limit_bytes=8)
    folder = await _folder(fx)
    node = await fx.node(b"out.txt", parent=folder)

    answer = await _put(
        files_client, drive.id, node.id, b"x" * 64, idem, real_session, fence=_HANDING_BACK
    )
    assert answer.status_code == 507, answer.text
    assert answer.json()["code"] == "files.quota_bytes"
    assert await _bytes_on_the_drive(real_session, folder.id, "out.txt") == 0


async def _no_room_for_a_node(fx: Any) -> Any:
    """The drive with bytes to spare and no room for another node.

    Nodes rather than bytes because the thing a push adds that carries no bytes
    is a folder: the skeleton a box wrote locally has to be able to land, and
    the only ceiling that can refuse a folder is the node count.
    """
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = 0
    await fx._session.commit()
    return drive


async def _children_of(client: AsyncClient, drive_id: uuid.UUID, folder_id: uuid.UUID) -> list[str]:
    listed = await client.get(f"{_item(drive_id, folder_id)}/children")
    assert listed.status_code == 200, listed.text
    return sorted(row["name"] for row in listed.json()["value"])


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("children", id="one-folder-the-push-adds"),
        pytest.param("tree", id="the-skeleton-the-push-walks"),
    ],
)
async def test_no_create_a_holder_can_make_lands_past_the_drives_node_ceiling(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    shape: str,
) -> None:
    """The node ceiling answers the same question the byte ceiling does.

    A folder create under a live lease used to be exempt for as long as the
    fence was on it, so a box awake inside a drive that had reached its node
    count could keep minting folders with nothing able to refuse them. Saying
    the create is the hand-back's own is not a second chance at it: the drive
    cannot tell a holder that means it from one that says it on every create.
    """
    drive = await _no_room_for_a_node(fx)
    folder = await _folder(fx)
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    held = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }

    async def create(fence: dict[str, str]) -> Any:
        if shape == "children":
            return await files_client.post(
                f"{_item(drive.id, folder.id)}/children",
                json={"kind": "folder", "name": "outputs"},
                headers={**idem(), **fence, **await _precondition(real_session, folder.id)},
            )
        return await files_client.post(
            f"{_item(drive.id, folder.id)}/tree",
            json={"paths": ["outputs"]},
            headers={**idem(), **fence},
        )

    refused = await create(held)
    assert refused.status_code == 507, refused.text
    assert refused.json()["code"] == "files.quota_nodes"
    assert await _children_of(files_client, drive.id, folder.id) == []

    claimed = await create({**held, **_HANDING_BACK})
    assert claimed.status_code == 507, claimed.text
    assert claimed.json()["code"] == "files.quota_nodes"
    assert await _children_of(files_client, drive.id, folder.id) == []


async def test_an_unfenced_upload_into_someone_elses_mount_is_refused_before_the_node(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A writer with no epoch is told the folder is leased, and no node is left
    behind — the refusal has to come before the create, or the mount's folder
    grows a headless file the holder never wrote."""
    drive = await _room(fx)
    folder = await _folder(fx)
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    refused = await _upload(files_client, folder.id, "stranger.txt", b"hello", idem, fence={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"

    listed = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert [row["name"] for row in listed.json()["value"]] == []


async def test_an_unfenced_create_into_someone_elses_mount_leaves_no_child(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Creation is fenced like every other write into a held folder.

    A holds the folder; an unfenced ``children`` POST is 409 ``files.leased``
    and the folder's listing is unchanged. Without the fence the create
    answered 201 and A's next reconcile had to discover a node it never wrote
    as a conflict."""
    drive = await _room(fx)
    folder = await _folder(fx)
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    refused = await files_client.post(
        f"{_item(drive.id, folder.id)}/children",
        json={"kind": "folder", "name": "intruder"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"

    listed = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert [row["name"] for row in listed.json()["value"]] == []


async def test_an_unfenced_tree_create_into_someone_elses_mount_leaves_no_folders(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The same for the dropped-directory skeleton: a ``tree`` POST with no
    epoch is refused before the first folder, so a drag-and-drop into a mount
    the caller does not hold plants nothing."""
    drive = await _room(fx)
    folder = await _folder(fx)
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    refused = await files_client.post(
        f"{_item(drive.id, folder.id)}/tree",
        json={"paths": ["a/b", "a/c"]},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"

    listed = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert [row["name"] for row in listed.json()["value"]] == []


async def test_the_holders_own_fenced_skeleton_lands_inside_its_mount(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The holder's own ``tree`` and ``children`` POSTs, fenced, create.

    This is the first two hops of every push a box makes of a chat's folder:
    a ``tree`` at the drive root that only walks into the folder the lease
    is on (nothing made, 201), then a ``tree`` on the leased node itself for
    the folders the chat wrote. The route fenced the parent correctly and then
    asked the library to create each folder without the caller's lease — so
    the library refused the holder's own write as "leased", and no chat's
    work ever reached Files.
    """
    drive = await _room(fx)
    folder = await _folder(fx)
    root_id = (await files_client.get(f"{PREFIX}/drives")).json()["rootId"]
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    held = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }

    walked = await files_client.post(
        f"{PREFIX}/drives/{drive.id}/items/{root_id}/tree",
        json={"paths": ["project"]},
        headers={**idem(), **held},
    )
    assert walked.status_code == 201, walked.text
    assert walked.json() == []

    made = await files_client.post(
        f"{_item(drive.id, folder.id)}/tree",
        json={"paths": [".runtime/agent", "scratch"]},
        headers={**idem(), **held},
    )
    assert made.status_code == 201, made.text
    assert sorted(row["name"] for row in made.json()) == [".runtime", "agent", "scratch"]

    child = await files_client.post(
        f"{_item(drive.id, folder.id)}/children",
        json={"kind": "folder", "name": "outputs"},
        headers={**idem(), **held, **await _precondition(real_session, folder.id)},
    )
    assert child.status_code == 201, child.text

    listed = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert sorted(row["name"] for row in listed.json()["value"]) == [
        ".runtime",
        "outputs",
        "scratch",
    ]
    runtime = next(row for row in listed.json()["value"] if row["name"] == ".runtime")
    nested = await files_client.get(f"{_item(drive.id, uuid.UUID(runtime['id']))}/children")
    assert [row["name"] for row in nested.json()["value"]] == ["agent"]


@pytest.mark.parametrize(
    ("epoch_shift", "instance", "code"),
    [
        pytest.param(-1, "instance-a", "files.lease_fenced", id="a-superseded-epoch"),
        pytest.param(0, "instance-z", "files.lease_fenced", id="another-instance"),
    ],
)
async def test_a_fenced_skeleton_under_the_wrong_lease_plants_nothing(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    epoch_shift: int,
    instance: str,
    code: str,
) -> None:
    """Carrying *a* fence is not holding the folder: a box whose lease was
    taken over, or a second holder id, is fenced on the tree route exactly as
    on the snapshot route, and the folder's listing is unchanged."""
    drive = await _room(fx)
    folder = await _folder(fx)
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    refused = await files_client.post(
        f"{_item(drive.id, folder.id)}/tree",
        json={"paths": ["scratch"]},
        headers={
            **idem(),
            "X-Alkera-Lease-Epoch": str(grant["epoch"] + epoch_shift),
            "X-Alkera-Lease-Instance": instance,
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == code

    listed = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert [row["name"] for row in listed.json()["value"]] == []


# ---------------------------------------------------------------------------
# What everyone else sees while it holds
# ---------------------------------------------------------------------------


async def _reader(client: AsyncClient, fx: Any, files_org: Any, node: Any) -> AsyncClient:
    """The org's plain member, granted ``reader`` on ``node`` and logged in."""
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo, ctx, node, Principal(kind="user", id=files_org.member.id), ROLE_READER
        )
    await fx.repo.session.commit()
    member = app_client()
    await login(member, files_org.member.email, files_org.member_password)
    return member


async def _sync(session: AsyncSession, node_id: uuid.UUID, *, behind_seconds: int) -> None:
    """Pin how far the live lease on ``node_id`` has fallen behind its beat.

    ``stale`` is the SPEC's clock rule — a holder still beating whose last push
    is more than two sync intervals behind — so a test that wants one answer or
    the other has to move the lease's own clocks, never who is asking.
    """
    await session.execute(
        text(
            "UPDATE file_leases SET last_sync_at = heartbeat_at - make_interval(secs => :behind) "
            "WHERE node_id = :node AND released_at IS NULL"
        ),
        {"behind": behind_seconds, "node": node_id},
    )
    await session.commit()


async def _reap(session: AsyncSession, node_id: uuid.UUID) -> None:
    """Stamp the lease reaped while pushing its deadline an hour out.

    That combination is not contrived: the reaper stamps whatever row is there
    at that instant, and a beat that landed just before it leaves a deadline far
    in the future. A predicate that read only the deadline would go on calling
    the row live after everything it had in flight was undone.
    """
    await session.execute(
        text(
            "UPDATE file_leases SET reaped_at = now(), "
            "expires_at = now() + make_interval(secs => 3600) "
            "WHERE node_id = :node AND released_at IS NULL"
        ),
        {"node": node_id},
    )
    await session.commit()


async def test_a_live_lease_renders_on_the_item_for_the_holder_and_for_a_second_member(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The half of "someone has this mounted" a client can act on: the item read
    carries the lease for whoever can read the node, and says whose copy it is.

    ``stale`` is not who is asking. A holder that pushed a moment ago is
    current for everyone — the holder AND the second member — and the same
    lease left two sync intervals behind its own heartbeat is stale for the
    same everyone. Answering it from ``mine`` told a second member that a
    folder synced one second ago was a stale copy."""
    folder = await _folder(fx)
    drive = await fx.drive()
    granted = await _acquire(files_client, drive.id, folder.id, idem, real_session)
    assert granted.status_code == 200, granted.text
    await _sync(real_session, folder.id, behind_seconds=1)

    mine = await files_client.get(_item(drive.id, folder.id))
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["lease"] is not None, "the holder's own item read renders no lease"
    assert body["lease"]["machine"] == "machine-a"
    # The item routes label the holder the way they label an owner: a person's
    # name, because the facet's holder is what a badge puts in a sentence.
    assert body["lease"]["holder"] == "Test Admin"
    assert body["lease"]["purpose"] == "mount"
    assert body["lease"]["since"] and body["lease"]["expires_at"]
    assert body["lease"]["mine"] is True
    assert body["stale"] is False

    member = await _reader(client, fx, files_org, folder)
    theirs = await member.get(_item(drive.id, folder.id))
    assert theirs.status_code == 200, theirs.text
    seen = theirs.json()
    assert seen["lease"] is not None, "a second member is told nothing holds the folder"
    assert seen["lease"]["machine"] == "machine-a"
    assert seen["lease"]["holder"] == "Test Admin"
    assert seen["lease"]["mine"] is False
    assert seen["stale"] is False, "a folder the holder synced a second ago read as stale"

    # The same lease, the same reader, only the clock moved.
    await _sync(real_session, folder.id, behind_seconds=300)
    behind = await member.get(_item(drive.id, folder.id))
    assert behind.status_code == 200, behind.text
    assert behind.json()["stale"] is True
    # And the holder is told the same thing about its own mount.
    holder = await files_client.get(_item(drive.id, folder.id))
    assert holder.status_code == 200, holder.text
    assert holder.json()["lease"]["mine"] is True
    assert holder.json()["stale"] is True


async def test_the_lease_governs_a_file_under_the_folder_and_its_listing_row(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A lease is held on a folder and governs its subtree, so a file inside it
    carries the facet too — on its own read and on the row the parent's children
    page renders, which is where a browser draws the badge."""
    folder = await _folder(fx)
    inside = await fx.node(b"inside.txt", parent=folder)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    await _sync(real_session, folder.id, behind_seconds=300)

    member = await _reader(client, fx, files_org, folder)
    child = await member.get(_item(drive.id, inside.id))
    assert child.status_code == 200, child.text
    assert child.json()["lease"] is not None, "a file under the mount renders no lease"
    assert child.json()["lease"]["machine"] == "machine-a"
    assert child.json()["stale"] is True

    page = await member.get(f"{_item(drive.id, folder.id)}/children")
    assert page.status_code == 200, page.text
    rows = {row["id"]: row for row in page.json()["value"]}
    assert str(inside.id) in rows, page.text
    assert rows[str(inside.id)]["lease"] is not None, "the listing row renders no lease"
    assert rows[str(inside.id)]["lease"]["machine"] == "machine-a"
    assert rows[str(inside.id)]["stale"] is True

    # The listing row answers the clock too, not the reader: the same page for
    # the same member reads current once the holder has pushed.
    await _sync(real_session, folder.id, behind_seconds=1)
    fresh = await member.get(f"{_item(drive.id, folder.id)}/children")
    assert fresh.status_code == 200, fresh.text
    fresh_rows = {row["id"]: row for row in fresh.json()["value"]}
    assert fresh_rows[str(inside.id)]["lease"] is not None
    assert fresh_rows[str(inside.id)]["stale"] is False


async def test_a_released_lease_leaves_the_item_with_no_lease(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The facet is a statement about now: once the mount is released the item
    reads exactly as it did before it, so a stale badge cannot outlive it."""
    folder = await _folder(fx)
    drive = await fx.drive()
    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    released = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": grant["epoch"], "instanceId": "instance-a"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert released.status_code in (200, 204), released.text

    after = await files_client.get(_item(drive.id, folder.id))
    assert after.status_code == 200, after.text
    assert after.json()["lease"] is None
    assert after.json()["stale"] is False


async def test_a_reaped_lease_renders_on_neither_the_item_nor_its_listing_row(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A reaped lease is over, whatever its deadline says.

    The reaper aborted the holder's upload sessions and released their holds, so
    every write the row would still admit is answered ``files.lease_fenced``.
    Rendering it as a live mount — with a holder, on the folder and on every row
    under it — told a member a folder was being worked on that nothing can be
    written to.
    """
    folder = await _folder(fx)
    inside = await fx.node(b"inside.txt", parent=folder)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    await _reap(real_session, folder.id)

    held = await files_client.get(_item(drive.id, folder.id))
    assert held.status_code == 200, held.text
    assert held.json()["lease"] is None
    assert held.json()["stale"] is False

    child = await files_client.get(_item(drive.id, inside.id))
    assert child.status_code == 200, child.text
    assert child.json()["lease"] is None

    page = await files_client.get(f"{_item(drive.id, folder.id)}/children")
    assert page.status_code == 200, page.text
    rows = {row["id"]: row for row in page.json()["value"]}
    assert rows[str(inside.id)]["lease"] is None


async def test_a_reaped_lease_is_off_my_list_however_far_ahead_its_deadline_is(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The listing is what a mount client reconciles its own folders against, so
    a row it can no longer write to must not read as one it still holds."""
    folder = await _folder(fx)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200
    listed = await files_client.get(f"{PREFIX}/drives/{drive.id}/leases?mine=true")
    assert [row["nodeId"] for row in listed.json()] == [str(folder.id)]

    await _reap(real_session, folder.id)

    after = await files_client.get(f"{PREFIX}/drives/{drive.id}/leases?mine=true")
    assert after.status_code == 200, after.text
    assert after.json() == []


async def test_a_holder_whose_lease_was_reaped_comes_back_at_a_new_epoch(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A resume keeps its epoch because nothing it had in flight was fenced. The
    reaper is precisely the thing that fences it — the sessions that epoch was
    uploading into are aborted and their holds are zeroed — so the same holder
    asking again is starting over, not resuming, and starts over at a fresh
    epoch with no last sync to its name.
    """
    folder = await _folder(fx)
    drive = await fx.drive()
    first = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    await _sync(real_session, folder.id, behind_seconds=1)

    await _reap(real_session, folder.id)
    again = await _acquire(files_client, drive.id, folder.id, idem, real_session)

    assert again.status_code == 200, again.text
    assert again.json()["epoch"] > first["epoch"]
    row = (
        await real_session.execute(
            text("SELECT epoch, reaped_at, last_sync_at FROM file_leases WHERE node_id = :node"),
            {"node": folder.id},
        )
    ).one()
    assert row.epoch == again.json()["epoch"]
    assert row.reaped_at is None
    assert row.last_sync_at is None, "the new epoch inherited the reaped epoch's last sync"


async def test_the_lease_facet_is_not_an_oracle_for_a_node_the_caller_cannot_read(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Rendering the lease widened what an item read says, so the no-oracle
    contract is re-asserted on top of it: a same-org member with no grant on a
    leased folder gets the same opaque 404 as an id that does not exist."""
    folder = await _folder(fx)
    drive = await fx.drive()
    assert (
        await _acquire(files_client, drive.id, folder.id, idem, real_session)
    ).status_code == 200

    other = await login(client, files_org.member.email, files_org.member_password)
    unreadable = await other.get(_item(drive.id, folder.id))
    nonexistent = await other.get(_item(drive.id, uuid.uuid4()))
    assert unreadable.status_code == nonexistent.status_code == 404
    assert refusal(unreadable) == refusal(nonexistent) == NOT_FOUND


# ---------------------------------------------------------------------------
# A chat's folder, through the same routes
# ---------------------------------------------------------------------------


async def test_a_chat_folder_leases_through_the_routes_and_is_handed_back(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The lease an awake chat takes is the wire's fourth purpose.

    Asserted on the row rather than on the echo, so a route that accepted the
    word and stored something else fails here.
    """
    folder = await _folder(fx)
    drive = await fx.drive()

    granted = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": "mirror-1", "machineId": "box-7", "purpose": "chat"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text
    grant = granted.json()
    stored = (
        await real_session.execute(
            text("SELECT purpose FROM file_leases WHERE node_id = :n"), {"n": folder.id}
        )
    ).scalar_one()
    assert stored == "chat"

    beat = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/heartbeat",
        json={"epoch": grant["epoch"], "instanceId": "mirror-1"},
    )
    assert beat.status_code == 200, beat.text

    # A second box asking for the same chat is refused while the first holds it.
    second = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": "mirror-2", "machineId": "box-8", "purpose": "chat"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert second.status_code == 409, second.text

    # A box's lease is forced off by the owner or an org admin, with a reason.
    unexplained = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/force-release",
        json={},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert unexplained.status_code == 422, unexplained.text
    assert unexplained.json()["code"] == "files.force_reason_required"
    forced = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/force-release",
        json={"reason": "the box is gone"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["forced"] is True

    released = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": grant["epoch"], "instanceId": "mirror-1", "final": None},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert released.status_code == 200, released.text

    # Sleep returned it: the next box wakes the chat at a higher epoch.
    resumed = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": "mirror-2", "machineId": "box-8", "purpose": "chat"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["epoch"] > grant["epoch"]


async def test_an_unknown_lease_purpose_is_refused_by_the_wire(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The purpose vocabulary is closed: a fifth word is a 422, never a row."""
    folder = await _folder(fx)
    drive = await fx.drive()

    refused = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": "mirror-1", "machineId": "box-7", "purpose": "chatt"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert refused.status_code == 422, refused.text
    count = (
        await real_session.execute(
            text("SELECT count(*) FROM file_leases WHERE node_id = :n"), {"n": folder.id}
        )
    ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# The box reads its own chat folder back down; nobody else downloads a chat
# ---------------------------------------------------------------------------


async def _sealed_chat_folder(fx: Any, session: AsyncSession, *, machine: str | None = None) -> Any:
    """A chat's folder as the bridge makes it: ``NO_DOWNLOAD`` on the folder,
    inherited by the file the chat wrote inside it.

    ``machine`` binds the conversation to the box running it — the second fact
    the drive needs before a request may act as that chat's machine, the first
    being a verified assertion. It also marks the node a chat, so the
    machine-only confinement applies to it the way it does in the product.
    """
    from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT

    folder = await fx.node(
        b"Kickoff.alkerachat", kind="folder", flags=NO_DOWNLOAD_BIT, parent=await fx.home()
    )
    await session.execute(
        text("UPDATE file_nodes SET subtype = 'chat' WHERE id = :node"), {"node": folder.id}
    )
    await session.commit()
    await bind_chat(fx, session, folder, machine=machine)
    return folder


async def _redeem_on_the_content_origin(
    client: AsyncClient, location: str, headers: dict[str, str]
) -> Any:
    """Follow a mint to the content origin: the same app, under the Host the
    content mount is gated on (the minted URL names it)."""
    from urllib.parse import urlsplit

    parts = urlsplit(location)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return await client.get(path, headers={"Host": parts.netloc, **headers})


def _can_download(row: dict[str, Any]) -> bool:
    capabilities = row["capabilities"]
    return bool(capabilities.get("can_download", capabilities.get("canDownload")))


async def test_the_box_holding_a_chat_folders_lease_reads_its_bytes_and_nobody_else_does(
    files_client: AsyncClient,
    proven_box: tuple[AsyncClient, str],
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume on a fresh box is a pull of a ``NO_DOWNLOAD`` folder.

    The box — an agent whose assertion was checked against its registration —
    holds the chat folder's live lease, sends its fence on the read, is told
    ``canDownload`` on the listing row and is served the content (302 to the
    signed URL). The same box without its fence, the same box at a stale epoch,
    and the person who owns the chat sending the box's own fence are all
    refused the bytes: the item reads, so the content answers the visible 403 a
    sealed folder always has.
    """
    # The content origin the mint points at, as the deployment has one; the
    # redeem below is sent under that Host, the way the content route tests do.
    monkeypatch.setattr(settings, "files_content_base_url", "http://files.localhost:8000")
    box, machine_id = proven_box
    drive = await _room(fx)
    folder = await _sealed_chat_folder(fx, real_session, machine=machine_id)
    instance = f"{machine_id}:chat-a"

    granted = await box.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": instance, "machineId": machine_id, "purpose": "chat"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text
    epoch = granted.json()["epoch"]
    fence = {"X-Alkera-Lease-Epoch": str(epoch), "X-Alkera-Lease-Instance": instance}
    stale = {**fence, "X-Alkera-Lease-Epoch": str(epoch + 1)}

    finished = await _upload(box, folder.id, "qau.txt", b"one", idem, fence=fence)
    assert finished.status_code == 202, finished.text
    listed = await box.get(f"{_item(drive.id, folder.id)}/children", headers=fence)
    assert listed.status_code == 200, listed.text
    rows = {row["name"]: row for row in listed.json()["value"]}
    file_id = uuid.UUID(rows["qau.txt"]["id"])
    inherited = (
        await real_session.execute(
            text("SELECT flags FROM file_nodes WHERE id = :n"), {"n": file_id}
        )
    ).scalar_one()
    assert inherited & 1, "the file the box wrote inherits the chat's NO_DOWNLOAD"

    # The holder, under its fence: the listing says so and the bytes come down.
    assert _can_download(rows["qau.txt"]) is True
    served = await box.get(
        f"{_item(drive.id, file_id)}/content", headers=fence, follow_redirects=False
    )
    assert served.status_code == 302, served.text
    folder_item = await box.get(f"{_item(drive.id, folder.id)}", headers=fence)
    assert folder_item.status_code == 200 and _can_download(folder_item.json()) is True
    # The signed URL is redeemed on the content origin, where the policy runs
    # again — as the box, under the fence it minted with — and the bytes come.
    redeemed = await _redeem_on_the_content_origin(box, served.headers["location"], fence)
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.content == b"one"
    # The same mint, redeemed without the fence, is refused: the URL alone is
    # not a way out of a sealed folder (visibly — the box can read the node).
    minted_again = await box.get(
        f"{_item(drive.id, file_id)}/content", headers=fence, follow_redirects=False
    )
    assert minted_again.status_code == 302, minted_again.text
    unfenced = await _redeem_on_the_content_origin(box, minted_again.headers["location"], {})
    assert unfenced.status_code == 403, unfenced.text

    # Everyone else is still refused the chat's bytes — the item itself reads.
    # The last two are the attack the fence exists for: the SAME session the box
    # runs under is not the box, and the box's own public machine id, spelled by
    # a member on their own credential, is not the box either.
    async with app_client(
        cookies=files_client.cookies, headers=agent_headers(machine_id)
    ) as impostor:
        refused = {
            "the-box-unfenced": (box, {}),
            "the-box-at-a-stale-epoch": (box, stale),
            "the-owner-with-the-box's-fence": (files_client, fence),
            "the-owner": (files_client, {}),
            "the-owner-asserting-the-box's-public-machine-id": (impostor, fence),
        }
        for label, (caller, headers) in refused.items():
            row = await caller.get(f"{_item(drive.id, folder.id)}/children", headers=headers)
            assert row.status_code == 200, (label, row.text)
            listing = {r["name"]: r for r in row.json()["value"]}
            assert _can_download(listing["qau.txt"]) is False, label
            content = await caller.get(
                f"{_item(drive.id, file_id)}/content", headers=headers, follow_redirects=False
            )
            assert content.status_code == 403, (label, content.text)
            item = await caller.get(f"{_item(drive.id, file_id)}", headers=headers)
            assert item.status_code == 200, (label, item.text)

        # …and the impostor cannot write under the fence either: a live report
        # is the box's own verb, and the batch it sends is refused rather than
        # recorded against the holder's plane.
        reported = await impostor.post(
            f"{_item(drive.id, folder.id)}/lease/live",
            json={"entries": [{"nodeId": str(file_id), "state": "uploading"}]},
            headers=fence,
        )
        assert reported.status_code in (403, 409), reported.text

    # Once the box hands the folder back, its fence proves nothing any more.
    released = await box.post(
        f"{_item(drive.id, folder.id)}/lease/release",
        json={"epoch": epoch, "instanceId": instance, "final": None},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert released.status_code == 200, released.text
    after = await box.get(
        f"{_item(drive.id, file_id)}/content", headers=fence, follow_redirects=False
    )
    assert after.status_code == 403, after.text


# ---------------------------------------------------------------------------
# Deleting inside a mount: the holder's own fence has to reach the library
# ---------------------------------------------------------------------------


async def _trashed(session: AsyncSession, node_id: uuid.UUID) -> bool:
    return (
        await session.execute(
            text("SELECT trashed_at FROM file_nodes WHERE id = :n"), {"n": node_id}
        )
    ).scalar_one() is not None


async def test_the_holder_may_trash_a_file_inside_its_own_mount(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A box deleting a file in the folder it holds is its own write, not a
    stranger's.

    The fence refuses every unfenced write inside a mount, so a route that
    decides the delete and then calls the library without the caller's epoch
    tells the holder its own folder is leased — the box can create and replace
    the files it mirrors but can never remove one, and the deletion is stranded
    on the machine forever.
    """
    drive = await _room(fx)
    folder = await _folder(fx)
    doomed = await fx.node(b"gone.txt", parent=folder)
    await fx.version(doomed)

    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()
    held = {
        "X-Alkera-Lease-Epoch": str(grant["epoch"]),
        "X-Alkera-Lease-Instance": "instance-a",
    }

    deleted = await files_client.delete(
        _item(drive.id, doomed.id),
        headers={**idem(), **held, **await _precondition(real_session, doomed.id)},
    )
    assert deleted.status_code == 200, deleted.text
    assert await _trashed(real_session, doomed.id)

    # The holder is not told about its own deletion: the live plane carries the
    # differences a mount has yet to see, and this one it made itself.
    inbound = (
        await real_session.execute(
            text(
                "SELECT state FROM file_lease_live_entries WHERE node_id = :n "
                "AND state IN ('inbound', 'inbound_delete', 'inbound_rename')"
            ),
            {"n": doomed.id},
        )
    ).all()
    assert inbound == []


@pytest.mark.parametrize(
    ("epoch_shift", "instance"),
    [
        pytest.param(-1, "instance-a", id="a-superseded-epoch"),
        pytest.param(0, "instance-z", id="another-instance"),
    ],
)
async def test_a_trash_under_the_wrong_lease_leaves_the_file_where_it_is(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    epoch_shift: int,
    instance: str,
) -> None:
    """Threading the epoch must not make any epoch do: a superseded fence and a
    second instance are both refused, and the file stays live."""
    drive = await _room(fx)
    folder = await _folder(fx)
    doomed = await fx.node(b"stays.txt", parent=folder)
    await fx.version(doomed)

    grant = (await _acquire(files_client, drive.id, folder.id, idem, real_session)).json()

    refused = await files_client.delete(
        _item(drive.id, doomed.id),
        headers={
            **idem(),
            "X-Alkera-Lease-Epoch": str(grant["epoch"] + epoch_shift),
            "X-Alkera-Lease-Instance": instance,
            **await _precondition(real_session, doomed.id),
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_fenced"
    assert not await _trashed(real_session, doomed.id)


async def test_an_unfenced_trash_inside_an_awake_chat_is_refused(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """A member deleting inside a chat folder a box has awake is told it is
    leased: the mount takes no inbound writes, so the file stays live."""
    drive = await _room(fx)
    folder = await _folder(fx)
    doomed = await fx.node(b"mirrored.txt", parent=folder)
    await fx.version(doomed)

    granted = await files_client.post(
        f"{_item(drive.id, folder.id)}/lease",
        json={"instanceId": "mirror-1", "machineId": "box-7", "purpose": "chat"},
        headers={**idem(), **await _precondition(real_session, folder.id)},
    )
    assert granted.status_code == 200, granted.text

    refused = await files_client.delete(
        _item(drive.id, doomed.id),
        headers={**idem(), **await _precondition(real_session, doomed.id)},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.leased"
    assert not await _trashed(real_session, doomed.id)


async def test_a_trash_outside_every_lease_is_unchanged(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The ordinary delete — no lease anywhere near it — still trashes and
    still answers with the operation a client undoes."""
    drive = await _room(fx)
    folder = await _folder(fx)
    doomed = await fx.node(b"ordinary.txt", parent=folder)
    await fx.version(doomed)

    deleted = await files_client.delete(
        _item(drive.id, doomed.id),
        headers={**idem(), **await _precondition(real_session, doomed.id)},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["kind"] == "trash"
    assert await _trashed(real_session, doomed.id)


# ---------------------------------------------------------------------------
# What a person drops into an awake chat from the composer
# ---------------------------------------------------------------------------


async def _awake_chat(fx: Any, session: AsyncSession) -> tuple[Any, Any]:
    """A chat folder as the bridge mints it — the object behind the node is
    what makes it a chat, never its name — holding its ``scratch/`` working
    directory."""
    chat = await fx.node(b"Kickoff.alkerachat", kind="folder", parent=await fx.home())
    scratch = await fx.node(b"scratch", kind="folder", parent=chat)
    await session.execute(
        text("UPDATE file_nodes SET subtype = 'chat', target_object_id = :object WHERE id = :node"),
        {"object": uuid.uuid4(), "node": chat.id},
    )
    await session.commit()
    return chat, scratch


async def _wake(
    client: AsyncClient, drive_id: uuid.UUID, chat_id: uuid.UUID, idem: Any, session: AsyncSession
) -> dict[str, str]:
    """The box takes the chat's lease; answers the fence headers it writes under."""
    granted = await client.post(
        f"{_item(drive_id, chat_id)}/lease",
        json={"instanceId": "mirror-1", "machineId": "box-7", "purpose": "chat"},
        headers={**idem(), **await _precondition(session, chat_id)},
    )
    assert granted.status_code == 200, granted.text
    return {
        "X-Alkera-Lease-Epoch": str(granted.json()["epoch"]),
        "X-Alkera-Lease-Instance": "mirror-1",
    }


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


async def test_a_composer_upload_into_an_awake_chat_lands_in_its_working_directory(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The composer aims an upload at the chat node with no lease headers while
    a box holds the chat. It is taken as inbound: the file lands in the working
    directory the agent reads, the live plane records it, and the holder finds
    it on its next drain through the route the box actually calls."""
    drive = await _room(fx)
    chat, scratch = await _awake_chat(fx, real_session)
    held = await _wake(files_client, drive.id, chat.id, idem, real_session)

    finished = await _upload(
        files_client, chat.id, "apollo_leads_import.csv", b"name,email\n", idem, fence={}
    )
    assert finished.status_code == 202, finished.text

    landed = await _landed(real_session, drive.id, b"apollo_leads_import.csv")
    assert landed is not None
    assert landed.parent_id == scratch.id

    recorded = (
        await real_session.execute(
            text("SELECT state FROM file_lease_live_entries WHERE node_id = :node"),
            {"node": landed.id},
        )
    ).scalar_one_or_none()
    assert recorded == "inbound"

    drained = await files_client.get(
        f"{_item(drive.id, chat.id)}/lease/live", params={"inbound": "true"}, headers=held
    )
    assert drained.status_code == 200, drained.text
    assert [(row["nodeId"], row["state"]) for row in drained.json()["entries"]] == [
        (str(landed.id), "inbound")
    ]


@pytest.mark.parametrize(
    "fence",
    [
        pytest.param({}, id="no-fence-at-all"),
        pytest.param(
            {"X-Alkera-Lease-Epoch": "0", "X-Alkera-Lease-Instance": "mirror-1"},
            id="a-superseded-epoch",
        ),
        pytest.param(
            {"X-Alkera-Lease-Epoch": "{epoch}", "X-Alkera-Lease-Instance": "mirror-2"},
            id="another-instance",
        ),
    ],
)
async def test_the_inbound_drain_is_fenced_to_the_holder(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    fence: dict[str, str],
) -> None:
    """Whatever is waiting for the box is not readable — and so not drainable —
    by anyone who does not hold the lease at its live epoch."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session)
    held = await _wake(files_client, drive.id, chat.id, idem, real_session)
    headers = {k: v.format(epoch=held["X-Alkera-Lease-Epoch"]) for k, v in fence.items()}

    refused = await files_client.get(
        f"{_item(drive.id, chat.id)}/lease/live", params={"inbound": "true"}, headers=headers
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_fenced"


async def test_an_applied_entry_is_not_offered_again(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The box says it materialised the drop; the drive stops offering it, so
    the next beat does not download it a second time over a file the agent may
    since have edited."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session)
    held = await _wake(files_client, drive.id, chat.id, idem, real_session)
    assert (
        await _upload(files_client, chat.id, "leads.csv", b"a,b\n", idem, fence={})
    ).status_code == 202
    landed = await _landed(real_session, drive.id, b"leads.csv")
    assert landed is not None

    settled = await files_client.post(
        f"{_item(drive.id, chat.id)}/lease/live",
        json={"entries": [{"nodeId": str(landed.id), "state": "applied"}]},
        headers=held,
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["pending"] == 0
    assert settled.json()["liveSeq"] > 0

    drained = await files_client.get(
        f"{_item(drive.id, chat.id)}/lease/live", params={"inbound": "true"}, headers=held
    )
    assert drained.status_code == 200, drained.text
    assert drained.json()["entries"] == []

    # Resent — the row is already gone, and that is not an error.
    again = await files_client.post(
        f"{_item(drive.id, chat.id)}/lease/live",
        json={"entries": [{"nodeId": str(landed.id), "state": "applied"}]},
        headers=held,
    )
    assert again.status_code == 200, again.text


@pytest.mark.parametrize(
    "fence",
    [
        pytest.param({}, id="no-fence-at-all"),
        pytest.param(
            {"X-Alkera-Lease-Epoch": "0", "X-Alkera-Lease-Instance": "mirror-1"},
            id="a-superseded-epoch",
        ),
        pytest.param(
            {"X-Alkera-Lease-Epoch": "{epoch}", "X-Alkera-Lease-Instance": "mirror-2"},
            id="another-instance",
        ),
    ],
)
async def test_a_stranger_cannot_settle_what_the_holder_is_owed(
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Any,
    fence: dict[str, str],
) -> None:
    """Only the holder at its live epoch may say a drop was applied: anyone
    else is fenced, and the entry is still there for the box to drain."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session)
    held = await _wake(files_client, drive.id, chat.id, idem, real_session)
    assert (
        await _upload(files_client, chat.id, "leads.csv", b"a,b\n", idem, fence={})
    ).status_code == 202
    landed = await _landed(real_session, drive.id, b"leads.csv")
    assert landed is not None
    headers = {k: v.format(epoch=held["X-Alkera-Lease-Epoch"]) for k, v in fence.items()}

    refused = await files_client.post(
        f"{_item(drive.id, chat.id)}/lease/live",
        json={"entries": [{"nodeId": str(landed.id), "state": "applied"}]},
        headers=headers,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.lease_fenced"

    drained = await files_client.get(
        f"{_item(drive.id, chat.id)}/lease/live", params={"inbound": "true"}, headers=held
    )
    assert [row["nodeId"] for row in drained.json()["entries"]] == [str(landed.id)]


async def test_the_holders_own_record_stays_at_the_chat_root(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """The box hands its records back to the chat's top level under its epoch.
    Only an unfenced drop is steered into the working directory; a hand-back
    lands exactly where the holder aimed it, and nothing is recorded as inbound
    for a write the holder made itself."""
    drive = await _room(fx)
    chat, _scratch = await _awake_chat(fx, real_session)
    held = await _wake(files_client, drive.id, chat.id, idem, real_session)

    finished = await _upload(files_client, chat.id, "manifest.json", b"{}", idem, fence=held)
    assert finished.status_code == 202, finished.text

    landed = await _landed(real_session, drive.id, b"manifest.json")
    assert landed is not None
    assert landed.parent_id == chat.id
    assert (
        await real_session.execute(
            text("SELECT count(*) FROM file_lease_live_entries WHERE node_id = :node"),
            {"node": landed.id},
        )
    ).scalar_one() == 0


async def test_a_drop_onto_a_sleeping_chat_lands_in_its_working_directory_too(
    files_client: AsyncClient, fx: Any, real_session: AsyncSession, idem: Any
) -> None:
    """Where a drop lands is the chat's layout, not the lease's: with no box
    holding it, the composer's file still goes to the working directory and
    nothing is owed to a holder that does not exist."""
    drive = await _room(fx)
    chat, scratch = await _awake_chat(fx, real_session)

    finished = await _upload(files_client, chat.id, "notes.csv", b"a,b\n", idem, fence={})
    assert finished.status_code == 202, finished.text

    landed = await _landed(real_session, drive.id, b"notes.csv")
    assert landed is not None
    assert landed.parent_id == scratch.id
    assert (
        await real_session.execute(
            text("SELECT count(*) FROM file_lease_live_entries WHERE node_id = :node"),
            {"node": landed.id},
        )
    ).scalar_one() == 0


async def test_a_content_url_a_box_minted_dies_with_its_machine_credential(
    files_client: AsyncClient,
    fx: Any,
    files_org: Any,
    real_session: AsyncSession,
    idem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The content origin has no session to re-verify, so it trusts the
    machine the signed claim names. A URL a platform box minted a moment before
    its credential was revoked must not carry the box's standing past the
    revoke: the redeem re-asks whether the machine still stands, and a box the
    platform took away is served nothing from its sealed chat folder."""
    monkeypatch.setattr(settings, "files_content_base_url", "http://files.localhost:8000")
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
        platform=True,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as box:
        drive = await _room(fx)
        folder = await _sealed_chat_folder(fx, real_session, machine=machine_id)
        # The chat was shared with the box's operator, so the box's reach on
        # its folder is a rung of its own rather than an org-admin floor.
        async with fx.repo.transaction():
            await acl.grant(
                fx.repo,
                ActingContext.for_user(
                    user_id=files_org.org.admin_id,
                    org_id=files_org.org.org_id,
                    email="fixture@test",
                ),
                folder,
                Principal(kind="user", id=files_org.org.admin_id),
                "writer",
            )
        instance = f"{machine_id}:chat-a"
        granted = await box.post(
            f"{_item(drive.id, folder.id)}/lease",
            json={"instanceId": instance, "machineId": machine_id, "purpose": "chat"},
            headers={**idem(), **await _precondition(real_session, folder.id)},
        )
        assert granted.status_code == 200, granted.text
        fence = {
            "X-Alkera-Lease-Epoch": str(granted.json()["epoch"]),
            "X-Alkera-Lease-Instance": instance,
        }
        finished = await _upload(box, folder.id, "notes.txt", b"sealed", idem, fence=fence)
        assert finished.status_code == 202, finished.text
        listed = await box.get(f"{_item(drive.id, folder.id)}/children", headers=fence)
        file_id = uuid.UUID(listed.json()["value"][0]["id"])

        # Two single-use URLs minted while the box stands.
        urls = []
        for _ in range(2):
            minted = await box.get(
                f"{_item(drive.id, file_id)}/content", headers=fence, follow_redirects=False
            )
            assert minted.status_code == 302, minted.text
            urls.append(minted.headers["location"])
        served = await _redeem_on_the_content_origin(box, urls[0], fence)
        assert served.status_code == 200, served.text
        assert served.content == b"sealed"

        await real_session.execute(
            text("UPDATE machine_credentials SET revoked_at = now() WHERE machine_id = :m"),
            {"m": uuid.UUID(machine_id)},
        )
        await real_session.commit()

        after = await _redeem_on_the_content_origin(box, urls[1], fence)
        assert after.status_code != 200, after.text
        assert after.content != b"sealed"
