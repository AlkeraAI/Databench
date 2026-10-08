"""Team membership route tests."""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member


async def _create_subteam(client: AsyncClient, name: str, parent_id: str | None = None) -> str:
    body: dict[str, str] = {"name": name}
    if parent_id is not None:
        body["parent_team_id"] = parent_id
    resp = await client.post("/api/v1/teams", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_add_member_then_list(client: AsyncClient, org_admin: OrgWithAdmin, real_session):
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Engineering")

    add = await client.post(
        f"/api/v1/teams/{team_id}/memberships",
        json={"user_id": str(member.id), "team_id": team_id, "role": "member"},
    )
    assert add.status_code == 201

    listed = await client.get(f"/api/v1/teams/{team_id}/memberships")
    assert listed.status_code == 200
    user_ids = {m["user_id"] for m in listed.json()}
    assert str(member.id) in user_ids


@pytest.mark.asyncio
async def test_add_member_requires_team_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    # Pre-create the subteam as the org admin.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Locked")

    # Now log in as a plain member; they should be denied.
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await client.post("/api/v1/auth/logout")
    await login(client, member.email, member_pw or "")
    target, _ = await make_member(real_session, org_id=org_admin.org_id)
    resp = await client.post(
        f"/api/v1/teams/{team_id}/memberships",
        json={"user_id": str(target.id), "team_id": team_id, "role": "member"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_change_role_then_remove(client: AsyncClient, org_admin: OrgWithAdmin, real_session):
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Mutable")

    await client.post(
        f"/api/v1/teams/{team_id}/memberships",
        json={"user_id": str(member.id), "team_id": team_id, "role": "member"},
    )
    patch = await client.patch(
        f"/api/v1/teams/{team_id}/memberships/{member.id}",
        json={"role": "admin"},
    )
    assert patch.status_code == 200
    assert patch.json()["role"] == "admin"

    delete = await client.delete(f"/api/v1/teams/{team_id}/memberships/{member.id}")
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_cannot_remove_last_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Org root has exactly one admin; removing them must be refused — and the
    refusal must leave them fully intact.

    Only an org-root ADMIN can delete a root membership (`require_team_admin`
    won't descend upward), so "remove the last org admin" is necessarily a
    SELF-removal. Root removal now deprovisions, so the self-removal guard is
    what answers first (400, matching `PUT /org/members/{id}/active`); either
    way the org keeps its admin and their session."""
    from backend.services.org import memberships as membership_service

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{org_admin.admin_id}")
    assert resp.status_code == 400
    assert "yourself" in resp.json()["error"]["message"]

    still_there = await membership_service.get(
        real_session, team_id=org_admin.org_id, user_id=org_admin.admin_id
    )
    assert still_there is not None
    assert await _is_active(org_admin.admin_id) is True
    assert (await client.get("/api/v1/teams")).status_code == 200  # session survives


@pytest.mark.asyncio
async def test_membership_ops_on_deep_grandchild_team(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Depth-3 regression: a grandchild team must be recognized as in-org.
    Before the fix `_team_in_caller_org` only saw the root + direct children, so
    membership management on a 3-deep team wrongly 404'd (it now walks the full
    ancestor chain, like the teams router)."""
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    parent = await _create_subteam(client, "Parent")
    child = await _create_subteam(client, "Child", parent_id=parent)
    grandchild = await _create_subteam(client, "Grandchild", parent_id=child)

    add = await client.post(
        f"/api/v1/teams/{grandchild}/memberships",
        json={"user_id": str(member.id), "team_id": grandchild, "role": "member"},
    )
    assert add.status_code == 201, add.text

    listed = await client.get(f"/api/v1/teams/{grandchild}/memberships")
    assert listed.status_code == 200
    assert str(member.id) in {m["user_id"] for m in listed.json()}


@pytest.mark.asyncio
async def test_move_membership_endpoint(client: AsyncClient, org_admin: OrgWithAdmin, real_session):
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    a = await _create_subteam(client, "A")
    b = await _create_subteam(client, "B")
    await client.post(
        f"/api/v1/teams/{a}/memberships",
        json={"user_id": str(member.id), "team_id": a, "role": "member"},
    )
    resp = await client.post(
        f"/api/v1/teams/{a}/memberships/{member.id}/move",
        json={"target_team_id": b},
    )
    assert resp.status_code == 200
    a_members = (await client.get(f"/api/v1/teams/{a}/memberships")).json()
    assert str(member.id) not in {m["user_id"] for m in a_members}
    b_members = (await client.get(f"/api/v1/teams/{b}/memberships")).json()
    assert str(member.id) in {m["user_id"] for m in b_members}


@pytest.mark.asyncio
async def test_enriched_members_endpoint(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _create_subteam(client, "Eng")
    await client.post(
        f"/api/v1/teams/{team}/memberships",
        json={"user_id": str(member.id), "team_id": team, "role": "member"},
    )
    resp = await client.get(f"/api/v1/teams/{team}/members")
    assert resp.status_code == 200
    me = next(r for r in resp.json() if r["user_id"] == str(member.id))
    assert me["email"] == member.email
    assert me["role"] == "member"
    assert me["team_name"] == "Eng"
    assert me["display_name"]


@pytest.mark.asyncio
async def test_enriched_members_include_descendants_on_root(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _create_subteam(client, "Sub")
    await client.post(
        f"/api/v1/teams/{team}/memberships",
        json={"user_id": str(member.id), "team_id": team, "role": "member"},
    )
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/members?include_descendants=true")
    assert resp.status_code == 200
    user_ids = {r["user_id"] for r in resp.json()}
    assert str(member.id) in user_ids
    assert str(org_admin.admin_id) in user_ids


@pytest.mark.asyncio
async def test_enriched_members_requires_team_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await _create_subteam(client, "Closed")
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await client.post("/api/v1/auth/logout")
    await login(client, member.email, member_pw or "")
    resp = await client.get(f"/api/v1/teams/{team}/members")
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# removal from the ORG ROOT is deprovisioning, not a team edit
# --------------------------------------------------------------------------- #


async def _member_of(user_id, org_id) -> bool:
    """Whether the person still holds a membership in the org, read fresh."""
    from alkera_core.db.session import AsyncSessionLocal
    from backend.services.org import org_memberships as org_membership_service

    async with AsyncSessionLocal() as s:
        return (
            await org_membership_service.get(s, user_id=user_id, org_team_id=org_id)
        ) is not None


async def _is_active(user_id) -> bool:
    """Read `is_active` on a FRESH session — the setup session's identity map
    still holds the pre-request instance."""
    from alkera_core.db.session import AsyncSessionLocal
    from backend.services.identity import users as user_service

    async with AsyncSessionLocal() as s:
        user = await user_service.get_by_id(s, user_id)
        assert user is not None
        return user.is_active


@pytest.mark.asyncio
async def test_remove_from_org_root_deprovisions_the_member(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """`current_user` never consults `team_memberships`, so dropping the rows
    alone left the account authenticating fine — with continued access to
    everything scoped by `org_team_id` and continued spend on the org pool,
    until their 90-day CLI token expired on its own."""
    from tests.conftest import mint_cli_token

    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    token = await mint_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 200

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{member.id}")
    assert resp.status_code == 204, resp.text

    # The bearer credential they still hold on disk is dead, and they are no
    # longer a member; the identity itself is not the org's to disable.
    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 401
    assert await _member_of(member.id, org_admin.org_id) is False
    assert await _is_active(member.id) is True


@pytest.mark.asyncio
async def test_remove_from_a_subteam_is_not_deprovisioning(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """The asymmetric case: taking someone off ONE team must not kill their
    account or their session."""
    from tests.conftest import mint_cli_token

    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    token = await mint_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {token}"}

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Transient")
    add = await client.post(
        f"/api/v1/teams/{team_id}/memberships",
        json={"user_id": str(member.id), "team_id": team_id, "role": "member"},
    )
    assert add.status_code == 201

    resp = await client.delete(f"/api/v1/teams/{team_id}/memberships/{member.id}")
    assert resp.status_code == 204

    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 200
    assert await _is_active(member.id) is True


@pytest.mark.asyncio
async def test_admin_cannot_remove_themselves_from_the_org_root(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Even with a co-admin present: root removal deactivates + revokes, so
    self-removal is a self-lockout. Mirrors `set_member_active`'s own guard."""
    from alkera_core.models import TeamRole
    from backend.services.org import memberships as membership_service

    co_admin, co_pw = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    await login(client, co_admin.email, co_pw or "")
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{co_admin.id}")
    assert resp.status_code == 400
    assert "yourself" in resp.json()["error"]["message"]

    await real_session.rollback()
    assert (
        await membership_service.get(real_session, team_id=org_admin.org_id, user_id=co_admin.id)
        is not None
    )
    assert await _is_active(co_admin.id) is True


@pytest.mark.asyncio
async def test_offboarding_works_with_three_root_admins(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Three or more root admins is an ordinary shape (role=admin invitations,
    SSO group mapping). The last-org-admin guard used to blow up on it inside
    the service with an error the route doesn't translate, so the canonical
    offboarding gesture failed outright instead of deprovisioning."""
    from alkera_core.models import TeamRole
    from backend.services.org import memberships as membership_service
    from tests.conftest import mint_cli_token

    co_a, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    co_b, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    token = await mint_cli_token(user_id=co_b.id, email=co_b.email, org_team_id=org_admin.org_id)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 200

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{co_b.id}")
    assert resp.status_code == 204, resp.text

    await real_session.rollback()
    assert (
        await membership_service.get(real_session, team_id=org_admin.org_id, user_id=co_b.id)
        is None
    )
    assert await _member_of(co_b.id, org_admin.org_id) is False
    assert await _is_active(co_b.id) is True
    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 401

    # The org keeps the two admins it still has.
    admins = await membership_service.org_admins(real_session, org_id=org_admin.org_id)
    assert {a.id for a in admins} == {org_admin.admin_id, co_a.id}


@pytest.mark.asyncio
async def test_removing_a_person_who_also_belongs_elsewhere_never_reaches_the_other_org(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Org-root removal acts on the person's membership in THIS org. A person
    who also belongs to another org keeps that org, their account and the
    credential they hold there: removal is never a cross-tenant account-disable
    or session-kill primitive."""
    from alkera_core.models import OrgMembership, TeamMembership, TeamRole
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, _unique_org_name, mint_cli_token

    foreign_org, _foreign_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("foreign-admin"),
        admin_first_name="Foreign",
        admin_last_name="Admin",
        admin_password="foreign-pass-12345",
    )
    await real_session.commit()
    foreign_user, _ = await make_member(real_session, org_id=foreign_org.id, verified=True)
    token = await mint_cli_token(
        user_id=foreign_user.id, email=foreign_user.email, org_team_id=foreign_org.id
    )
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 200

    # The person also belongs to OUR org (a second membership and its seat).
    real_session.add(OrgMembership(user_id=foreign_user.id, org_team_id=org_admin.org_id))
    real_session.add(
        TeamMembership(user_id=foreign_user.id, team_id=org_admin.org_id, role=TeamRole.MEMBER)
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{foreign_user.id}")

    # The other tenant's account is untouched and the credential they hold
    # there still authenticates; only our membership went.
    assert resp.status_code == 204, resp.text
    await client.post("/api/v1/auth/logout")
    assert await _is_active(foreign_user.id) is True
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 200
    assert await _member_of(foreign_user.id, foreign_org.id) is True
    assert await _member_of(foreign_user.id, org_admin.org_id) is False

    await real_session.rollback()
    assert (
        await membership_service.get(
            real_session, team_id=org_admin.org_id, user_id=foreign_user.id
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_sub_team_removal_of_a_person_who_belongs_elsewhere_is_a_team_edit(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Below the root the route edits one team: the person stays a member of
    this org and of their other org, account untouched."""
    from alkera_core.models import OrgMembership, TeamMembership, TeamRole
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, _unique_org_name

    foreign_org, _foreign_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("foreign-admin2"),
        admin_first_name="Foreign",
        admin_last_name="Admin",
        admin_password="foreign-pass-12345",
    )
    await real_session.commit()
    foreign_user, _ = await make_member(real_session, org_id=foreign_org.id, verified=True)

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Shared")
    real_session.add(OrgMembership(user_id=foreign_user.id, org_team_id=org_admin.org_id))
    real_session.add(
        TeamMembership(user_id=foreign_user.id, team_id=UUID(team_id), role=TeamRole.MEMBER)
    )
    await real_session.commit()

    resp = await client.delete(f"/api/v1/teams/{team_id}/memberships/{foreign_user.id}")
    assert resp.status_code == 204, resp.text
    assert await _is_active(foreign_user.id) is True
    await real_session.rollback()
    assert (
        await membership_service.get(real_session, team_id=UUID(team_id), user_id=foreign_user.id)
        is None
    )
    assert await _member_of(foreign_user.id, org_admin.org_id) is True
    assert await _member_of(foreign_user.id, foreign_org.id) is True


@pytest.mark.asyncio
async def test_removing_an_in_org_member_from_the_root_still_deprovisions(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """The asymmetric half of the tenancy gate: the check must not be so eager
    that it refuses the ordinary same-org offboarding it exists to protect."""
    from tests.conftest import mint_cli_token

    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    token = await mint_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id
    )
    headers = {"Authorization": f"Bearer {token}"}

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{member.id}")
    assert resp.status_code == 204, resp.text

    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/teams", headers=headers)).status_code == 401
    assert await _member_of(member.id, org_admin.org_id) is False
    assert await _is_active(member.id) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param({"user_id": "not-a-uuid"}, "an id that is not one", id="bad user id"),
        pytest.param({"role": "member"}, "no user named at all", id="missing user"),
        pytest.param({"user_id": None, "team_id": None}, "nulls for both ids", id="null ids"),
    ],
)
async def test_a_refused_membership_body_says_what_a_membership_needs(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, object], reason: str
):
    """A malformed membership used to answer the generic ``validation_error``
    and a pydantic array. The non-obvious part of this body is that ``team_id``
    has to be repeated from the URL, and a caller reading ``loc``/``msg`` pairs
    has to work that out; the refusal now says it in words.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Refusals")

    resp = await client.post(f"/api/v1/teams/{team_id}/memberships", json=body)

    assert resp.status_code == 422, f"{reason}: {resp.text}"
    error = resp.json()["error"]
    assert error["code"] == "invalid_membership"
    assert "team_id" in error["message"] and "user_id" in error["message"]
    assert error["details"]["errors"]
    # Nothing joined the team on the way to the refusal.
    listed = await client.get(f"/api/v1/teams/{team_id}/memberships")
    assert listed.json() == []
