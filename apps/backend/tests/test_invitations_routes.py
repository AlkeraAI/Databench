"""Invitation route tests."""

from __future__ import annotations

import secrets
from uuid import UUID

import pytest
from _statement_log import counting
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login

#: Fields of an invitation that differ between any two requests, whoever they
#: name: the row's id, the address the caller supplied, and the two timestamps
#: taken at insert. Everything else is what the SERVER decided, and is where an
#: existence oracle would have to show up.
_PER_REQUEST_FIELDS = frozenset({"id", "email", "created_at", "expires_at"})


async def _create_subteam(client: AsyncClient, name: str) -> str:
    resp = await client.post("/api/v1/teams", json={"name": name})
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_admin_creates_invitation_and_email_attempted(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    sent = monkeypatch_email_send
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Eng")

    resp = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": f"newcomer-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["team_id"] == team_id
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_invite_existing_user_in_same_org_auto_accepts(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """If the email already maps to a user in this org, the invitation is
    synthesized with status=accepted; no email is sent."""
    from backend.services.identity import users as user_service
    from tests.conftest import _unique_email

    member_email = _unique_email("member")
    await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=member_email,
        first_name="Member",
        last_name="User",
        password="member-pass-12345",
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "AutoAccept")
    resp = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": member_email, "role": "member"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "accepted"
    assert len(monkeypatch_email_send) == 0


@pytest.mark.asyncio
async def test_inviting_a_foreign_org_address_discloses_nothing_to_the_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """An org admin must not be able to use this route to learn whether an
    arbitrary address has an Alkera account in someone else's tenant.

    So the answer for an address that belongs to another org is byte-identical
    to the answer for an address that has no account at all — status, body
    shape and the email that goes out. The single-org rule is still enforced;
    it is enforced at acceptance, by the one person entitled to the fact.
    """
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email

    foreign_email = _unique_email("other")
    await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=foreign_email,
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    stranger_email = _unique_email("nobody")

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "CrossOrg")

    foreign = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": foreign_email, "role": "member"},
    )
    stranger = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": stranger_email, "role": "member"},
    )

    assert foreign.status_code == stranger.status_code == 201, foreign.text
    # Nothing in the body distinguishes them but the values the caller supplied.
    assert {k: v for k, v in foreign.json().items() if k not in _PER_REQUEST_FIELDS} == {
        k: v for k, v in stranger.json().items() if k not in _PER_REQUEST_FIELDS
    }
    assert foreign.json()["status"] == "pending"
    # And both got an invitation email — a silently-dropped send would be its own
    # oracle, visible in the admin's own mailbox timing.
    assert {m["email"] for m in monkeypatch_email_send} >= {foreign_email, stranger_email}


@pytest.mark.asyncio
async def test_the_foreign_org_invitation_is_refused_at_acceptance(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """The single-org rule still holds — and the refusal reaches the address's
    OWNER, who already knows their own account exists."""
    from alkera_core.models import TeamMembership
    from backend.services.org import teams as team_service
    from sqlalchemy import select
    from tests.conftest import _unique_email, make_member

    other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=_unique_email("other-admin"),
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    invitee_email = _unique_email("foreign-member")
    invitee, invitee_pw = await make_member(
        real_session, org_id=other_org.id, email=invitee_email, verified=True
    )

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "CrossOrgAccept")
    created = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "member"},
    )
    assert created.status_code == 201
    invitation_id = created.json()["id"]
    await client.post("/api/v1/auth/logout")

    await login(client, invitee.email, invitee_pw or "")
    accept = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert accept.status_code == 409, accept.text
    assert accept.json()["error"]["code"] == "other_org"

    joined = set(
        (
            await real_session.execute(
                select(TeamMembership.team_id).where(TeamMembership.user_id == invitee.id)
            )
        )
        .scalars()
        .all()
    )
    assert team_id not in {str(t) for t in joined}


