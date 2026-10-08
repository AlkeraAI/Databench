"""Revision 0220 against real Postgres: compute funding moves into billing's tables.

Before it, ``compute_grants.funding_account_id`` and
``compute_allocations.billing_account_id`` pointed into ``billing_accounts``.
After it, ``billing_compute_grant_funding`` and
``billing_compute_allocation_funding`` hold the same facts and point the other
way. A funded row keeps its account across the upgrade and gets it back on the
downgrade, an unfunded one stays unfunded both ways, and deleting an account
afterwards leaves the grant and the allocation in place with no funding, as the
old ``ON DELETE SET NULL`` did.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.models import Team, User
from alkera_core.models.compute import ComputeAllocation, ComputeGrant, ComputeMachineType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import ScratchDatabase, migration_scratch

pytestmark = pytest.mark.asyncio

_PARENT = "0219"
GRANT_LINK = "billing_compute_grant_funding"
ALLOC_LINK = "billing_compute_allocation_funding"


class Seeded:
    def __init__(self, account: uuid.UUID) -> None:
        self.account = account
        self.funded_grant = uuid.uuid4()
        self.unfunded_grant = uuid.uuid4()
        self.funded_alloc = uuid.uuid4()
        self.unfunded_alloc = uuid.uuid4()


async def _seed_compute(session: AsyncSession) -> Seeded:
    """An org pool account, and a funded and an unfunded grant and allocation."""
    team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
    session.add(team)
    await session.flush()
    user = User(
        home_org_team_id=team.id,
        email=f"u-{secrets.token_hex(6)}@alkera.dev",
        first_name="U",
        last_name="Ser",
    )
    machine_type = ComputeMachineType(
        provider="runpod",
        provider_type_id=f"TEST {uuid.uuid4().hex[:10]}",
        display_name="Test CPU",
        compute_class="cpu",
        vcpu=2,
        provider_price_per_minute_nanos=1,
    )
    session.add_all([user, machine_type])
    await session.flush()
    seeded = Seeded(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO billing_accounts (id, scope, owner_team_id, created_at) "
            "VALUES (:id, 'org', :team, now())"
        ),
        {"id": seeded.account, "team": team.id},
    )
    expires = datetime.now(UTC) + timedelta(days=7)
    for grant_id in (seeded.funded_grant, seeded.unfunded_grant):
        session.add(
            ComputeGrant(
                id=grant_id,
                org_team_id=team.id,
                ceiling=1,
                rate_per_minute_nanos=0,
                expires_at=expires,
            )
        )
    for alloc_id in (seeded.funded_alloc, seeded.unfunded_alloc):
        session.add(
            ComputeAllocation(
                id=alloc_id,
                user_id=user.id,
                org_team_id=team.id,
                machine_type_id=machine_type.id,
                lifecycle="session",
                state="ready",
                provider_machine_id=f"pod-{uuid.uuid4().hex[:12]}",
            )
        )
    await session.flush()
    return seeded


async def _seed_on_the_parent(scratch: ScratchDatabase) -> Seeded:
    """Rows written the way the parent revision's code wrote them: the account
    on the compute row itself."""
    await scratch.downgrade(_PARENT)
    async with scratch.session() as session:
        seeded = await _seed_compute(session)
        await session.execute(
            text("UPDATE compute_grants SET funding_account_id = :acct WHERE id = :id"),
            {"acct": seeded.account, "id": seeded.funded_grant},
        )
        await session.execute(
            text("UPDATE compute_allocations SET billing_account_id = :acct WHERE id = :id"),
            {"acct": seeded.account, "id": seeded.funded_alloc},
        )
        await session.commit()
    return seeded


async def _links(session: AsyncSession, table: str, key: str) -> dict[uuid.UUID, uuid.UUID]:
    rows = await session.execute(text(f"SELECT {key}, billing_account_id FROM {table}"))
    return {row[0]: row[1] for row in rows.all()}


async def _column(session: AsyncSession, table: str, column: str, row_id: uuid.UUID) -> object:
    return (
        await session.execute(
            text(f"SELECT {column} FROM {table} WHERE id = :id"),
            {"id": row_id},
        )
    ).scalar_one()


async def _has_column(session: AsyncSession, table: str, column: str) -> bool:
    found = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return found.first() is not None


async def _exists(session: AsyncSession, table: str) -> bool:
    found = await session.execute(text("SELECT to_regclass(:t)"), {"t": table})
    return found.scalar_one() is not None


async def test_funding_moves_into_the_link_tables_and_back() -> None:
    async with migration_scratch() as scratch:
        seeded = await _seed_on_the_parent(scratch)

        await scratch.upgrade()
        async with scratch.session() as session:
            grants = await _links(session, GRANT_LINK, "grant_id")
            allocs = await _links(session, ALLOC_LINK, "allocation_id")
            assert grants == {seeded.funded_grant: seeded.account}
            assert allocs == {seeded.funded_alloc: seeded.account}
            assert not await _has_column(session, "compute_grants", "funding_account_id")
            assert not await _has_column(session, "compute_allocations", "billing_account_id")

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert not await _exists(session, GRANT_LINK)
            assert not await _exists(session, ALLOC_LINK)
            assert (
                await _column(session, "compute_grants", "funding_account_id", seeded.funded_grant)
                == seeded.account
            )
            assert (
                await _column(
                    session, "compute_grants", "funding_account_id", seeded.unfunded_grant
                )
                is None
            )
            assert (
                await _column(
                    session, "compute_allocations", "billing_account_id", seeded.funded_alloc
                )
                == seeded.account
            )
            assert (
                await _column(
                    session, "compute_allocations", "billing_account_id", seeded.unfunded_alloc
                )
                is None
            )
            # The restored foreign key still lets go of a deleted account, as before.
            await session.execute(
                text("DELETE FROM billing_accounts WHERE id = :id"), {"id": seeded.account}
            )
            assert (
                await _column(
                    session, "compute_allocations", "billing_account_id", seeded.funded_alloc
                )
                is None
            )
            await session.rollback()
        await scratch.upgrade()


async def test_deleting_the_account_leaves_the_grant_and_the_allocation_unfunded() -> None:
    async with migration_scratch() as scratch:
        seeded = await _seed_on_the_parent(scratch)
        await scratch.upgrade()
        async with scratch.session() as session:
            await session.execute(
                text("DELETE FROM billing_accounts WHERE id = :id"), {"id": seeded.account}
            )
            await session.commit()
            assert await _links(session, GRANT_LINK, "grant_id") == {}
            assert await _links(session, ALLOC_LINK, "allocation_id") == {}
            grants = await session.execute(
                text("SELECT count(*) FROM compute_grants WHERE id = ANY(:ids)"),
                {"ids": [seeded.funded_grant, seeded.unfunded_grant]},
            )
            allocs = await session.execute(
                text("SELECT count(*) FROM compute_allocations WHERE id = ANY(:ids)"),
                {"ids": [seeded.funded_alloc, seeded.unfunded_alloc]},
            )
            assert (grants.scalar_one(), allocs.scalar_one()) == (2, 2)


async def test_deleting_the_grant_or_allocation_takes_its_link_with_it() -> None:
    async with migration_scratch() as scratch:
        seeded = await _seed_on_the_parent(scratch)
        await scratch.upgrade()
        async with scratch.session() as session:
            await session.execute(
                text("DELETE FROM compute_allocations WHERE id = :id"),
                {"id": seeded.funded_alloc},
            )
            await session.execute(
                text("DELETE FROM compute_grants WHERE id = :id"), {"id": seeded.funded_grant}
            )
            await session.commit()
            assert await _links(session, GRANT_LINK, "grant_id") == {}
            assert await _links(session, ALLOC_LINK, "allocation_id") == {}
            account = await session.execute(
                text("SELECT count(*) FROM billing_accounts WHERE id = :id"),
                {"id": seeded.account},
            )
            assert account.scalar_one() == 1
