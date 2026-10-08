"""The revision that moves lifecycle, SSO and audit onto the membership.

``alembic check`` sees the new column, constraint and table; it does not see
a dropped trigger, a replaced trigger function, the back-port of drifted rows
or the inverse a downgrade must be. So this module drives the revision on a
scratch copy (down, up, up again, down, up) against seeded rows, and pins the
two trigger contracts on the suite's own database at head.
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alkera_core.auth.tenancy import HOME_REPOINT_SETTING
from alkera_core.models import MembershipStatus, OrgMembership, Team, User
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch, script_head

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0186_membership_lifecycle.py"
_PARENT = "0185"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _org(session: AsyncSession) -> uuid.UUID:
    org = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
        {"id": org, "name": f"org-{org.hex[:8]}"},
    )
    return org


async def _user(session: AsyncSession, org: uuid.UUID, **columns: Any) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_team_id, email) VALUES (:id, :org, :email)"),
        {"id": user_id, "org": org, "email": f"mig-{secrets.token_hex(6)}@alkera.dev"},
    )
    if columns:
        sets = ", ".join(f"{key} = :{key}" for key in columns)
        await session.execute(
            text(f"UPDATE users SET {sets} WHERE id = :id"), {"id": user_id, **columns}
        )
    return user_id


async def _home(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    row = (
        (
            await session.execute(
                text(
                    "SELECT om.status, om.credential_epoch, om.sso_exempt, om.scim_external_id "
                    "FROM org_memberships om JOIN users u "
                    "ON u.id = om.user_id AND u.org_team_id = om.org_team_id WHERE u.id = :u"
                ),
                {"u": user_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def _trigger_exists(session: AsyncSession, name: str) -> bool:
    return bool(await _scalar(session, "SELECT count(*) FROM pg_trigger WHERE tgname = :n", n=name))


async def test_the_revision_round_trips_and_back_ports_what_drifted() -> None:
    assert _MIGRATION.is_file()
    async with migration_scratch() as scratch:
        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            org = await _org(session)
            # Disabled on the user row while the home membership stayed active:
            # what the mirror kept from happening, made to happen anyway.
            drifted = await _user(session, org)
            await session.execute(text("ALTER TABLE users DISABLE TRIGGER trg_users_mirror_active"))
            await session.execute(
                text("UPDATE users SET is_active = false WHERE id = :u"), {"u": drifted}
            )
            await session.execute(text("ALTER TABLE users ENABLE TRIGGER trg_users_mirror_active"))
            # Break-glass and SCIM id written on the user row after the back-fill.
            flagged = await _user(session, org)
            await session.execute(
                text(
                    "UPDATE users SET sso_exempt = true, scim_external_id = 'okta-9' WHERE id = :u"
                ),
                {"u": flagged},
            )
            steady = await _user(session, org)
            await session.commit()
            assert (await _home(session, drifted))["status"] == "active"
            assert (await _home(session, flagged))["sso_exempt"] is False

        await scratch.upgrade()
        async with scratch.session() as session:
            back_ported = await _home(session, drifted)
            assert back_ported["status"] == "deactivated"
            assert back_ported["credential_epoch"] == 1
            copied = await _home(session, flagged)
            assert (copied["sso_exempt"], copied["scim_external_id"]) == (True, "okta-9")
            assert (await _home(session, steady))["credential_epoch"] == 0
            assert not await _trigger_exists(session, "trg_users_mirror_active")
            assert await _trigger_exists(session, "trg_users_home_org_immutable")
            assert await _scalar(
                session,
                "SELECT col_description('users'::regclass, "
                "(SELECT attnum FROM pg_attribute WHERE attrelid = 'users'::regclass "
                "AND attname = 'is_active'))",
            )
            assert (
                await _scalar(
                    session,
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'sso_connections' "
                    "AND column_name = 'session_max_age_seconds'",
                )
                == "86400"
            )
            assert await _scalar(session, "SELECT to_regclass('identity_security_events')")

        # A run that died after committing part of itself runs again cleanly,
        # and does not bump an epoch twice.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade()
        async with scratch.session() as session:
            assert (await _home(session, drifted))["credential_epoch"] == 1

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert await _trigger_exists(session, "trg_users_mirror_active")
            assert await _scalar(session, "SELECT to_regclass('identity_security_events')") is None
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'sso_connections' "
                    "AND column_name = 'session_max_age_seconds'",
                )
                == 0
            )
            # The code before this revision deactivates and reads the flag on
            # the user row: what the membership says is copied back there.
            assert (
                await _scalar(session, "SELECT is_active FROM users WHERE id = :u", u=drifted)
                is False
            )
            assert (
                await _scalar(session, "SELECT sso_exempt FROM users WHERE id = :u", u=flagged)
                is True
            )
            # The restored mirror works again.
            await session.execute(
                text("UPDATE users SET is_active = false WHERE id = :u"), {"u": steady}
            )
            await session.commit()
            assert (await _home(session, steady))["status"] == "deactivated"
            with pytest.raises(DBAPIError, match="immutable"):
                await session.execute(
                    text("SELECT set_config(:name, 'on', true)"), {"name": HOME_REPOINT_SETTING}
                )
                await session.execute(
                    text("UPDATE users SET org_team_id = :o WHERE id = :u"),
                    {"o": await _org(session), "u": steady},
                )
            await session.rollback()

        await scratch.upgrade()
        assert scratch.revision() == script_head(scratch.config)


# --- the triggers, at head on the suite's database ---------------------------------


async def _team(session: AsyncSession) -> Team:
    team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
    session.add(team)
    await session.flush()
    return team


async def test_disabling_an_identity_leaves_its_memberships_as_they_are(
    real_session: AsyncSession,
) -> None:
    """The platform disable is the identity's; what each org decided about the
    person stays on their membership, so re-enabling restores exactly that."""
    org = await _team(real_session)
    user = User(
        home_org_team_id=org.id,
        email=f"off-{secrets.token_hex(5)}@alkera.dev",
        first_name="O",
        last_name="F",
    )
    real_session.add(user)
    await real_session.commit()
    user.is_active = False
    await real_session.commit()
    membership = await real_session.scalar(
        select(OrgMembership)
        .where(OrgMembership.user_id == user.id)
        .execution_options(populate_existing=True)
    )
    assert membership is not None
    assert membership.status is MembershipStatus.ACTIVE
    assert membership.credential_epoch == 0


async def test_the_home_org_moves_only_inside_a_transaction_that_opened_the_guard(
    real_session: AsyncSession,
) -> None:
    org = await _team(real_session)
    other = await _team(real_session)
    user = User(
        home_org_team_id=org.id,
        email=f"mv-{secrets.token_hex(5)}@alkera.dev",
        first_name="M",
        last_name="V",
    )
    real_session.add(user)
    await real_session.commit()
    user_id, home, away = user.id, org.id, other.id
    with pytest.raises(DBAPIError, match="immutable"):
        await real_session.execute(
            update(User).where(User.id == user_id).values(home_org_team_id=away)
        )
    await real_session.rollback()

    await real_session.execute(
        text("SELECT set_config(:name, 'on', true)"), {"name": HOME_REPOINT_SETTING}
    )
    await real_session.execute(update(User).where(User.id == user_id).values(home_org_team_id=away))
    await real_session.commit()
    # SET LOCAL ended with the transaction that set it.
    with pytest.raises(DBAPIError, match="immutable"):
        await real_session.execute(
            update(User).where(User.id == user_id).values(home_org_team_id=home)
        )
    await real_session.rollback()
