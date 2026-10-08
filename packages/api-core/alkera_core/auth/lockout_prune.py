"""Retention for the brute-force counters kept for addresses with no account.

Shared shape rather than backend code, because the caller is the WORKER: the
scheduled prune beside the token and device-code ones. It lives here for the
same reason the email stack does — the worker depends on ``alkera-core``, never
on ``alkera-backend``.

The predicate is the interesting part and is a correctness constraint, not
housekeeping policy. ``login_lockouts`` exists so that an address nobody has
answers a failed sign-in on exactly the schedule an address somebody has
answers it on; the ``users`` half of that pair is never pruned. So a row may
only go once it can no longer change an answer — past its failure window AND
past its cool-off. ``AUTH_LOCKOUT_DURATION_SECONDS`` may legally exceed
``AUTH_LOCKOUT_WINDOW_SECONDS``, and a prune keyed on the window alone would
release the unknown address from a cool-off the real one is still serving,
which is the account-existence oracle back again one window later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.models import LoginLockout


async def prune_login_lockouts(
    db: AsyncSession, *, window_seconds: int, now: datetime | None = None
) -> int:
    """Delete the counters that can no longer change an answer. Returns the count.

    Spelled as two comparisons rather than the ``GREATEST(last_failed_at +
    window, locked_until) <= now`` it is equivalent to, because an expression
    over two columns is not indexable: the planner scans for it even with
    ``enable_seqscan = off``. This form leads on ``last_failed_at``, so the
    delete reads an index range and rechecks ``locked_until`` on the rows it
    fetched. The ``IS NULL`` arm is what ``GREATEST`` was doing implicitly — it
    ignores a NULL argument, so a counter that never reached the threshold is
    judged on its window alone, which is all it has.
    """
    now = now or datetime.now(UTC)
    result = await db.execute(
        delete(LoginLockout).where(
            LoginLockout.last_failed_at <= now - timedelta(seconds=window_seconds),
            or_(LoginLockout.locked_until.is_(None), LoginLockout.locked_until <= now),
        )
    )
    await db.flush()
    return result.rowcount if isinstance(result, CursorResult) else 0
