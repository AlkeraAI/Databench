"""The owner back-fill the authorization migration runs, against real Postgres.

Two layers. The back-fill SQL itself is idempotent, so it is driven directly on
orgs whose owner row was removed to model the pre-migration state: which admin
becomes the owner, who gets none, and that a second run changes nothing. Then
the real migration is downgraded to its parent and upgraded again on the lane
database, so "an org that predates the table gets its owner on upgrade" and
"the downgrade drops both tables cleanly" are proven by Alembic, not by a
re-implementation of it.

The migration file is located by its slug rather than a spelled revision, so
renumbering it moves this test with it; the revision it declares is read back
from the file name it was found under.
"""

from __future__ import annotations

import importlib.util
import secrets
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
from alembic.config import Config
from alkera_core.authz import PrincipalKind, Role, ScopeKind
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import RoleAssignment, Team, TeamMembership, TeamRole, User
from backend.services.identity import users as user_service
from backend.services.org import teams as team_service
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = next(iter(sorted((_BACKEND / "alembic" / "versions").glob("*_authz_foundations.py"))))
#: The revision number the file is filed under; the module must agree with it.
_REVISION = _MIGRATION.name.split("_", 1)[0]


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("authz_foundations_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic_config() -> Config:
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url_sync)
    return config


async def _org(session: AsyncSession) -> tuple[Team, User]:
    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=f"Backfill Org {secrets.token_hex(4)}",
        admin_email=f"backfill-admin-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Backfill",
        admin_last_name="Admin",
        admin_password="admin-pass-12345",
    )
    await session.commit()
    return org, admin


async def _root_admin(session: AsyncSession, org: Team) -> User:
    """A second admin of the root, added AFTER the founder (a later membership row)."""
    user = await user_service.create_user(
        session,
        org_team_id=org.id,
        email=f"backfill-second-{secrets.token_hex(6)}@alkera.dev",
        first_name="Second",
        last_name="Admin",
        password="admin-pass-12345",
    )
    session.add(TeamMembership(user_id=user.id, team_id=org.id, role=TeamRole.ADMIN))
    await session.commit()
    return user


async def _forget_owner(session: AsyncSession, org_id: UUID) -> None:
    """Model the pre-migration state: the org exists, its owner row does not."""
    await session.execute(delete(RoleAssignment).where(RoleAssignment.org_team_id == org_id))
    await session.commit()


async def _deactivate(session: AsyncSession, user: User) -> None:
    user.is_active = False
    await session.commit()


async def _owners(session: AsyncSession, org_id: UUID) -> list[RoleAssignment]:
    rows = await session.execute(
        select(RoleAssignment)
        .where(
            RoleAssignment.org_team_id == org_id,
            RoleAssignment.role == Role.OWNER,
            RoleAssignment.revoked_at.is_(None),
        )
        .order_by(RoleAssignment.created_at)
    )
    return list(rows.scalars().all())


async def _run_backfill(session: AsyncSession) -> None:
    await session.execute(text(_load_migration().OWNER_BACKFILL_SQL))
    await session.commit()


# --------------------------------------------------------------------------- #
# the SQL
# --------------------------------------------------------------------------- #


async def test_the_migration_module_names_its_parent_and_head() -> None:
    module = _load_migration()
    assert module.revision == _REVISION
    assert module.down_revision is not None
    assert module.down_revision < module.revision


async def test_the_oldest_active_root_admin_becomes_the_owner() -> None:
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        second = await _root_admin(session, org)
        await _forget_owner(session, org.id)
        await _run_backfill(session)

        owners = await _owners(session, org.id)
        assert [o.principal_id for o in owners] == [founder.id]
        (owner,) = owners
        assert owner.principal_kind is PrincipalKind.USER
        assert owner.scope_kind is ScopeKind.ORG
        assert owner.scope_id == org.id
        assert owner.granted_by_id is None
        assert owner.principal_id != second.id


async def test_a_deactivated_founder_yields_the_next_active_admin() -> None:
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        second = await _root_admin(session, org)
        await _forget_owner(session, org.id)
        await _deactivate(session, founder)
        await _run_backfill(session)

        owners = await _owners(session, org.id)
        assert [o.principal_id for o in owners] == [second.id]


async def test_an_org_with_no_active_root_admin_gets_no_owner() -> None:
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        await _forget_owner(session, org.id)
        await _deactivate(session, founder)
        await _run_backfill(session)
        assert await _owners(session, org.id) == []


async def test_a_member_of_the_root_is_never_picked() -> None:
    """Only an ADMIN membership of the root qualifies — a plain member row on the
    root (every org member has one by chain materialisation) does not."""
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        member = await user_service.create_user(
            session,
            org_team_id=org.id,
            email=f"backfill-member-{secrets.token_hex(6)}@alkera.dev",
            first_name="Plain",
            last_name="Member",
            password="member-pass-12345",
        )
        session.add(TeamMembership(user_id=member.id, team_id=org.id, role=TeamRole.MEMBER))
        await session.commit()
        await _forget_owner(session, org.id)
        await _deactivate(session, founder)
        await _run_backfill(session)
        assert await _owners(session, org.id) == []


