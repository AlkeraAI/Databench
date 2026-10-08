"""Permission descent: admin in any ancestor grants admin perms below.

Also the tenancy face of the same gate: `require_team_admin` must answer for a
team OUTSIDE the caller's org exactly as it does for a team that doesn't exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member


async def _create_subteam(client: AsyncClient, name: str) -> str:
    resp = await client.post("/api/v1/teams", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_org_admin_can_admin_subteam(client: AsyncClient, org_admin: OrgWithAdmin):
    """Org Admin (admin of root) can rename a subteam without an explicit
    admin row at the subteam level. Descent rule walks up the chain."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    sub_id = await _create_subteam(client, "Engineering")

    resp = await client.patch(f"/api/v1/teams/{sub_id}", json={"name": "Eng"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Eng"


@pytest.mark.asyncio
async def test_subteam_admin_does_not_become_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Sanity inverse: admin of a subteam should NOT have admin perms on
    the org root. Only descent goes downward."""
    from alkera_core.models import TeamRole
    from backend.services.identity import users as user_service
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email

    # Set up a regular user, then make them admin of a subteam (via the
    # service so chain materialization fills in MEMBER ancestors).
    sub = await team_service.create_subteam(
        real_session,
        org_team_id=org_admin.org_id,
        name="Subteam X",
        parent_team_id=org_admin.org_id,
    )
    await real_session.commit()

    sub_admin_email = _unique_email("subadm")
    sub_admin_pw = "subadm-pass-12345"
    sub_admin = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=sub_admin_email,
        first_name="Sub",
        last_name="Admin",
        password=sub_admin_pw,
    )
    # Verified so the 403 below comes purely from the descent boundary, not the
    # email-verification gate on team mutations.
    sub_admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    await membership_service.add_member(
        real_session, team_id=sub.id, user_id=sub_admin.id, role=TeamRole.ADMIN
    )
    await real_session.commit()

    # Subteam admin can't act as org admin (e.g. delete a sibling team).
    sibling = await team_service.create_subteam(
        real_session,
        org_team_id=org_admin.org_id,
        name="Sibling",
        parent_team_id=org_admin.org_id,
    )
    await real_session.commit()

    await login(client, sub_admin_email, sub_admin_pw)
    # Try to PATCH the sibling team — should be 403 (sub admin has no
    # admin row above the sibling's chain).
    resp = await client.patch(f"/api/v1/teams/{sibling.id}", json={"name": "Hijacked"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_subteam_admin_can_admin_their_own_subtree(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    from alkera_core.models import TeamRole
    from backend.services.identity import users as user_service
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email

    parent = await team_service.create_subteam(
        real_session,
        org_team_id=org_admin.org_id,
        name="Parent Team",
        parent_team_id=org_admin.org_id,
    )
    await real_session.commit()
    child = await team_service.create_subteam(
        real_session,
        org_team_id=org_admin.org_id,
        name="Child Team",
        parent_team_id=parent.id,
    )
    await real_session.commit()

    sub_email = _unique_email("desc")
    sub_pw = "desc-pass-12345"
    sub_user = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=sub_email,
        first_name="Desc",
        last_name="User",
        password=sub_pw,
    )
    # Verified: this test exercises permission descent (parent-admin renames
    # child), not the email-verification gate on team mutations.
    sub_user.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    await membership_service.add_member(
        real_session, team_id=parent.id, user_id=sub_user.id, role=TeamRole.ADMIN
    )
    await real_session.commit()

    await login(client, sub_email, sub_pw)
    # Admin of parent can rename child.
    resp = await client.patch(f"/api/v1/teams/{child.id}", json={"name": "Renamed Child"})
    assert resp.status_code == 200


# --- the team-admin gate's tenancy boundary ---------------------------------
# Probed through GET /teams/{team_id}/invitations, whose only guard is
# require_team_admin — so the responses below are the dependency's own.


def _without_trace_ids(body: Any) -> Any:
    """The error envelope minus its per-request trace ids, at any depth."""
    if isinstance(body, dict):
        return {k: _without_trace_ids(v) for k, v in body.items() if k != "trace_id"}
    if isinstance(body, list):
        return [_without_trace_ids(v) for v in body]
    return body


async def _foreign_org_root(real_session) -> object:
    import secrets

    from backend.services.org import teams as team_service

    other_root, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Foreign Org {secrets.token_hex(4)}",
        admin_email=f"foreign-admin-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Foreign",
        admin_last_name="Admin",
        admin_password="x" * 16,
    )
    await real_session.commit()
    return other_root


@pytest.mark.asyncio
async def test_a_foreign_team_is_indistinguishable_from_a_nonexistent_one(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """A REAL team in another org and a random UUID must draw the same 404 with the
    same body — a 403 (or any body difference) would confirm to an outsider that
    the id is live."""
    other_root = await _foreign_org_root(real_session)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    foreign = await client.get(f"/api/v1/teams/{other_root.id}/invitations")
    ghost = await client.get(f"/api/v1/teams/{uuid4()}/invitations")
    assert foreign.status_code == 404
    assert ghost.status_code == 404
    assert _without_trace_ids(foreign.json()) == _without_trace_ids(ghost.json())


@pytest.mark.asyncio
async def test_an_in_org_non_admin_still_draws_403_not_404(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    # The tenancy 404 must not swallow the in-org authorization answer: a plain
    # member of the org root is told "forbidden", not "no such team".
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/invitations")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_an_in_org_admin_still_passes_the_gate(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/teams/{org_admin.org_id}/invitations")
    assert resp.status_code == 200
    assert resp.json() == []  # a fresh org has no pending invitations
