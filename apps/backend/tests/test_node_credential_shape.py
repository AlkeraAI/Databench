"""The credential a provisioned node boots with, through the seam provisioning
calls: one machine credential bound to the allocation and no user session
beside it, the allocation's in every fact a box could misstate, rotated on a
re-issue, and owned by the caller's transaction.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from uuid import UUID

import pytest
from alkera_core.auth import InvalidTokenError, decode_session_token
from alkera_core.auth.machine_token import MACHINE_TOKEN_PREFIX, looks_like_machine_token
from alkera_core.compute.machines import WORKSPACE
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential
from alkera_core.models.compute import READY, ComputeAllocation
from backend.api.routes.compute.machines import heartbeat_decision_sink
from backend.services.compute.machine_credential import (
    NODE_CREDENTIAL_KEY,
    NodeCredential,
    node_credential_for,
)
from backend.services.credentials import machine_credentials as machine_credential_service
from httpx import AsyncClient
from sqlalchemy import select
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


@pytest.fixture(autouse=True)
def _forget_beats() -> Iterator[None]:
    heartbeat_decision_sink().clear()
    yield
    heartbeat_decision_sink().clear()


async def _allocation(
    operator: OrgWithAdmin, *, tenancy: str = "pool", name: str = "pool-a"
) -> ComputeAllocation:
    """The row ``provision()`` has flushed by the time it asks for the node's
    credential: a platform workspace machine in the operator's org."""
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        alloc = ComputeAllocation(
            user_id=operator.admin_id,
            org_team_id=operator.org_id,
            machine_type_id=machine_type.id,
            lifecycle=WORKSPACE,
            state=READY,
            tenancy=tenancy,
            name=name,
            provider_machine_id=f"pod-{secrets.token_hex(4)}",
        )
        session.add(alloc)
        await session.commit()
        return alloc


async def _issue(alloc: ComputeAllocation, **kwargs: object) -> NodeCredential:
    async with AsyncSessionLocal() as session:
        row = await session.get(ComputeAllocation, alloc.id)
        assert row is not None
        issued = await node_credential_for(session, row, **kwargs)  # type: ignore[arg-type]
        await session.commit()
        return issued


async def _live_credentials(machine_id: UUID) -> list[MachineCredential]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(MachineCredential)
            .where(MachineCredential.machine_id == machine_id)
            .order_by(MachineCredential.created_at)
        )
        return [row for row in rows.scalars().all() if row.revoked_at is None]


async def _resolves(raw: str) -> UUID | None:
    async with AsyncSessionLocal() as session:
        row = await machine_credential_service.resolve_active(session, raw)
        return row.machine_id if row is not None else None


def _bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def test_a_node_secret_is_one_machine_credential_and_no_session(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The shape the bootstrap reads: exactly the credential key, holding a
    machine credential that the API resolves as the machine and refuses as a
    person. There is no JWT anywhere in it to become an operator's session."""
    alloc = await _allocation(org_admin)
    issued = await _issue(alloc)

    assert set(issued.secrets) == {NODE_CREDENTIAL_KEY}
    raw = issued.raw
    assert raw.startswith(MACHINE_TOKEN_PREFIX)
    assert looks_like_machine_token(raw)
    for value in issued.secrets.values():
        with pytest.raises(InvalidTokenError):
            decode_session_token(value)

    beat = await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat",
        json={"capacity": 2, "chats_served": 0},
        headers=_bearer(raw),
    )
    assert beat.status_code == 204, beat.text
    person = await client.get("/api/v1/auth/me", headers=_bearer(raw))
    assert person.status_code == 401


async def test_the_credential_is_the_allocations_in_every_fact(
    org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A box can never say what it is: its org, tenancy, kind and name are the
    allocation's, and the author defaults to the allocation's own user unless
    the caller names the admin who asked."""
    alloc = await _allocation(org_admin, tenancy="dedicated", name="acme-1")
    issued = await _issue(alloc, region="us-west-2")
    row = issued.credential
    assert row.machine_id == alloc.id
    assert row.org_team_id == org_admin.org_id
    assert row.tenancy == "dedicated"
    assert row.label == "acme-1"
    assert row.machine_type_id == alloc.machine_type_id
    assert row.region == "us-west-2"
    assert row.created_by == org_admin.admin_id
    assert row.revoked_at is None

    named = await _issue(alloc, created_by=platform_admin.admin_id)
    assert named.credential.created_by == platform_admin.admin_id


async def test_an_unnamed_allocation_still_gets_a_label(org_admin: OrgWithAdmin) -> None:
    alloc = await _allocation(org_admin, name="")
    issued = await _issue(alloc)
    assert issued.credential.label.startswith("node-")
    assert issued.credential.label != "node-"


async def test_a_reissue_retires_the_previous_secret(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """One live credential per machine: the second issue revokes the first,
    the old secret stops resolving and is refused at the door, the new one
    beats, and the retired row still names the machine it belonged to."""
    alloc = await _allocation(org_admin)
    first = await _issue(alloc)
    second = await _issue(alloc)
    assert first.raw != second.raw

    assert await _resolves(first.raw) is None
    assert await _resolves(second.raw) == alloc.id
    live = await _live_credentials(alloc.id)
    assert [row.id for row in live] == [second.credential.id]
    async with AsyncSessionLocal() as session:
        retired = await session.get(MachineCredential, first.credential.id)
        assert retired is not None
        assert retired.revoked_at is not None
        assert retired.machine_id == alloc.id

    refused = await client.post(
        f"/api/v1/machines/{alloc.id}/heartbeat", headers=_bearer(first.raw)
    )
    assert refused.status_code == 401, refused.text
    beat = await client.post(f"/api/v1/machines/{alloc.id}/heartbeat", headers=_bearer(second.raw))
    assert beat.status_code == 204, beat.text


async def test_a_node_that_is_not_a_platform_box_gets_no_credential(
    org_admin: OrgWithAdmin,
) -> None:
    """An org's own workspace machine registers on a member's session; the
    node credential is for the platform's boxes. Asking for one is a caller
    bug and writes nothing."""
    alloc = await _allocation(org_admin, tenancy="org")
    with pytest.raises(ValueError, match="pool or dedicated"):
        await _issue(alloc)
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(MachineCredential).where(MachineCredential.machine_id == alloc.id)
        )
        assert rows.scalars().all() == []


async def test_the_seam_flushes_and_the_caller_commits(org_admin: OrgWithAdmin) -> None:
    """A provision that fails after the credential was minted rolls it back
    with everything else: no live secret is left behind for a node that
    never ran, and the secret it would have booted with resolves to nothing."""
    alloc = await _allocation(org_admin)
    async with AsyncSessionLocal() as session:
        row = await session.get(ComputeAllocation, alloc.id)
        assert row is not None
        issued = await node_credential_for(session, row)
        raw = issued.raw
        await session.rollback()
    assert await _live_credentials(alloc.id) == []
    assert await _resolves(raw) is None
