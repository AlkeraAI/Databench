"""What the open platform gets from the compute meter when no biller registers,
and how the point that carries a biller behaves.

Every case builds a point of its own: reading the process-wide point would
freeze it before a composition root installs into it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.meter import (
    UNMETERED,
    ComputeMetering,
    HeartbeatWatch,
    MeterSummary,
    RunwayRefusal,
    compute_metering,
)
from alkera_core.compute.notices import MachineNotice, NoticeSender, notice_sender
from alkera_core.compute.provider import ComputeProvider
from alkera_core.compute.runway import CreditState
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.org_machines import OrgMachine
from sqlalchemy.ext.asyncio import AsyncSession

#: The open answers never touch the session, the provider or the rows.
_DB = cast(AsyncSession, None)
_PROVIDER = cast(ComputeProvider, None)
_NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


class _Biller:
    """A stand-in biller whose answers no open default gives."""

    async def meter_and_cutoff(
        self,
        db: AsyncSession,
        *,
        provider: ComputeProvider,
        now: datetime | None,
        max_minutes: int | None,
        reason: str,
        watch: HeartbeatWatch | None,
        send_notice: NoticeSender | None,
    ) -> MeterSummary:
        return MeterSummary(checked=1)

    async def settle_final(
        self,
        db: AsyncSession,
        alloc: ComputeAllocation,
        *,
        now: datetime,
        max_minutes: int | None,
        reason: str,
    ) -> None:
        return None

    async def machine_funding_account(
        self, db: AsyncSession, *, org_id: UUID, owner_team_id: UUID
    ) -> UUID | None:
        return org_id

    async def machine_runway(
        self, db: AsyncSession, om: OrgMachine
    ) -> tuple[int | None, CreditState]:
        return 5, "urgent"

    async def carry_start_runway(
        self,
        db: AsyncSession,
        *,
        account_id: UUID,
        owner_team_id: UUID,
        org_id: UUID,
        need_nanos: int,
        request_id: str,
        now: datetime,
    ) -> RunwayRefusal | None:
        return None

    async def spend_this_cycle(
        self,
        db: AsyncSession,
        *,
        org_id: UUID,
        machine_ids: Sequence[UUID],
        now: datetime | None,
    ) -> dict[UUID, int]:
        return dict.fromkeys(machine_ids, 9)

    def notice_sender(self) -> NoticeSender | None:
        return None

    def add_credit_url(self, org_id: UUID) -> str | None:
        return f"https://bill.example/{org_id}"


def _point() -> ExtensionPoint[ComputeMetering]:
    return ExtensionPoint("compute_metering_under_test")


def test_with_nothing_registered_machines_run_unmetered() -> None:
    assert compute_metering(_point()) is UNMETERED


def test_a_registered_biller_answers_instead() -> None:
    point = _point()
    biller = _Biller()
    point.register(biller)
    assert compute_metering(point) is biller


def test_two_billers_are_refused_rather_than_ordered() -> None:
    point = _point()
    point.register(_Biller())
    point.register(_Biller())
    with pytest.raises(ExtensionError, match="exactly one"):
        compute_metering(point)


def test_registering_after_the_tick_read_the_point_is_refused() -> None:
    point = _point()
    compute_metering(point)
    with pytest.raises(ExtensionError, match="already read"):
        point.register(_Biller())


async def test_an_unmetered_tick_checks_bills_and_stops_nothing() -> None:
    summary = await UNMETERED.meter_and_cutoff(
        _DB,
        provider=_PROVIDER,
        now=_NOW,
        max_minutes=None,
        reason="compute",
        watch=None,
        send_notice=None,
    )
    assert summary == MeterSummary()


async def test_unmetered_machines_have_no_account_no_known_runway_and_no_spend() -> None:
    om = cast(OrgMachine, None)
    machines = [uuid4(), uuid4()]
    assert (
        await UNMETERED.machine_funding_account(_DB, org_id=uuid4(), owner_team_id=uuid4()) is None
    )
    assert await UNMETERED.machine_runway(_DB, om) == (None, "ok")
    assert await UNMETERED.spend_this_cycle(
        _DB, org_id=uuid4(), machine_ids=machines, now=_NOW
    ) == dict.fromkeys(machines, 0)
    assert UNMETERED.add_credit_url(uuid4()) is None
    assert UNMETERED.notice_sender() is None


async def test_without_a_biller_no_start_runway_can_be_carried() -> None:
    refusal = await UNMETERED.carry_start_runway(
        _DB,
        account_id=uuid4(),
        owner_team_id=uuid4(),
        org_id=uuid4(),
        need_nanos=1,
        request_id="compute-admit:new:x",
        now=_NOW,
    )
    assert refusal == "insufficient"


async def _deliver(_db: AsyncSession, _notice: MachineNotice) -> None:
    return None


async def _deliver_too(_db: AsyncSession, _notice: MachineNotice) -> None:
    return None


def _senders(*senders: NoticeSender) -> ExtensionPoint[NoticeSender]:
    point: ExtensionPoint[NoticeSender] = ExtensionPoint(f"probe-senders-{uuid4()}")
    for sender in senders:
        point.register(sender)
    return point


def test_a_process_with_no_notice_sender_drops_notices() -> None:
    assert notice_sender(_senders()) is None


def test_the_registered_notice_sender_is_the_one_used() -> None:
    assert notice_sender(_senders(_deliver)) is _deliver


def test_two_notice_senders_are_a_composition_error() -> None:
    with pytest.raises(ExtensionError, match="more than one"):
        notice_sender(_senders(_deliver, _deliver_too))
