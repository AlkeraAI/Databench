"""Who may stand up the box an org's chats run on, and which box they land on.

A workspace machine receives every prompt, every attachment and every leased
connection credential of every chat placed on it, so registering one is an
org-admin act, not a member's. The other half is placement: a box may not win
the org's chats by heartbeating more often than the org's real box, and a box
whose operator is not an admin of the org is not eligible at all — which also
answers for the rows that exist from before the rule and for the box whose
operator was demoted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import agent_headers
from alkera_core.compute.machines import current_machine
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, TeamMembership
from alkera_core.models._enums import TeamRole
from alkera_core.models.compute import DRAINING, ORG_TENANCY, ComputeAllocation
from backend.services.compute.placement import place_for_org
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, make_member, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


async def _headers(*, user_id: UUID, email: str, org_id: UUID) -> dict[str, str]:
    """What the daemon on a box sends: the operator's Bearer JWT plus the agent
    assertion the box is speaking under."""
    jwt = await mint_cli_token(user_id=user_id, email=email, org_team_id=org_id)
    return {"Authorization": f"Bearer {jwt}", **agent_headers("sess-box")}


def _body(mt: Any, pod: str, name: str) -> dict[str, str]:
    return {
        "provider": "runpod",
        "provider_pod_id": pod,
        "name": name,
        "machine_type_code": mt.provider_type_id,
    }


async def _decisions(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "compute_machine",
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _allocations(session: AsyncSession, org_id: UUID) -> list[ComputeAllocation]:
    rows = await session.execute(
        select(ComputeAllocation).where(ComputeAllocation.org_team_id == org_id)
    )
    return list(rows.scalars().all())


async def _box(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    machine_type_id: UUID,
    name: str,
    heartbeat: datetime | None,
    created_at: datetime,
    state: str = "ready",
) -> ComputeAllocation:
    """A workspace row straight into the database — the shape a registration
    left behind before the admin rule, or one whose operator was since demoted."""
    alloc = ComputeAllocation(
        user_id=user_id,
        org_team_id=org_id,
        machine_type_id=machine_type_id,
        lifecycle="workspace",
        origin="registered",
        name=name,
        tenancy=ORG_TENANCY,
        state=state,
        provider_machine_id=f"pod-{name}",
        created_at=created_at,
        ready_at=created_at,
        last_metered_at=created_at,
        last_heartbeat_at=heartbeat,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=1,
    )
    session.add(alloc)
    await session.commit()
    return alloc


# --------------------------------------------------------------------------- #
# registration authority
# --------------------------------------------------------------------------- #


async def test_a_member_cannot_register_the_orgs_box(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The hijack: a verified member controls their own laptop and every chat
    in the org lands on it. Refused, on record, and nothing is created."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    headers = await _headers(user_id=member.id, email=member.email, org_id=org_admin.org_id)

    resp = await client.post(
        "/api/v1/machines/register", json=_body(mt, "pod-laptop", "laptop"), headers=headers
    )

    assert resp.status_code == 403, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["message"]) == (
        "org_admin_required",
        "Only an org admin may set up or take down the organization's machine.",
    )
    assert await _allocations(real_session, org_admin.org_id) == []
    (decision,) = await _decisions(org_admin.org_id)
    assert (decision.payload["effect"], decision.payload["reason"]) == (
        "deny",
        "org_admin_required",
    )
    assert decision.payload["attrs"]["controls"] is True
    assert decision.payload["attrs"]["lifecycle"] == "workspace"
    assert decision.actor["acting"]["kind"] == "agent"
    assert decision.actor["chain"][0]["id"] == str(member.id)


async def test_an_org_admin_registers_the_box_and_the_allow_is_on_record(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    headers = await _headers(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_id=org_admin.org_id
    )

    resp = await client.post(
        "/api/v1/machines/register", json=_body(mt, "pod-box", "the-box"), headers=headers
    )

    assert resp.status_code == 201, resp.text
    (decision,) = await _decisions(org_admin.org_id)
    assert (decision.payload["effect"], decision.payload["reason"]) == ("allow", "admin_controls")
    assert decision.payload["attrs"]["controls"] is True


async def test_a_member_still_heartbeats_a_box_they_operate(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Registering is the admin act; keeping a box alive is not. A daemon whose
    row is its own still beats — ``controls`` is what the rule turns on, not
    every write on the machine."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    alloc = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="members-own",
        heartbeat=None,
        created_at=NOW,
    )
    headers = await _headers(user_id=member.id, email=member.email, org_id=org_admin.org_id)

    resp = await client.post(f"/api/v1/machines/{alloc.id}/heartbeat", headers=headers)

    assert resp.status_code == 204, resp.text


async def test_another_orgs_machine_is_opaque_to_an_admin(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Being an admin buys authority inside one's own org only: another org's
    box reads as missing, never as forbidden."""
    from backend.services.org import teams as team_service

    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other {uuid4().hex[:8]}",
        admin_email=f"other-{uuid4().hex[:8]}@example.com",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    mt = await make_machine_type(real_session)
    foreign = await _box(
        real_session,
        org_id=other_org.id,
        user_id=other_admin.id,
        machine_type_id=mt.id,
        name="their-box",
        heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    headers = await _headers(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_id=org_admin.org_id
    )

    resp = await client.post(f"/api/v1/machines/{foreign.id}/heartbeat", headers=headers)

    assert resp.status_code == 404, resp.text
    (decision,) = await _decisions(org_admin.org_id)
    assert (decision.payload["effect"], decision.payload["reason"]) == ("deny", "not_owner")
    assert await _decisions(other_org.id) == []


# --------------------------------------------------------------------------- #
# placement
# --------------------------------------------------------------------------- #


async def test_a_member_registered_box_never_wins_on_a_fresher_heartbeat(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The rows that predate the rule: a member's box beating a second ago must
    not take the org's chats from the admin's box that has held them for days."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    admin_box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="admin-box",
        heartbeat=datetime.now(UTC) - timedelta(seconds=20),
        created_at=datetime.now(UTC) - timedelta(days=3),
    )
    await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="member-box",
        heartbeat=datetime.now(UTC),  # the freshest beat in the org
        created_at=datetime.now(UTC),
    )

    chosen = await current_machine(real_session, org_id=org_admin.org_id)
    placed = await place_for_org(real_session, org_team_id=org_admin.org_id)

    assert chosen is not None and chosen.id == admin_box.id
    assert placed is not None and placed.id == admin_box.id


