"""Putting an org on the Enterprise plan, for tests of Enterprise-only surfaces.

The open platform has no plans: with no billing system registered, every org
holds the Enterprise surfaces (SSO and IdP configuration, the audit log). A
distribution that sells plans registers how it enrolls an org, so the same tests
drive its gating too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final
from uuid import UUID

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.extensions import ExtensionPoint
from sqlalchemy.ext.asyncio import AsyncSession

#: Enrolls ``org_id`` onto an active Enterprise plan inside the session it is
#: handed; the caller commits.
EnterpriseEnroller = Callable[[AsyncSession, UUID], Awaitable[None]]

ENTERPRISE_ENROLLERS: Final[ExtensionPoint[EnterpriseEnroller]] = ExtensionPoint(
    "test_support.enterprise_enrollers"
)


async def make_org_enterprise(org_id: UUID) -> None:
    """Put ``org_id`` on an active Enterprise plan through every registered
    enroller. With none registered it does nothing: the org already holds the
    Enterprise surfaces."""
    enrollers = ENTERPRISE_ENROLLERS.items()
    if not enrollers:
        return
    async with AsyncSessionLocal() as session:
        for enroll in enrollers:
            await enroll(session, org_id)
        await session.commit()
