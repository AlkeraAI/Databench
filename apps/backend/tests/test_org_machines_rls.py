"""The four org-machine tenant tables under a tenant-bound session.

Org B's rows are invisible to a session bound to org A, and a row naming org B
written under that binding is refused by the policy (or, for a move, by its
trigger first, which cannot see B's workspace). The canary judges the live
database clean with the new tables policed.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from alkera_core.db.row_security import (
    CONTENT_TABLES,
    CONTENT_TIER,
    content_tier_tables,
    judge,
    take_snapshot,
)
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.db.tenant_session import bind_tenant
from alkera_core.models import (
    ComputeMachineType,
    ComputeOffering,
    OrgComputeSettings,
    OrgMachine,
    OrgMachineAudience,
    WorkspaceMachineMove,
    WorkspaceObject,
)
from backend.services.org import teams as team_service
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio]

RLS_REFUSED = "42501"
PARENT_HIDDEN = "23503"
TABLES = (
    "org_machines",
    "org_machine_audiences",
    "org_compute_settings",
    "workspace_machine_moves",
)


@dataclass
class Seeded:
    org: uuid.UUID
    user: uuid.UUID
    machine: uuid.UUID
    workspace: uuid.UUID


async def _seed(session: AsyncSession, offering: uuid.UUID) -> Seeded:
    tag = secrets.token_hex(4)
    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=f"OM RLS {tag}",
        admin_email=f"om-rls-{tag}@example.com",
        admin_first_name="O",
        admin_last_name="M",
        admin_password="admin-pass-12345",
    )
    await session.flush()
    workspace = WorkspaceObject(
        org_team_id=org.id,
        logical_id=f"om-rls-{tag}",
        type="workspace",
        owner_user_id=admin.id,
        visibility_scope="org",
    )
    machine = OrgMachine(
        org_team_id=org.id,
        owner_team_id=org.id,
        offering_id=offering,
        name=f"om-{tag}",
        acquisition="purchased",
        use_mode="assigned",
        storage_gb=10,
    )
    session.add_all([workspace, machine])
    await session.flush()
    session.add_all(
        [
            OrgMachineAudience(org_team_id=org.id, org_machine_id=machine.id, grantee_kind="org"),
            OrgComputeSettings(org_team_id=org.id),
            WorkspaceMachineMove(
                org_team_id=org.id, workspace_id=workspace.id, to_org_machine_id=machine.id
            ),
        ]
    )
    await session.commit()
    return Seeded(org=org.id, user=admin.id, machine=machine.id, workspace=workspace.id)


@dataclass
class Two:
    a: Seeded
    b: Seeded
    offering: uuid.UUID


@pytest_asyncio.fixture
async def two(real_session: AsyncSession) -> Two:
    machine_type = ComputeMachineType(
        provider="runpod", provider_type_id=f"om-rls-{secrets.token_hex(4)}", display_name="T"
    )
    real_session.add(machine_type)
    await real_session.flush()
    offering = ComputeOffering(
        machine_type_id=machine_type.id,
        name="T",
        pricing_mode="fixed",
        fixed_rate_per_minute_nanos=0,
        storage_gb_default=10,
        storage_gb_max=10,
        audience="all",
    )
    real_session.add(offering)
    await real_session.commit()
    offering_id = offering.id
    return Two(
        a=await _seed(real_session, offering_id),
        b=await _seed(real_session, offering_id),
        offering=offering_id,
    )


def _sqlstate(error: DBAPIError) -> str | None:
    orig = error.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def test_the_four_tables_are_content_tables() -> None:
    assert {table: CONTENT_TABLES.get(table) for table in TABLES} == dict.fromkeys(
        TABLES, "org_team_id"
    )


@pytest.mark.parametrize("table", TABLES)
async def test_a_session_bound_to_a_sees_none_of_bs_rows(two: Two, table: str) -> None:
    async with AsyncSessionLocal() as bound:
        bind_tenant(bound, [two.a.org])
        seen = {
            row[0] for row in (await bound.execute(text(f"SELECT org_team_id FROM {table}"))).all()
        }
        await bound.rollback()
    assert two.a.org in seen
    assert two.b.org not in seen


def _foreign_insert(table: str, two: Two) -> tuple[str, dict[str, object]]:
    """A row naming org B, written under org A's binding."""
    b = two.b
    statements: dict[str, tuple[str, dict[str, object]]] = {
        "org_machines": (
            "INSERT INTO org_machines (id, org_team_id, owner_team_id, offering_id, name, "
            "acquisition, use_mode, storage_gb) VALUES (:id, :org, :org, :offering, :name, "
            "'purchased', 'assigned', 10)",
            {
                "id": uuid.uuid4(),
                "org": b.org,
                "offering": two.offering,
                "name": f"x-{secrets.token_hex(3)}",
            },
        ),
        "org_machine_audiences": (
            "INSERT INTO org_machine_audiences (id, org_team_id, org_machine_id, grantee_kind, "
            "user_id) VALUES (:id, :org, :machine, 'user', :user)",
            {"id": uuid.uuid4(), "org": b.org, "machine": b.machine, "user": b.user},
        ),
        "org_compute_settings": (
            "INSERT INTO org_compute_settings (org_team_id) VALUES (:org) "
            "ON CONFLICT (org_team_id) DO NOTHING",
            {"org": b.org},
        ),
        "workspace_machine_moves": (
            "INSERT INTO workspace_machine_moves (id, org_team_id, workspace_id, state) "
            "VALUES (:id, :org, :workspace, 'done')",
            {"id": uuid.uuid4(), "org": b.org, "workspace": b.workspace},
        ),
    }
    return statements[table]


