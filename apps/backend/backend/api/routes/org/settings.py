"""Org-wide settings — readable by any org member, editable by Org Admins."""

from __future__ import annotations

from alkera_core.schemas.tenancy.org import OrgSettingsRead, OrgSettingsUpdate
from fastapi import APIRouter, Depends

from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    OrgAdmin,
    require_email_verified,
)
from backend.services.audit import org_audit as org_audit_service
from backend.services.org import settings as org_settings_service

router = APIRouter(prefix="/api/v1/org", tags=["org-settings"])


@router.get("/settings", response_model=OrgSettingsRead)
async def get_settings(user: CurrentUser, db: DbSession, org_id: CurrentOrg) -> OrgSettingsRead:
    settings = await org_settings_service.get_effective(db, org_id)
    return org_settings_service.read_shape(settings)


@router.put(
    "/settings",
    response_model=OrgSettingsRead,
    dependencies=[Depends(require_email_verified)],
)
async def update_settings(
    payload: OrgSettingsUpdate,
    admin: OrgAdmin,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> OrgSettingsRead:
    # What moved goes on the org's audit chain in the same transaction: every
    # field here decides who may sign in or what the org's agents may do, so
    # turning one off is exactly what the log is for.
    fields = set(payload.model_fields_set)
    before = org_settings_service.read_shape(
        await org_settings_service.get_effective(db, org_id)
    ).model_dump(mode="json", include=fields)
    settings = await org_settings_service.apply_update(db, org_id, payload)
    read = org_settings_service.read_shape(settings)
    changed = org_audit_service.changes(before, read.model_dump(mode="json", include=fields))
    if changed:
        await org_audit_service.record(
            db,
            org_id=org_id,
            actor=admin,
            action="org_settings.updated",
            target=", ".join(sorted(changed)),
            detail={"changes": changed},
            acting=ctx,
        )
    return read
