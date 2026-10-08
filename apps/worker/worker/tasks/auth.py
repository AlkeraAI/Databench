"""Auth maintenance: the async cores behind the token and device-code prunes.

The activities in ``worker.activities.auth`` call these; each is a predicate
DELETE in its own session, committed before it returns.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alkera_core.auth import prune_expired, prune_login_lockouts
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import DeviceAuthorization
from sqlalchemy import CursorResult, delete


async def run_prune_expired_tokens() -> int:
    """Delete expired ``auth_tokens`` registry rows. Returns the number removed.

    Housekeeping only — expired rows are already ignored by every query's
    ``expires_at`` filter; this keeps the table from growing unbounded.
    """
    async with AsyncSessionLocal() as session:
        count = await prune_expired(session)
        await session.commit()
        return count


async def run_prune_expired_device_codes() -> int:
    """Delete expired ``device_authorizations`` rows. Returns the number removed.

    Housekeeping only — expired rows are already ignored by ``get_for_approval``
    and rejected by the token endpoint; this keeps the table bounded.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(DeviceAuthorization).where(DeviceAuthorization.expires_at < datetime.now(UTC))
        )
        await session.commit()
        return result.rowcount if isinstance(result, CursorResult) else 0


async def run_prune_login_lockouts() -> int:
    """Delete the brute-force counters for addresses with no account. Returns the count.

    Only rows past BOTH their failure window and their cool-off go: a row that
    is still locked answers 429 for an address nobody has, which is precisely
    what the `users` half answers for an address somebody does — releasing it
    early would put the account-existence oracle back for the length of a
    cool-off longer than the window.
    """
    async with AsyncSessionLocal() as session:
        removed = await prune_login_lockouts(
            session, window_seconds=settings.auth_lockout_window_seconds
        )
        await session.commit()
        return removed
