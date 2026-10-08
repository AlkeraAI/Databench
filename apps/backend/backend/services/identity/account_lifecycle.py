"""The account lifecycle as the HTTP surface shows it.

The decisions (plan, rate limit, grace window, erasure) live in
``alkera_core.account``; this module turns a plan into what the confirmation
screen reads (org names, the recipient's name, one sentence per blocker), and
builds the read models.
"""

from __future__ import annotations

import uuid

from alkera_core.account.plan import compute_plan
from alkera_core.config import settings
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.models import AccountDeletionRequest, Team, User
from alkera_core.schemas.account import (
    DeletionPlan,
    DeletionPlanRead,
    DeletionStatusRead,
    PlanBlocker,
    PlanBlockerRead,
    PlannedOrgRead,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: What one blocker asks of the person, by code. ``{org}`` is the org's name.
BLOCKER_MESSAGES: dict[str, str] = {
    "last_admin": "Make another member an admin of {org}.",
    "live_compute": "Stop the machines running for you in {org}.",
    "org_machines": "Delete the machines in {org}.",
    "paid_plan": "Cancel the paid plan on your seat in {org}.",
    "unpaid_balance": "Pay the open balance on your seat in {org}.",
    "legal_hold": "Data in {org} is under a legal hold. Contact support.",
    "platform_staff": "Remove your platform staff role first.",
}

UNNAMED_ORG = "an unnamed organization"


async def _names(
    db: AsyncSession, org_ids: set[uuid.UUID], user_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    async with cross_tenant_write(db, reason="account.plan_names"):
        orgs = {
            team_id: name
            for team_id, name in (
                await db.execute(select(Team.id, Team.name).where(Team.id.in_(org_ids)))
            ).all()
        }
        users = {
            user.id: (user.display_name or user.email)
            for user in (await db.execute(select(User).where(User.id.in_(user_ids))))
            .scalars()
            .all()
        }
    return orgs, users


def blocker_read(blocker: PlanBlocker, org_names: dict[uuid.UUID, str]) -> PlanBlockerRead:
    name = org_names.get(blocker.org_id) if blocker.org_id is not None else None
    message = BLOCKER_MESSAGES[blocker.code].format(org=name or UNNAMED_ORG)
    return PlanBlockerRead(code=blocker.code, org_id=blocker.org_id, org_name=name, message=message)


async def present_plan(db: AsyncSession, user: User, plan: DeletionPlan) -> DeletionPlanRead:
    org_ids = {o.org_id for o in plan.orgs} | {b.org_id for b in plan.blockers if b.org_id}
    recipients = {o.transfer_to_user_id for o in plan.orgs if o.transfer_to_user_id}
    org_names, user_names = await _names(db, org_ids, recipients)
    return DeletionPlanRead(
        orgs=[
            PlannedOrgRead(
                org_id=o.org_id,
                org_name=org_names.get(o.org_id) or UNNAMED_ORG,
                fate=o.fate,
                transfer_to_name=(
                    user_names.get(o.transfer_to_user_id) if o.transfer_to_user_id else None
                ),
                shared_items=o.shared_items,
                private_items=o.private_items,
            )
            for o in plan.orgs
        ],
        blockers=[blocker_read(b, org_names) for b in plan.blockers],
        forfeited_credit_nanos=plan.forfeited_credit_nanos,
        can_proceed=plan.can_proceed,
        grace_days=settings.account_deletion_grace_days,
        reauth="password" if user.password_hash is not None else "recent_sign_in",
        mfa_required=user.mfa_enabled,
    )


async def plan_for(db: AsyncSession, user: User) -> DeletionPlanRead:
    return await present_plan(db, user, await compute_plan(db, user.id))


def deletion_read(request: AccountDeletionRequest) -> DeletionStatusRead:
    return DeletionStatusRead.model_validate(request, from_attributes=True)


__all__ = [
    "BLOCKER_MESSAGES",
    "blocker_read",
    "deletion_read",
    "plan_for",
    "present_plan",
]
