"""Auth maintenance activities: the token and device-code prunes.

Thin ``@activity.defn`` wrappers around the async cores in ``worker.tasks.auth``.
Each activity type is the historical task name, so an operator searching the
Temporal UI for the job finds it under the name they already know.
"""

from __future__ import annotations

from alkera_core.logging import get_logger
from temporalio import activity

from worker.tasks.auth import (
    run_prune_expired_device_codes,
    run_prune_expired_tokens,
    run_prune_login_lockouts,
)

log = get_logger(__name__)


@activity.defn(name="auth.prune_expired_tokens")
async def prune_expired_tokens() -> int:
    """Delete expired ``auth_tokens`` registry rows. Returns the number removed.

    Housekeeping only — expired rows are already ignored by every query's
    ``expires_at`` filter; this keeps the table from growing unbounded.
    """
    removed = await run_prune_expired_tokens()
    log.info("auth.prune_expired_tokens", removed=removed)
    return removed


@activity.defn(name="auth.prune_expired_device_codes")
async def prune_expired_device_codes() -> int:
    """Delete expired ``device_authorizations`` rows. Returns the number removed.

    Housekeeping only — expired rows are already ignored by ``get_for_approval``
    and rejected by the token endpoint; this keeps the table bounded.
    """
    removed = await run_prune_expired_device_codes()
    log.info("auth.prune_expired_device_codes", removed=removed)
    return removed


@activity.defn(name="auth.prune_login_lockouts")
async def prune_login_lockouts() -> int:
    """Delete brute-force counters for addresses with no account. Returns the count.

    Only rows past both their window and their cool-off — see
    ``lockout_service.prune_expired`` for why releasing one early would re-open
    the account-existence oracle the table exists to close.
    """
    removed = await run_prune_login_lockouts()
    log.info("auth.prune_login_lockouts", removed=removed)
    return removed
