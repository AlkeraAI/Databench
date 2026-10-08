"""A team's allowance and a member's storage cap inside it, decided through the
real routes: the status contract each refusal answers AND the ``authz.decision``
row that records it — allow rows riding the request transaction, deny rows
surviving the refused request's rollback.

The storage cases mirror the budget cap on a team pool one for one: a team
admin rations the team's storage allocation among its members, but never widens
their own storage — that needs an admin of a team above this one — and nobody,
the org admin included, lifts a member above the ceiling on the team's chain:
the refusal names the figure and the team that set it.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgAuditEvent, TeamRole, User
from alkera_core.models.allocations import AllocationResource, TeamAllocation
from alkera_core.models.storage_limits import UserStorageLimit
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio

AUTHZ = "authz.decision"
ALLOC = "/api/v1/org/teams/{team}/allocations/{resource}"
CAP = "/api/v1/org/storage/teams/{team}/members/{user}/limit"
GB = 1_000_000_000


async def _decisions(org_id: UUID, entity: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ,
                EventOutbox.entity == entity,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(r.payload["effect"], r.payload["reason"]) for r in rows]


async def _audit_count(org_id: UUID, action: str) -> int:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action == action
            )
        )
        return len(rows.scalars().all())


async def _team_under(org_id: UUID, name: str, parent: UUID | None = None) -> UUID:
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        team = await team_service.create_subteam(
            s, org_team_id=org_id, name=name, parent_team_id=parent or org_id
        )
        await s.commit()
        return team.id


async def _add_to_team(user_id: UUID, team_id: UUID, *, role: TeamRole = TeamRole.MEMBER) -> None:
    from backend.services.org import memberships as membership_service

    async with AsyncSessionLocal() as s:
        await membership_service.add_member(s, team_id=team_id, user_id=user_id, role=role)
        await s.commit()


async def _team_with_admin(org_id: UUID, name: str) -> tuple[UUID, User, str, User]:
    """A team under the org root with a team ADMIN and a plain teammate."""
    team_id = await _team_under(org_id, name)
    async with AsyncSessionLocal() as s:
        admin, pw = await make_member(s, org_id=org_id, verified=True)
        mate, _ = await make_member(s, org_id=org_id, verified=True)
    await _add_to_team(admin.id, team_id, role=TeamRole.ADMIN)
    await _add_to_team(mate.id, team_id)
    return team_id, admin, pw or "", mate


async def _allocation(team_id: UUID, resource: AllocationResource) -> TeamAllocation | None:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                select(TeamAllocation).where(
                    TeamAllocation.team_id == team_id, TeamAllocation.resource == resource.value
                )
            )
        ).scalar_one_or_none()


async def _other_org_admin() -> tuple[UUID, str, str]:
    from backend.services.org import teams as team_service

    email = f"alloc-other-{secrets.token_hex(6)}@alkera.dev"
    async with AsyncSessionLocal() as s:
        org, _admin = await team_service.create_org_with_admin(
            s,
            org_name=f"Other allowance org {secrets.token_hex(3)}",
            admin_email=email,
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await s.commit()
    return org.id, email, "pw-1234567890"


# =========================================================================== #
# a team's own allowance
# =========================================================================== #


@pytest.mark.parametrize(
    ("resource", "body"),
    [
        pytest.param("budget", {"amount_usd": "5.00"}, id="budget"),
        pytest.param("storage", {"limit_bytes": 2 * GB}, id="storage"),
    ],
)
async def test_the_teams_own_admin_is_refused_and_the_deny_survives(
    client: AsyncClient, org_admin: OrgWithAdmin, resource: str, body: dict[str, Any]
) -> None:
    """The refusal the hand-rolled check used to answer with no record: a team's
    admin trying to set their own team's allowance, and to remove it."""
    team, admin, pw, _mate = await _team_with_admin(org_admin.org_id, "Data")
    await login(client, admin.email, pw)

    put = await client.put(ALLOC.format(team=team, resource=resource), json=body)
    assert put.status_code == 403, put.text
    assert put.json()["error"]["message"] == "An admin of a team above this one sets its allowance."
    delete = await client.delete(ALLOC.format(team=team, resource=resource))
    assert delete.status_code == 403, delete.text

    assert await _allocation(team, AllocationResource(resource)) is None
    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [
        ("deny", "allocation_from_above"),
        ("deny", "allocation_from_above"),
    ]
    assert [r.payload["method"] for r in rows] == ["PUT", "DELETE"]
    assert rows[0].entity_id == str(team)
    assert rows[0].payload["attrs"] == {
        "email_verified": True,
        "exceeds_ceiling": False,
        "in_org": True,
        "is_admin_above": False,
        "is_org_root": False,
        "roles": ["admin", "member", "viewer"],
    }
    # A clear names no figure, so it carries no ceiling fact.
    assert "exceeds_ceiling" not in rows[1].payload["attrs"]


