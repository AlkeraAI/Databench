"""The compute-plane schema against real Postgres: every row round-trips, the
constraints the migration names actually hold, the SSH private key rests as
ciphertext, and the migration downgrades to its parent and back cleanly.

The plaintext-key test is the hard blocker of the plane: the column bytes must
not contain the key, and the key must come back through the one opener.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.keys import generate_ssh_keypair, open_private_key, seal_private_key
from alkera_core.config import settings
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, User
from alkera_core.models.compute import (
    COMPUTE_ACTIVE_STATES,
    COMPUTE_LIFECYCLES,
    COMPUTE_TERMINAL_STATES,
    DRAINING,
    ComputeAllocation,
    ComputeGrant,
    ComputeMachineType,
)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin
from tests.migration_harness import migration_scratch

pytestmark = pytest.mark.compute_rows

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = next(iter(sorted((_BACKEND / "alembic" / "versions").glob("*_compute_plane.py"))))
_REVISION = _MIGRATION.name.split("_", 1)[0]
_TABLES = ("compute_machine_types", "compute_grants", "compute_allocations")


async def _machine_type(session: AsyncSession, **overrides: Any) -> ComputeMachineType:
    row = ComputeMachineType(
        provider="runpod",
        provider_type_id=f"test-{uuid4().hex[:10]}",
        display_name="Test CPU",
        compute_class="cpu",
        vcpu=2,
        memory_gb=8,
        provider_price_per_minute_nanos=1_000_000,
        provider_config={"image": "example/image:1", "disk_gb": 20},
        **overrides,
    )
    session.add(row)
    await session.commit()
    return row


async def _grant(
    session: AsyncSession, org_id: UUID, machine_type_id: UUID | None, **overrides: Any
) -> ComputeGrant:
    fields: dict[str, Any] = {
        "org_team_id": org_id,
        "machine_type_id": machine_type_id,
        "ceiling": 1,
        "rate_per_minute_nanos": 0,
        "expires_at": datetime.now(UTC) + timedelta(days=14),
        "note": "test",
    }
    fields.update(overrides)
    row = ComputeGrant(**fields)
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


async def test_the_state_vocabularies_partition_the_lifecycle() -> None:
    """Every allocation state is live or terminal, and never both.

    ``draining`` is on the live side. A box told to stop is still metered, still
    counts against its grant's ceiling, and still answers the turns it already
    holds; the only thing it may not take is a NEW chat, which is placement's
    business and is pinned in ``test_placement_draining``. Calling it terminal
    here would stop the meter on a box that is still costing money and free its
    seat under the ceiling while it is still serving.

    Both sets are spelled out rather than derived, so a state added to the model
    without being classified fails here instead of quietly belonging to neither
    — which is a row nothing counts and nothing ever reaps.
    """
    assert set(COMPUTE_ACTIVE_STATES) == {
        "pending",
        "provisioning",
        "bootstrapping",
        "ready",
        DRAINING,
        # Asleep is stopped but KEPT for its org, so it still counts against the
        # grant ceiling even though it is neither metered nor placeable.
        "asleep",
    }
    assert set(COMPUTE_TERMINAL_STATES) == {"released", "failed", "lost"}
    assert not set(COMPUTE_ACTIVE_STATES) & set(COMPUTE_TERMINAL_STATES)
    assert set(COMPUTE_LIFECYCLES) == {"session", "workspace"}


async def test_a_machine_type_round_trips_with_its_provider_config(
    real_session: AsyncSession,
) -> None:
    created = await _machine_type(real_session)
    async with AsyncSessionLocal() as fresh:
        row = await fresh.get(ComputeMachineType, created.id)
    assert row is not None
    assert row.provider_config == {"image": "example/image:1", "disk_gb": 20}
    assert row.provider_price_per_minute_nanos == 1_000_000
    assert (row.availability, row.available_for_new, row.active) == ("unknown", True, True)
    assert row.synced_at is None


async def test_a_grant_round_trips_and_a_wildcard_grant_has_no_machine_type(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await _machine_type(real_session)
    typed = await _grant(real_session, org_admin.org_id, mt.id, per_user_max=1)
    wildcard = await _grant(
        real_session,
        org_admin.org_id,
        None,
        ceiling=3,
        rate_per_minute_nanos=2_500_000,
        created_by=org_admin.admin_id,
    )
    async with AsyncSessionLocal() as fresh:
        rows = {
            g.id: g
            for g in (
                await fresh.execute(
                    select(ComputeGrant).where(ComputeGrant.org_team_id == org_admin.org_id)
                )
            )
            .scalars()
            .all()
        }
    assert rows[typed.id].machine_type_id == mt.id
    assert rows[typed.id].per_user_max == 1
    assert await compute_funding().grant_funding(real_session, typed.id) is None
    assert rows[wildcard.id].machine_type_id is None
    assert rows[wildcard.id].ceiling == 3
    assert rows[wildcard.id].rate_per_minute_nanos == 2_500_000
    assert rows[wildcard.id].created_by == org_admin.admin_id
    assert rows[wildcard.id].expires_at.tzinfo is not None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param({"ceiling": -1}, "ck_compute_grants_ceiling_nonnegative", id="ceiling"),
        pytest.param(
            {"rate_per_minute_nanos": -1}, "ck_compute_grants_rate_nonnegative", id="rate"
        ),
        pytest.param(
            {"per_user_max": 0}, "ck_compute_grants_per_user_max_positive", id="per-user-max"
        ),
    ],
)
async def test_a_grant_outside_its_checks_is_refused_by_name(
    real_session: AsyncSession, org_admin: OrgWithAdmin, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        await _grant(real_session, org_admin.org_id, None, **overrides)
    await real_session.rollback()


async def test_a_grant_may_be_comped_and_may_admit_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Rate 0 and ceiling 0 are both legal rows — the comped customer and the
    grant that has been dialled to nothing without being deleted."""
    row = await _grant(real_session, org_admin.org_id, None, ceiling=0, rate_per_minute_nanos=0)
    assert (row.ceiling, row.rate_per_minute_nanos) == (0, 0)


