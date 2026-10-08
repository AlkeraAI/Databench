"""Org memberships: the revision, the back-fill and the triggers that keep it.

``alembic check`` compares columns and types. It does not see a back-fill, a
trigger, a deferred foreign key or the inverse a downgrade must be, and this
revision is mostly those. So this module drives the revision itself on a
scratch copy (down, up, up again, down, up) against seeded rows, and the
triggers on the suite's own database at head, where every existing writer of
``users`` and ``team_memberships`` keeps working through them.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alkera_core.models import MembershipStatus, OrgMembership, Team, TeamMembership, User
from backend.services.org import org_memberships as org_membership_service
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch, script_head

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0185_org_memberships.py"
_PARENT = "0184"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


def _email(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}@alkera.dev"


async def _org(session: AsyncSession, *, child: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    """A root team and (optionally) a child team under it, in raw SQL so the
    rows are what the back-fill reads regardless of the model's shape."""
    root = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
        {"id": root, "name": f"org-{root.hex[:8]}"},
    )
    sub = uuid.uuid4()
    if child:
        await session.execute(
            text(
                "INSERT INTO teams (id, name, is_root, parent_team_id) "
                "VALUES (:id, :name, false, :parent)"
            ),
            {"id": sub, "name": f"team-{sub.hex[:8]}", "parent": root},
        )
    return root, sub


async def _user(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    active: bool = True,
    sso_exempt: bool = False,
    scim: str | None = None,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, org_team_id, email, is_active, sso_exempt, scim_external_id) "
            "VALUES (:id, :org, :email, :active, :exempt, :scim)"
        ),
        {
            "id": user_id,
            "org": org,
            "email": _email("mig"),
            "active": active,
            "exempt": sso_exempt,
            "scim": scim,
        },
    )
    return user_id


async def _membership_row(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT org_team_id, status, credential_epoch, sso_exempt, scim_external_id, "
                    "deactivated_at FROM org_memberships WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1, rows
    return dict(rows[0])


# --- the revision, on a scratch copy --------------------------------------------


async def test_the_revision_back_fills_memberships_and_survives_a_round_trip() -> None:
    assert _MIGRATION.is_file()
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert await _scalar(session, "SELECT to_regclass('org_memberships')") is None
            org, team = await _org(session, child=True)
            active = await _user(session, org, sso_exempt=True, scim="idp-42")
            gone = await _user(session, org, active=False)
            for user_id, team_id in ((active, org), (active, team), (gone, org)):
                await session.execute(
                    text(
                        "INSERT INTO team_memberships (id, user_id, team_id, role) "
                        "VALUES (:id, :u, :t, 'member')"
                    ),
                    {"id": uuid.uuid4(), "u": user_id, "t": team_id},
                )
            await session.commit()

        await scratch.upgrade()
        async with scratch.session() as session:
            kept = await _membership_row(session, active)
            assert kept["org_team_id"] == org
            assert kept["status"] == "active"
            assert kept["credential_epoch"] == 0
            assert kept["sso_exempt"] is True
            assert kept["scim_external_id"] == "idp-42"
            assert kept["deactivated_at"] is None
            off = await _membership_row(session, gone)
            assert off["status"] == "deactivated"
            assert off["deactivated_at"] is not None
            orgs = (
                await session.execute(
                    text("SELECT team_id, org_team_id FROM team_memberships WHERE user_id = :u"),
                    {"u": active},
                )
            ).all()
            assert {tuple(row) for row in orgs} == {(org, org), (team, org)}

        # A run that died after committing part of itself runs again cleanly.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade()
        async with scratch.session() as session:
            assert (
                await _scalar(
                    session, "SELECT count(*) FROM org_memberships WHERE user_id = :u", u=active
                )
                == 1
            )

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert await _scalar(session, "SELECT to_regclass('org_memberships')") is None
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'team_memberships' AND column_name = 'org_team_id'",
                )
                == 0
            )
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM pg_trigger WHERE tgname IN ("
                    "'trg_team_memberships_org', 'trg_users_home_membership', "
                    "'trg_users_mirror_active', 'trg_users_home_org_immutable')",
                )
                == 0
            )
            assert await _scalar(session, "SELECT to_regproc('team_root_of')") is None
        await scratch.upgrade()
        assert scratch.revision() == script_head(scratch.config)
        async with scratch.session() as session:
            assert (await _membership_row(session, active))["status"] == "active"


