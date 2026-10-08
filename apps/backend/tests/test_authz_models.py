"""The authorization tables as the migrated database actually has them.

``alembic check`` compares columns, named indexes and unique constraints, but
not CHECK constraints, partial-index predicates or ON DELETE actions — so this
file asks the live catalog and drives the constraints with raw inserts that
bypass the ORM's own enum coercion. Every name asserted here is one a later
migration may need to drop by.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import PrincipalKind, Role, ScopeKind
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import PersonalAccessToken, RoleAssignment, Team, User
from backend.services.identity import users as user_service
from backend.services.org import teams as team_service
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _org(session: AsyncSession) -> tuple[Team, User]:
    """A fresh org and its founding admin, committed."""
    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=f"Authz Org {secrets.token_hex(4)}",
        admin_email=f"authz-admin-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Authz",
        admin_last_name="Admin",
        admin_password="admin-pass-12345",
    )
    await session.commit()
    return org, admin


async def _user(session: AsyncSession, org_id: UUID) -> User:
    user = await user_service.create_user(
        session,
        org_team_id=org_id,
        email=f"authz-user-{secrets.token_hex(6)}@alkera.dev",
        first_name="Authz",
        last_name="User",
        password="user-pass-12345",
    )
    await session.commit()
    return user


def _assignment(org: Team, user: User, **overrides: Any) -> RoleAssignment:
    values: dict[str, Any] = {
        "org_team_id": org.id,
        "principal_kind": PrincipalKind.USER,
        "principal_id": user.id,
        "scope_kind": ScopeKind.TEAM,
        "scope_id": org.id,
        "role": Role.VIEWER,
        "granted_by_id": None,
    }
    values.update(overrides)
    return RoleAssignment(**values)


async def _live_owner_rows(session: AsyncSession, org_id: UUID) -> list[RoleAssignment]:
    rows = await session.execute(
        select(RoleAssignment).where(
            RoleAssignment.org_team_id == org_id,
            RoleAssignment.role == Role.OWNER,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# role_assignments
# --------------------------------------------------------------------------- #


async def test_a_role_assignment_round_trips_through_the_orm() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        member = await _user(session, org.id)
        row = _assignment(org, member, role=Role.VIEWER, granted_by_id=admin.id)
        session.add(row)
        await session.commit()
        row_id = row.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(RoleAssignment).where(RoleAssignment.id == row_id))
        ).scalar_one()
        assert loaded.org_team_id == org.id
        assert loaded.principal_kind is PrincipalKind.USER
        assert loaded.principal_id == member.id
        assert loaded.scope_kind is ScopeKind.TEAM
        assert loaded.scope_id == org.id
        assert loaded.role is Role.VIEWER
        assert loaded.granted_by_id == admin.id
        assert loaded.created_at.tzinfo is not None
        assert loaded.revoked_at is None
        # Stored as the enum VALUES, not the member names, so raw SQL readers and
        # the CHECK constraints see the same spelling the migration wrote.
        raw = (
            await session.execute(
                text(
                    "SELECT principal_kind, scope_kind, role FROM role_assignments WHERE id = :id"
                ),
                {"id": row_id},
            )
        ).one()
        assert tuple(raw) == ("user", "team", "viewer")


async def test_two_live_grants_of_one_key_collide_and_a_revoked_one_frees_it() -> None:
    async with AsyncSessionLocal() as session:
        org, _admin = await _org(session)
        member = await _user(session, org.id)
        first = _assignment(org, member, role=Role.VIEWER)
        session.add(first)
        await session.commit()
        first_id = first.id

        # Same (principal, scope), a different role: the key is the principal and
        # the scope, so a second LIVE grant is refused whatever its role says.
        session.add(_assignment(org, member, role=Role.MEMBER))
        with pytest.raises(IntegrityError) as excinfo:
            await session.commit()
        assert "uq_role_assignments_live_principal_scope" in str(excinfo.value)
        await session.rollback()
        # The rollback expired every loaded instance; reload the two the rest of
        # the test reads ids off.
        await session.refresh(org)
        await session.refresh(member)

        first = (
            await session.execute(select(RoleAssignment).where(RoleAssignment.id == first_id))
        ).scalar_one()
        first.revoked_at = datetime.now(UTC)
        await session.commit()

        session.add(_assignment(org, member, role=Role.MEMBER))
        await session.commit()

        live = (
            await session.execute(
                select(func.count())
                .select_from(RoleAssignment)
                .where(
                    RoleAssignment.principal_id == member.id,
                    RoleAssignment.revoked_at.is_(None),
                )
            )
        ).scalar_one()
        total = (
            await session.execute(
                select(func.count())
                .select_from(RoleAssignment)
                .where(RoleAssignment.principal_id == member.id)
            )
        ).scalar_one()
        assert (live, total) == (1, 2)


async def test_a_grant_at_a_different_scope_is_a_different_key() -> None:
    async with AsyncSessionLocal() as session:
        org, _admin = await _org(session)
        member = await _user(session, org.id)
        sub = await team_service.create_subteam(
            session, org_team_id=org.id, name="Sub", parent_team_id=org.id
        )
        await session.commit()
        session.add(_assignment(org, member, scope_kind=ScopeKind.ORG, scope_id=org.id))
        session.add(_assignment(org, member, scope_kind=ScopeKind.TEAM, scope_id=sub.id))
        await session.commit()


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        pytest.param(
            "principal_kind", "robot", "ck_role_assignments_principal_kind", id="principal-kind"
        ),
        pytest.param(
            "principal_kind", "USER", "ck_role_assignments_principal_kind", id="kind-case"
        ),
        pytest.param("scope_kind", "project", "ck_role_assignments_scope_kind", id="scope-kind"),
        pytest.param("role", "superuser", "ck_role_assignments_role", id="role"),
        pytest.param("role", "OWNER", "ck_role_assignments_role", id="role-case"),
    ],
)
async def test_the_check_constraints_refuse_a_value_outside_the_vocabulary(
    column: str, value: str, constraint: str
) -> None:
    """A raw INSERT, because the ORM's Enum column would refuse the value
    client-side and never reach the table's own guard."""
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        values = {
            "id": uuid4(),
            "org_team_id": org.id,
            "principal_kind": "user",
            "principal_id": admin.id,
            "scope_kind": "team",
            "scope_id": org.id,
            "role": "viewer",
        }
        values[column] = value
        with pytest.raises(IntegrityError) as excinfo:
            await session.execute(
                text(
                    "INSERT INTO role_assignments"
                    " (id, org_team_id, principal_kind, principal_id, scope_kind, scope_id, role)"
                    " VALUES (:id, :org_team_id, :principal_kind, :principal_id,"
                    " :scope_kind, :scope_id, :role)"
                ),
                values,
            )
        assert constraint in str(excinfo.value)
        await session.rollback()


