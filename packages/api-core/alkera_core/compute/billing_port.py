"""What compute admission asks of billing, as an extension point.

Deciding whether an org may hold, buy or pin a machine needs facts only a
billing system knows: how many machines the org's plan admits, whether it holds
an Enterprise plan, which account a priced machine bills when its grant names
none, and whether that account can cover a minute. (Whether the org may use its
own machine pool is read through the org entitlements port,
``alkera_core.org_entitlements``.) Compute reads them through
:class:`ComputeBilling`; the product registers its billing-backed
implementation into :data:`COMPUTE_BILLING` at composition, and compute never
imports billing itself. The point admits one implementation, so admission never
depends on registration order.

With nothing registered, :data:`UNBILLED` answers, matching a self-hosted
install: every org is its operator's own, on the Enterprise plan with the
Enterprise machine quota (``MACHINE_QUOTA_ENTERPRISE``, set by the operator).
There is nothing to bill a priced machine to, so a grant with a nonzero rate is
refused as unfundable while a free (rate 0) one starts.

Which account funds a grant or an allocation is billing's record; no compute
table names a billing account. Compute reads and writes it through
:class:`ComputeFunding`, a separate point because the worker writes it too (the
org machine reconcile funds every allocation it makes) and registers no
admission. With nothing registered, :data:`UNFUNDED` answers: nothing is
funded, and naming an account to fund from is refused.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import settings
from alkera_core.extensions import ExtensionError, ExtensionPoint


class ComputeBilling(Protocol):
    """The billing facts compute admission reads."""

    async def machine_quota(self, db: AsyncSession, org_id: UUID) -> int:
        """How many machines the org's plan lets it hold."""
        ...

    async def holds_enterprise_plan(self, db: AsyncSession, org_id: UUID) -> bool:
        """Whether the org holds an Enterprise plan on a hosted deployment."""
        ...

    async def default_funding_account(
        self, db: AsyncSession, *, user_id: UUID | None, org_id: UUID
    ) -> UUID | None:
        """The account a grant without its own funding bills, or ``None``."""
        ...

    async def can_cover(self, db: AsyncSession, account_id: UUID, nanos: int) -> bool:
        """Whether one of the account's balances has ``nanos`` available."""
        ...


class UnbilledCompute:
    """The answers of a deployment with no billing system registered."""

    async def machine_quota(self, db: AsyncSession, org_id: UUID) -> int:
        return settings.machine_quota_enterprise

    async def holds_enterprise_plan(self, db: AsyncSession, org_id: UUID) -> bool:
        return True

    async def default_funding_account(
        self, db: AsyncSession, *, user_id: UUID | None, org_id: UUID
    ) -> UUID | None:
        return None

    async def can_cover(self, db: AsyncSession, account_id: UUID, nanos: int) -> bool:
        return False


UNBILLED: Final[ComputeBilling] = UnbilledCompute()


class ComputeFunding(Protocol):
    """Which billing account a compute grant or allocation is funded from."""

    async def grant_funding(self, db: AsyncSession, grant_id: UUID) -> UUID | None:
        """The account the grant names to fund its machines, or ``None``."""
        ...

    async def set_grant_funding(
        self, db: AsyncSession, grant_id: UUID, account_id: UUID | None
    ) -> None:
        """Name the account the grant funds its machines from; ``None`` clears it."""
        ...

    async def allocation_funding(self, db: AsyncSession, allocation_id: UUID) -> UUID | None:
        """The account the allocation's minutes debit, or ``None`` (unbillable)."""
        ...

    async def allocation_funding_many(
        self, db: AsyncSession, allocation_ids: Collection[UUID]
    ) -> dict[UUID, UUID]:
        """The funding account of each of ``allocation_ids`` that has one."""
        ...

    async def fund_allocation(
        self, db: AsyncSession, allocation_id: UUID, account_id: UUID | None
    ) -> None:
        """Debit the allocation's minutes from ``account_id``; ``None`` clears it."""
        ...


class UnfundedComputeError(RuntimeError):
    """An account was named to fund compute on a deployment with no billing."""


class UnfundedCompute:
    """The answers of a deployment with no billing system: nothing is funded."""

    async def grant_funding(self, db: AsyncSession, grant_id: UUID) -> UUID | None:
        return None

    async def set_grant_funding(
        self, db: AsyncSession, grant_id: UUID, account_id: UUID | None
    ) -> None:
        _refuse_account(account_id)

    async def allocation_funding(self, db: AsyncSession, allocation_id: UUID) -> UUID | None:
        return None

    async def allocation_funding_many(
        self, db: AsyncSession, allocation_ids: Collection[UUID]
    ) -> dict[UUID, UUID]:
        return {}

    async def fund_allocation(
        self, db: AsyncSession, allocation_id: UUID, account_id: UUID | None
    ) -> None:
        _refuse_account(account_id)


def _refuse_account(account_id: UUID | None) -> None:
    if account_id is not None:
        raise UnfundedComputeError(
            f"billing account {account_id} was named to fund compute, but no billing "
            "system is registered to record it"
        )


UNFUNDED: Final[ComputeFunding] = UnfundedCompute()

#: The billing system compute admission reads. At most one registers.
COMPUTE_BILLING: ExtensionPoint[ComputeBilling] = ExtensionPoint("compute_billing")


def compute_billing(
    point: ExtensionPoint[ComputeBilling] = COMPUTE_BILLING,
) -> ComputeBilling:
    """The billing system registered on ``point``, or :data:`UNBILLED`. Freezes
    the point. Callers pass nothing; a test passes a point of its own so it never
    freezes the process-wide one before a composition root installs into it."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} billing systems registered on {point.name!r}; "
            "compute admission takes exactly one"
        )
    return registered[0] if registered else UNBILLED


#: The billing system that records compute funding. At most one registers.
COMPUTE_FUNDING: ExtensionPoint[ComputeFunding] = ExtensionPoint("compute_funding")


def compute_funding(
    point: ExtensionPoint[ComputeFunding] = COMPUTE_FUNDING,
) -> ComputeFunding:
    """The funding record registered on ``point``, or :data:`UNFUNDED`. Freezes
    the point; a test passes a point of its own, as for :func:`compute_billing`."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} funding records registered on {point.name!r}; "
            "compute takes exactly one"
        )
    return registered[0] if registered else UNFUNDED


__all__ = [
    "COMPUTE_BILLING",
    "COMPUTE_FUNDING",
    "UNBILLED",
    "UNFUNDED",
    "ComputeBilling",
    "ComputeFunding",
    "UnbilledCompute",
    "UnfundedCompute",
    "UnfundedComputeError",
    "compute_billing",
    "compute_funding",
]
