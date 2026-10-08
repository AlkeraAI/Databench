"""A provider's shutdown notice drains the machine it names, through the real
route, against Postgres.

The notice is signed with the same module the email hook signs with, so the
pair is proved to interoperate; the drain is the one an admin's click runs,
so the machine takes no new chats afterwards.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.shutdown_notice import NOTICE_PATH, SIGNATURE_HEADER, sign
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OrgComputeAssignment
from alkera_core.models.compute import ComputeAllocation, ComputeAllocationEvent
from backend.services.compute import placement
from backend.services.credentials import machine_credentials as machine_credential_service
from freezegun import freeze_time
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

SECRET = "s" * 40


@pytest.fixture(autouse=True)
async def _quiet_plane(real_session: AsyncSession) -> None:
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(
        update(ComputeAllocation)
        .where(
            ComputeAllocation.lifecycle == "workspace",
            ComputeAllocation.state.not_in(("released", "failed")),
        )
        .values(state="released")
    )
    await real_session.commit()


@pytest.fixture
def notices_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "compute_shutdown_notice_secret", SecretStr(SECRET))
    yield


async def _pool_box(session: AsyncSession, org: OrgWithAdmin, *, state: str = "ready") -> str:
    """A serving EC2 pool box that heartbeated just now; its instance id."""
    mt = await make_machine_type(session, provider="ec2")
    now = datetime.now(UTC)
    instance = f"i-{uuid4().hex[:17]}"
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        tenancy="pool",
        sandbox="gvisor",
        name=f"box-{instance[-6:]}",
        state=state,
        provider_machine_id=instance,
        created_at=now,
        state_changed_at=now,
        ready_at=now if state == "ready" else None,
        last_heartbeat_at=now,
        capacity=6,
    )
    session.add(alloc)
    await session.flush()
    # The credential the box claimed with: a box nobody stands behind serves nothing.
    credential, _raw = await machine_credential_service.mint(
        session,
        org_id=org.org_id,
        created_by=org.admin_id,
        machine_type=mt,
        tenancy="pool",
        label=alloc.name,
    )
    credential.machine_id = alloc.id
    await session.commit()
    return instance


def _body(instance: str, **over: object) -> bytes:
    notice = {
        "provider": "ec2",
        "machine_id": instance,
        "reason": "EC2 instance retirement",
        "source": "email",
        **over,
    }
    return json.dumps(notice).encode()


async def _post(client: AsyncClient, body: bytes, *, at: float | None = None) -> int:
    header = sign(SECRET, body, timestamp=int(time.time() if at is None else at))
    resp = await client.post(
        NOTICE_PATH,
        content=body,
        headers={SIGNATURE_HEADER: header, "Content-Type": "application/json"},
    )
    return resp.status_code


async def _row(instance: str) -> ComputeAllocation:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ComputeAllocation).where(ComputeAllocation.provider_machine_id == instance)
            )
        ).scalar_one()
        return row


async def _edges(alloc_id: UUID) -> list[tuple[str, str, dict[str, object]]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ComputeAllocationEvent)
            .where(ComputeAllocationEvent.allocation_id == alloc_id)
            .order_by(ComputeAllocationEvent.at)
        )
        return [(e.from_state, e.to_state, e.actor) for e in rows.scalars().all()]


async def test_a_signed_notice_drains_the_box_and_it_takes_no_new_chats(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    notices_on: None,
) -> None:
    instance = await _pool_box(real_session, org_admin)
    async with AsyncSessionLocal() as db:
        placed = await placement.place_for_org(db, org_team_id=org_admin.org_id)
        assert placed is not None and placed.provider_machine_id == instance

    body = _body(instance)
    header = sign(SECRET, body, timestamp=int(time.time()))
    resp = await client.post(NOTICE_PATH, content=body, headers={SIGNATURE_HEADER: header})

    assert resp.status_code == 200, resp.text
    assert resp.json()["drained"] is True and resp.json()["state"] == "draining"
    row = await _row(instance)
    assert row.state == "draining"
    assert row.auto_terminate is True, "a retiring machine is released once it is empty"
    assert row.drain_reason == "shutdown notice: EC2 instance retirement"
    (edge,) = [e for e in await _edges(row.id) if e[1] == "draining"]
    assert edge[0] == "ready"
    assert edge[2]["kind"] == "system" and edge[2]["name"] == "shutdown notice (email)"
    async with AsyncSessionLocal() as db:
        assert await placement.place_for_org(db, org_team_id=org_admin.org_id) is None


async def test_a_repeated_notice_changes_nothing(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    notices_on: None,
) -> None:
    instance = await _pool_box(real_session, org_admin)
    assert await _post(client, _body(instance)) == 200
    body = _body(instance)
    resp = await client.post(
        NOTICE_PATH,
        content=body,
        headers={SIGNATURE_HEADER: sign(SECRET, body, timestamp=int(time.time()))},
    )
    assert resp.status_code == 200
    assert resp.json()["drained"] is False
    row = await _row(instance)
    assert [e[1] for e in await _edges(row.id)].count("draining") == 1


@pytest.mark.parametrize(
    ("header", "why"),
    [
        pytest.param(None, "unsigned", id="no-signature"),
        pytest.param("wrong-secret", "another secret", id="wrong-secret"),
        pytest.param("t=abc,v1=00", "garbage", id="malformed"),
    ],
)
async def test_an_unverified_notice_drains_nothing(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    notices_on: None,
    header: str | None,
    why: str,
) -> None:
    instance = await _pool_box(real_session, org_admin)
    body = _body(instance)
    headers = {}
    if header == "wrong-secret":
        headers[SIGNATURE_HEADER] = sign("x" * 40, body, timestamp=int(time.time()))
    elif header is not None:
        headers[SIGNATURE_HEADER] = header
    resp = await client.post(NOTICE_PATH, content=body, headers=headers)
    assert resp.status_code == 401, why
    assert (await _row(instance)).state == "ready"


async def test_a_signature_is_honoured_only_inside_its_window(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    notices_on: None,
) -> None:
    """A captured notice cannot be replayed later: signed at T, it is refused
    once the receiver's clock is past T + 300 s and accepted just inside."""
    instance = await _pool_box(real_session, org_admin)
    signed_at = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    body = _body(instance)
    with freeze_time(signed_at + timedelta(seconds=301), real_asyncio=True):
        assert await _post(client, body, at=signed_at.timestamp()) == 401
    assert (await _row(instance)).state == "ready"
    with freeze_time(signed_at + timedelta(seconds=299), real_asyncio=True):
        assert await _post(client, body, at=signed_at.timestamp()) == 200
    assert (await _row(instance)).state == "draining"


async def test_notices_are_off_without_a_secret(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "compute_shutdown_notice_secret", None)
    instance = await _pool_box(real_session, org_admin)
    assert await _post(client, _body(instance)) == 404
    assert (await _row(instance)).state == "ready"


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        pytest.param({"machine_id": "i-00000000000000000"}, 404, id="unknown-machine"),
        pytest.param({"provider": "runpod"}, 404, id="same-id-other-provider"),
        pytest.param({"provider": "gcp"}, 422, id="unknown-provider"),
        pytest.param({"machine_id": "i-1; drop table"}, 422, id="bad-machine-id"),
    ],
)
async def test_a_notice_for_no_machine_of_ours_is_refused(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    notices_on: None,
    over: dict[str, object],
    expected: int,
) -> None:
    instance = await _pool_box(real_session, org_admin)
    assert await _post(client, _body(instance, **over)) == expected
    assert (await _row(instance)).state == "ready"


async def test_a_machine_still_booting_is_not_drained(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    notices_on: None,
) -> None:
    instance = await _pool_box(real_session, org_admin, state="bootstrapping")
    assert await _post(client, _body(instance)) == 409
    assert (await _row(instance)).state == "bootstrapping"
