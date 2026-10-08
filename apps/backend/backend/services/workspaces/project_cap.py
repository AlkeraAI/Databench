"""How many project workspaces one person may own in one org.

An abuse limit, not a plan feature: every member may start project workspaces,
and a script that starts them without end would otherwise fill the org's
drive with folders and the table with rows. The deployment sets the default
(``workspaces_max_projects_per_member``); platform staff may override it for
one org (``org_settings.workspace_project_cap``).

Only live project workspaces count. A member's main workspace and the
workspace of one a chat is given are made by the product, not asked for, and
an ended workspace stops counting the moment it ends.

The count is taken under a transaction-scoped advisory lock keyed by the org
and the person, so two creates racing at one below the cap serialize: the
second counts the first's row once the first commits, and is refused.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import OrgSettings, WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: The refusal's stable code, which the web reads to word it.
PROJECT_CAP_REACHED = "workspace_project_cap_reached"


class WorkspaceProjectCapError(Exception):
    """The person already owns as many live project workspaces as the org allows."""

    def __init__(self, cap: int) -> None:
        super().__init__(f"You've reached the limit of {cap} project workspaces.")
        self.cap = cap


async def project_cap(db: AsyncSession, org_id: UUID) -> int:
    """The org's cap: staff's override where one is set, else the deployment's."""
    override = await db.scalar(
        select(OrgSettings.workspace_project_cap).where(OrgSettings.org_team_id == org_id)
    )
    return int(override) if override is not None else settings.workspaces_max_projects_per_member


async def hold_project_count(db: AsyncSession, *, owner_id: UUID, org_id: UUID) -> int:
    """The person's live project workspaces in the org, counted under a lock
    held until the caller's transaction ends, so a create that follows in the
    same transaction is the only one counted against this figure."""
    await advisory_xact_lock(db, advisory_key("workspace-projects", org_id, owner_id))
    return int(
        await db.scalar(
            select(func.count())
            .select_from(WorkspaceObject)
            .where(
                WorkspaceObject.org_team_id == org_id,
                WorkspaceObject.owner_user_id == owner_id,
                WorkspaceObject.type == WORKSPACE_TYPE,
                WorkspaceObject.namespace == DEFAULT_NAMESPACE,
                WorkspaceObject.deleted_at == 0,
            )
        )
        or 0
    )


async def admit_project(db: AsyncSession, *, owner_id: UUID, org_id: UUID) -> None:
    """Refuse a new project workspace past the cap; otherwise hold the count
    until the caller commits."""
    cap = await project_cap(db, org_id)
    if await hold_project_count(db, owner_id=owner_id, org_id=org_id) >= cap:
        raise WorkspaceProjectCapError(cap)


__all__ = [
    "PROJECT_CAP_REACHED",
    "WorkspaceProjectCapError",
    "admit_project",
    "hold_project_count",
    "project_cap",
]
