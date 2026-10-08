"""The org's compute settings: the shared pool fallback, how many pool machines
stay awake, and the default machine new workspaces run on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.authz.policies import org_machine as policy
from alkera_core.models.org_machines import (
    OrgComputeSettings,
    OrgMachine,
)
from alkera_core.schemas.org_machines import (
    OrgComputeSettingsRead,
    OrgComputeSettingsUpdate,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import record_org_audit
from backend.services.compute.org_machine_access import (
    COMPUTE_SETTINGS_CHANGED,
    OrgMachineError,
    Viewer,
    uuid_or_none,
)

# --------------------------------------------------------------------------- #
# the org's compute settings
# --------------------------------------------------------------------------- #


async def _live_machine_id(db: AsyncSession, *, org_id: UUID, machine_id: UUID) -> UUID | None:
    """``machine_id`` when it names a live org machine of ``org_id``: the org
    is in the SQL, so another org's id finds nothing."""
    found = await db.execute(
        select(OrgMachine.id).where(
            OrgMachine.id == machine_id,
            OrgMachine.org_team_id == org_id,
            OrgMachine.deleted_at.is_(None),
        )
    )
    return found.scalar_one_or_none()


async def default_machine_id(db: AsyncSession, *, org_id: UUID) -> UUID | None:
    """The org's default machine for new workspaces, or ``None`` when unset or
    the machine it names is deleted (a deleted default reads as none)."""
    row = await db.get(OrgComputeSettings, org_id)
    if row is None or row.default_org_machine_id is None:
        return None
    return await _live_machine_id(db, org_id=org_id, machine_id=row.default_org_machine_id)


async def read_settings(db: AsyncSession, *, org_id: UUID) -> OrgComputeSettingsRead:
    """The org's compute settings; the defaults (at version 0) without a row."""
    row = await db.get(OrgComputeSettings, org_id)
    if row is None:
        return OrgComputeSettingsRead(shared_pool_fallback=True, min_awake_pool=0, version=0)
    default = await default_machine_id(db, org_id=org_id)
    return OrgComputeSettingsRead(
        shared_pool_fallback=row.shared_pool_fallback,
        min_awake_pool=row.min_awake_pool,
        default_org_machine_id=str(default) if default is not None else None,
        version=row.version,
    )


async def update_settings(
    db: AsyncSession, viewer: Viewer, body: OrgComputeSettingsUpdate
) -> OrgComputeSettings:
    row = (
        await db.execute(
            select(OrgComputeSettings)
            .where(OrgComputeSettings.org_team_id == viewer.org_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = OrgComputeSettings(
            org_team_id=viewer.org_id, shared_pool_fallback=True, min_awake_pool=0, version=0
        )
        db.add(row)
    changed: dict[str, Any] = {}
    sent = body.model_fields_set
    if "shared_pool_fallback" in sent and body.shared_pool_fallback is not None:
        changed["shared_pool_fallback"] = body.shared_pool_fallback
        row.shared_pool_fallback = body.shared_pool_fallback
    if "min_awake_pool" in sent and body.min_awake_pool is not None:
        if body.min_awake_pool < 0:
            raise OrgMachineError(
                "min_awake_negative", "Machines kept awake can't be negative.", status=422
            )
        changed["min_awake_pool"] = body.min_awake_pool
        row.min_awake_pool = body.min_awake_pool
    if "default_org_machine_id" in sent:
        chosen: UUID | None = None
        if body.default_org_machine_id is not None:
            wanted = uuid_or_none(body.default_org_machine_id)
            chosen = (
                await _live_machine_id(db, org_id=viewer.org_id, machine_id=wanted)
                if wanted is not None
                else None
            )
            if chosen is None:
                # Another org's machine, a deleted one and an id that names
                # nothing read the same.
                raise OrgMachineError("not_found", policy.NOT_FOUND_MESSAGE, status=404)
        changed["default_org_machine_id"] = str(chosen) if chosen is not None else None
        row.default_org_machine_id = chosen
    row.version = (row.version or 0) + 1
    row.updated_at = datetime.now(UTC)
    await db.flush()
    await record_org_audit(
        db,
        org_id=viewer.org_id,
        actor=viewer.user,
        action=COMPUTE_SETTINGS_CHANGED,
        target=str(viewer.org_id),
        detail=changed,
        acting=viewer.ctx,
    )
    return row


__all__ = [
    "default_machine_id",
    "read_settings",
    "update_settings",
]
