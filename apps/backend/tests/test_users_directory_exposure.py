"""The organization directory: who may read it, and what it is allowed to say.

The roster read exposes every email in the tenant, so it sits at the same
privilege bar as the narrower per-team member listing beside it. And a read of
someone ELSE never carries their security posture: which colleagues have no
second factor, which sign in with a password, and which are Alkera staff is a
ranked target list, not directory data.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio

_DIRECTORY = "/api/v1/users"

#: Fields that describe how an account is DEFENDED rather than who it belongs
#: to. Every one of them tells an attacker which colleague to try next.
_POSTURE_FIELDS = [
    "mfa_enabled",
    "has_password",
    "platform_role",
    "platform_role_display",
    "email_verified_at",
    "email_verification_required",
    "email_verification_deadline",
    "verification_resend_available_at",
]


async def test_a_plain_member_cannot_read_the_org_directory(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """The lowest-privileged account the product issues gets nothing here — a
    contractor or a stolen member session must not be able to export the roster.
    """
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    resp = await client.get(_DIRECTORY)
    assert resp.status_code == 403


async def test_an_org_admin_still_reads_their_own_roster(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """The tightening is a privilege bar, not a removal: the surface still works
    for the role it exists for."""
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(_DIRECTORY)
    assert resp.status_code == 200
    rows = resp.json()
    by_email = {row["email"]: row for row in rows}
    assert member.email in by_email
    assert org_admin.admin_email in by_email
    # Still a usable directory entry.
    assert by_email[member.email]["display_name"]
    assert by_email[member.email]["id"] == str(member.id)


async def test_the_roster_never_carries_a_colleagues_security_posture(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """Even for the admin who is allowed to see the roster: listing colleagues is
    not a reason to publish who has no second factor."""
    await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    rows = (await client.get(_DIRECTORY)).json()
    assert rows, "the fixture org has members"
    leaked = {field for row in rows for field in _POSTURE_FIELDS if field in row}
    assert leaked == set(), f"roster exposes security posture: {sorted(leaked)}"


async def test_a_lookup_by_id_never_carries_security_posture(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """The single-user read is the same leak one id at a time, and it is open to
    any member — so it answers with the same narrow shape."""
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    colleague, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    resp = await client.get(f"{_DIRECTORY}/{colleague.id}")
    assert resp.status_code == 200
    leaked = {field for field in _POSTURE_FIELDS if field in resp.json()}
    assert leaked == set(), f"colleague lookup exposes security posture: {sorted(leaked)}"


async def test_a_lookup_by_id_still_identifies_the_colleague(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    colleague, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    body = (await client.get(f"{_DIRECTORY}/{colleague.id}")).json()
    assert body["id"] == str(colleague.id)
    assert body["email"] == colleague.email
    # The org's view of a colleague names no org: an identity may belong to
    # several, and which others it is in is not this org's to read.
    assert "org_team_id" not in body


async def test_the_roster_is_paged_rather_than_dumped_whole(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """One request must not be able to export an entire tenant. Rows come back in
    a stable order, so a page boundary is meaningful."""
    for _ in range(3):
        await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    everyone = (await client.get(_DIRECTORY)).json()
    assert len(everyone) >= 4

    first = (await client.get(f"{_DIRECTORY}?limit=2")).json()
    assert [row["id"] for row in first] == [row["id"] for row in everyone[:2]]

    second = (await client.get(f"{_DIRECTORY}?limit=2&offset=2")).json()
    assert [row["id"] for row in second] == [row["id"] for row in everyone[2:4]]


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("limit=100000", id="above-the-ceiling"),
        pytest.param("limit=0", id="zero-page"),
        pytest.param("offset=-1", id="negative-offset"),
    ],
)
async def test_a_page_outside_the_bounds_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, query: str
) -> None:
    """The ceiling is server-side: a caller cannot ask for the whole table back
    by naming a bigger page."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"{_DIRECTORY}?{query}")).status_code == 422


async def test_a_directory_read_still_stops_at_the_org_boundary(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """Narrowing the shape must not have loosened tenancy: another org's user is
    still indistinguishable from one that does not exist."""
    import secrets

    from backend.services.org import teams as team_service

    _other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=f"other-{secrets.token_hex(6)}@otherorg.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"{_DIRECTORY}/{other_admin.id}")).status_code == 404
    emails = {row["email"] for row in (await client.get(_DIRECTORY)).json()}
    assert other_admin.email not in emails