async def test_an_admin_above_sets_and_clears_and_both_allows_are_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Admin of the parent team — not an org admin — sets the child's allowance."""
    parent, parent_admin, pw, _mate = await _team_with_admin(org_admin.org_id, "Eng")
    child = await _team_under(org_admin.org_id, "Platform", parent=parent)
    await login(client, parent_admin.email, pw)

    put = await client.put(ALLOC.format(team=child, resource="budget"), json={"amount_usd": "3.00"})
    assert put.status_code == 200, put.text
    assert (await client.delete(ALLOC.format(team=child, resource="budget"))).status_code == 204
    assert await _allocation(child, AllocationResource.BUDGET) is None

    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [("allow", "admin_above_sets"), ("allow", "admin_above_sets")]
    assert rows[0].payload["attrs"]["is_admin_above"] is True
    assert await _audit_count(org_admin.org_id, "allocations.budget_set") == 1
    assert await _audit_count(org_admin.org_id, "allocations.budget_cleared") == 1


async def test_the_org_admin_sets_the_roots_own_allowance(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    put = await client.put(
        ALLOC.format(team=org_admin.org_id, resource="storage"), json={"limit_bytes": 5 * GB}
    )
    assert put.status_code == 200, put.text
    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [("allow", "org_admin_sets_root")]
    assert rows[0].payload["attrs"]["is_org_root"] is True


async def test_a_sibling_teams_admin_is_refused_as_not_an_admin_here(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    target, _a, _p, _m = await _team_with_admin(org_admin.org_id, "Data")
    _sib, sib_admin, sib_pw, _sm = await _team_with_admin(org_admin.org_id, "Sales")
    await login(client, sib_admin.email, sib_pw)
    resp = await client.put(
        ALLOC.format(team=target, resource="budget"), json={"amount_usd": "1.00"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["message"] == "team admin role required"
    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [("deny", "team_admin_required")]
    assert rows[0].payload["attrs"]["roles"] == []


async def test_an_unverified_admin_above_is_refused_with_the_verification_code(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    parent, parent_admin, pw, _mate = await _team_with_admin(org_admin.org_id, "Eng")
    child = await _team_under(org_admin.org_id, "Platform", parent=parent)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(User).where(User.id == parent_admin.id))).scalar_one()
        row.email_verified_at = None
        await s.commit()
    await login(client, parent_admin.email, pw)
    resp = await client.put(
        ALLOC.format(team=child, resource="budget"), json={"amount_usd": "1.00"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_verification_required"
    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [("deny", "email_verification_required")]
    assert await _allocation(child, AllocationResource.BUDGET) is None


async def test_a_foreign_team_is_an_opaque_404_recorded_under_the_caller(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    other_org, _email, _pw = await _other_org_admin()
    foreign, _a, _p, _m = await _team_with_admin(other_org, "Theirs")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(
        ALLOC.format(team=foreign, resource="budget"), json={"amount_usd": "1.00"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Team not found"
    assert await _allocation(foreign, AllocationResource.BUDGET) is None
    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [("deny", "team_not_in_org")]
    assert await _decisions(other_org, "team_allocation") == []


# =========================================================================== #
# a member's storage cap inside a team
# =========================================================================== #


async def _storage_cap(team_id: UUID, user_id: UUID) -> int | None:
    async with AsyncSessionLocal() as s:
        found = (
            await s.execute(
                select(UserStorageLimit.limit_bytes).where(
                    UserStorageLimit.team_id == team_id, UserStorageLimit.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        return None if found is None else int(found)


async def _allocated_team_with_own_cap(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> tuple[UUID, User, User, int]:
    """A team the org admin allocated 5 GB of storage to (inside the org's own
    10 GB plan ceiling), whose team admin the org admin capped at 1 GB. Leaves
    the client logged in as that team admin and returns how many decision rows
    the setup wrote (the outbox is append-only, so a test reads the rows after
    that mark)."""
    team, admin, pw, mate = await _team_with_admin(org_admin.org_id, "Data")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    alloc = await client.put(
        ALLOC.format(team=team, resource="storage"), json={"limit_bytes": 5 * GB}
    )
    assert alloc.status_code == 200, alloc.text
    own = await client.put(CAP.format(team=team, user=admin.id), json={"limit_bytes": 1 * GB})
    assert own.status_code == 200, own.text
    seen = len(await _decisions(org_admin.org_id, "team_storage_cap"))
    await login(client, admin.email, pw)
    return team, admin, mate, seen


@pytest.mark.parametrize(
    ("limit_bytes", "reason", "message"),
    [
        pytest.param(
            6 * GB,
            "allocation_ceiling",
            "A member's storage can't exceed the 5 GB allowed to Data.",
            id="above-the-allocation",
        ),
        pytest.param(
            2 * GB,
            "own_storage_raise",
            "Only an admin of a team above this one can raise your own storage here.",
            id="within-the-allocation",
        ),
    ],
)
async def test_a_team_admin_cannot_raise_their_own_storage_and_the_deny_is_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin, limit_bytes: int, reason: str, message: str
) -> None:
    team, admin, _mate, seen = await _allocated_team_with_own_cap(client, org_admin)
    resp = await client.put(CAP.format(team=team, user=admin.id), json={"limit_bytes": limit_bytes})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["message"] == message
    rows = (await _decisions(org_admin.org_id, "team_storage_cap"))[seen:]
    assert _effects(rows) == [("deny", reason)]
    assert rows[0].payload["attrs"]["target_is_self"] is True
    assert rows[0].payload["attrs"]["is_admin_above"] is False
    assert await _storage_cap(team, admin.id) == 1 * GB


async def test_a_team_admin_may_lower_their_own_storage(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    team, admin, _mate, seen = await _allocated_team_with_own_cap(client, org_admin)
    resp = await client.put(CAP.format(team=team, user=admin.id), json={"limit_bytes": 500_000_000})
    assert resp.status_code == 200, resp.text
    rows = (await _decisions(org_admin.org_id, "team_storage_cap"))[seen:]
    assert _effects(rows) == [("allow", "team_admin_set")]
    assert await _storage_cap(team, admin.id) == 500_000_000


async def test_a_team_admin_cannot_cap_a_teammate_above_the_allocation(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    team, _admin, mate, seen = await _allocated_team_with_own_cap(client, org_admin)
    over = await client.put(CAP.format(team=team, user=mate.id), json={"limit_bytes": 5 * GB + 1})
    assert over.status_code == 403
    assert await _storage_cap(team, mate.id) is None
    at = await client.put(CAP.format(team=team, user=mate.id), json={"limit_bytes": 5 * GB})
    assert at.status_code == 200, at.text
    rows = (await _decisions(org_admin.org_id, "team_storage_cap"))[seen:]
    assert _effects(rows) == [("deny", "allocation_ceiling"), ("allow", "team_admin_set")]
    # The deny row names the ceiling it was judged against; the allow needs no name.
    assert rows[0].payload["attrs"]["exceeds_ceiling"] is True
    assert rows[0].payload["attrs"]["ceiling_amount"] == "5 GB"
    assert rows[0].payload["attrs"]["ceiling_team"] == "Data"
    assert rows[1].payload["attrs"]["exceeds_ceiling"] is False
    assert "ceiling_amount" not in rows[1].payload["attrs"]


async def test_the_org_admin_is_held_to_the_allocation_too(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The org admin may raise the team's allocation, so they raise it first: a
    cap past it would show a figure that never binds."""
    team, admin, _mate, seen = await _allocated_team_with_own_cap(client, org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(CAP.format(team=team, user=admin.id), json={"limit_bytes": 6 * GB})
    assert resp.status_code == 403, resp.text
    assert (
        resp.json()["error"]["message"]
        == "A member's storage can't exceed the 5 GB allowed to Data."
    )
    rows = (await _decisions(org_admin.org_id, "team_storage_cap"))[seen:]
    assert _effects(rows) == [("deny", "allocation_ceiling")]
    assert rows[0].payload["attrs"]["exceeds_ceiling"] is True
    assert rows[0].payload["attrs"]["is_org_admin"] is True
    assert await _storage_cap(team, admin.id) == 1 * GB


async def test_the_orgs_own_storage_ceiling_tops_the_chain(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A team's storage allowance is held to the org's own ceiling (the plan's
    10 GB here) — a 5 TB team on a 10 GB org is refused, and the refusal names
    the org."""
    team, _admin, _pw, _mate = await _team_with_admin(org_admin.org_id, "Data")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(
        ALLOC.format(team=team, resource="storage"), json={"limit_bytes": 5_000 * GB}
    )
    assert resp.status_code == 403, resp.text
    message = resp.json()["error"]["message"]
    assert message.startswith("A team's allowance can't exceed the 10 GB allowed to ")
    assert await _allocation(team, AllocationResource.STORAGE) is None
    rows = await _decisions(org_admin.org_id, "team_allocation")
    assert _effects(rows) == [("deny", "allocation_ceiling")]
    assert rows[0].payload["attrs"]["ceiling_amount"] == "10 GB"


async def test_a_sub_teams_allowance_is_held_to_its_parents(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The middle admin's escape: given 5 GB, Eng's admin hands Eng/Web 6 GB.
    The write is refused with the figure and the team that set it, whoever
    the writer is — the org admin meets the same refusal."""
    parent, parent_admin, pw, _mate = await _team_with_admin(org_admin.org_id, "Eng")
    child = await _team_under(org_admin.org_id, "Web", parent=parent)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (
        await client.put(
            ALLOC.format(team=parent, resource="storage"), json={"limit_bytes": 5 * GB}
        )
    ).status_code == 200
    seen = len(await _decisions(org_admin.org_id, "team_allocation"))

    await login(client, parent_admin.email, pw)
    over = await client.put(
        ALLOC.format(team=child, resource="storage"), json={"limit_bytes": 6 * GB}
    )
    assert over.status_code == 403, over.text
    assert (
        over.json()["error"]["message"]
        == "A team's allowance can't exceed the 5 GB allowed to Eng."
    )
    at = await client.put(
        ALLOC.format(team=child, resource="storage"), json={"limit_bytes": 5 * GB}
    )
    assert at.status_code == 200, at.text

    await login(client, org_admin.admin_email, org_admin.admin_password)
    org_over = await client.put(
        ALLOC.format(team=child, resource="storage"), json={"limit_bytes": 6 * GB}
    )
    assert org_over.status_code == 403, org_over.text
    rows = (await _decisions(org_admin.org_id, "team_allocation"))[seen:]
    assert _effects(rows) == [
        ("deny", "allocation_ceiling"),
        ("allow", "admin_above_sets"),
        ("deny", "allocation_ceiling"),
    ]
    row = await _allocation(child, AllocationResource.STORAGE)
    assert row is not None and row.limit_bytes == 5 * GB


async def test_a_team_admin_cannot_clear_their_own_storage_cap_and_the_deny_survives(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Clearing the 1 GB cap falls back to the team's whole 5 GB: a raise."""
    team, admin, _mate, seen = await _allocated_team_with_own_cap(client, org_admin)
    resp = await client.delete(CAP.format(team=team, user=admin.id))
    assert resp.status_code == 403
    rows = (await _decisions(org_admin.org_id, "team_storage_cap"))[seen:]
    assert _effects(rows) == [("deny", "own_storage_raise")]
    assert rows[0].payload["method"] == "DELETE"
    assert "exceeds_ceiling" not in rows[0].payload["attrs"]
    assert await _storage_cap(team, admin.id) == 1 * GB


async def test_a_team_admin_clears_a_teammates_storage_cap(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    team, _admin, mate, seen = await _allocated_team_with_own_cap(client, org_admin)
    assert (
        await client.put(CAP.format(team=team, user=mate.id), json={"limit_bytes": 2 * GB})
    ).status_code == 200
    resp = await client.delete(CAP.format(team=team, user=mate.id))
    assert resp.status_code == 204
    assert await _storage_cap(team, mate.id) is None
    rows = (await _decisions(org_admin.org_id, "team_storage_cap"))[seen:]
    assert _effects(rows) == [("allow", "team_admin_set"), ("allow", "team_admin_clear")]
    assert await _audit_count(org_admin.org_id, "storage.team_member_limit_cleared") == 1


async def test_with_no_allocation_a_team_admin_still_cannot_raise_their_own_cap(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """No allocation means no ceiling to exceed — but a higher own cap still
    widens the caller's own storage, so it still needs an admin above."""
    team, admin, pw, _mate = await _team_with_admin(org_admin.org_id, "Data")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (
        await client.put(CAP.format(team=team, user=admin.id), json={"limit_bytes": 1 * GB})
    ).status_code == 200
    await login(client, admin.email, pw)
    resp = await client.put(CAP.format(team=team, user=admin.id), json={"limit_bytes": 2 * GB})
    assert resp.status_code == 403
    assert await _storage_cap(team, admin.id) == 1 * GB


async def test_a_storage_cap_on_a_foreign_team_is_an_opaque_404_with_a_deny(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    other_org, _email, _pw = await _other_org_admin()
    foreign, _a, _p, foreign_mate = await _team_with_admin(other_org, "Theirs")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(CAP.format(team=foreign, user=foreign_mate.id), json={"limit_bytes": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "Team not found"
    assert await _storage_cap(foreign, foreign_mate.id) is None
    rows = await _decisions(org_admin.org_id, "team_storage_cap")
    assert _effects(rows) == [("deny", "team_not_in_org")]
