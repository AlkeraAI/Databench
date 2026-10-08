"""One person, two orgs: org admin, teams, members, connections, ops.

The adversarial suite for the org-administration surfaces. Its subject is a
person U in org A (their home) who also holds an active membership in org B,
each org with its own admin (``two_org_identity``), with multi-org on for the
test (``multi_org``). Every case drives the real route or service against real
Postgres and asserts what the B credential can and cannot reach of A.

The riskiest surface is the team-connections set: it is what U's daemon writes
into a workspace, and a credential lease hands out the secret itself. Every
"teams this person is in" and "rows this person owns" read there is pinned to
the request's org, so A's warehouse connections never reach a B session.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alkera_core.auth.secret_box import encrypt_secret
from alkera_core.connections.models import ConnectionInventoryEntry, TeamConnection
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    CrashReport,
    Invitation,
    InvitationStatus,
    MembershipStatus,
    OrgMembership,
    OrgSettings,
    TeamMembership,
    TeamRole,
    User,
)
from backend.services.connections import team_connections as team_connection_service
from backend.services.org import memberships as membership_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import TwoOrg, mint_cli_token

pytestmark = [pytest.mark.usefixtures("multi_org")]


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(user: User, org_id: UUID) -> str:
    """A verified admin's bearer for ``org_id`` (the admin gates on writes want
    a proven address)."""
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(User).where(User.id == user.id).values(email_verified_at=datetime.now(UTC))
        )
        await s.commit()
    return await mint_cli_token(user_id=user.id, email=user.email, org_team_id=org_id)


async def _make_admin(session: AsyncSession, *, user_id: UUID, team_id: UUID) -> None:
    """Grant ADMIN on an existing row without the role-change path, which ends
    every live session of the account (and with it the fixture's tokens)."""
    await session.execute(
        update(TeamMembership)
        .where(TeamMembership.user_id == user_id, TeamMembership.team_id == team_id)
        .values(role=TeamRole.ADMIN)
    )
    await session.commit()


async def _subteam(session: AsyncSession, org_id: UUID, name: str) -> UUID:
    team = await team_service.create_subteam(
        session, org_team_id=org_id, name=f"{name}-{uuid4().hex[:6]}", parent_team_id=org_id
    )
    await session.commit()
    return team.id


async def _connection(
    session: AsyncSession,
    *,
    team_id: UUID,
    owner: UUID | None = None,
    auth_mode: str = "shared",
) -> UUID:
    conn = TeamConnection(
        team_id=team_id,
        owner_user_id=owner,
        plugin="snowflake",
        handle=f"wh-{uuid4().hex[:10]}",
        auth_mode=auth_mode,
        shared_secret_encrypted=encrypt_secret("s3cret") if auth_mode == "shared" else None,
    )
    session.add(conn)
    await session.commit()
    return conn.id


@dataclass
class Connections:
    """One team row and one personal row of U's in each org."""

    a_team: UUID
    a_personal: UUID
    b_team: UUID
    b_personal: UUID

    @property
    def a(self) -> set[UUID]:
        return {self.a_team, self.a_personal}

    @property
    def b(self) -> set[UUID]:
        return {self.b_team, self.b_personal}


@pytest.fixture
async def conns(real_session: AsyncSession, two_org_identity: TwoOrg) -> Connections:
    t = two_org_identity
    return Connections(
        a_team=await _connection(real_session, team_id=t.org_a),
        a_personal=await _connection(real_session, team_id=t.org_a, owner=t.user.id),
        b_team=await _connection(real_session, team_id=t.org_b),
        b_personal=await _connection(real_session, team_id=t.org_b, owner=t.user.id),
    )


async def _listed(client: AsyncClient, path: str, token: str) -> set[UUID]:
    resp = await client.get(path, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body["connections"] if isinstance(body, dict) else body
    return {UUID(row["id"]) for row in rows}


# --- team connections: the set a workspace syncs -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/v1/me/team-connections", id="daemon-sync-down"),
        pytest.param("/api/v1/me/connections", id="connections-page"),
    ],
)
async def test_each_credential_lists_only_its_own_orgs_connections(
    client: AsyncClient, two_org_identity: TwoOrg, conns: Connections, path: str
) -> None:
    t = two_org_identity
    assert await _listed(client, path, t.token_b) == conns.b
    assert await _listed(client, path, t.token_a) == conns.a


