"""A person stands on a team two ways, and the two are not exclusive.

A membership row written on the team (direct standing) and admin reaching the
team from a team above (descent) are read together, never confused. The
regressions these pin: adding an org admin to a sub-team must not move them
out of the root's direct list, must list them on the sub-team both directly
and by descent, and must leave their standing there uneditable — descent is
the team above's fact, refused through the policy with the reason on record.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, TeamMembership, TeamRole
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


async def _sub(s: AsyncSession, *, org_id: UUID, parent_id: UUID, name: str) -> UUID:
    team = await team_service.create_subteam(
        s, org_team_id=org_id, name=name, parent_team_id=parent_id
    )
    await s.commit()
    return team.id


def _entry(entries: list[membership_service.RosterEntry], user_id: UUID) -> Any:
    hits = [e for e in entries if e.user.id == user_id]
    assert len(hits) <= 1, "one entry per person per team"
    return hits[0] if hits else None


async def _decisions(org_id: UUID) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "team_membership",
            )
            .order_by(EventOutbox.id)
        )
        return [(r.payload["effect"], r.payload["reason"]) for r in rows.scalars().all()]


async def _row_role(team_id: UUID, user_id: UUID) -> TeamRole | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(TeamMembership.role).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        return row


# --------------------------------------------------------------------------- #
# the service: what the roster says
# --------------------------------------------------------------------------- #


async def test_adding_the_org_admin_to_a_sub_team_keeps_their_direct_root_standing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    sub = await _sub(real_session, org_id=org_admin.org_id, parent_id=org_admin.org_id, name="Test")
    await membership_service.add_member(
        real_session, team_id=sub, user_id=org_admin.admin_id, role=TeamRole.MEMBER
    )
    await real_session.commit()

    at_root = _entry(
        await membership_service.roster(real_session, team_id=org_admin.org_id), org_admin.admin_id
    )
    assert at_root is not None
    assert at_root.direct_role is TeamRole.ADMIN
    assert at_root.descent is None
    assert at_root.effective_role is TeamRole.ADMIN

    on_sub = _entry(await membership_service.roster(real_session, team_id=sub), org_admin.admin_id)
    assert on_sub is not None
    assert on_sub.direct_role is TeamRole.MEMBER
    assert on_sub.descent is not None
    assert on_sub.descent.role is TeamRole.ADMIN
    assert on_sub.descent.from_team.id == org_admin.org_id
    # Descent wins: the row says member, the standing is admin.
    assert on_sub.effective_role is TeamRole.ADMIN


async def test_the_org_admin_reaches_a_sub_team_by_descent_with_no_row_there(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    sub = await _sub(real_session, org_id=org_admin.org_id, parent_id=org_admin.org_id, name="Test")
    on_sub = _entry(await membership_service.roster(real_session, team_id=sub), org_admin.admin_id)
    assert on_sub is not None
    assert on_sub.direct_role is None
    assert on_sub.descent is not None and on_sub.descent.from_team.id == org_admin.org_id
    assert on_sub.effective_role is TeamRole.ADMIN
    assert (
        await membership_service.descent_for(real_session, team_id=sub, user_id=org_admin.admin_id)
        == on_sub.descent
    )


async def test_a_member_row_above_grants_nothing_below(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    plain, _ = await make_member(real_session, org_id=org_admin.org_id)
    sub = await _sub(real_session, org_id=org_admin.org_id, parent_id=org_admin.org_id, name="Test")
    assert _entry(await membership_service.roster(real_session, team_id=sub), plain.id) is None
    assert await membership_service.descent_for(real_session, team_id=sub, user_id=plain.id) is None


# root → A → A1. `grants` are (team, role) rows written in order; `expect` is, per
# team, (direct_role, descent source team, effective role) or None when the
# person does not stand on that team at all.
_CASES = [
    pytest.param(
        [("A", TeamRole.ADMIN)],
        {
            "root": (TeamRole.MEMBER, None, TeamRole.MEMBER),
            "A": (TeamRole.ADMIN, None, TeamRole.ADMIN),
            "A1": (None, "A", TeamRole.ADMIN),
        },
        id="admin-of-the-middle-team-only",
    ),
    pytest.param(
        [("root", TeamRole.ADMIN), ("A1", TeamRole.MEMBER)],
        {
            "root": (TeamRole.ADMIN, None, TeamRole.ADMIN),
            "A": (TeamRole.MEMBER, "root", TeamRole.ADMIN),
            "A1": (TeamRole.MEMBER, "root", TeamRole.ADMIN),
        },
        id="root-admin-with-a-leaf-row",
    ),
    pytest.param(
        [("root", TeamRole.ADMIN), ("A", TeamRole.ADMIN)],
        {
            "root": (TeamRole.ADMIN, None, TeamRole.ADMIN),
            "A": (TeamRole.ADMIN, "root", TeamRole.ADMIN),
            # The NEAREST ancestor holding the admin row is the source named.
            "A1": (None, "A", TeamRole.ADMIN),
        },
        id="admin-above-and-admin-in-between-names-the-nearest",
    ),
    pytest.param(
        [("A1", TeamRole.MEMBER)],
        {
            "root": (TeamRole.MEMBER, None, TeamRole.MEMBER),
            "A": (TeamRole.MEMBER, None, TeamRole.MEMBER),
            "A1": (TeamRole.MEMBER, None, TeamRole.MEMBER),
        },
        id="a-leaf-member-is-a-plain-member-all-the-way-up",
    ),
]


@pytest.mark.parametrize(("grants", "expect"), _CASES)
async def test_roster_reads_direct_and_descent_together(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    grants: list[tuple[str, TeamRole]],
    expect: dict[str, tuple[TeamRole | None, str | None, TeamRole] | None],
) -> None:
    a = await _sub(real_session, org_id=org_admin.org_id, parent_id=org_admin.org_id, name="A")
    a1 = await _sub(real_session, org_id=org_admin.org_id, parent_id=a, name="A1")
    teams = {"root": org_admin.org_id, "A": a, "A1": a1}
    person, _ = await make_member(real_session, org_id=org_admin.org_id)
    # The fixture wrote a root member row; a root grant re-states it.
    for team_key, role in grants:
        existing = await membership_service.get(
            real_session, team_id=teams[team_key], user_id=person.id
        )
        if existing is not None:
            await membership_service.change_role(real_session, existing, role)
        else:
            await membership_service.add_member(
                real_session, team_id=teams[team_key], user_id=person.id, role=role
            )
    await real_session.commit()

    for team_key, want in expect.items():
        got = _entry(
            await membership_service.roster(real_session, team_id=teams[team_key]), person.id
        )
        if want is None:
            assert got is None, team_key
            continue
        direct, source, effective = want
        assert got is not None, team_key
        assert got.direct_role is direct, team_key
        if source is None:
            assert got.descent is None, team_key
        else:
            assert got.descent is not None and got.descent.from_team.id == teams[source], team_key
        assert got.effective_role is effective, team_key


# --------------------------------------------------------------------------- #
# the routes: the wire shape and the locked standing
# --------------------------------------------------------------------------- #


async def test_the_member_list_carries_direct_descent_and_effective_roles(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    sub = (await client.post("/api/v1/teams", json={"name": "Test"})).json()["id"]
    plain, _ = await make_member(real_session, org_id=org_admin.org_id)
    await membership_service.add_member(real_session, team_id=UUID(sub), user_id=plain.id)
    await real_session.commit()

    rows = {r["user_id"]: r for r in (await client.get(f"/api/v1/teams/{sub}/members")).json()}
    admin = rows[str(org_admin.admin_id)]
    assert admin["direct_role"] is None
    assert admin["descent_role"] == "admin"
    assert admin["descent_from_team_id"] == str(org_admin.org_id)
    assert admin["descent_from_team_name"]
    assert admin["effective_role"] == admin["role"] == "admin"
    member = rows[str(plain.id)]
    assert member["direct_role"] == "member"
    assert member["descent_role"] is None
    assert member["descent_from_team_id"] is None
    assert member["effective_role"] == member["role"] == "member"

    root_rows = {
        r["user_id"]: r
        for r in (await client.get(f"/api/v1/teams/{org_admin.org_id}/members")).json()
    }
    assert root_rows[str(org_admin.admin_id)]["direct_role"] == "admin"
    assert root_rows[str(org_admin.admin_id)]["descent_role"] is None


@pytest.mark.parametrize("requested", ["member", "admin"])
async def test_a_role_change_for_an_admin_by_descent_is_refused_with_the_source_named(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession, requested: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    sub = (await client.post("/api/v1/teams", json={"name": "Test"})).json()["id"]
    await membership_service.add_member(
        real_session, team_id=UUID(sub), user_id=org_admin.admin_id, role=TeamRole.MEMBER
    )
    await real_session.commit()

    resp = await client.patch(
        f"/api/v1/teams/{sub}/memberships/{org_admin.admin_id}", json={"role": requested}
    )
    assert resp.status_code == 403, resp.text
    message = resp.json()["error"]["message"]
    assert "by descent from" in message
    root_name = (await client.get(f"/api/v1/teams/{org_admin.org_id}")).json()["name"]
    assert root_name in message
    assert await _decisions(org_admin.org_id) == [("deny", "role_fixed_by_descent")]
    assert await _row_role(UUID(sub), org_admin.admin_id) is TeamRole.MEMBER


async def test_adding_an_admin_by_descent_as_a_member_is_refused_but_as_an_admin_is_allowed(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    sub = (await client.post("/api/v1/teams", json={"name": "Test"})).json()["id"]
    body = {"user_id": str(org_admin.admin_id), "team_id": sub}

    refused = await client.post(f"/api/v1/teams/{sub}/memberships", json={**body, "role": "member"})
    assert refused.status_code == 403, refused.text
    assert "by descent from" in refused.json()["error"]["message"]
    assert await _row_role(UUID(sub), org_admin.admin_id) is None

    added = await client.post(f"/api/v1/teams/{sub}/memberships", json={**body, "role": "admin"})
    assert added.status_code == 201, added.text
    assert await _row_role(UUID(sub), org_admin.admin_id) is TeamRole.ADMIN
    assert await _decisions(org_admin.org_id) == [
        ("deny", "descent_outranks_request"),
        ("allow", "direct_admin_added"),
    ]
    rows = {r["user_id"]: r for r in (await client.get(f"/api/v1/teams/{sub}/members")).json()}
    me = rows[str(org_admin.admin_id)]
    assert me["direct_role"] == "admin" and me["descent_role"] == "admin"


async def test_a_role_change_nothing_reaches_from_above_is_allowed_and_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    sub = (await client.post("/api/v1/teams", json={"name": "Test"})).json()["id"]
    plain, _ = await make_member(real_session, org_id=org_admin.org_id)
    await membership_service.add_member(real_session, team_id=UUID(sub), user_id=plain.id)
    await real_session.commit()

    resp = await client.patch(f"/api/v1/teams/{sub}/memberships/{plain.id}", json={"role": "admin"})
    assert resp.status_code == 200, resp.text
    assert await _row_role(UUID(sub), plain.id) is TeamRole.ADMIN
    assert await _decisions(org_admin.org_id) == [("allow", "no_descent")]
