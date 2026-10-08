"""What compute admission reads from billing when no billing system registers,
what compute's funding record answers when none registers, and how the points
that carry them behave.

Every case builds a point of its own: reading the process-wide point would
freeze it before the backend's composition root installs into it.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import cast
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.billing_port import (
    UNBILLED,
    UNFUNDED,
    ComputeBilling,
    ComputeFunding,
    UnfundedComputeError,
    compute_billing,
    compute_funding,
)
from alkera_core.config import settings
from alkera_core.extensions import ExtensionError, ExtensionPoint
from sqlalchemy.ext.asyncio import AsyncSession

_ORG = uuid4()
#: The open answers never touch the session.
_DB = cast(AsyncSession, None)


class _Plans:
    """A stand-in billing system with answers no open default gives."""

    async def machine_quota(self, db: AsyncSession, org_id: UUID) -> int:
        return 7

    async def holds_enterprise_plan(self, db: AsyncSession, org_id: UUID) -> bool:
        return False

    async def default_funding_account(
        self, db: AsyncSession, *, user_id: UUID | None, org_id: UUID
    ) -> UUID | None:
        return org_id

    async def can_cover(self, db: AsyncSession, account_id: UUID, nanos: int) -> bool:
        return True


def _point() -> ExtensionPoint[ComputeBilling]:
    return ExtensionPoint("compute_billing_under_test")


def test_with_nothing_registered_the_unbilled_answers_stand() -> None:
    assert compute_billing(_point()) is UNBILLED


def test_a_registered_billing_system_answers_instead() -> None:
    point = _point()
    plans = _Plans()
    point.register(plans)
    assert compute_billing(point) is plans


def test_two_billing_systems_are_refused_rather_than_ordered() -> None:
    point = _point()
    point.register(_Plans())
    point.register(_Plans())
    with pytest.raises(ExtensionError, match="exactly one"):
        compute_billing(point)


def test_registering_after_admission_read_the_point_is_refused() -> None:
    point = _point()
    compute_billing(point)
    with pytest.raises(ExtensionError, match="already read"):
        point.register(_Plans())


async def test_unbilled_quota_is_the_enterprise_quota_the_operator_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "machine_quota_enterprise", 13)
    monkeypatch.setattr(settings, "machine_quota_free", 2)
    assert await UNBILLED.machine_quota(_DB, _ORG) == 13


async def test_unbilled_orgs_stand_as_enterprise() -> None:
    assert await UNBILLED.holds_enterprise_plan(_DB, _ORG) is True


async def test_unbilled_compute_has_no_account_to_bill_and_covers_nothing() -> None:
    assert await UNBILLED.default_funding_account(_DB, user_id=uuid4(), org_id=_ORG) is None
    assert await UNBILLED.can_cover(_DB, uuid4(), 0) is False


class _Ledger:
    """A stand-in funding record: every grant and allocation funded from one account."""

    def __init__(self) -> None:
        self.account = uuid4()

    async def grant_funding(self, db: AsyncSession, grant_id: UUID) -> UUID | None:
        return self.account

    async def set_grant_funding(
        self, db: AsyncSession, grant_id: UUID, account_id: UUID | None
    ) -> None:
        return None

    async def allocation_funding(self, db: AsyncSession, allocation_id: UUID) -> UUID | None:
        return self.account

    async def allocation_funding_many(
        self, db: AsyncSession, allocation_ids: Collection[UUID]
    ) -> dict[UUID, UUID]:
        return dict.fromkeys(allocation_ids, self.account)

    async def fund_allocation(
        self, db: AsyncSession, allocation_id: UUID, account_id: UUID | None
    ) -> None:
        return None


def _funding_point() -> ExtensionPoint[ComputeFunding]:
    return ExtensionPoint("compute_funding_under_test")


def test_with_nothing_registered_nothing_is_funded() -> None:
    assert compute_funding(_funding_point()) is UNFUNDED


def test_a_registered_funding_record_answers_instead() -> None:
    point = _funding_point()
    ledger = _Ledger()
    point.register(ledger)
    assert compute_funding(point) is ledger


def test_two_funding_records_are_refused_rather_than_ordered() -> None:
    point = _funding_point()
    point.register(_Ledger())
    point.register(_Ledger())
    with pytest.raises(ExtensionError, match="exactly one"):
        compute_funding(point)


def test_registering_a_funding_record_after_compute_read_the_point_is_refused() -> None:
    point = _funding_point()
    compute_funding(point)
    with pytest.raises(ExtensionError, match="already read"):
        point.register(_Ledger())


async def test_unfunded_compute_reads_no_funding_account() -> None:
    assert await UNFUNDED.grant_funding(_DB, uuid4()) is None
    assert await UNFUNDED.allocation_funding(_DB, uuid4()) is None
    assert await UNFUNDED.allocation_funding_many(_DB, [uuid4(), uuid4()]) == {}


async def test_unfunded_compute_takes_no_account_as_a_no_op() -> None:
    await UNFUNDED.set_grant_funding(_DB, uuid4(), None)
    await UNFUNDED.fund_allocation(_DB, uuid4(), None)


@pytest.mark.parametrize("target", ["grant", "allocation"])
async def test_unfunded_compute_refuses_to_name_an_account(target: str) -> None:
    """A deployment with no billing has no account to fund from, so being told
    one is a wiring fault, never something to drop silently."""
    account = uuid4()
    with pytest.raises(UnfundedComputeError, match=str(account)):
        if target == "grant":
            await UNFUNDED.set_grant_funding(_DB, uuid4(), account)
        else:
            await UNFUNDED.fund_allocation(_DB, uuid4(), account)
