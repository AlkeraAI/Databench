"""Org settings domain operations.

A missing row means "all defaults" (every provider allowed) so existing orgs
need no backfill — `get_or_default` is the single read path that encodes that.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from alkera_core.config import settings as app_settings
from alkera_core.models import OrgSettings
from alkera_core.org_entitlements import org_entitlements
from alkera_core.sandbox_tiers import resolve_limits
from alkera_core.schemas.tenancy.org import (
    AdminOrgSettingsUpdate,
    OrgSettingsRead,
    OrgSettingsUpdate,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org.settings_sections import section_fields


async def get(db: AsyncSession, org_team_id: UUID) -> OrgSettings | None:
    return (
        await db.execute(select(OrgSettings).where(OrgSettings.org_team_id == org_team_id))
    ).scalar_one_or_none()


def _defaults(org_team_id: UUID) -> OrgSettings:
    """A transient (NOT session-attached) defaults row. Explicit Python values
    because SQLAlchemy column defaults only apply at INSERT flush, not at
    construction — a bare `OrgSettings()` would have None fields."""
    return OrgSettings(
        org_team_id=org_team_id,
        allow_login_google=True,
        allow_login_github=True,
        # NULL on purpose: "follow the deployment default" (SaaS on, self-hosted
        # off) — resolved at read time by `OrgSettings.web_search_effective`.
        web_search_enabled=None,
        ownership_escalation_enabled=False,
    )


async def get_effective(db: AsyncSession, org_team_id: UUID) -> OrgSettings:
    """Return the org's settings WITHOUT writing — a missing row reads as a
    transient defaults object. Use this on read paths (a GET must not INSERT,
    which is both surprising and a PK race)."""
    existing = await get(db, org_team_id)
    return existing if existing is not None else _defaults(org_team_id)


async def get_or_default(db: AsyncSession, org_team_id: UUID) -> OrgSettings:
    """Return the org's settings row, materializing + persisting a defaults row
    if missing. Use only on WRITE paths (`update`)."""
    existing = await get(db, org_team_id)
    if existing is not None:
        return existing
    settings = _defaults(org_team_id)
    db.add(settings)
    await db.flush()
    await db.refresh(settings)
    return settings


#: "Not provided" sentinel for update fields whose None means CLEAR.
_UNSET: object = object()

#: The plain boolean columns `update` takes by name. None (or absence) leaves one
#: standing, since True and False are their only real values.
_TOGGLES = ("allow_login_google", "allow_login_github", "ownership_escalation_enabled")


async def update(
    db: AsyncSession,
    org_team_id: UUID,
    *,
    web_search_enabled: bool | None | object = _UNSET,
    gate_force_rules: list[str] | None | object = _UNSET,
    sandbox_vcpu: int | None | object = _UNSET,
    sandbox_memory_mb: int | None | object = _UNSET,
    workspace_project_cap: int | None | object = _UNSET,
    **toggles: bool | None,
) -> OrgSettings:
    """Write the provided fields. ``toggles`` accepts the names in ``_TOGGLES``
    and raises on any other, because a misspelled toggle would otherwise write
    nothing and report success."""
    unknown = sorted(set(toggles) - set(_TOGGLES))
    if unknown:
        raise TypeError(f"unknown org settings toggle(s): {', '.join(unknown)}")
    settings = await get_or_default(db, org_team_id)
    for name, value in toggles.items():
        if value is not None:
            setattr(settings, name, value)
    # None is a real value here (reset to the deployment default), so absence is
    # the sentinel — same contract as the gate fields below.
    if isinstance(web_search_enabled, bool):
        settings.web_search_enabled = web_search_enabled
    elif web_search_enabled is None:
        settings.web_search_enabled = None
    # None is a real value for the gate fields (clear), so absence is the sentinel.
    if isinstance(gate_force_rules, list):
        settings.gate_force_rules = gate_force_rules
    elif gate_force_rules is None:
        settings.gate_force_rules = None
    # The sandbox limits: None resets to the box's default, absence leaves them.
    if sandbox_vcpu is None or isinstance(sandbox_vcpu, int):
        settings.sandbox_vcpu = sandbox_vcpu
    if sandbox_memory_mb is None or isinstance(sandbox_memory_mb, int):
        settings.sandbox_memory_mb = sandbox_memory_mb
    # The project-workspace cap: None returns to the deployment's default.
    if workspace_project_cap is None or isinstance(workspace_project_cap, int):
        settings.workspace_project_cap = workspace_project_cap
    await db.flush()
    await db.refresh(settings)
    return settings


async def apply_update(
    db: AsyncSession, org_team_id: UUID, payload: OrgSettingsUpdate
) -> OrgSettings:
    """Apply a partial-update payload — the ONE translation both the org-admin
    and platform-admin routes share. ``model_fields_set`` distinguishes an
    explicit null (clear a gate field) from an absent field (leave it)."""
    kwargs: dict[str, Any] = {}
    if "web_search_enabled" in payload.model_fields_set:
        kwargs["web_search_enabled"] = payload.web_search_enabled
    if "gate_force_rules" in payload.model_fields_set:
        kwargs["gate_force_rules"] = payload.gate_force_rules
    # Only the staff payload carries the sandbox limits and the project cap:
    # an org admin's body that names them is read as the plain shape, which
    # drops them.
    if isinstance(payload, AdminOrgSettingsUpdate):
        for name in ("sandbox_vcpu", "sandbox_memory_mb", "workspace_project_cap"):
            if name in payload.model_fields_set:
                kwargs[name] = getattr(payload, name)
    return await update(
        db,
        org_team_id,
        allow_login_google=payload.allow_login_google,
        allow_login_github=payload.allow_login_github,
        ownership_escalation_enabled=payload.ownership_escalation_enabled,
        **kwargs,
    )


def read_shape(settings: OrgSettings) -> OrgSettingsRead:
    """The read response: the stored row plus what each registered domain
    states about it (the CI gate's built-in floor and every rule id it knows),
    so an editor never guesses what exists or what is always on."""
    read = OrgSettingsRead.model_validate(settings)
    return read.model_copy(update=section_fields(settings))


async def sandbox_limits(db: AsyncSession, org_team_id: UUID) -> tuple[int | None, int | None]:
    """The org's chat sandbox limits as ``(vcpu, memory_mb)``, each the org's
    own override where staff set one, else the figure its plan tier is
    entitled to (``alkera_core.sandbox_tiers``), else ``None`` — the box's own
    default, which is what an enterprise org with no override gets: its
    dedicated box's leak guard. A tier the table does not know resolves to the
    smallest, never to no limit."""
    row = await get(db, org_team_id)
    plan = await org_entitlements().plan(db, org_team_id)
    return resolve_limits(
        plan,
        override_vcpu=row.sandbox_vcpu if row is not None else None,
        override_memory_mb=row.sandbox_memory_mb if row is not None else None,
        table=app_settings.sandbox_tier_table,
    )


async def set_live_editing(db: AsyncSession, org_team_id: UUID, enabled: bool | None) -> None:
    """Store the org's own live-editing setting: ``None`` clears it (the org
    follows the deployment's ``LIVE_EDITING_ENABLED`` again)."""
    settings = await get_or_default(db, org_team_id)
    settings.live_editing_enabled = enabled
    await db.flush()


async def provider_allowed(db: AsyncSession, org_team_id: UUID, provider: str) -> bool:
    """Whether `provider` may be used to sign in to this org.

    Providers without a per-org toggle (e.g. the dev mock) are always allowed.
    """
    settings = await get(db, org_team_id)
    if settings is None:
        return True  # default-allow
    if provider == "google":
        return settings.allow_login_google
    if provider == "github":
        return settings.allow_login_github
    return True
