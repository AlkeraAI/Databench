"""Whether a machine's paying account can carry its start.

A machine starts only when its paying account holds
``machine_start_runway_minutes`` of compute plus storage at the rates it will
pin, and the owning team's budget allocation has that much left.
:func:`runway_shortfall` is that check for every path that starts one: a
manager's purchase, start, wake or replace
(``backend.services.compute.org_admission``) and a replacement the org
machine reconcile makes on its own (``alkera_core.compute.org_reconcile``).
The credit itself is the compute biller's to read
(``compute_metering().carry_start_runway``, a real reserve of the whole
runway, released at once); a deployment with no biller can carry nothing.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.compute.meter import compute_metering
from alkera_core.config import settings


class RunwayShortfall(enum.StrEnum):
    """Why a start's runway cannot be carried."""

    #: Nothing pays for the machine.
    NO_ACCOUNT = "no_account"
    #: The owning team's budget allocation has less left than the runway.
    TEAM_BUDGET = "team_budget"
    #: The paying account's machine credit is short of the runway.
    CREDIT = "insufficient"


def start_runway_nanos(rate_per_minute_nanos: int, storage_rate_per_minute_nanos: int) -> int:
    """What the paying account must hold to start a machine at these rates:
    ``machine_start_runway_minutes`` of compute and storage."""
    return settings.machine_start_runway_minutes * (
        rate_per_minute_nanos + storage_rate_per_minute_nanos
    )


async def runway_shortfall(
    db: AsyncSession,
    *,
    account_id: UUID | None,
    owner_team_id: UUID,
    org_id: UUID,
    need_nanos: int,
    request_id: str,
    now: datetime,
) -> RunwayShortfall | None:
    """Why ``need_nanos`` of runway cannot be carried now, or ``None`` when it
    can (a free machine needs none). Holds nothing afterwards."""
    if need_nanos <= 0:
        return None
    if account_id is None:
        return RunwayShortfall.NO_ACCOUNT
    refusal = await compute_metering().carry_start_runway(
        db,
        account_id=account_id,
        owner_team_id=owner_team_id,
        org_id=org_id,
        need_nanos=need_nanos,
        request_id=request_id,
        now=now,
    )
    if refusal is None:
        return None
    return RunwayShortfall.TEAM_BUDGET if refusal == "team_budget" else RunwayShortfall.CREDIT


__all__ = ["RunwayShortfall", "runway_shortfall", "start_runway_nanos"]
