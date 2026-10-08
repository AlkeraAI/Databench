"""The admin ops summary: machines, chats served, gateway spend, the platform
cap, crash reports and deployment health, read in one pass.

Read-only. The deployment checks run in-process under a short budget so the
page, which refreshes every 30 s, never waits on a slow dependency;
:data:`HEALTH_CHECKS` is the seam a test swaps for its own checks.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from alkera_core.compute.machines import live_workspace_machines, machine_state
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.deployment_health import Check, CheckContext, registered_checks, run_all_checks
from alkera_core.http import async_client
from alkera_core.models import ComputeAllocation, ComputeMachineType, CrashReport
from alkera_core.org_entitlements import org_entitlements
from alkera_core.schemas.system.ops import (
    OpsCount,
    OpsHealth,
    OpsHealthCheck,
    OpsMachine,
    OpsSpend,
    OpsSummary,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute.machines import ChatLoad, chat_load
from backend.services.files import admin_store

#: The checks the summary runs. ``None`` is the full deployment registry.
HEALTH_CHECKS: Sequence[Check] | None = None
#: The whole health run's budget; past it the summary reports one failed row.
HEALTH_BUDGET_S = 4.0

_SEVERITY = {"ok": 0, "warn": 1, "fail": 2}


async def _machines(db: AsyncSession) -> list[OpsMachine]:
    rows = (
        await db.execute(
            live_workspace_machines(eligible=False)
            .add_columns(ComputeMachineType.provider)
            .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
        )
    ).all()
    # The same split the machines console reads, so the total here is the sum
    # of the "served" figures there: a chat its box parked is not load.
    load = await chat_load(db, [alloc.id for alloc, _ in rows])
    return [
        OpsMachine(
            id=str(alloc.id),
            name=alloc.name,
            org_id=str(alloc.org_team_id),
            provider=provider,
            tenancy=alloc.tenancy,
            state=machine_state(alloc),
            chats_served=load.get(alloc.id, ChatLoad()).served,
        )
        for alloc, provider in rows
    ]


def _counts(values: Sequence[str]) -> list[OpsCount]:
    return [OpsCount(key=k, count=n) for k, n in sorted(Counter(values).items())]


async def _spend(db: AsyncSession, now: datetime) -> OpsSpend:
    spend = await org_entitlements().platform_spend(db, now=now)
    cap = spend.monthly_cap_nanos
    return OpsSpend(
        today_nanos=spend.today_nanos,
        month_to_date_nanos=spend.month_to_date_nanos,
        monthly_cap_nanos=cap,
        cap_headroom_nanos=None if cap is None else cap - spend.month_to_date_nanos,
    )


async def _crashes(db: AsyncSession, now: datetime) -> tuple[int, int]:
    recent = (
        await db.execute(
            select(func.count())
            .select_from(CrashReport)
            .where(CrashReport.created_at >= now - timedelta(hours=24))
        )
    ).scalar_one()
    unread = (
        await db.execute(
            select(func.count()).select_from(CrashReport).where(CrashReport.read_at.is_(None))
        )
    ).scalar_one()
    return int(recent), int(unread)


async def health() -> OpsHealth:
    """Run the deployment checks now, bounded by :data:`HEALTH_BUDGET_S`."""
    client = async_client(timeout=HEALTH_BUDGET_S)
    try:
        ctx = CheckContext(
            trigger="manual",
            session_factory=AsyncSessionLocal,
            http_client=client,
            files_admin_store=admin_store,
        )
        checks = HEALTH_CHECKS if HEALTH_CHECKS is not None else registered_checks()
        try:
            results = await asyncio.wait_for(run_all_checks(ctx, checks), HEALTH_BUDGET_S)
            rows = [
                OpsHealthCheck(key=r.key, label=r.label, status=r.status, detail=r.detail)
                for r in results
                if r.org_team_id is None
            ]
        except TimeoutError:
            rows = [
                OpsHealthCheck(
                    key="run", label="Health run", status="fail", detail="The checks timed out"
                )
            ]
    finally:
        await client.aclose()
    worst = max((_SEVERITY[r.status] for r in rows if r.status != "skipped"), default=0)
    overall: tuple[Literal["ok"], Literal["warn"], Literal["fail"]] = ("ok", "warn", "fail")
    return OpsHealth(overall=overall[worst], checks=rows)


async def summary(db: AsyncSession, *, now: datetime | None = None) -> OpsSummary:
    at = now or datetime.now(UTC)
    machines = await _machines(db)
    recent, unread = await _crashes(db, at)
    return OpsSummary(
        machines=machines,
        machines_by_state=_counts([m.state for m in machines]),
        machines_by_provider=_counts([m.provider for m in machines]),
        chats_served_total=sum(m.chats_served for m in machines),
        spend=await _spend(db, at),
        crash_reports_24h=recent,
        crash_reports_unread=unread,
        health=await health(),
        build_id=settings.build_id,
        app_version=settings.app_version,
    )
