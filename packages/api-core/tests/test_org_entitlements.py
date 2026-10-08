"""What the platform reads about an org when no plan system registers, and how
the point that carries a plan system behaves.

Every case builds a point of its own: reading the process-wide point would
freeze it before the backend's composition root installs into it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from alkera_core.extensions import ExtensionError, ExtensionPoint
from alkera_core.org_entitlements import (
    SELF_HOSTED,
    OrgEntitlements,
    PlatformSpend,
    UserSpend,
    org_entitlements,
)
from alkera_core.sandbox_tiers import resolve_limits
from sqlalchemy.ext.asyncio import AsyncSession

_ORG = uuid4()
_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class _Untouchable:
    """A session that fails on any use: the open answers read nothing and
    write nothing, so a self-hosted seat or team never reaches the database."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the open default used the session ({name})")


_DB = cast(AsyncSession, _Untouchable())


class _Plans:
    """A stand-in plan system with answers no open default gives."""

    async def plan(self, db: AsyncSession, org_id: UUID) -> str:
        return "free"

    async def enterprise_features(self, db: AsyncSession, org_id: UUID | None) -> bool:
        return False

    async def plan_storage_bytes(self, db: AsyncSession, org_id: UUID) -> int | None:
        return 10

    async def open_seat(
        self, db: AsyncSession, *, user_id: UUID, org_id: UUID, now: datetime | None = None
    ) -> None:
        return None

    async def team_holds_credit(self, db: AsyncSession, team_id: UUID) -> bool:
        return True

    async def user_spend_this_month(
        self, db: AsyncSession, *, now: datetime
    ) -> Mapping[UUID, UserSpend]:
        return {_ORG: UserSpend(billed_nanos=1, request_count=1)}

    async def platform_spend(self, db: AsyncSession, *, now: datetime) -> PlatformSpend:
        return PlatformSpend(today_nanos=1, month_to_date_nanos=2, monthly_cap_nanos=3)


def _point() -> ExtensionPoint[OrgEntitlements]:
    return ExtensionPoint("org_entitlements_under_test")


def test_with_nothing_registered_the_self_hosted_answers_stand() -> None:
    assert org_entitlements(_point()) is SELF_HOSTED


def test_a_registered_plan_system_answers_instead() -> None:
    point = _point()
    plans = _Plans()
    point.register(plans)
    assert org_entitlements(point) is plans


def test_two_plan_systems_are_refused_rather_than_ordered() -> None:
    point = _point()
    point.register(_Plans())
    point.register(_Plans())
    with pytest.raises(ExtensionError, match="exactly one"):
        org_entitlements(point)


def test_registering_after_the_platform_read_the_point_is_refused() -> None:
    point = _point()
    org_entitlements(point)
    with pytest.raises(ExtensionError, match="already read"):
        point.register(_Plans())


async def test_a_self_hosted_org_is_held_to_no_paid_tier_sandbox() -> None:
    """The self-hosted plan is the one the sandbox table gives no figure of its
    own; any other key, a typo included, would fail closed to the smallest
    tier and squeeze every self-hosted chat into a free seat's sandbox."""
    plan = await SELF_HOSTED.plan(_DB, _ORG)
    assert resolve_limits(plan, override_vcpu=None, override_memory_mb=None) == (None, None)


async def test_a_self_hosted_org_has_every_enterprise_feature() -> None:
    assert await SELF_HOSTED.enterprise_features(_DB, _ORG) is True
    assert await SELF_HOSTED.enterprise_features(_DB, None) is True


async def test_a_self_hosted_plan_sets_no_storage_figure_so_the_drive_default_binds() -> None:
    assert await SELF_HOSTED.plan_storage_bytes(_DB, _ORG) is None


async def test_a_self_hosted_seat_provisions_nothing() -> None:
    assert await SELF_HOSTED.open_seat(_DB, user_id=uuid4(), org_id=_ORG, now=_NOW) is None


async def test_no_self_hosted_team_holds_credit() -> None:
    assert await SELF_HOSTED.team_holds_credit(_DB, uuid4()) is False


async def test_nothing_is_spent_where_nothing_meters() -> None:
    assert await SELF_HOSTED.user_spend_this_month(_DB, now=_NOW) == {}
    assert await SELF_HOSTED.platform_spend(_DB, now=_NOW) == PlatformSpend(
        today_nanos=0, month_to_date_nanos=0, monthly_cap_nanos=None
    )