async def test_deleting_the_scope_team_cascades_its_grants() -> None:
    async with AsyncSessionLocal() as session:
        org, _admin = await _org(session)
        member = await _user(session, org.id)
        sub = await team_service.create_subteam(
            session, org_team_id=org.id, name="Doomed", parent_team_id=org.id
        )
        await session.commit()
        row = _assignment(org, member, scope_kind=ScopeKind.TEAM, scope_id=sub.id)
        session.add(row)
        await session.commit()
        row_id = row.id

        await team_service.delete_team(session, sub)
        await session.commit()

        assert (
            await session.execute(select(RoleAssignment).where(RoleAssignment.id == row_id))
        ).scalar_one_or_none() is None


async def test_deleting_the_granting_user_keeps_the_grant_and_nulls_the_grantor() -> None:
    async with AsyncSessionLocal() as session:
        org, _admin = await _org(session)
        member = await _user(session, org.id)
        grantor = await _user(session, org.id)
        row = _assignment(org, member, granted_by_id=grantor.id)
        session.add(row)
        await session.commit()
        row_id = row.id

        await session.delete(grantor)
        await session.commit()

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(select(RoleAssignment).where(RoleAssignment.id == row_id))
        ).scalar_one()
        assert loaded.granted_by_id is None
        assert loaded.principal_id == member.id


