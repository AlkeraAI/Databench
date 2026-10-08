"""What an org may do, and what it has spent against its caps, as an extension point.

The open platform asks a handful of questions only a plan system can answer:
which plan an org is on, whether it may use Enterprise-only surfaces (SSO, SCIM,
the audit log, its own machine pool), how much its plan lets it store, whether
a team still holds money that deleting it would destroy, and what people and
the platform have spent this month. A person joining an org also opens a seat
there, which the plan system may need to provision. The platform reads all of
it through :class:`OrgEntitlements`; the product registers its billing-backed
implementation into :data:`ORG_ENTITLEMENTS` at composition, and no open module
imports billing to answer.

With nothing registered, :data:`SELF_HOSTED` answers. A deployment without a
plan system is its operator's own install, the posture billing already takes on
a self-hosted deployment: every org is on the top plan (:data:`SELF_HOSTED_PLAN`,
the key the sandbox and storage tables treat as Enterprise), has every
Enterprise feature, and stores up to the figure its drive was created with
(``FILES_QUOTA_DEFAULT_BYTES``). Opening a seat provisions nothing, no team
holds money, and nothing has been spent because nothing meters.

The point admits one implementation: two plan systems answering the same
question would leave the answer to registration order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.extensions import ExtensionError, ExtensionPoint

#: The plan key of an org on a deployment with no plan system: the top plan.
SELF_HOSTED_PLAN: Final = "enterprise"


@dataclass(frozen=True, slots=True)
class UserSpend:
    """What one person's requests were billed over a window, and how many."""

    billed_nanos: int
    request_count: int


@dataclass(frozen=True, slots=True)
class PlatformSpend:
    """The platform's billed spend today and this month, against its monthly
    cap. ``monthly_cap_nanos`` is ``None`` when no cap applies."""

    today_nanos: int
    month_to_date_nanos: int
    monthly_cap_nanos: int | None


class OrgEntitlements(Protocol):
    """The plan facts the open platform reads."""

    async def plan(self, db: AsyncSession, org_id: UUID) -> str:
        """The key of the plan the org is on."""
        ...

    async def enterprise_features(self, db: AsyncSession, org_id: UUID | None) -> bool:
        """Whether the org may use Enterprise-only surfaces: SSO and SCIM, the
        audit log, and its own machine pool."""
        ...

    async def plan_storage_bytes(self, db: AsyncSession, org_id: UUID) -> int | None:
        """How many bytes the org's plan lets it store, or ``None`` when the plan
        sets no figure and the drive's own applies."""
        ...

    async def open_seat(
        self, db: AsyncSession, *, user_id: UUID, org_id: UUID, now: datetime | None = None
    ) -> None:
        """Provision the seat a person takes when they join an org, in the
        caller's transaction. Idempotent: a seat that exists is left alone."""
        ...

    async def team_holds_credit(self, db: AsyncSession, team_id: UUID) -> bool:
        """Whether the team still holds money, or a record of it, that deleting
        the team would destroy."""
        ...

    async def user_spend_this_month(
        self, db: AsyncSession, *, now: datetime
    ) -> Mapping[UUID, UserSpend]:
        """Every person's billed spend since the start of ``now``'s UTC month.
        A person with no spend is absent."""
        ...

    async def platform_spend(self, db: AsyncSession, *, now: datetime) -> PlatformSpend:
        """The whole platform's billed spend at ``now``, against its cap."""
        ...


class SelfHostedEntitlements:
    """The answers of a deployment with no plan system registered."""

    async def plan(self, db: AsyncSession, org_id: UUID) -> str:
        return SELF_HOSTED_PLAN

    async def enterprise_features(self, db: AsyncSession, org_id: UUID | None) -> bool:
        return True

    async def plan_storage_bytes(self, db: AsyncSession, org_id: UUID) -> int | None:
        return None

    async def open_seat(
        self, db: AsyncSession, *, user_id: UUID, org_id: UUID, now: datetime | None = None
    ) -> None:
        return None

    async def team_holds_credit(self, db: AsyncSession, team_id: UUID) -> bool:
        return False

    async def user_spend_this_month(
        self, db: AsyncSession, *, now: datetime
    ) -> Mapping[UUID, UserSpend]:
        return {}

    async def platform_spend(self, db: AsyncSession, *, now: datetime) -> PlatformSpend:
        return PlatformSpend(today_nanos=0, month_to_date_nanos=0, monthly_cap_nanos=None)


SELF_HOSTED: Final[OrgEntitlements] = SelfHostedEntitlements()

#: The plan system the platform reads. At most one registers.
ORG_ENTITLEMENTS: ExtensionPoint[OrgEntitlements] = ExtensionPoint("org_entitlements")


def org_entitlements(
    point: ExtensionPoint[OrgEntitlements] = ORG_ENTITLEMENTS,
) -> OrgEntitlements:
    """The plan system registered on ``point``, or :data:`SELF_HOSTED`. Freezes
    the point. Callers pass nothing; a test passes a point of its own so it never
    freezes the process-wide one before a composition root installs into it."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} plan systems registered on {point.name!r}; "
            "the platform takes exactly one"
        )
    return registered[0] if registered else SELF_HOSTED


__all__ = [
    "ORG_ENTITLEMENTS",
    "SELF_HOSTED",
    "SELF_HOSTED_PLAN",
    "OrgEntitlements",
    "PlatformSpend",
    "SelfHostedEntitlements",
    "UserSpend",
    "org_entitlements",
]
