"""One person, two orgs: the reads that still took "the person's org" off the person.

The subject is the suite's two-org identity: a person in org A (their home) and
org B, each org with its own admin. Each case pins a place that used to answer
for B with A's facts, or for A with facts that ended in A:

* a personal access token is minted in the org the person holds a membership
  of, whichever org is their home;
* verifying an address files a row in every org the person belongs to, and the
  row in B names B, never A;
* a box an operator registered in A stops taking A's chats once their
  membership of A is deactivated, even though their admin seat survives the
  deactivation and they still stand in B;
* the sessions list and a session revoke stay inside the request's org;
* the teams ``/me`` says the person administers are the request's org's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import decode_session_token
from alkera_core.compute.machines import stands_behind_its_org
from alkera_core.events.types import EventType
from alkera_core.models import (
    AuthToken,
    EventOutbox,
    PersonalAccessToken,
    Team,
    TeamMembership,
    TokenType,
)
from alkera_core.models._enums import TeamRole
from alkera_core.models.compute import ORG_TENANCY, ComputeAllocation
from backend.services.credentials import pats as pat_service
from backend.services.identity import email_verification as email_verification_service
from backend.services.org import org_memberships as org_membership_service
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import TwoOrg, app_client

pytestmark = [pytest.mark.asyncio]


# --- personal access tokens --------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_a_pat_is_minted_in_the_org_the_person_belongs_to_not_only_their_home(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    w = two_org_identity
    token, raw = await pat_service.mint(
        real_session, org_id=w.org_b, user_id=w.user.id, label="ci in b"
    )
    await real_session.commit()

    assert token.org_team_id == w.org_b
    assert token.membership_id == w.membership_b.id
    resolved = await pat_service.resolve_active(real_session, raw)
    assert resolved is not None and resolved.token.org_team_id == w.org_b


@pytest.mark.usefixtures("multi_org")
async def test_a_pat_is_refused_in_an_org_the_person_left_whatever_their_home(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    w = two_org_identity
    membership = await org_membership_service.get(
        real_session, user_id=w.user.id, org_team_id=w.org_b
    )
    assert membership is not None
    await org_membership_service.deactivate(real_session, membership, actor=None)
    await real_session.commit()

    with pytest.raises(ValueError, match="member of the org"):
        await pat_service.mint(real_session, org_id=w.org_b, user_id=w.user.id, label=None)
    with pytest.raises(ValueError, match="member of the org"):
        await pat_service.mint(real_session, org_id=uuid4(), user_id=w.user.id, label=None)
    rows = (
        await real_session.execute(
            select(PersonalAccessToken.id).where(PersonalAccessToken.user_id == w.user.id)
        )
    ).all()
    assert rows == []


# --- email verification ------------------------------------------------------


async def test_verifying_an_address_files_a_row_in_each_org_naming_that_org(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    """The address is the identity's, so each org's stream hears it; but the
    actor on B's row is the person acting in B. Recording their home org there
    would hand B the id of every other org the person belongs to."""
    w = two_org_identity
    w.user.email_verified_at = None
    raw = await email_verification_service.issue_token(real_session, w.user)
    await real_session.commit()

    await email_verification_service.consume_token(real_session, raw)
    await real_session.commit()

    rows = (
        await real_session.execute(
            select(EventOutbox).where(
                EventOutbox.type == EventType.USER_EMAIL_VERIFIED.value,
                EventOutbox.entity_id == str(w.user.id),
            )
        )
    ).scalars()
    by_org = {row.org_id: row for row in rows}
    assert set(by_org) == {w.org_a, w.org_b}
    for org_id, other in ((w.org_a, w.org_b), (w.org_b, w.org_a)):
        actor = by_org[org_id].actor
        assert actor["acting"]["id"] == str(w.user.id)
        assert actor["acting"]["org_id"] == str(org_id)
        assert str(other) not in str(actor)


# --- a box's standing ----------------------------------------------------------


async def _admin_of_root(session: AsyncSession, *, user_id: UUID, org_id: UUID) -> None:
    await session.execute(
        update(TeamMembership)
        .where(TeamMembership.user_id == user_id, TeamMembership.team_id == org_id)
        .values(role=TeamRole.ADMIN)
    )
    await session.commit()


async def _org_box(session: AsyncSession, *, org_id: UUID, user_id: UUID) -> ComputeAllocation:
    machine_type = await make_machine_type(session)
    now = datetime.now(UTC)
    alloc = ComputeAllocation(
        user_id=user_id,
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        origin="registered",
        name=f"box-{uuid4().hex[:6]}",
        tenancy=ORG_TENANCY,
        sandbox="none",
        state="ready",
        provider_machine_id=f"pod-{uuid4().hex[:6]}",
        created_at=now,
        ready_at=now,
        last_metered_at=now,
        last_heartbeat_at=now,
        capacity=10,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=1,
    )
    session.add(alloc)
    await session.commit()
    return alloc


async def _stands(session: AsyncSession, box_id: UUID) -> bool:
    found = await session.execute(
        select(ComputeAllocation.id).where(ComputeAllocation.id == box_id, stands_behind_its_org())
    )
    return found.scalar_one_or_none() is not None


async def test_a_box_stops_standing_when_its_operators_membership_of_that_org_ends(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    """Deactivation keeps the person's seats (a reactivation restores them),
    so an admin seat alone is no proof anyone still stands behind A's box.
    Their standing in B keeps B's box serving and says nothing about A's."""
    w = two_org_identity
    user_id, org_a, org_b = w.user.id, w.org_a, w.org_b
    for org_id in (org_a, org_b):
        await _admin_of_root(real_session, user_id=user_id, org_id=org_id)
    box_a = (await _org_box(real_session, org_id=org_a, user_id=user_id)).id
    box_b = (await _org_box(real_session, org_id=org_b, user_id=user_id)).id
    assert await _stands(real_session, box_a) and await _stands(real_session, box_b)

    membership = await org_membership_service.get(real_session, user_id=user_id, org_team_id=org_a)
    assert membership is not None
    await org_membership_service.deactivate(real_session, membership, actor=None)
    await real_session.commit()
    seat = await real_session.scalar(
        select(TeamMembership.role).where(
            TeamMembership.user_id == user_id, TeamMembership.team_id == org_a
        )
    )
    assert seat == TeamRole.ADMIN, "the seat survives; only the membership ended"

    assert not await _stands(real_session, box_a)
    assert await _stands(real_session, box_b)

    membership = await org_membership_service.get(real_session, user_id=user_id, org_team_id=org_a)
    assert membership is not None
    await org_membership_service.reactivate(real_session, membership, actor=None)
    await real_session.commit()
    assert await _stands(real_session, box_a)


# --- sessions --------------------------------------------------------------------


async def _sessions(token: str) -> set[str]:
    async with app_client(headers={"Authorization": f"Bearer {token}"}) as client:
        listed = await client.get("/api/v1/auth/sessions")
    assert listed.status_code == 200, listed.text
    return {row["jti"] for row in listed.json()["sessions"]}


@pytest.mark.usefixtures("multi_org")
async def test_each_orgs_sessions_list_holds_that_orgs_tokens_and_legacy_ones(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    w = two_org_identity
    jti_a = decode_session_token(w.token_a).jti
    jti_b = decode_session_token(w.token_b).jti
    legacy = uuid4().hex
    real_session.add(
        AuthToken(
            jti=legacy,
            user_id=w.user.id,
            token_type=TokenType.CLI,
            issued_at=datetime.now(UTC),
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )
    await real_session.commit()

    assert await _sessions(w.token_a) == {jti_a, legacy}
    assert await _sessions(w.token_b) == {jti_b, legacy}


@pytest.mark.usefixtures("multi_org")
async def test_a_session_is_revoked_only_from_its_own_org(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    w = two_org_identity
    jti_a = decode_session_token(w.token_a).jti
    jti_b = decode_session_token(w.token_b).jti
    async with app_client(headers={"Authorization": f"Bearer {w.token_b}"}) as from_b:
        crossed = await from_b.delete(f"/api/v1/auth/sessions/{jti_a}")
        own = await from_b.delete(f"/api/v1/auth/sessions/{jti_b}")
    assert crossed.status_code == 404
    assert own.status_code == 200

    revoked = dict(
        (
            await real_session.execute(
                select(AuthToken.jti, AuthToken.revoked_at).where(AuthToken.jti.in_([jti_a, jti_b]))
            )
        ).all()
    )
    assert revoked[jti_a] is None, "A's token stands"
    assert revoked[jti_b] is not None


# --- the teams a person administers ----------------------------------------------


async def _admin_teams(token: str) -> set[str]:
    async with app_client(headers={"Authorization": f"Bearer {token}"}) as client:
        me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    return set(me.json()["admin_team_ids"])


@pytest.mark.usefixtures("multi_org")
async def test_me_names_only_the_teams_administered_in_the_credentials_org(
    two_org_identity: TwoOrg, real_session: AsyncSession
) -> None:
    """The person administers A's root (so every team under it, by descent)
    and one sub-team of B. Each credential's answer is its own org's set: a
    team picker on B must not offer A's teams."""
    w = two_org_identity
    user_id, org_a, org_b = w.user.id, w.org_a, w.org_b
    await _admin_of_root(real_session, user_id=user_id, org_id=org_a)
    a_sub = Team(name=f"a-sub-{uuid4().hex[:6]}", parent_team_id=org_a)
    b_sub = Team(name=f"b-sub-{uuid4().hex[:6]}", parent_team_id=org_b)
    real_session.add_all([a_sub, b_sub])
    await real_session.flush()
    a_sub_id, b_sub_id = a_sub.id, b_sub.id
    real_session.add(TeamMembership(user_id=user_id, team_id=b_sub_id, role=TeamRole.ADMIN))
    await real_session.commit()

    assert await _admin_teams(w.token_b) == {str(b_sub_id)}
    assert await _admin_teams(w.token_a) == {str(org_a), str(a_sub_id)}