async def test_an_admin_of_a_sub_team_only_is_never_picked() -> None:
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        sub = await team_service.create_subteam(
            session, org_team_id=org.id, name="Sub", parent_team_id=org.id
        )
        await session.commit()
        sub_admin = await user_service.create_user(
            session,
            org_team_id=org.id,
            email=f"backfill-subadmin-{secrets.token_hex(6)}@alkera.dev",
            first_name="Sub",
            last_name="Admin",
            password="admin-pass-12345",
        )
        session.add(TeamMembership(user_id=sub_admin.id, team_id=sub.id, role=TeamRole.ADMIN))
        await session.commit()
        await _forget_owner(session, org.id)
        await _deactivate(session, founder)
        await _run_backfill(session)
        assert await _owners(session, org.id) == []


async def test_an_org_that_already_has_its_owner_keeps_exactly_one() -> None:
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        before = await _owners(session, org.id)
        assert [o.principal_id for o in before] == [founder.id]
        await _run_backfill(session)
        after = await _owners(session, org.id)
        assert [(o.id, o.principal_id) for o in after] == [(before[0].id, founder.id)]


async def test_running_the_backfill_twice_adds_nothing() -> None:
    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        await _forget_owner(session, org.id)
        await _run_backfill(session)
        first = await _owners(session, org.id)
        await _run_backfill(session)
        second = await _owners(session, org.id)
        assert [o.id for o in first] == [o.id for o in second]
        assert [o.principal_id for o in second] == [founder.id]


async def test_a_revoked_owner_row_is_not_resurrected_but_a_fresh_one_is_minted() -> None:
    """The conflict target is the LIVE index: an org whose owner grant was
    revoked has no live owner, so the back-fill mints a new row beside the
    revoked one rather than reviving it."""
    from datetime import UTC, datetime

    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        (owner,) = await _owners(session, org.id)
        owner.revoked_at = datetime.now(UTC)
        await session.commit()
        revoked_id = owner.id
        await _run_backfill(session)
        live = await _owners(session, org.id)
        assert len(live) == 1
        assert live[0].id != revoked_id
        assert live[0].principal_id == founder.id
        still_revoked = (
            await session.execute(select(RoleAssignment).where(RoleAssignment.id == revoked_id))
        ).scalar_one()
        assert still_revoked.revoked_at is not None


# --------------------------------------------------------------------------- #
# the real migration, down and up again
# --------------------------------------------------------------------------- #


async def _regclass(session: AsyncSession, table: str) -> Any:
    return (
        await session.execute(text("SELECT to_regclass(:table)"), {"table": table})
    ).scalar_one()


async def test_downgrade_drops_both_tables_and_upgrade_backfills_a_pre_existing_org() -> None:
    """An org created BEFORE the migration (its owner row gone with the table on
    downgrade) has its owner after the upgrade — the production upgrade path,
    driven by Alembic itself, against a copy of the worker's database."""
    module = _load_migration()
    async with migration_scratch() as db:
        async with db.session() as session:
            org, founder = await _org(session)
            second = await _root_admin(session, org)
            org_id, founder_id, second_id = org.id, founder.id, second.id

        await db.downgrade(module.down_revision)
        async with db.session() as session:
            assert await _regclass(session, "role_assignments") is None
            assert await _regclass(session, "personal_access_tokens") is None
            assert (
                await session.execute(select(Team.id).where(Team.id == org_id))
            ).scalar_one() == org_id

        await db.upgrade()
        async with db.session() as session:
            assert await _regclass(session, "role_assignments") is not None
            assert await _regclass(session, "personal_access_tokens") is not None
            owners = await _owners(session, org_id)
            assert [o.principal_id for o in owners] == [founder_id]
            assert second_id not in {o.principal_id for o in owners}


async def test_a_demoted_founders_revoked_owner_row_stays_revoked_across_the_backfill() -> None:
    """A demotion revokes the founder's owner row. The back-fill keys on the
    LIVE index and on an ADMIN membership, so it neither resurrects that row
    nor mints the demoted founder a new one: the oldest remaining root admin
    is picked instead, and a second run changes nothing."""
    from backend.services.org import memberships as membership_service

    async with AsyncSessionLocal() as session:
        org, founder = await _org(session)
        second = await _root_admin(session, org)
        founder_row = (
            await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == org.id, TeamMembership.user_id == founder.id
                )
            )
        ).scalar_one()
        await membership_service.change_role(session, founder_row, TeamRole.MEMBER)
        await session.commit()
        assert await _owners(session, org.id) == []
        revoked_id = (
            await session.execute(
                select(RoleAssignment.id).where(
                    RoleAssignment.org_team_id == org.id, RoleAssignment.role == Role.OWNER
                )
            )
        ).scalar_one()

        await _run_backfill(session)
        first = await _owners(session, org.id)
        assert [o.principal_id for o in first] == [second.id]
        assert first[0].id != revoked_id
        await _run_backfill(session)
        second_run = await _owners(session, org.id)
        assert [o.id for o in second_run] == [o.id for o in first]
        still_revoked = (
            await session.execute(select(RoleAssignment).where(RoleAssignment.id == revoked_id))
        ).scalar_one()
        assert still_revoked.revoked_at is not None
        assert still_revoked.principal_id == founder.id