@pytest.mark.asyncio
async def test_non_admin_cannot_invite(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    from tests.conftest import make_member

    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)

    # Create a subteam first as the org admin.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Locked")
    await client.post("/api/v1/auth/logout")

    await login(client, member.email, member_pw or "")
    resp = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": "outsider@alkera.dev", "role": "member"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_public_preview_returns_org_and_team(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "PreviewTeam")
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": f"preview-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]

    # Public route — no auth needed.
    await client.post("/api/v1/auth/logout")
    preview = await client.get(f"/api/v1/invitations/by-token/{token}")
    assert preview.status_code == 200
    body = preview.json()
    assert body["team_id"] == team_id
    assert body["team_name"] == "PreviewTeam"
    assert body["org_team_id"] == str(org_admin.org_id)
    assert "expires_at" in body


@pytest.mark.asyncio
async def test_public_preview_carries_the_invited_address(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    """The preview names the address the token is bound to — in the normalized
    form signup demands — so the landing page can fill the field in. An
    unresolvable token stays opaque: a 404 with no address in it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "AddressTeam")
    # Mixed case on the way in: signup compares against the stored lowercase
    # form, so that is the form the preview has to hand back.
    invited = f"Preview-{secrets.token_hex(4)}@Alkera.Dev"
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invited, "role": "member"},
    )
    assert create.status_code == 201
    token = monkeypatch_email_send[-1]["invitation_token"]

    await client.post("/api/v1/auth/logout")
    preview = await client.get(f"/api/v1/invitations/by-token/{token}")
    assert preview.status_code == 200
    assert preview.json()["email"] == invited.lower()

    # A token nobody issued answers the same way it always has, and says nothing.
    unknown = await client.get(f"/api/v1/invitations/by-token/{secrets.token_urlsafe(32)}")
    assert unknown.status_code == 404
    assert "email" not in unknown.json()
    assert invited.lower() not in unknown.text.lower()


@pytest.mark.asyncio
async def test_signup_accepts_the_previewed_address_and_refuses_another(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    """Why the preview carries the address at all: the invited one is the only
    one the token signs up, so the page must not let the invitee guess."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "BoundTeam")
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": f"bound-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]

    await client.post("/api/v1/auth/logout")
    previewed = (await client.get(f"/api/v1/invitations/by-token/{token}")).json()["email"]

    mismatch = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"mismatch-{secrets.token_hex(4)}@alkera.dev",
            "password": "a-good-password-1",
            "invite_token": token,
        },
    )
    assert mismatch.status_code == 400

    accepted = await client.post(
        "/api/v1/auth/signup",
        json={"email": previewed, "password": "a-good-password-1", "invite_token": token},
    )
    assert accepted.status_code == 201


@pytest.mark.asyncio
async def test_revoke_invitation(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Revokable")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": f"revoke-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
    )
    invitation_id = create.json()["id"]

    revoke = await client.delete(f"/api/v1/teams/{team_id}/invitations/{invitation_id}")
    assert revoke.status_code == 204


@pytest.mark.asyncio
async def test_invitation_token_stored_hashed_and_not_exposed(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    """The row stores only the HMAC hash; the read shape omits the raw token; the
    public preview resolves by the RAW token (hashed lookup), not by the hash."""
    from uuid import UUID

    from alkera_core.auth import hash_lookup_token
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import Invitation
    from sqlalchemy import select

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "HashTeam")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": f"hash-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
    )
    assert create.status_code == 201
    assert "token" not in create.json()  # raw token never returned by the API
    invitation_id = create.json()["id"]
    raw_token = monkeypatch_email_send[-1]["invitation_token"]

    async with AsyncSessionLocal() as session:
        inv = (
            await session.execute(select(Invitation).where(Invitation.id == UUID(invitation_id)))
        ).scalar_one()
        stored = inv.token
    assert stored != raw_token
    assert stored == hash_lookup_token(raw_token)

    await client.post("/api/v1/auth/logout")
    ok = await client.get(f"/api/v1/invitations/by-token/{raw_token}")
    assert ok.status_code == 200
    bad = await client.get(f"/api/v1/invitations/by-token/{stored}")
    assert bad.status_code == 404


