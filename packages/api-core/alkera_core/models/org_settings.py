"""Per-organization settings — one row per org (root team).

The home for org-wide policy that an Org Admin (or Alkera staff) controls. The
first such policy is which external login providers org members may use. Designed
to grow: future enterprise toggles (`require_sso`, `allowed_email_domains`, …)
land here as new columns.

Read through `org_settings_service.get_or_default` — a missing row means
"defaults" (both providers allowed), so existing orgs need no backfill.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.db.retired import retired_column, unmapped

#: The bounds a staff-set chat sandbox limit must fall in. The database holds
#: them too, so a row written around the API cannot hand a box an absurd figure.
SANDBOX_VCPU_MIN = 1
SANDBOX_VCPU_MAX = 64
SANDBOX_MEMORY_MB_MIN = 512
SANDBOX_MEMORY_MB_MAX = 262144
#: The bounds a staff-set project-workspace cap must fall in.
WORKSPACE_PROJECT_CAP_MIN = 1
WORKSPACE_PROJECT_CAP_MAX = 100000

#: The removed gate dollar budgets (per PR, and the monthly pool). Nothing reads
#: or writes them; a later contract migration drops them (see deploy-notes).
_RETIRED = (
    retired_column("gate_pr_budget_nanos", BigInteger, nullable=True),
    retired_column("gate_monthly_budget_nanos", BigInteger, nullable=True),
)


class OrgSettings(Base):
    __tablename__ = "org_settings"
    __table_args__ = (
        *_RETIRED,
        CheckConstraint(
            "sandbox_vcpu IS NULL OR sandbox_vcpu BETWEEN "
            f"{SANDBOX_VCPU_MIN} AND {SANDBOX_VCPU_MAX}",
            name="ck_org_settings_sandbox_vcpu",
        ),
        CheckConstraint(
            "sandbox_memory_mb IS NULL OR sandbox_memory_mb BETWEEN "
            f"{SANDBOX_MEMORY_MB_MIN} AND {SANDBOX_MEMORY_MB_MAX}",
            name="ck_org_settings_sandbox_memory_mb",
        ),
        CheckConstraint(
            "workspace_project_cap IS NULL OR workspace_project_cap BETWEEN "
            f"{WORKSPACE_PROJECT_CAP_MIN} AND {WORKSPACE_PROJECT_CAP_MAX}",
            name="ck_org_settings_workspace_project_cap",
        ),
    )
    __mapper_args__ = unmapped(_RETIRED)

    # PK *and* FK to the org's root team — one settings row per org.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    allow_login_google: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    allow_login_github: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    web_search_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Whether the org's agents may use the local web tools (`web.search` /
    `web.fetch`). NULL = follow the deployment default — on for the Alkera-operated
    SaaS, off for self-hosted (see `web_search_effective`). A stored value is an
    explicit org-admin choice and wins on either deployment."""
    ownership_escalation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    """Whether a write reaching assets owned by a team the acting user is not on
    escalates to a human. Off keeps ownership display-only. A plain
    default, unlike `web_search_enabled`, because no per-deployment split
    applies."""
    gate_force_rules: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    """Extra gate rule IDs the org forces un-waivable, ON TOP of the built-in
    floor (TABLE_DROPPED, COLUMN_DROPPED). NULL = no additions. Enforced
    App-side at waiver creation regardless of repo YAML; the admin surface for
    editing it ships with org policy admin."""
    sandbox_vcpu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """How many vCPUs each of the org's chat sandboxes gets. NULL = the box's
    own default. Set by Alkera staff only; the box reads it off the chat
    listing."""
    sandbox_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Memory per chat sandbox, in MiB. NULL = the box's own default."""
    live_editing_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Whether the org's files open live (co-edited, saved as typed) and its
    boxes merge agent edits as text peers. NULL = follow the deployment's
    ``LIVE_EDITING_ENABLED``; a stored value is a platform admin's choice and
    wins over the deployment's in either direction."""
    workspace_project_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """How many live project workspaces one person may own in this org. NULL =
    the deployment's ``workspaces_max_projects_per_member``. Set by Alkera
    staff only, for an org that needs more (or fewer) than the default."""

    @property
    def web_search_effective(self) -> bool:
        """The resolved web-search toggle: the stored value when set, else the
        deployment default (SaaS on, self-hosted off). Lives here — not in a
        backend service — because the model-gateway (which depends only on
        alkera-core) resolves it too."""
        if self.web_search_enabled is not None:
            return self.web_search_enabled
        from alkera_core.config import get_settings

        return not get_settings().is_self_hosted

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