async def test_a_grant_needs_an_expiry(real_session: AsyncSession, org_admin: OrgWithAdmin) -> None:
    with pytest.raises(IntegrityError, match="expires_at"):
        await _grant(real_session, org_admin.org_id, None, expires_at=None)
    await real_session.rollback()


async def test_deleting_the_machine_type_cascades_to_its_grants_only(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await _machine_type(real_session)
    typed = await _grant(real_session, org_admin.org_id, mt.id)
    wildcard = await _grant(real_session, org_admin.org_id, None)
    await real_session.delete(mt)
    await real_session.commit()
    async with AsyncSessionLocal() as fresh:
        assert await fresh.get(ComputeGrant, typed.id) is None
        assert await fresh.get(ComputeGrant, wildcard.id) is not None


async def test_an_allocation_round_trips_with_both_pinned_prices_and_a_sealed_key(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await _machine_type(real_session)
    grant = await _grant(real_session, org_admin.org_id, mt.id, rate_per_minute_nanos=0)
    private_pem, public_line = generate_ssh_keypair()
    alloc = ComputeAllocation(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        name="demo-box",
        state="ready",
        provider_machine_id=f"pod-{secrets.token_hex(4)}",
        ssh_public_key=public_line,
        ssh_private_key_enc=seal_private_key(private_pem),
        grant_id=grant.id,
        price_per_minute_nanos=grant.rate_per_minute_nanos,
        true_cost_per_minute_nanos=mt.provider_price_per_minute_nanos,
    )
    real_session.add(alloc)
    await real_session.commit()

    async with AsyncSessionLocal() as fresh:
        row = await fresh.get(ComputeAllocation, alloc.id)
        assert row is not None
        raw = (
            await fresh.execute(
                text("SELECT ssh_private_key_enc FROM compute_allocations WHERE id = :id"),
                {"id": alloc.id},
            )
        ).scalar_one()
    assert (row.lifecycle, row.name, row.state) == ("workspace", "demo-box", "ready")
    assert row.grant_id == grant.id
    assert (row.price_per_minute_nanos, row.true_cost_per_minute_nanos) == (0, 1_000_000)
    assert (row.billed_nanos, row.true_cost_nanos, row.minutes_billed) == (0, 0, 0)
    assert row.last_heartbeat_at is None and row.last_reported_status == ""
    assert row.max_lease_minutes is None
    # The hard blocker: the stored bytes are NOT the key, and the opener restores it.
    assert private_pem not in raw
    assert "PRIVATE KEY" not in raw
    assert raw.startswith("gAAAA")
    assert open_private_key(raw) == private_pem
    assert not hasattr(row, "ssh_private_key_pem"), "no plaintext key column may exist"


async def test_an_allocation_refuses_a_lifecycle_outside_the_two(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await _machine_type(real_session)
    real_session.add(
        ComputeAllocation(
            user_id=org_admin.admin_id,
            org_team_id=org_admin.org_id,
            machine_type_id=mt.id,
            lifecycle="pool",
        )
    )
    with pytest.raises(IntegrityError, match="ck_compute_allocations_lifecycle"):
        await real_session.commit()
    await real_session.rollback()


async def test_a_machine_type_with_a_live_allocation_cannot_be_deleted(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    mt = await _machine_type(real_session)
    real_session.add(
        ComputeAllocation(
            user_id=org_admin.admin_id, org_team_id=org_admin.org_id, machine_type_id=mt.id
        )
    )
    await real_session.commit()
    await real_session.delete(mt)
    with pytest.raises(IntegrityError, match="fk_compute_allocations_machine_type_id"):
        await real_session.commit()
    await real_session.rollback()


async def test_deleting_the_grant_orphans_the_allocation_rather_than_removing_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A billing record outlives the admission that created it."""
    mt = await _machine_type(real_session)
    grant = await _grant(real_session, org_admin.org_id, mt.id)
    alloc = ComputeAllocation(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        machine_type_id=mt.id,
        grant_id=grant.id,
        state="released",
    )
    real_session.add(alloc)
    await real_session.commit()
    await real_session.delete(grant)
    await real_session.commit()
    async with AsyncSessionLocal() as fresh:
        row = await fresh.get(ComputeAllocation, alloc.id)
    assert row is not None and row.grant_id is None


# --------------------------------------------------------------------------- #
# The migration itself
# --------------------------------------------------------------------------- #


def _alembic_config() -> Config:
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url_sync)
    return config


async def _regclass(session: AsyncSession, table: str) -> Any:
    return (
        await session.execute(text("SELECT to_regclass(:table)"), {"table": table})
    ).scalar_one()


async def _column_exists(session: AsyncSession, table: str, column: str) -> bool:
    return (
        await session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
    ).first() is not None


async def _constraint_names(session: AsyncSession, table: str) -> set[str]:
    rows = await session.execute(
        text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:table)"),
        {"table": table},
    )
    return {str(r[0]) for r in rows}


def test_the_compute_migration_is_part_of_the_schema_the_code_expects() -> None:
    """The compute plane's tables arrive in ``0103``.

    That revision stopped being the head the moment a later migration landed on
    top of it, so pinning the two to the same literal only says "nobody has
    migrated since" — which is not a fact about the compute plane at all. What
    matters here is that the head the running code expects still carries this
    migration; the head itself is held against the real Alembic head in
    ``test_schema_head.py``.
    """
    assert _REVISION == "0103"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


async def test_every_constraint_and_index_is_named_as_the_models_spell_them() -> None:
    async with AsyncSessionLocal() as session:
        names = set()
        for table in _TABLES:
            names |= await _constraint_names(session, table)
        indexes = {
            str(r[0])
            for r in await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = ANY(:tables)"),
                {"tables": list(_TABLES)},
            )
        }
    assert {
        "pk_compute_machine_types",
        "uq_compute_machine_types_provider_type",
        "pk_compute_grants",
        "fk_compute_grants_org_team_id",
        "fk_compute_grants_machine_type_id",
        "fk_compute_grants_created_by",
        "ck_compute_grants_ceiling_nonnegative",
        "ck_compute_grants_rate_nonnegative",
        "ck_compute_grants_per_user_max_positive",
        "pk_compute_allocations",
        "fk_compute_allocations_user_id",
        "fk_compute_allocations_org_team_id",
        "fk_compute_allocations_machine_type_id",
        "fk_compute_allocations_grant_id",
        "ck_compute_allocations_lifecycle",
    } <= names
    assert {
        "ix_compute_grants_org_team_id",
        "ix_compute_grants_expires_at",
        "ix_compute_allocations_user_state",
        "ix_compute_allocations_org_team_id",
        "ix_compute_allocations_machine_type_id",
        "ix_compute_allocations_grant_id",
        "ix_compute_allocations_provider_machine_id",
    } <= indexes
    # No constraint or index on these tables is anonymous. ``ux_`` is a unique
    # index (the key an org machine's composite foreign key points at).
    anonymous = {
        n for n in names | indexes if not n.startswith(("pk_", "uq_", "ux_", "fk_", "ck_", "ix_"))
    }
    assert anonymous == set(), anonymous


async def test_downgrade_removes_the_plane_and_upgrade_restores_it() -> None:
    """The production round trip, driven by Alembic against a copy of the worker's database:
    the three tables and the ledger column are gone at the parent revision and
    back at head, and rows written before the round trip in untouched tables
    survive it."""
    async with migration_scratch() as db:
        async with db.session() as session:
            team = Team(name=f"compute-migration-{secrets.token_hex(4)}", is_root=True)
            session.add(team)
            await session.commit()
            team_id = team.id
        await db.downgrade("0102")
        async with db.session() as session:
            for table in _TABLES:
                assert await _regclass(session, table) is None, table
            assert not await _column_exists(session, "billing_credit_ledger", "true_cost_nanos")
            assert (
                await session.execute(select(Team.id).where(Team.id == team_id))
            ).scalar_one() == team_id

        await db.upgrade()
        async with db.session() as session:
            for table in _TABLES:
                assert await _regclass(session, table) is not None, table
            assert await _column_exists(session, "billing_credit_ledger", "true_cost_nanos")
            assert await _column_exists(session, "compute_allocations", "ssh_private_key_enc")
            assert not await _column_exists(session, "compute_allocations", "ssh_private_key_pem")
            assert not await _column_exists(
                session, "compute_machine_types", "price_per_minute_nanos"
            )
            user = await session.get(User, uuid4())
            assert user is None  # the session works normally after the round trip
