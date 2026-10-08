"""Per-identity limits that keep one person from farming billing across orgs.

A seat belongs to a membership, so every org a person creates opens a new seat
and a new place to spend. The creation of orgs is therefore capped per identity:
:func:`org_creation_allowed` admits at most
``settings.max_org_creations_per_identity_30d`` creations in any rolling 30-day
window, counted from ``identity_org_creations`` (written by
``teams.create_org_with_admin``). A deleted org still counts: the row outlives it.

Signup creates exactly one org for a brand-new identity and never asks. The
caller is the path that lets an existing identity create another org.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from alkera_core.config import settings
from alkera_core.models import IdentityOrgCreation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: The rolling window the creation cap counts over.
ORG_CREATION_WINDOW = timedelta(days=30)


async def org_creation_allowed(db: AsyncSession, user_id: UUID, *, now: datetime) -> bool:
    """Whether this identity may create one more org at ``now``: fewer than the
    configured cap created in the 30 days up to ``now``."""
    created = (
        await db.execute(
            select(func.count())
            .select_from(IdentityOrgCreation)
            .where(
                IdentityOrgCreation.user_id == user_id,
                IdentityOrgCreation.created_at > now - ORG_CREATION_WINDOW,
            )
        )
    ).scalar_one()
    return int(created) < settings.max_org_creations_per_identity_30d


__all__ = ["ORG_CREATION_WINDOW", "org_creation_allowed"]
