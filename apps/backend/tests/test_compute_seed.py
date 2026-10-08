"""The open compute catalog seed: the types a deployment's own boxes register
as, never a provider flavor it could sell, and whatever a distribution
registers on ``COMPUTE_CATALOG_LOADERS``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.compute.provider import EC2, LOCALDEV, RUNPOD, SELF_HOSTED
from alkera_core.config import settings
from alkera_core.extensions import ExtensionPoint
from alkera_core.models import MachineCredential, OrgComputeAssignment
from alkera_core.models.compute import ComputeAllocation, ComputeGrant, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering
from backend.seeds import SEEDS
from backend.seeds.compute import (
    LOCAL_OFFERING_NAME,
    LOCAL_OFFERING_RATE_NANOS,
    LOCALDEV_ENTRY,
    SELF_HOSTED_ENTRY,
    seed_compute,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._offering_helpers import delete_offerings_of_types

_KINDS = [EC2, LOCALDEV, RUNPOD, SELF_HOSTED]


@pytest.fixture(autouse=True)
def loaders(monkeypatch: pytest.MonkeyPatch) -> ExtensionPoint[Any]:
    """The open seed alone: a fresh loader point, whatever the suite installed."""
    point: ExtensionPoint[Any] = ExtensionPoint("compute_catalog_loaders")
    monkeypatch.setattr("backend.seeds.compute.COMPUTE_CATALOG_LOADERS", point)
    return point


@pytest_asyncio.fixture(autouse=True)
async def _clean_catalog(real_session: AsyncSession) -> AsyncIterator[None]:
    """Each test starts with none of the kinds a seed could write."""
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(delete(MachineCredential))
    await real_session.execute(delete(ComputeAllocation))
    await real_session.execute(delete(ComputeGrant))
    await delete_offerings_of_types(real_session, _KINDS)
    await real_session.execute(
        delete(ComputeMachineType).where(ComputeMachineType.provider.in_(_KINDS))
    )
    await real_session.commit()
    yield


async def _rows(session: AsyncSession) -> dict[tuple[str, str], ComputeMachineType]:
    rows = await session.execute(
        select(ComputeMachineType).where(ComputeMachineType.provider.in_(_KINDS))
    )
    return {(r.provider, r.provider_type_id): r for r in rows.scalars().all()}


async def _offerings_of(session: AsyncSession, provider: str) -> list[ComputeOffering]:
    rows = await session.execute(
        select(ComputeOffering)
        .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
        .where(ComputeMachineType.provider == provider)
    )
    return list(rows.scalars().all())


def test_the_seed_is_registered() -> None:
    assert "compute_catalog" in [name for name, _ in SEEDS]


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        pytest.param(
            "local",
            {(SELF_HOSTED, "self-hosted"), (LOCALDEV, LOCALDEV_ENTRY.provider_type_id)},
            id="local",
        ),
        pytest.param("staging", {(SELF_HOSTED, "self-hosted")}, id="cloud"),
    ],
)
async def test_the_open_seed_writes_no_provider_flavor(
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
    expected: set[tuple[str, str]],
) -> None:
    """No RunPod or EC2 type: an open install has no account to start one."""
    monkeypatch.setattr(settings, "app_env", app_env)

    await seed_compute(real_session)
    await real_session.commit()

    assert set(await _rows(real_session)) == expected


async def test_the_self_hosted_type_is_registrable_and_never_offered(
    real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    await seed_compute(real_session)
    await real_session.commit()

    row = (await _rows(real_session))[(SELF_HOSTED, SELF_HOSTED_ENTRY.provider_type_id)]
    assert (row.active, row.available_for_new, row.provider_price_per_minute_nanos) == (
        True,
        False,
        0,
    )
    assert await _offerings_of(real_session, SELF_HOSTED) == []


async def test_the_seed_is_idempotent(
    real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    first = await seed_compute(real_session)
    await real_session.commit()
    ids = {key: row.id for key, row in (await _rows(real_session)).items()}

    second = await seed_compute(real_session)
    await real_session.commit()

    assert first == "created: 2, updated: 0, unchanged: 0; offerings created: 1"
    assert second == "created: 0, updated: 0, unchanged: 2"
    assert {key: row.id for key, row in (await _rows(real_session)).items()} == ids


async def test_a_local_deployment_sells_the_local_box_at_a_visible_fixed_rate(
    real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    await seed_compute(real_session)
    await real_session.commit()

    (local,) = await _offerings_of(real_session, LOCALDEV)
    assert (local.name, local.pricing_mode, local.fixed_rate_per_minute_nanos) == (
        LOCAL_OFFERING_NAME,
        "fixed",
        LOCAL_OFFERING_RATE_NANOS,
    )
    assert (local.audience, local.purchasable, local.retired_at) == ("all", True, None)


async def test_a_reseed_keeps_an_offering_a_developer_changed(
    real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "local")
    await seed_compute(real_session)
    await real_session.commit()
    (local,) = await _offerings_of(real_session, LOCALDEV)
    local.fixed_rate_per_minute_nanos = 5
    await real_session.commit()

    await seed_compute(real_session)
    await real_session.commit()

    (after,) = await _offerings_of(real_session, LOCALDEV)
    assert (after.id, after.fixed_rate_per_minute_nanos) == (local.id, 5)


async def test_a_registered_catalog_loader_runs_after_the_open_rows(
    real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, loaders: ExtensionPoint[Any]
) -> None:
    """How a distribution adds what it sells: the step sees the open rows and
    its summary joins the seed's."""
    seen: list[set[tuple[str, str]]] = []

    async def step(session: AsyncSession) -> str:
        seen.append(set(await _rows(session)))
        return "hosted: 0"

    loaders.register(step)
    monkeypatch.setattr(settings, "app_env", "staging")

    summary = await seed_compute(real_session)

    assert seen == [{(SELF_HOSTED, SELF_HOSTED_ENTRY.provider_type_id)}]
    assert summary.endswith("; hosted: 0")