@pytest.mark.asyncio
async def test_recipient_accepts_pending_invitation_by_id(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """A registered in-org recipient accepts a pending invite by id (the raw
    token is no longer retrievable from the API)."""
    from tests.conftest import _unique_email, make_member

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "AcceptById")
    invitee_email = _unique_email("invitee")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "member"},
    )
    assert create.status_code == 201  # pending: the invitee has no account yet
    invitation_id = create.json()["id"]
    await client.post("/api/v1/auth/logout")

    invitee, invitee_pw = await make_member(
        real_session, org_id=org_admin.org_id, email=invitee_email, verified=True
    )
    await login(client, invitee.email, invitee_pw or "")
    mine = await client.get("/api/v1/invitations/me")
    assert mine.status_code == 200
    assert invitation_id in {inv["id"] for inv in mine.json()}

    accept = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert accept.status_code == 200, accept.text
    assert team_id in accept.json()["joined_team_ids"]


@pytest.mark.asyncio
async def test_accept_by_id_rejects_non_recipient(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """Accepting someone else's invitation (or an unknown id) is a 404 — no
    cross-account accept, no probing."""
    from tests.conftest import _unique_email, make_member

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "NotYours")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": _unique_email("invited"), "role": "member"},
    )
    invitation_id = create.json()["id"]
    await client.post("/api/v1/auth/logout")

    other, other_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await login(client, other.email, other_pw or "")
    resp = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# single-org rule at ACCEPTANCE
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_accept_refused_when_the_recipient_lives_in_another_org(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """An address invited before it had an account can sign up into its OWN org
    instead of clicking the link. The invitation must then be dead: accepting it
    would materialize memberships — and every entitlement keyed off them, down
    to the team's decrypted warehouse credential — inside a tenant the user does
    not belong to, while `org_team_id`-scoped surfaces (the member list) keep
    them invisible to that tenant's admins."""
    from uuid import UUID

    from alkera_core.models import TeamMembership
    from backend.services.org import teams as team_service
    from sqlalchemy import select
    from tests.conftest import _unique_email, make_member

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Contractors")
    invitee_email = _unique_email("contractor")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "admin"},
    )
    assert create.status_code == 201
    invitation_id = create.json()["id"]
    await client.post("/api/v1/auth/logout")

    other_org, _other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Vendor Inc {secrets.token_hex(4)}",
        admin_email=_unique_email("vendor-admin"),
        admin_first_name="Vendor",
        admin_last_name="Admin",
        admin_password="vendor-pass-12345",
    )
    await real_session.commit()
    invitee, invitee_pw = await make_member(
        real_session, org_id=other_org.id, email=invitee_email, verified=True
    )

    await login(client, invitee.email, invitee_pw or "")
    # The recipient sees it, with the refusal instead of an Accept.
    mine = await client.get("/api/v1/invitations/me")
    assert mine.status_code == 200
    listed = {inv["id"]: inv for inv in mine.json()}
    assert listed[invitation_id]["refusal"]["code"] == "other_org"
    board = await client.get("/api/v1/dashboard")
    assert invitation_id not in {inv["id"] for inv in board.json()["pending_invitations"]}

    accept = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert accept.status_code == 409, accept.text
    assert accept.json()["error"]["code"] == "other_org"

    joined = set(
        (
            await real_session.execute(
                select(TeamMembership.team_id).where(TeamMembership.user_id == invitee.id)
            )
        )
        .scalars()
        .all()
    )
    assert UUID(team_id) not in joined
    assert org_admin.org_id not in joined