async def test_a_downgrade_clears_what_the_previous_schema_would_misread() -> None:
    """The code before this revision reads a person's org off their user row.
    So a downgrade revokes a credential minted for a second org (it would act
    in the home org) and removes a seat in a second org's team (it would fund
    the home org's spend), and leaves everything in the home org as it was."""
    async with migration_scratch() as scratch:
        async with scratch.session() as session:
            home, _ = await _org(session)
            second, second_team = await _org(session, child=True)
            person = await _user(session, home)
            await session.execute(
                text("INSERT INTO org_memberships (id, user_id, org_team_id) VALUES (:i, :u, :o)"),
                {"i": uuid.uuid4(), "u": person, "o": second},
            )
            for team in (home, second, second_team):
                await session.execute(
                    text(
                        "INSERT INTO team_memberships (id, user_id, team_id, role) "
                        "VALUES (:id, :u, :t, 'member')"
                    ),
                    {"id": uuid.uuid4(), "u": person, "t": team},
                )
            tokens = {"home": uuid.uuid4().hex, "second": uuid.uuid4().hex}
            for key, org in (("home", home), ("second", second)):
                await session.execute(
                    text(
                        "INSERT INTO auth_tokens (id, jti, user_id, token_type, issued_at, "
                        "expires_at, org_team_id) VALUES (:id, :jti, :u, 'cli', now(), "
                        "now() + interval '1 day', :o)"
                    ),
                    {"id": uuid.uuid4(), "jti": tokens[key], "u": person, "o": org},
                )
            families = {"home": uuid.uuid4(), "second": uuid.uuid4()}
            for key, org in (("home", home), ("second", second)):
                await session.execute(
                    text(
                        "INSERT INTO auth_refresh_tokens (id, family_id, user_id, token_hash, "
                        "family_started_at, created_at, idle_expires_at, absolute_expires_at, "
                        "active_org_team_id) VALUES (:id, :f, :u, :h, now(), now(), "
                        "now() + interval '1 day', now() + interval '2 days', :o)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "f": families[key],
                        "u": person,
                        "h": secrets.token_hex(32),
                        "o": org,
                    },
                )
            await session.commit()

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            seats = (
                await session.execute(
                    text("SELECT team_id FROM team_memberships WHERE user_id = :u"), {"u": person}
                )
            ).scalars()
            assert set(seats) == {home}
            for key, revoked in (("home", False), ("second", True)):
                token_revoked = await _scalar(
                    session,
                    "SELECT revoked_at IS NOT NULL FROM auth_tokens WHERE jti = :j",
                    j=tokens[key],
                )
                family_revoked = await _scalar(
                    session,
                    "SELECT revoked_at IS NOT NULL FROM auth_refresh_tokens WHERE family_id = :f",
                    f=families[key],
                )
                assert (token_revoked, family_revoked) == (revoked, revoked), key


async def test_the_revision_refuses_a_user_seated_outside_their_home_org() -> None:
    """A team row that places a person in another org's team has no membership
    to hang on: the revision stops and says so rather than tightening over it."""
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            home, _ = await _org(session)
            foreign, _ = await _org(session)
            stray = await _user(session, home)
            await session.execute(
                text(
                    "INSERT INTO team_memberships (id, user_id, team_id, role) "
                    "VALUES (:id, :u, :t, 'member')"
                ),
                {"id": uuid.uuid4(), "u": stray, "t": foreign},
            )
            await session.commit()
        with pytest.raises(RuntimeError, match="not their home org"):
            await scratch.upgrade()


# --- the triggers and constraints, at head ---------------------------------------


async def _team(session: AsyncSession, *, parent: uuid.UUID | None = None) -> Team:
    team = Team(name=f"t-{secrets.token_hex(4)}", is_root=parent is None, parent_team_id=parent)
    session.add(team)
    await session.flush()
    return team


async def _new_user(session: AsyncSession, org_id: uuid.UUID, **kw: Any) -> User:
    user = User(home_org_team_id=org_id, email=_email("trg"), **kw)
    session.add(user)
    await session.flush()
    return user


