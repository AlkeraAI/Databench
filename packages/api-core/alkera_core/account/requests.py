"""Deletion requests: creating, cancelling and reading them.

The backend's routes and the platform support tool both come through here, so
the one-live-deletion rule and the grace window are decided in one place.
Each function works in the caller's transaction and never commits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.account.plan import compute_plan
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.models import AccountDeletionRequest
from alkera_core.schemas.account import DeletionPlan


class DeletionBlockedError(Exception):
    """The plan has blockers; nothing was scheduled."""

    def __init__(self, plan: DeletionPlan) -> None:
        self.plan = plan
        super().__init__("deletion blocked")


class DeletionAlreadyScheduledError(Exception):
    def __init__(self, request: AccountDeletionRequest) -> None:
        self.request = request
        super().__init__("a deletion is already scheduled")


async def _lock(db: AsyncSession, kind: str, user_id: uuid.UUID) -> None:
    """Serialize one person's requests of one kind, so two at once cannot both
    pass a check the other is about to invalidate."""
    await advisory_xact_lock(db, advisory_key(f"account:{kind}", user_id))


# --------------------------------------------------------------------------- #
# Deletions
# --------------------------------------------------------------------------- #


async def live_deletion(db: AsyncSession, user_id: uuid.UUID) -> AccountDeletionRequest | None:
    found: AccountDeletionRequest | None = await db.scalar(
        select(AccountDeletionRequest).where(
            AccountDeletionRequest.user_id == user_id,
            AccountDeletionRequest.status == "scheduled",
        )
    )
    return found


async def latest_deletion(db: AsyncSession, user_id: uuid.UUID) -> AccountDeletionRequest | None:
    found: AccountDeletionRequest | None = await db.scalar(
        select(AccountDeletionRequest)
        .where(AccountDeletionRequest.user_id == user_id)
        .order_by(AccountDeletionRequest.requested_at.desc())
        .limit(1)
    )
    return found


async def schedule_deletion(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    requested_by: uuid.UUID,
    source: str = "self",
    now: datetime | None = None,
    grace: timedelta | None = None,
) -> AccountDeletionRequest:
    """Schedule the account's erasure after the grace window.

    Refuses when one is already scheduled (:class:`DeletionAlreadyScheduledError`)
    or when the plan has blockers (:class:`DeletionBlockedError`): a request is
    only ever accepted for an account that could be erased as it stands."""
    at = now or datetime.now(UTC)
    window = grace if grace is not None else timedelta(days=settings.account_deletion_grace_days)
    await _lock(db, "deletion", user_id)
    existing = await live_deletion(db, user_id)
    if existing is not None:
        raise DeletionAlreadyScheduledError(existing)
    plan = await compute_plan(db, user_id, now=at)
    if plan.blockers:
        raise DeletionBlockedError(plan)
    request = AccountDeletionRequest(
        user_id=user_id,
        requested_by_id=requested_by,
        source=source,
        status="scheduled",
        requested_at=at,
        purge_after=at + window,
        plan=plan.model_dump(mode="json"),
    )
    db.add(request)
    await db.flush()
    return request


async def cancel_deletion(
    db: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> AccountDeletionRequest | None:
    """Cancel the scheduled deletion, if there is one. Returns it, or ``None``."""
    await _lock(db, "deletion", user_id)
    request = await live_deletion(db, user_id)
    if request is None:
        return None
    request.status = "cancelled"
    request.cancelled_at = now or datetime.now(UTC)
    await db.flush()
    return request


async def due_deletions(
    db: AsyncSession, *, now: datetime | None = None, limit: int = 20
) -> list[uuid.UUID]:
    """Scheduled requests whose grace window has ended, oldest first."""
    rows = await db.execute(
        select(AccountDeletionRequest.id)
        .where(
            AccountDeletionRequest.status == "scheduled",
            AccountDeletionRequest.purge_after <= (now or datetime.now(UTC)),
        )
        .order_by(AccountDeletionRequest.purge_after)
        .limit(limit)
    )
    return list(rows.scalars().all())


__all__ = [
    "DeletionAlreadyScheduledError",
    "DeletionBlockedError",
    "cancel_deletion",
    "due_deletions",
    "latest_deletion",
    "live_deletion",
    "schedule_deletion",
]
