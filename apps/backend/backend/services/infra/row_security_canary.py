"""The backend's boot canary for tenant isolation.

Row-level security binds only while facts nothing else checks still hold (see
:mod:`alkera_core.db.row_security`): a deployment whose tenant role was made
``BYPASSRLS``, or whose new tenant table shipped without FORCE, serves every
org's rows to every org with no error anywhere. Both policed sets are asked
about: the Files tables and the content tables every authenticated request is
held to. This asks the database once at boot and, until the answer is clean,
again from every readiness probe, so a task whose database cannot be trusted
is kept out of rotation with a named reason instead of serving, and the moment
an operator fixes the role the next probe puts it back.

A clean answer is remembered for the life of the process: the facts are
schema and role state, which a running deployment does not change under
itself, and a readiness probe that re-read the catalog every few seconds would
be paying for nothing. A failed answer is never remembered, only logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alkera_core.config import settings
from alkera_core.db.row_security import (
    CONTENT_TIER,
    FILES_TIER,
    content_tier_tables,
    describe,
    judge,
    take_snapshot,
    tenant_tables,
)
from alkera_core.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

#: What a failed canary tells the (unauthenticated) readiness caller. The
#: problems themselves name roles and tables, so they go to the log only.
FAILED_DETAIL = "row-level security canary failed"


@dataclass
class _State:
    passed: bool = False
    last_problems: list[str] = field(default_factory=list)


_STATE = _State()


def reset() -> None:
    """Forget a clean answer. For a test that needs the next check to read."""
    _STATE.passed = False
    _STATE.last_problems = []


async def check(db: AsyncSession) -> list[str]:
    """The problems with tenant isolation on ``db``'s database; empty when it
    can be trusted, when it already was, or when the canary is off here.

    Leaves ``db``'s transaction rolled back, so the probe's savepoint and the
    catalog reads hold nothing open for the caller."""
    if not settings.row_security_canary_enabled or _STATE.passed:
        return []
    problems: list[str] = []
    snapshots = []
    for tier, expected in ((FILES_TIER, tenant_tables()), (CONTENT_TIER, content_tier_tables())):
        try:
            snapshot = await take_snapshot(db, expected=expected, tier=tier)
        finally:
            await db.rollback()
        snapshots.append(snapshot)
        tier_problems = judge(snapshot, expected=expected)
        if tier_problems:
            log.error("row_security.canary.failed", problems=tier_problems, **describe(snapshot))
        problems.extend(tier_problems)
    if problems:
        _STATE.last_problems = problems
        return problems
    _STATE.passed = True
    _STATE.last_problems = []
    for snapshot in snapshots:
        log.info("row_security.canary.passed", **describe(snapshot))
    return []


__all__ = ["FAILED_DETAIL", "check", "reset"]