@pytest.mark.asyncio
async def test_admin_descent_in_one_org_reaches_nothing_in_a_session_of_the_other(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    """U administers A's root, so A's sub-team rows are theirs to use in A. On
    the B credential the administered set is B's, and A's sub-team row stays
    out of the sync-down even though U holds no membership row on that team."""
    t = two_org_identity
    await _make_admin(real_session, user_id=t.user.id, team_id=t.org_a)
    a_sub = await _subteam(real_session, t.org_a, "a-sub")
    a_sub_row = await _connection(real_session, team_id=a_sub)

    assert a_sub_row in await _listed(client, "/api/v1/me/team-connections", t.token_a)
    assert a_sub_row not in await _listed(client, "/api/v1/me/team-connections", t.token_b)
    assert (
        await team_service.admin_team_ids(real_session, t.user.id, org_team_id=t.org_b)
    ).isdisjoint({t.org_a, a_sub})


@pytest.mark.asyncio
async def test_entitlement_is_decided_in_the_org_asked(
    real_session: AsyncSession, two_org_identity: TwoOrg, conns: Connections
) -> None:
    t = two_org_identity
    for org, mine, theirs in ((t.org_b, conns.b, conns.a), (t.org_a, conns.a, conns.b)):
        for connection_id in mine:
            assert await team_connection_service.is_member_entitled(
                real_session, user_id=t.user.id, connection_id=connection_id, org_team_id=org
            )
        for connection_id in theirs:
            assert not await team_connection_service.is_member_entitled(
                real_session, user_id=t.user.id, connection_id=connection_id, org_team_id=org
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        pytest.param("GET", "credential", id="fetch"),
        pytest.param("POST", "credential-lease", id="lease"),
    ],
)
@pytest.mark.parametrize("which", ["a_team", "a_personal"])
async def test_a_b_credential_never_receives_an_a_secret(
    client: AsyncClient,
    two_org_identity: TwoOrg,
    conns: Connections,
    method: str,
    suffix: str,
    which: str,
) -> None:
    t = two_org_identity
    a_row = getattr(conns, which)
    b_row = conns.b_team if which == "a_team" else conns.b_personal
    refused = await client.request(
        method, f"/api/v1/me/team-connections/{a_row}/{suffix}", headers=_bearer(t.token_b)
    )
    assert refused.status_code == 404, refused.text
    assert "s3cret" not in refused.text
    # The same person, the same door, the B row: handed out.
    served = await client.request(
        method, f"/api/v1/me/team-connections/{b_row}/{suffix}", headers=_bearer(t.token_b)
    )
    assert served.status_code == 200, served.text
    assert served.json()["secret"] == "s3cret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        pytest.param("POST", "/rotate-secret", {"shared_secret": "new-secret"}, id="rotate"),
        pytest.param("POST", "/verify", None, id="verify"),
        pytest.param("DELETE", "", None, id="delete"),
    ],
)
async def test_the_personal_routes_refuse_the_persons_own_row_from_the_other_org(
    client: AsyncClient,
    two_org_identity: TwoOrg,
    conns: Connections,
    method: str,
    suffix: str,
    body: dict[str, str] | None,
) -> None:
    t = two_org_identity
    resp = await client.request(
        method,
        f"/api/v1/me/connections/{conns.a_personal}{suffix}",
        headers=_bearer(t.token_b),
        json=body,
    )
    assert resp.status_code == 404, resp.text
    async with AsyncSessionLocal() as s:
        row = await s.get(TeamConnection, conns.a_personal)
        assert row is not None
        assert row.credential_version == 0


@pytest.mark.asyncio
async def test_a_b_credential_cannot_move_an_a_connection(
    client: AsyncClient, two_org_identity: TwoOrg, conns: Connections
) -> None:
    t = two_org_identity
    resp = await client.post(
        f"/api/v1/connections/{conns.a_team}/owner",
        headers=_bearer(t.token_b),
        json={"team_id": None},
    )
    assert resp.status_code in (403, 404), resp.text
    async with AsyncSessionLocal() as s:
        row = await s.get(TeamConnection, conns.a_team)
        assert row is not None
        assert (row.team_id, row.owner_user_id) == (t.org_a, None)


@pytest.mark.asyncio
async def test_an_administered_set_naming_another_orgs_team_is_ignored(
    real_session: AsyncSession, two_org_identity: TwoOrg, conns: Connections
) -> None:
    """The listing trusts no caller to have scoped the administered set: a team
    of another org in it contributes nothing."""
    t = two_org_identity
    pairs = await team_connection_service.list_visible_to_member(
        real_session, t.user.id, org_team_id=t.org_b, also_team_ids=[t.org_a]
    )
    assert {conn.id for conn, _ in pairs} == conns.b