async def test_a_member_registered_box_is_the_only_box_and_still_serves_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Fail closed: with no eligible box the org has no compute, rather than
    falling back to the one a member stood up."""
    mt = await make_machine_type(real_session)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=member.id,
        machine_type_id=mt.id,
        name="member-only",
        heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    assert await current_machine(real_session, org_id=org_admin.org_id) is None
    assert await place_for_org(real_session, org_team_id=org_admin.org_id) is None


async def test_a_demoted_operators_box_stops_taking_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Eligibility is asked again on every placement read, so the admin who set
    a box up and was demoted does not keep the org's chats on their machine."""
    mt = await make_machine_type(real_session)
    operator, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    box = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=operator.id,
        machine_type_id=mt.id,
        name="operators-box",
        heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    chosen = await current_machine(real_session, org_id=org_admin.org_id)
    assert chosen is not None and chosen.id == box.id

    await real_session.execute(
        update(TeamMembership)
        .where(TeamMembership.user_id == operator.id, TeamMembership.team_id == org_admin.org_id)
        .values(role=TeamRole.MEMBER)
    )
    await real_session.commit()
    real_session.expire_all()

    assert await current_machine(real_session, org_id=org_admin.org_id) is None


async def test_the_oldest_answering_admin_box_wins_over_a_newer_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Two boxes an admin stands behind: the tie-break is the allocation's age,
    not whichever beat last — placement gives the same answer whenever it is
    asked."""
    mt = await make_machine_type(real_session)
    oldest = await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="first-box",
        heartbeat=datetime.now(UTC) - timedelta(seconds=20),
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="second-box",
        heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    chosen = await current_machine(real_session, org_id=org_admin.org_id)
    assert chosen is not None and chosen.id == oldest.id


async def test_a_draining_admin_box_is_still_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The eligibility filter is added to the placeable read, not in place of
    it: a box on its way out takes nothing new, admin or no admin."""
    mt = await make_machine_type(real_session)
    await _box(
        real_session,
        org_id=org_admin.org_id,
        user_id=org_admin.admin_id,
        machine_type_id=mt.id,
        name="draining-box",
        heartbeat=datetime.now(UTC),
        created_at=datetime.now(UTC),
        state=DRAINING,
    )

    assert await current_machine(real_session, org_id=org_admin.org_id) is None


async def test_the_admin_registered_box_serves_the_orgs_chats(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The flow a demo provisions: the daemon controls as the org admin, and
    the org's chats place on that box."""
    mt = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id)
    headers = await _headers(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_id=org_admin.org_id
    )
    resp = await client.post(
        "/api/v1/machines/register", json=_body(mt, "pod-demo", "demo-box"), headers=headers
    )
    assert resp.status_code == 201, resp.text

    banner = await client.get("/api/v1/machines/current", headers=headers)
    assert banner.status_code == 200
    assert banner.json()["machine_id"] == resp.json()["id"]
    placed = await place_for_org(real_session, org_team_id=org_admin.org_id)
    assert placed is not None and str(placed.id) == resp.json()["id"]
