"""What else an org is created with.

The org services write an org's own rows (its root team and its settings). A
domain that keeps a per-org row of its own registers a hook here, run inside
the creating transaction once the root team has its id, so the org and every
row it starts with commit or roll back together. The knowledge base seeds its
sync settings this way. With nothing registered an org starts with its own
rows alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from alkera_core.extensions import ExtensionPoint
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org.removal_hooks import NamedHook, hooks_of


@dataclass(frozen=True, slots=True)
class OrgCreated:
    """An org whose root team was just written."""

    org_id: UUID


ORG_CREATED: ExtensionPoint[NamedHook[OrgCreated]] = ExtensionPoint("org_created")


async def org_created(db: AsyncSession, *, org_id: UUID) -> None:
    event = OrgCreated(org_id=org_id)
    for hook in hooks_of(ORG_CREATED).values():
        await hook.run(db, event)


__all__ = ["ORG_CREATED", "OrgCreated", "org_created"]