@pytest.mark.asyncio
async def test_the_oauth_relay_refuses_an_a_connection_on_a_b_credential(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    a_row = await _connection(real_session, team_id=t.org_a, auth_mode="per_user")
    refused = await client.get(
        f"/api/v1/me/team-connections/{a_row}/oauth/spec", headers=_bearer(t.token_b)
    )
    assert refused.status_code == 404, refused.text
    # On A's credential the entitlement passes and the route answers on the row
    # itself (this one has no browser sign-in to relay).
    passed = await client.get(
        f"/api/v1/me/team-connections/{a_row}/oauth/spec", headers=_bearer(t.token_a)
    )
    assert passed.status_code != 404, passed.text


# --- teams, members, settings -------------------------------------------------


@pytest.mark.asyncio
async def test_a_b_credential_lists_only_b_teams_and_settings(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    a_sub = await _subteam(real_session, t.org_a, "a-only")
    b_sub = await _subteam(real_session, t.org_b, "b-only")
    await real_session.execute(
        update(OrgSettings)
        .where(OrgSettings.org_team_id == t.org_a)
        .values(allow_login_github=False)
    )
    await real_session.commit()

    teams = await client.get("/api/v1/teams", headers=_bearer(t.token_b))
    assert teams.status_code == 200, teams.text
    ids = {UUID(row["id"]) for row in teams.json()}
    assert {t.org_b, b_sub} <= ids
    assert ids.isdisjoint({t.org_a, a_sub})
    for team_id in (t.org_a, a_sub):
        resp = await client.get(f"/api/v1/teams/{team_id}", headers=_bearer(t.token_b))
        assert resp.status_code == 404, resp.text

    for token, github in ((t.token_b, True), (t.token_a, False)):
        resp = await client.get("/api/v1/org/settings", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        assert resp.json()["allow_login_github"] is github


@pytest.mark.asyncio
async def test_each_admin_lists_only_their_own_orgs_members(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    await _make_admin(real_session, user_id=t.user.id, team_id=t.org_b)
    for token, expected, foreign in (
        (t.token_b, {t.admin_b.id, t.user.id}, t.admin_a.id),
        (await _admin_token(t.admin_a, t.org_a), {t.admin_a.id, t.user.id}, t.admin_b.id),
    ):
        resp = await client.get("/api/v1/org/members", headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        ids = {UUID(row["user_id"]) for row in resp.json()}
        assert ids == expected
        assert foreign not in ids


@pytest.mark.asyncio
async def test_a_admins_roster_carries_none_of_the_persons_b_teams(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    b_sub = await _subteam(real_session, t.org_b, "b-team")
    await membership_service.add_member(real_session, team_id=b_sub, user_id=t.user.id)
    await real_session.commit()
    token = await _admin_token(t.admin_a, t.org_a)
    resp = await client.get(
        f"/api/v1/teams/{t.org_a}/members",
        params={"include_descendants": "true"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    team_ids = {UUID(row["team_id"]) for row in resp.json()}
    assert team_ids <= {team.id for team in await team_service.list_in_org(real_session, t.org_a)}
    assert b_sub not in team_ids
    assert t.user.id in {UUID(row["user_id"]) for row in resp.json()}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        pytest.param("POST", "", "add", id="add"),
        pytest.param("PATCH", "/{admin_a}", {"role": "member"}, id="role-change"),
        pytest.param("DELETE", "/{admin_a}", None, id="remove"),
    ],
)
async def test_a_b_credential_cannot_write_memberships_in_a(
    client: AsyncClient,
    real_session: AsyncSession,
    two_org_identity: TwoOrg,
    method: str,
    suffix: str,
    body: object,
) -> None:
    """U administers both roots: the authority exists in A, and still the B
    credential reaches nothing there."""
    t = two_org_identity
    await _make_admin(real_session, user_id=t.user.id, team_id=t.org_a)
    await _make_admin(real_session, user_id=t.user.id, team_id=t.org_b)
    a_sub = await _subteam(real_session, t.org_a, "a-write")
    json_body = (
        {"user_id": str(t.admin_a.id), "team_id": str(a_sub), "role": "member"}
        if body == "add"
        else body
    )
    team = a_sub if body == "add" else t.org_a
    resp = await client.request(
        method,
        f"/api/v1/teams/{team}/memberships{suffix.format(admin_a=t.admin_a.id)}",
        headers=_bearer(t.token_b),
        json=json_body,
    )
    assert resp.status_code == 404, resp.text
    async with AsyncSessionLocal() as s:
        root_row = await membership_service.get(s, team_id=t.org_a, user_id=t.admin_a.id)
        assert root_row is not None
        assert root_row.role is TeamRole.ADMIN
        assert await membership_service.get(s, team_id=a_sub, user_id=t.admin_a.id) is None
        admin_a = await s.get(User, t.admin_a.id)
        assert admin_a is not None
        assert admin_a.is_active


@pytest.mark.asyncio
async def test_a_b_admin_cannot_seat_a_person_who_belongs_only_to_a(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    """U's home is A, as is A's admin's. Comparing home orgs would call A's
    admin "in the org" of U's B session; belonging is B's membership table."""
    t = two_org_identity
    await _make_admin(real_session, user_id=t.user.id, team_id=t.org_b)
    resp = await client.post(
        f"/api/v1/teams/{t.org_b}/memberships",
        headers=_bearer(t.token_b),
        json={"user_id": str(t.admin_a.id), "team_id": str(t.org_b), "role": "member"},
    )
    assert resp.status_code == 400, resp.text
    async with AsyncSessionLocal() as s:
        assert await membership_service.get(s, team_id=t.org_b, user_id=t.admin_a.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("org", ["home", "other"])
async def test_an_org_switches_off_its_own_membership_and_never_the_identity(
    client: AsyncClient, two_org_identity: TwoOrg, org: str
) -> None:
    """Deactivation and removal act on the person's membership in the admin's
    org, never on the identity, which is in both orgs: the credential for this
    org stops working, the other org's keeps working, and the identity stays
    active. Home or not makes no difference: home is not authority."""
    t = two_org_identity
    if org == "home":
        admin, org_id, here, there = t.admin_a, t.org_a, t.token_a, t.token_b
        other_org = t.org_b
    else:
        admin, org_id, here, there = t.admin_b, t.org_b, t.token_b, t.token_a
        other_org = t.org_a
    headers = _bearer(await _admin_token(admin, org_id))
    deactivate = await client.put(
        f"/api/v1/org/members/{t.user.id}/active", headers=headers, json={"active": False}
    )
    assert deactivate.status_code == 200, deactivate.text
    assert deactivate.json()["is_active"] is False
    assert (await client.get("/api/v1/teams", headers=_bearer(here))).status_code == 401
    assert (await client.get("/api/v1/teams", headers=_bearer(there))).status_code == 200

    deprovision = await client.delete(
        f"/api/v1/teams/{org_id}/memberships/{t.user.id}", headers=headers
    )
    assert deprovision.status_code == 204, deprovision.text
    async with AsyncSessionLocal() as s:
        user = await s.get(User, t.user.id)
        assert user is not None
        assert user.is_active
        gone = await org_membership_service.active(s, user_id=t.user.id, org_team_id=org_id)
        kept = await org_membership_service.active(s, user_id=t.user.id, org_team_id=other_org)
        assert gone is None
        assert kept is not None
    assert (await client.get("/api/v1/teams", headers=_bearer(there))).status_code == 200


@pytest.mark.asyncio
async def test_the_dashboard_on_a_b_credential_is_b(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    a_sub = await _subteam(real_session, t.org_a, "a-dash")
    real_session.add(TeamMembership(user_id=t.user.id, team_id=a_sub, role=TeamRole.MEMBER))
    await real_session.commit()
    resp = await client.get("/api/v1/dashboard", headers=_bearer(t.token_b))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert UUID(body["org"]["id"]) == t.org_b
    assert UUID(body["user"]["org_team_id"]) == t.org_b
    assert {UUID(team["id"]) for team in body["teams"]} == {t.org_b}


# --- invitations ----------------------------------------------------------------


async def _invite(session: AsyncSession, *, team_id: UUID, email: str, inviter: User) -> Invitation:
    from backend.services.org import invitations as invitation_service

    team = await team_service.get_by_id(session, team_id)
    assert team is not None
    root = await team_service.org_root_id(session, team_id)
    invitation, _auto, _ = await invitation_service.create_invitation(
        session, team=team, email=email, role=TeamRole.MEMBER, invited_by=inviter, org_team_id=root
    )
    await session.commit()
    return invitation


@pytest.mark.asyncio
async def test_an_invitation_into_an_org_the_person_belongs_to_is_seated_at_once(
    real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    """Belonging is the org's membership table: U belongs to B (not their home),
    so B's invitation seats them directly, as it would any B member."""
    t = two_org_identity
    b_sub = await _subteam(real_session, t.org_b, "b-invite")
    invitation = await _invite(real_session, team_id=b_sub, email=t.user.email, inviter=t.admin_b)
    assert invitation.status is InvitationStatus.ACCEPTED
    assert await membership_service.get(real_session, team_id=b_sub, user_id=t.user.id)


@pytest.mark.asyncio
async def test_acceptance_needs_an_active_membership_in_the_inviting_org(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    """With U's A membership deactivated, A's invitation is refused as a
    deactivated membership, naming A: an invitation never restores what the
    org's admins turned off, though U still belongs to B."""
    from backend.services.org import invitations as invitation_service

    t = two_org_identity
    user_id = t.user.id
    a_sub = await _subteam(real_session, t.org_a, "a-invite")
    invitation = Invitation(
        team_id=a_sub,
        email=t.user.email,
        role=TeamRole.MEMBER,
        token=uuid4().hex,
        status=InvitationStatus.PENDING,
        invited_by_id=t.admin_a.id,
        expires_at=invitation_service._now() + invitation_service.DEFAULT_TTL,
    )
    real_session.add(invitation)
    await real_session.execute(
        update(OrgMembership)
        .where(OrgMembership.id == t.membership_a.id)
        .values(status=MembershipStatus.DEACTIVATED)
    )
    await real_session.commit()

    resp = await client.post(
        f"/api/v1/invitations/{invitation.id}/accept", headers=_bearer(t.token_b)
    )
    assert resp.status_code == 409, resp.text
    error = resp.json().get("error") or resp.json().get("detail")
    assert error["code"] == "membership_deactivated"
    a_name = (await team_service.get_by_id(real_session, t.org_a)).name  # type: ignore[union-attr]
    assert a_name in error["message"]
    async with AsyncSessionLocal() as s:
        assert await membership_service.get(s, team_id=a_sub, user_id=user_id) is None
        still = await org_membership_service.get(s, user_id=user_id, org_team_id=t.org_a)
        assert still is not None and still.status is MembershipStatus.DEACTIVATED


# --- connections inventory, BYOK, crash reports --------------------------------


@pytest.mark.asyncio
async def test_the_inventory_is_kept_per_org_and_listed_only_in_its_own(
    client: AsyncClient, real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    """U reports the same workspace under a credential for each org. Each
    report lands in its credential's org only: A's admin sees U's A report and
    never the B one, B's admin the reverse, and U's own listing under each
    credential shows that org's report alone. A person who belongs only to A
    never appears in B's listing."""
    t = two_org_identity
    workspace = uuid4().hex
    for token, plugin in ((t.token_a, "snowflake"), (t.token_b, "postgres")):
        resp = await client.put(
            "/api/v1/me/connection-inventory",
            headers=_bearer(token),
            json={
                "workspace": workspace,
                "connections": [{"plugin": plugin, "status": "ok", "last_verified_at": None}],
            },
        )
        assert resp.status_code == 204, resp.text
    real_session.add(
        ConnectionInventoryEntry(
            user_id=t.admin_a.id,
            org_team_id=t.org_a,
            workspace_key=uuid4().hex,
            plugin="bigquery",
            status="ok",
        )
    )
    await real_session.commit()

    async def listed(token: str, path: str) -> set[tuple[UUID, str]]:
        resp = await client.get(path, headers=_bearer(token))
        assert resp.status_code == 200, resp.text
        return {(UUID(r["user_id"]), r["plugin"]) for r in resp.json()["connections"]}

    org_path = "/api/v1/org/connection-inventory"
    assert await listed(await _admin_token(t.admin_b, t.org_b), org_path) == {
        (t.user.id, "postgres")
    }
    assert await listed(await _admin_token(t.admin_a, t.org_a), org_path) == {
        (t.user.id, "snowflake"),
        (t.admin_a.id, "bigquery"),
    }
    me_path = "/api/v1/me/connection-inventory"
    assert await listed(t.token_a, me_path) == {(t.user.id, "snowflake")}
    assert await listed(t.token_b, me_path) == {(t.user.id, "postgres")}
    # An empty report under B clears B's rows for the workspace and leaves A's.
    resp = await client.put(
        me_path, headers=_bearer(t.token_b), json={"workspace": workspace, "connections": []}
    )
    assert resp.status_code == 204, resp.text
    assert await listed(t.token_b, me_path) == set()
    assert await listed(t.token_a, me_path) == {(t.user.id, "snowflake")}


@pytest.mark.asyncio
async def test_a_crash_report_is_filed_under_the_credentials_org(
    client: AsyncClient, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    for token, component in ((t.token_a, "cli"), (t.token_b, "web")):
        resp = await client.post(
            "/api/v1/errors/reports",
            headers=_bearer(token),
            json={"component": component, "error_type": "Boom", "message": "it broke"},
        )
        assert resp.status_code == 201, resp.text
    async with AsyncSessionLocal() as s:
        filed = dict(
            (
                await s.execute(
                    select(CrashReport.component, CrashReport.org_team_id).where(
                        CrashReport.user_id == t.user.id
                    )
                )
            ).all()
        )
    assert filed == {"cli": t.org_a, "web": t.org_b}
    listed = await client.get("/api/v1/errors/reports", headers=_bearer(t.token_b))
    assert listed.status_code == 200, listed.text
    assert [row["component"] for row in listed.json()] == ["web"]


@pytest.mark.asyncio
async def test_byok_provider_keys_are_the_request_orgs(
    client: AsyncClient,
    real_session: AsyncSession,
    two_org_identity: TwoOrg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U administers both orgs. A's configured provider key never shows on, is
    never tested by, and is never removed through, the B credential."""
    from datetime import UTC, datetime, timedelta

    from alkera_core import entitlements as ent
    from alkera_core.config import settings
    from alkera_core.models import ModelProviderConfig

    priv, pub = ent.generate_keypair()
    token = ent.mint_entitlement_token(
        customer="acme-corp",
        features=ent.Feature.BYOK,
        expires_on=(datetime.now(UTC) + timedelta(days=30)).date(),
        serial=1,
        signing_key_b64=priv,
    )
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "alkera_entitlements", token)
    monkeypatch.setattr(settings, "alkera_entitlements_public_key", pub)
    ent.get_entitlements.cache_clear()
    try:
        t = two_org_identity
        await _make_admin(real_session, user_id=t.user.id, team_id=t.org_a)
        await _make_admin(real_session, user_id=t.user.id, team_id=t.org_b)
        real_session.add(
            ModelProviderConfig(
                org_team_id=t.org_a,
                provider="anthropic",
                api_key_encrypted=encrypt_secret("sk-ant-a"),
                secret_hint="…aaaa",
            )
        )
        await real_session.commit()
        base = "/api/v1/org/model-providers"

        listed = await client.get(base, headers=_bearer(t.token_b))
        assert listed.status_code == 200, listed.text
        cards = {card["provider"]: card for card in listed.json()["providers"]}
        assert cards["anthropic"]["configured"] is False
        a_cards = (await client.get(base, headers=_bearer(t.token_a))).json()["providers"]
        assert {c["provider"]: c for c in a_cards}["anthropic"]["configured"] is True

        removed = await client.delete(f"{base}/anthropic", headers=_bearer(t.token_b))
        assert removed.status_code == 404, removed.text
        async with AsyncSessionLocal() as s:
            count = await s.scalar(
                select(func.count())
                .select_from(ModelProviderConfig)
                .where(ModelProviderConfig.org_team_id == t.org_a)
            )
        assert count == 1
    finally:
        ent.get_entitlements.cache_clear()


@pytest.mark.asyncio
async def test_org_membership_helpers_read_one_org(
    real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    in_b = {user.id for user, _ in await org_membership_service.members_of(real_session, t.org_b)}
    assert in_b == {t.admin_b.id, t.user.id}
    elsewhere = await org_membership_service.other_active_org(
        real_session, user_id=t.user.id, org_team_id=t.org_b
    )
    assert elsewhere is not None and elsewhere.org_team_id == t.org_a
    assert (
        await org_membership_service.other_active_org(
            real_session, user_id=t.admin_b.id, org_team_id=t.org_b
        )
        is None
    )
    rows = await membership_service.list_for_user(real_session, t.user.id, org_team_id=t.org_b)
    assert {row.team_id for row in rows} == {t.org_b}