@pytest.mark.parametrize("table", TABLES)
async def test_an_insert_naming_b_under_a_binding_is_refused(two: Two, table: str) -> None:
    statement, params = _foreign_insert(table, two)
    async with AsyncSessionLocal() as bound:
        bind_tenant(bound, [two.a.org])
        with pytest.raises(DBAPIError) as refused:
            await bound.execute(text(statement), params)
        await bound.rollback()
    expected = {RLS_REFUSED, PARENT_HIDDEN} if table == "workspace_machine_moves" else {RLS_REFUSED}
    assert _sqlstate(refused.value) in expected
    # Under B's own binding the same kind of row is accepted, so the refusal
    # above is the policy and not a malformed statement.
    async with AsyncSessionLocal() as own:
        bind_tenant(own, [two.b.org])
        if table == "org_compute_settings":
            await own.execute(
                text("UPDATE org_compute_settings SET min_awake_pool = 1 WHERE org_team_id = :o"),
                {"o": two.b.org},
            )
        else:
            fresh, fresh_params = _foreign_insert(table, two)
            await own.execute(text(fresh), fresh_params)
        await own.rollback()


@pytest.mark.parametrize("table", TABLES)
async def test_an_update_of_bs_rows_under_a_binding_touches_nothing(two: Two, table: str) -> None:
    statement = text(f"UPDATE {table} SET org_team_id = org_team_id WHERE org_team_id = :org")
    async with AsyncSessionLocal() as bound:
        bind_tenant(bound, [two.a.org])
        foreign = await bound.execute(statement, {"org": two.b.org})
        await bound.rollback()
    assert foreign.rowcount == 0


async def test_the_row_security_canary_passes_with_the_new_tables() -> None:
    expected = content_tier_tables()
    assert set(TABLES) <= expected
    async with AsyncSessionLocal() as session:
        snapshot = await take_snapshot(session, expected=expected, tier=CONTENT_TIER)
        await session.rollback()
    assert judge(snapshot, expected=expected) == []


async def test_the_canary_would_name_a_new_table_that_lost_its_force() -> None:
    """The canary judges these tables, so a revision that dropped FORCE on one
    would fail it. Rolled back: the change never leaves the transaction."""
    expected = content_tier_tables()
    async with AsyncSessionLocal() as session:
        await session.execute(text("ALTER TABLE org_machines NO FORCE ROW LEVEL SECURITY"))
        snapshot = await take_snapshot(session, expected=expected, tier=CONTENT_TIER)
        await session.rollback()
    assert judge(snapshot, expected=expected) == [
        "tenant table org_machines does not force row-level security"
    ]