# --------------------------------------------------------------------------- #
# an invitation must not outlive its inviter's authority
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offboard",
    [
        pytest.param("deactivated", id="inviter-deactivated"),
        pytest.param("removed_from_org", id="inviter-removed-from-org"),
        pytest.param("demoted", id="inviter-demoted-to-member"),
        pytest.param("deleted", id="inviter-hard-deleted"),
    ],
)
async def test_pending_invitation_dies_with_its_inviters_authority(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session,
    monkeypatch_email_send,
    offboard: str,
):
    """A 7-day standing grant of an attacker-chosen role, mailed to an inbox the
    planter controls, is a re-entry backdoor if it survives their offboarding."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import Invitation, TeamRole
    from backend.services.org import memberships as membership_service
    from sqlalchemy import select
    from tests.conftest import _unique_email, make_member

    inviter, inviter_pw = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    await login(client, inviter.email, inviter_pw or "")
    team_id = await _create_subteam(client, "Backdoor")
    invitee_email = _unique_email("planted")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "admin"},
    )
    assert create.status_code == 201
    invitation_id = create.json()["id"]
    await client.post("/api/v1/auth/logout")

    if offboard == "deactivated":
        async with AsyncSessionLocal() as s:
            from backend.services.identity import users as user_service

            target = await user_service.get_by_id(s, inviter.id)
            assert target is not None
            target.is_active = False
            await s.commit()
    elif offboard == "removed_from_org":
        await login(client, org_admin.admin_email, org_admin.admin_password)
        gone = await client.delete(f"/api/v1/teams/{org_admin.org_id}/memberships/{inviter.id}")
        assert gone.status_code == 204, gone.text
        await client.post("/api/v1/auth/logout")
    elif offboard == "demoted":
        await login(client, org_admin.admin_email, org_admin.admin_password)
        demote = await client.patch(
            f"/api/v1/teams/{org_admin.org_id}/memberships/{inviter.id}",
            json={"role": "member"},
        )
        assert demote.status_code == 200, demote.text
        await client.post("/api/v1/auth/logout")
    else:
        async with AsyncSessionLocal() as s:
            from backend.services.identity import users as user_service

            target = await user_service.get_by_id(s, inviter.id)
            assert target is not None
            await s.delete(target)
            await s.commit()

    invitee, invitee_pw = await make_member(
        real_session, org_id=org_admin.org_id, email=invitee_email, verified=True
    )
    await login(client, invitee.email, invitee_pw or "")
    accept = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert accept.status_code == 409, accept.text

    from uuid import UUID

    membership = await membership_service.get(
        real_session, team_id=UUID(team_id), user_id=invitee.id
    )
    assert membership is None

    if offboard == "removed_from_org":
        # Deprovisioning also SWEEPS the invitation, so it is not merely
        # unredeemable — it is closed out.
        row = (
            await real_session.execute(
                select(Invitation).where(Invitation.id == UUID(invitation_id))
            )
        ).scalar_one()
        await real_session.refresh(row)
        assert row.status.value == "revoked"


@pytest.mark.asyncio
async def test_revoke_pending_by_inviter_only_touches_that_inviters_pending_rows(
    real_session, org_admin: OrgWithAdmin
):
    from alkera_core.models import TeamRole
    from backend.services.identity import users as user_service
    from backend.services.org import invitations as invitation_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, make_member

    admin_a, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    admin_b, _ = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN, verified=True
    )
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name=f"Sweep {secrets.token_hex(3)}"
    )
    await real_session.commit()

    mine, _, _ = await invitation_service.create_invitation(
        real_session,
        team=team,
        email=_unique_email("sweep-a"),
        role=TeamRole.MEMBER,
        invited_by=admin_a,
        org_team_id=org_admin.org_id,
    )
    theirs, _, _ = await invitation_service.create_invitation(
        real_session,
        team=team,
        email=_unique_email("sweep-b"),
        role=TeamRole.MEMBER,
        invited_by=admin_b,
        org_team_id=org_admin.org_id,
    )
    # An already-resolved row of the same inviter must be left alone.
    existing = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=_unique_email("sweep-existing"),
        first_name="Already",
        last_name="Here",
        password="x" * 12,
    )
    await real_session.commit()
    accepted, auto, _ = await invitation_service.create_invitation(
        real_session,
        team=team,
        email=existing.email,
        role=TeamRole.MEMBER,
        invited_by=admin_a,
        org_team_id=org_admin.org_id,
    )
    assert auto is True
    await real_session.commit()

    swept = await invitation_service.revoke_pending_by_inviter(real_session, admin_a.id)
    await real_session.commit()

    assert swept == 1
    assert mine.status.value == "revoked"
    assert mine.resolved_at is not None
    assert theirs.status.value == "pending"
    assert accepted.status.value == "accepted"


# --------------------------------------------------------------------------- #
# listing pending invitations: same answer, constant number of queries
# --------------------------------------------------------------------------- #


async def _seed_pending_for_a_new_address(
    session: AsyncSession, *, org_id: UUID, invited_by: User, teams: int
) -> str:
    """`teams` fresh subteams of `org_id`, each holding a pending invitation for
    one brand-new address — then the account for that address. Invitations must
    predate the account, otherwise `create_invitation` auto-accepts them."""
    from alkera_core.models import TeamRole
    from backend.services.org import invitations as invitation_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, make_member

    email = _unique_email("pending-list")
    for i in range(teams):
        team = await team_service.create_subteam(
            session, org_team_id=org_id, name=f"Pending {secrets.token_hex(3)}-{i}"
        )
        await invitation_service.create_invitation(
            session,
            team=team,
            email=email,
            role=TeamRole.MEMBER,
            invited_by=invited_by,
            org_team_id=org_id,
        )
    await session.commit()
    await make_member(session, org_id=org_id, email=email)
    return email


@pytest.mark.asyncio
async def test_listing_pending_invitations_issues_a_constant_number_of_queries(
    real_session, org_admin: OrgWithAdmin
):
    """`/invitations/me` and the dashboard both filter every pending row down to
    the recipient's own org. That filter ran one recursive ancestor-walk CTE PER
    ROW, so an address holding 20 invitations fired 20 recursive queries on a
    request that had already loaded the rows. The org question has one answer for
    the whole list, so the query count must not grow with the row count."""
    from backend.services.identity import users as user_service
    from backend.services.org import invitations as invitation_service

    admin = await user_service.get_by_id(real_session, org_admin.admin_id)
    assert admin is not None

    few = await _seed_pending_for_a_new_address(
        real_session, org_id=org_admin.org_id, invited_by=admin, teams=1
    )
    many = await _seed_pending_for_a_new_address(
        real_session, org_id=org_admin.org_id, invited_by=admin, teams=12
    )

    counts: dict[int, int] = {}
    for expected, email in ((1, few), (12, many)):
        with counting() as statements:
            rows = await invitation_service.list_pending_for_email(
                real_session, email, org_team_id=org_admin.org_id
            )
        assert len(rows) == expected
        counts[expected] = len(statements)

    assert counts[1] == counts[12], (
        f"query count scales with row count: {counts[1]} statements for 1 pending "
        f"invitation, {counts[12]} for 12"
    )


@pytest.mark.asyncio
async def test_pending_invitation_list_is_scoped_to_the_recipients_own_org(
    real_session, org_admin: OrgWithAdmin
):
    """The org filter is depth-agnostic and fail-closed: an invitation into a team
    nested three levels under the recipient's own org root is listed, one into a
    foreign tenant's team never is. The single-org rule makes a foreign invitation
    permanently unacceptable, so surfacing it would render an Accept button on
    another tenant."""
    from alkera_core.models import TeamRole
    from backend.services.identity import users as user_service
    from backend.services.org import invitations as invitation_service
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, make_member

    admin = await user_service.get_by_id(real_session, org_admin.admin_id)
    assert admin is not None
    root = await team_service.get_by_id(real_session, org_admin.org_id)
    assert root is not None

    parent_id = org_admin.org_id
    for level in range(3):
        nested = await team_service.create_subteam(
            real_session,
            org_team_id=org_admin.org_id,
            name=f"Deep {secrets.token_hex(3)}-{level}",
            parent_team_id=parent_id,
        )
        parent_id = nested.id
    deep = await team_service.get_by_id(real_session, parent_id)
    assert deep is not None

    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Foreign Inc {secrets.token_hex(4)}",
        admin_email=_unique_email("foreign-admin"),
        admin_first_name="Foreign",
        admin_last_name="Admin",
        admin_password="foreign-pass-12345",
    )
    await real_session.commit()

    email = _unique_email("scoped")
    on_root, _, _ = await invitation_service.create_invitation(
        real_session,
        team=root,
        email=email,
        role=TeamRole.MEMBER,
        invited_by=admin,
        org_team_id=org_admin.org_id,
    )
    nested_invite, _, _ = await invitation_service.create_invitation(
        real_session,
        team=deep,
        email=email,
        role=TeamRole.MEMBER,
        invited_by=admin,
        org_team_id=org_admin.org_id,
    )
    foreign, _, _ = await invitation_service.create_invitation(
        real_session,
        team=other_org,
        email=email,
        role=TeamRole.ADMIN,
        invited_by=other_admin,
        org_team_id=other_org.id,
    )
    await real_session.commit()
    await make_member(real_session, org_id=org_admin.org_id, email=email)

    pending = await invitation_service.list_pending_for_email(
        real_session, email, org_team_id=org_admin.org_id
    )
    listed = {inv.id for inv in pending}
    assert listed == {on_root.id, nested_invite.id}
    assert foreign.id not in listed


@pytest.mark.asyncio
async def test_reinviting_an_existing_team_member_conflicts(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """Inviting someone already on the team is a 409 the admin can read.

    The auto-accept branch adds the membership outright, so a second invite to
    the same team hits an existing row. That has to surface as a conflict — it
    escaped as an unhandled MembershipError before.
    """
    from backend.services.identity import users as user_service
    from backend.services.org import memberships as membership_service
    from tests.conftest import _unique_email

    member_email = _unique_email("member")
    await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=member_email,
        first_name="Member",
        last_name="User",
        password="member-pass-12345",
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "Twice")
    payload = {"email": member_email, "role": "member"}

    first = await client.post(f"/api/v1/teams/{team_id}/invitations", json=payload)
    assert first.status_code == 201
    assert first.json()["status"] == "accepted"

    second = await client.post(f"/api/v1/teams/{team_id}/invitations", json=payload)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["message"] == membership_service.ALREADY_A_MEMBER


@pytest.mark.asyncio
async def test_accepting_when_already_a_member_conflicts(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch_email_send
):
    """A recipient added to the team while the invite was pending gets a 409.

    Accept calls the same `add_member`, so an invitation outliving a
    directly-granted membership used to fail on the recipient's own click.
    """
    from alkera_core.models import TeamRole
    from backend.services.org import memberships as membership_service
    from tests.conftest import _unique_email, make_member

    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = await _create_subteam(client, "AlreadyIn")
    invitee_email = _unique_email("invitee")
    create = await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "member"},
    )
    assert create.status_code == 201
    invitation_id = create.json()["id"]
    await client.post("/api/v1/auth/logout")

    invitee, invitee_pw = await make_member(
        real_session, org_id=org_admin.org_id, email=invitee_email, verified=True
    )
    # The admin grants the seat directly while the invitation is still pending.
    await membership_service.add_member(
        real_session, team_id=UUID(team_id), user_id=invitee.id, role=TeamRole.MEMBER
    )
    await real_session.commit()

    await login(client, invitee.email, invitee_pw or "")
    resp = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["message"] == membership_service.ALREADY_A_MEMBER