async def test_deleting_the_org_cascades_every_grant_of_the_org() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        owner_rows = await _live_owner_rows(session, org.id)
        assert len(owner_rows) == 1
        session.add(_assignment(org, admin, scope_kind=ScopeKind.TEAM, scope_id=org.id))
        await session.commit()
        org_id = org.id

        org = (await session.execute(select(Team).where(Team.id == org_id))).scalar_one()
        await team_service.delete_org(session, org)
        await session.commit()

        remaining = (
            await session.execute(
                select(func.count())
                .select_from(RoleAssignment)
                .where(RoleAssignment.org_team_id == org_id)
            )
        ).scalar_one()
        assert remaining == 0


async def test_create_org_with_admin_writes_exactly_one_owner_row_for_the_admin() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        rows = await _live_owner_rows(session, org.id)
        assert len(rows) == 1
        (owner,) = rows
        assert owner.principal_kind is PrincipalKind.USER
        assert owner.principal_id == admin.id
        assert owner.scope_kind is ScopeKind.ORG
        assert owner.scope_id == org.id
        assert owner.role is Role.OWNER
        assert owner.granted_by_id is None
        assert owner.revoked_at is None


async def test_the_owner_row_lands_in_the_same_transaction_as_the_org() -> None:
    """Rolling back the org creation leaves neither an org nor an owner row:
    an org can never exist without its owner, nor an owner without its org."""
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Rolled Back {secrets.token_hex(4)}",
            admin_email=f"rolled-back-{secrets.token_hex(6)}@alkera.dev",
            admin_first_name="Rolled",
            admin_last_name="Back",
            admin_password="admin-pass-12345",
        )
        org_id = org.id
        assert len(await _live_owner_rows(session, org_id)) == 1
        await session.rollback()

    async with AsyncSessionLocal() as session:
        assert (
            await session.execute(select(Team).where(Team.id == org_id))
        ).scalar_one_or_none() is None
        assert await _live_owner_rows(session, org_id) == []


# --------------------------------------------------------------------------- #
# personal_access_tokens
# --------------------------------------------------------------------------- #


async def test_a_personal_access_token_round_trips_through_the_orm() -> None:
    digest = secrets.token_hex(32)
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        row = PersonalAccessToken(
            org_team_id=org.id,
            user_id=admin.id,
            token_hash=digest,
            label="laptop",
            scopes=["kb:write"],
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    async with AsyncSessionLocal() as session:
        loaded = (
            await session.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.id == row_id)
            )
        ).scalar_one()
        assert loaded.org_team_id == org.id
        assert loaded.user_id == admin.id
        assert loaded.token_hash == digest
        assert loaded.label == "laptop"
        assert loaded.scopes == ["kb:write"]
        assert loaded.created_at.tzinfo is not None
        assert loaded.expires_at is None
        assert loaded.revoked_at is None
        assert loaded.last_used_at is None


async def test_scopes_default_to_an_empty_list_in_the_orm_and_in_the_table() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        row = PersonalAccessToken(
            org_team_id=org.id, user_id=admin.id, token_hash=secrets.token_hex(32)
        )
        session.add(row)
        await session.commit()
        assert row.scopes == []

        raw_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO personal_access_tokens (id, org_team_id, user_id, token_hash)"
                " VALUES (:id, :org, :user, :hash)"
            ),
            {"id": raw_id, "org": org.id, "user": admin.id, "hash": secrets.token_hex(32)},
        )
        await session.commit()
        stored = (
            await session.execute(
                select(PersonalAccessToken.scopes).where(PersonalAccessToken.id == raw_id)
            )
        ).scalar_one()
        assert stored == []


async def test_a_duplicate_token_hash_is_refused_by_name() -> None:
    digest = secrets.token_hex(32)
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        session.add(PersonalAccessToken(org_team_id=org.id, user_id=admin.id, token_hash=digest))
        await session.commit()
        session.add(PersonalAccessToken(org_team_id=org.id, user_id=admin.id, token_hash=digest))
        with pytest.raises(IntegrityError) as excinfo:
            await session.commit()
        assert "uq_personal_access_tokens_token_hash" in str(excinfo.value)
        await session.rollback()


