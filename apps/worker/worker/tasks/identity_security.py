"""The identity security log's retention: the async core behind its prune.

Every failed sign-in writes a row to ``identity_security_events``, so the log
grows with every password spray an address attracts. This deletes the rows
older than ``settings.identity_security_event_retention_days``, a bounded batch
per transaction, oldest first, until none are left. Each batch commits on its
own, so a run that is cut short keeps what it deleted and the next run (or a
retry) takes up where it stopped; running it twice deletes nothing twice.

The platform's disable and re-enable decisions are never pruned: whether the
platform last disabled an identity is read back from this log, and losing the
row would read as "never disabled".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import IdentitySecurityEvent
from alkera_core.models.identity_security_event import PLATFORM_DECISION_EVENTS
from sqlalchemy import CursorResult, delete, select

#: Rows deleted per transaction: small enough that one batch holds its row
#: locks for well under a second, large enough that a year of sprayed sign-ins
#: drains in minutes.
PRUNE_BATCH = 5000


def retention_cutoff(now: datetime) -> datetime:
    """The oldest ``created_at`` the log keeps at ``now``."""
    return now - timedelta(days=settings.identity_security_event_retention_days)


async def _prune_identity_security_events(now: datetime, *, batch: int = PRUNE_BATCH) -> int:
    """Delete every prunable row older than the retention window at ``now``.
    Returns how many rows were deleted."""
    if batch < 1:
        raise ValueError(f"batch must be positive, got {batch!r}")
    cutoff = retention_cutoff(now)
    doomed = (
        select(IdentitySecurityEvent.id)
        .where(
            IdentitySecurityEvent.created_at < cutoff,
            IdentitySecurityEvent.event.not_in(PLATFORM_DECISION_EVENTS),
        )
        .order_by(IdentitySecurityEvent.created_at)
        .limit(batch)
    )
    removed = 0
    while True:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(IdentitySecurityEvent)
                .where(IdentitySecurityEvent.id.in_(doomed.scalar_subquery()))
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        deleted = result.rowcount if isinstance(result, CursorResult) else 0
        removed += deleted
        if deleted < batch:
            return removed