async def _membership(
    session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID
) -> OrgMembership:
    row = await session.scalar(
        select(OrgMembership)
        .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row


async def test_a_new_user_gets_their_home_membership(real_session: AsyncSession) -> None:
    org = await _team(real_session)
    user = await _new_user(real_session, org.id, sso_exempt=True, scim_external_id="ext-1")
    off = await _new_user(real_session, org.id, is_active=False)
    await real_session.commit()
    home = await _membership(real_session, user.id, org.id)
    assert (home.status, home.credential_epoch, home.sso_exempt, home.scim_external_id) == (
        MembershipStatus.ACTIVE,
        0,
        True,
        "ext-1",
    )
    assert (await _membership(real_session, off.id, org.id)).status is MembershipStatus.DEACTIVATED


async def test_a_users_home_org_cannot_move(real_session: AsyncSession) -> None:
    org = await _team(real_session)
    other = await _team(real_session)
    user = await _new_user(real_session, org.id)
    await real_session.commit()
    with pytest.raises(DBAPIError, match="immutable"):
        await real_session.execute(
            text("UPDATE users SET org_team_id = :o WHERE id = :u"), {"o": other.id, "u": user.id}
        )
    await real_session.rollback()


async def test_a_team_membership_is_filled_with_its_teams_org(real_session: AsyncSession) -> None:
    org = await _team(real_session)
    team = await _team(real_session, parent=org.id)
    grandchild = await _team(real_session, parent=team.id)
    user = await _new_user(real_session, org.id)
    rows = [TeamMembership(user_id=user.id, team_id=team_id) for team_id in (org.id, grandchild.id)]
    real_session.add_all(rows)
    await real_session.commit()
    assert [row.org_team_id for row in rows] == [org.id, org.id]


async def test_a_team_membership_naming_the_wrong_org_is_refused(
    real_session: AsyncSession,
) -> None:
    org = await _team(real_session)
    other = await _team(real_session)
    user = await _new_user(real_session, org.id)
    await real_session.commit()
    real_session.add(TeamMembership(user_id=user.id, team_id=org.id, org_team_id=other.id))
    with pytest.raises(DBAPIError, match="not the root"):
        await real_session.flush()
    await real_session.rollback()


async def test_a_team_membership_without_an_org_membership_fails_at_commit(
    real_session: AsyncSession,
) -> None:
    """The key is deferred: the row is accepted into the transaction (a writer
    may add the org membership after it) and refused at commit."""
    home = await _team(real_session)
    foreign = await _team(real_session)
    user = await _new_user(real_session, home.id)
    await real_session.commit()
    real_session.add(TeamMembership(user_id=user.id, team_id=foreign.id))
    await real_session.flush()
    with pytest.raises(IntegrityError, match="fk_team_memberships_org_membership"):
        await real_session.commit()
    await real_session.rollback()


async def test_an_org_membership_added_in_the_same_transaction_satisfies_the_key(
    real_session: AsyncSession,
) -> None:
    home = await _team(real_session)
    second = await _team(real_session)
    user = await _new_user(real_session, home.id)
    await real_session.commit()
    real_session.add(TeamMembership(user_id=user.id, team_id=second.id))
    await real_session.flush()
    await org_membership_service.create(real_session, user_id=user.id, org_team_id=second.id)
    await real_session.commit()


async def test_removing_an_org_membership_removes_the_teams_in_that_org(
    real_session: AsyncSession,
) -> None:
    home = await _team(real_session)
    second = await _team(real_session)
    second_team = await _team(real_session, parent=second.id)
    user = await _new_user(real_session, home.id)
    await org_membership_service.create(real_session, user_id=user.id, org_team_id=second.id)
    real_session.add_all(
        [
            TeamMembership(user_id=user.id, team_id=home.id),
            TeamMembership(user_id=user.id, team_id=second.id),
            TeamMembership(user_id=user.id, team_id=second_team.id),
        ]
    )
    await real_session.commit()
    membership = await _membership(real_session, user.id, second.id)
    await real_session.delete(membership)
    await real_session.commit()
    left = await real_session.scalar(
        select(func.count()).select_from(TeamMembership).where(TeamMembership.user_id == user.id)
    )
    assert left == 1


@pytest.mark.parametrize("kind", ["missing", "child"])
async def test_a_membership_is_only_ever_in_an_org(real_session: AsyncSession, kind: str) -> None:
    org = await _team(real_session)
    user = await _new_user(real_session, org.id)
    target = uuid.uuid4() if kind == "missing" else (await _team(real_session, parent=org.id)).id
    with pytest.raises(org_membership_service.NotAnOrgError):
        await org_membership_service.create(real_session, user_id=user.id, org_team_id=target)
    await real_session.rollback()


async def test_the_membership_service_reads_and_bumps(real_session: AsyncSession) -> None:
    org = await _team(real_session)
    second = await _team(real_session)
    user = await _new_user(real_session, org.id)
    off = await org_membership_service.create(
        real_session,
        user_id=user.id,
        org_team_id=second.id,
        status=MembershipStatus.DEACTIVATED,
    )
    await real_session.commit()
    assert await org_membership_service.get(real_session, user_id=user.id, org_team_id=second.id)
    assert (
        await org_membership_service.active(real_session, user_id=user.id, org_team_id=second.id)
        is None
    )
    listed = await org_membership_service.list_active_for_user(real_session, user.id)
    assert [m.org_team_id for m in listed] == [org.id]
    first = org_membership_service.FIRST_MEMBERSHIP_EPOCH
    assert off.credential_epoch == first
    assert await org_membership_service.bump_epoch(real_session, off.id) == first + 1
    assert await org_membership_service.bump_epoch(real_session, off.id) == first + 2
    await real_session.commit()