async def test_deleting_the_owner_cascades_their_tokens() -> None:
    async with AsyncSessionLocal() as session:
        org, _admin = await _org(session)
        owner = await _user(session, org.id)
        row = PersonalAccessToken(
            org_team_id=org.id, user_id=owner.id, token_hash=secrets.token_hex(32)
        )
        session.add(row)
        await session.commit()
        row_id = row.id
        await session.delete(owner)
        await session.commit()
        assert (
            await session.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.id == row_id)
            )
        ).scalar_one_or_none() is None


async def test_deleting_the_org_cascades_its_tokens() -> None:
    async with AsyncSessionLocal() as session:
        org, admin = await _org(session)
        session.add(
            PersonalAccessToken(
                org_team_id=org.id, user_id=admin.id, token_hash=secrets.token_hex(32)
            )
        )
        await session.commit()
        org_id = org.id
        org = (await session.execute(select(Team).where(Team.id == org_id))).scalar_one()
        await team_service.delete_org(session, org)
        await session.commit()
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(PersonalAccessToken)
                .where(PersonalAccessToken.org_team_id == org_id)
            )
        ).scalar_one()
        assert remaining == 0


# --------------------------------------------------------------------------- #
# the live catalog: names a later migration must be able to drop by
# --------------------------------------------------------------------------- #


async def _constraint_names(session: AsyncSession, table: str) -> set[str]:
    rows = await session.execute(
        text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:table)"),
        {"table": table},
    )
    return {row[0] for row in rows}


async def _index_defs(session: AsyncSession, table: str) -> dict[str, str]:
    rows = await session.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :table"),
        {"table": table},
    )
    return {row[0]: row[1] for row in rows}


async def test_role_assignments_catalog_carries_every_named_constraint_and_index() -> None:
    async with AsyncSessionLocal() as session:
        constraints = await _constraint_names(session, "role_assignments")
        assert {
            "ck_role_assignments_principal_kind",
            "ck_role_assignments_scope_kind",
            "ck_role_assignments_role",
        } <= constraints
        indexes = await _index_defs(session, "role_assignments")
        assert {
            "ix_role_assignments_org_team_id",
            "ix_role_assignments_principal",
            "ix_role_assignments_scope",
            "uq_role_assignments_live_principal_scope",
        } <= set(indexes)
        live = indexes["uq_role_assignments_live_principal_scope"]
        assert live.startswith("CREATE UNIQUE INDEX")
        assert "WHERE (revoked_at IS NULL)" in live


async def test_personal_access_tokens_catalog_carries_every_named_constraint_and_index() -> None:
    async with AsyncSessionLocal() as session:
        constraints = await _constraint_names(session, "personal_access_tokens")
        assert "uq_personal_access_tokens_token_hash" in constraints
        indexes = await _index_defs(session, "personal_access_tokens")
        assert {
            "ix_personal_access_tokens_org_team_id",
            "ix_personal_access_tokens_user_id",
            "ix_personal_access_tokens_token_hash",
        } <= set(indexes)


@pytest.mark.parametrize(
    ("table", "column", "referenced", "action"),
    [
        pytest.param("role_assignments", "org_team_id", "teams", "c", id="grant-org-cascades"),
        pytest.param("role_assignments", "scope_id", "teams", "c", id="grant-scope-cascades"),
        pytest.param("role_assignments", "granted_by_id", "users", "n", id="grantor-set-null"),
        pytest.param(
            "personal_access_tokens", "org_team_id", "teams", "c", id="token-org-cascades"
        ),
        pytest.param("personal_access_tokens", "user_id", "users", "c", id="token-owner-cascades"),
    ],
)
async def test_every_foreign_key_carries_its_documented_on_delete_action(
    table: str, column: str, referenced: str, action: str
) -> None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT c.confdeltype::text, c.confrelid::regclass::text"
                    " FROM pg_constraint c"
                    " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)"
                    " WHERE c.conrelid = to_regclass(:table) AND c.contype = 'f'"
                    "   AND a.attname = :column"
                ),
                {"table": table, "column": column},
            )
        ).one()
        assert tuple(row) == (action, referenced)
