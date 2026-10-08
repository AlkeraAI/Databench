"""Admin-route schemas for org bootstrap.

Distinct from `TeamCreate` because creating an org is a bigger operation:
in one request the caller specifies the org name AND the initial admin
user's identity + password.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from alkera_core.extensions import ExtensionPoint
from alkera_core.models._enums import PlatformRole
from alkera_core.models.org_settings import (
    SANDBOX_MEMORY_MB_MAX,
    SANDBOX_MEMORY_MB_MIN,
    SANDBOX_VCPU_MAX,
    SANDBOX_VCPU_MIN,
    WORKSPACE_PROJECT_CAP_MAX,
    WORKSPACE_PROJECT_CAP_MIN,
)
from alkera_core.schemas.identity.user import UserRead
from alkera_core.schemas.tenancy.team import TeamRead
from alkera_core.validation.display_name import DisplayNameStr
from alkera_core.validation.password import USER_PASSWORD_SCHEMA


class OrgCreate(BaseModel):
    name: DisplayNameStr = Field(min_length=1, max_length=255)
    admin_email: EmailStr
    admin_first_name: DisplayNameStr = Field(min_length=1, max_length=255)
    admin_last_name: DisplayNameStr = Field(min_length=1, max_length=255)
    admin_password: str = Field(
        min_length=8, max_length=255, json_schema_extra=USER_PASSWORD_SCHEMA
    )
    # NOTE: org creation always mints a *regular* Org Admin (no platform role).
    # Granting Alkera platform staff is deliberately a separate, ADMIN-only call
    # (`PATCH /admin/v1/users/{id}/platform_role`) so a SUPPORT-tier staffer
    # can't escalate by crowning a platform admin during org bootstrap.


class OrgRead(TeamRead):
    """Same shape as TeamRead — separate name communicates intent at the
    admin route surface — plus, on the detail read, the org's storage picture."""

    storage_limit_bytes: int | None = None
    """The effective ceiling on the detail read; ``null`` is unlimited (or a list row)."""
    storage_limit_source: Literal["override", "plan", "default"] | None = None
    storage_used_bytes: int | None = None


class OrgUpdate(BaseModel):
    """Admin-side org edit — currently just the display name (the root team's
    name)."""

    name: DisplayNameStr = Field(min_length=1, max_length=255)


class OrgCreateResponse(BaseModel):
    org: OrgRead
    admin: UserRead


class PlatformRoleUpdate(BaseModel):
    """ADMIN-only — set or clear a user's platform_role."""

    platform_role: PlatformRole | None = None


class OrgSettingsRead(BaseModel):
    """Org-wide settings. A missing row reads as all-defaults (see service)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    allow_login_google: bool = True
    allow_login_github: bool = True
    web_search_enabled: bool = Field(default=True, validation_alias="web_search_effective")
    """Whether the org's agents may use the local web tools (`web.search` /
    `web.fetch`). Always resolved on the wire: sourced from the ORM's
    `web_search_effective` (stored value, else the deployment default — SaaS on,
    self-hosted off), so clients never see the "unset" NULL."""
    ownership_escalation_enabled: bool = False
    """Whether a write reaching assets owned by a team the acting user is not on
    escalates to a human. Off keeps ownership display-only."""
    gate_force_rules: list[str] | None = None
    """The org's un-waivable rule additions, ON TOP of the built-in floor."""
    gate_builtin_force_rules: list[str] = Field(default_factory=list)
    """The always-on floor, stated by the server so the portal never guesses
    (populated by the read path, not stored)."""
    gate_known_rules: list[str] = Field(default_factory=list)
    """Every rule id this server knows — the vocabulary the policy editor
    offers (populated by the read path, not stored)."""
    sandbox_vcpu: int | None = None
    """vCPUs per chat sandbox; ``null`` = the box's default. Staff-set."""
    sandbox_memory_mb: int | None = None
    """Memory per chat sandbox in MiB; ``null`` = the box's default. Staff-set."""
    workspace_project_cap: int | None = None
    """How many live project workspaces one person may own in the org; ``null``
    = the deployment's default. Staff-set."""


#: The rule ids an org may force through ``gate_force_rules``, one set per
#: domain that defines rules (the schema gate registers its diff rules). With
#: nothing registered no rule is known, so any forced rule is refused.
GATE_RULE_IDS: ExtensionPoint[frozenset[str]] = ExtensionPoint("org_settings.gate_rule_ids")


def known_gate_rules() -> frozenset[str]:
    """Every rule id a registered domain defines."""
    return frozenset().union(*GATE_RULE_IDS.items())


class OrgSettingsUpdate(BaseModel):
    """Partial update — only provided fields change (``model_fields_set``
    distinguishes an explicit null, which CLEARS a value, from an absent one)."""

    allow_login_google: bool | None = None
    allow_login_github: bool | None = None
    web_search_enabled: bool | None = None
    """Set the org's web-tools toggle; an explicit null RESETS it to the
    deployment default (SaaS on, self-hosted off)."""
    ownership_escalation_enabled: bool | None = None
    """Turn cross-team escalation on or off for the org. Absent leaves
    the setting standing."""
    gate_force_rules: list[str] | None = Field(default=None, max_length=64)
    """Replace (or, with an explicit null, clear) the org's un-waivable rule
    additions. Values must be rule ids this server knows — a typo would
    silently force nothing."""

    @field_validator("gate_force_rules")
    @classmethod
    def _known_rules_only(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        known = known_gate_rules()
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown gate rule id(s): {', '.join(unknown)}")
        return sorted(set(value))


class AdminOrgSettingsUpdate(OrgSettingsUpdate):
    """The platform-staff update: everything an org admin may change, plus the
    chat sandbox limits, which only Alkera staff set (they size the machine the
    org is billed for), and the project-workspace cap. An explicit null resets
    a limit to its default; an absent field leaves it standing."""

    sandbox_vcpu: int | None = Field(default=None, ge=SANDBOX_VCPU_MIN, le=SANDBOX_VCPU_MAX)
    sandbox_memory_mb: int | None = Field(
        default=None, ge=SANDBOX_MEMORY_MB_MIN, le=SANDBOX_MEMORY_MB_MAX
    )
    workspace_project_cap: int | None = Field(
        default=None, ge=WORKSPACE_PROJECT_CAP_MIN, le=WORKSPACE_PROJECT_CAP_MAX
    )
    """How many live project workspaces one person may own in the org. An
    explicit null returns the org to the deployment's default."""
