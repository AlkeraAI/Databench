"""A role granted to a principal at an org or team scope.

Team memberships (``team_memberships``) say who belongs where and carry the two
human roles the product has always had. This table is the second source the
role resolver reads: explicit grants of the wider vocabulary — ``owner`` on the
org root, ``viewer`` on one team, ``admin`` for a service token — without
turning a token into a team member. Every org has exactly one live ``owner``
row for the admin who created it (the migration back-fills the orgs that
predate the table; ``team_service.create_org_with_admin`` writes it for every
org since).

A grant is revoked, never deleted: ``revoked_at`` keeps the row as an audit
trail, and the partial unique index makes "one LIVE grant per principal, scope
and role key" the invariant while still allowing a re-grant after a revoke.
An owner or admin grant rides with the membership that justified it: the
membership service revokes a user's live owner/admin grants scoped at a team
(and every team beneath it) when their membership there is demoted below admin
or removed, and nothing re-grants on a promotion — owner standing returns only
through an explicit assignment.
``principal_id`` has no foreign key because the principal may be a user, a CI
or proxy token or a personal access token — the ``principal_kind`` column says
which table it names. ``org_team_id`` is the tenancy column every reader
filters on; both it and ``scope_id`` cascade with the team so a deleted org
leaves no orphaned grants.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.authz.enums import PrincipalKind, Role, ScopeKind
from alkera_core.db.base import Base


def _values(enum: type[PrincipalKind] | type[Role] | type[ScopeKind]) -> list[str]:
    # Store the StrEnum's value ("user"), not the Python member name ("USER"),
    # so the CHECK constraints below and every raw SQL reader agree with the ORM.
    return [member.value for member in enum]


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "principal_kind IN ('user', 'agent', 'service', 'pat')",
            name="ck_role_assignments_principal_kind",
        ),
        CheckConstraint("scope_kind IN ('org', 'team')", name="ck_role_assignments_scope_kind"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer', 'agent', 'service')",
            name="ck_role_assignments_role",
        ),
        # One LIVE grant per (principal, scope); a revoked row keeps its history and
        # a fresh grant may follow it. Declared here so autogenerate sees it.
        Index(
            "uq_role_assignments_live_principal_scope",
            "principal_kind",
            "principal_id",
            "scope_kind",
            "scope_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # The resolver reads every grant of one principal whose scope is on a
        # team's ancestor chain; the scope index serves the reverse question.
        Index("ix_role_assignments_principal", "principal_kind", "principal_id"),
        Index("ix_role_assignments_scope", "scope_kind", "scope_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_kind: Mapped[PrincipalKind] = mapped_column(
        Enum(
            PrincipalKind,
            native_enum=False,
            length=16,
            name="principal_kind",
            values_callable=_values,
        ),
        nullable=False,
    )
    # A user, token or personal-access-token id; ``principal_kind`` names the
    # table. Agent-kind rows are admitted by the schema and minted by nothing —
    # an agent's roles come from the user it acts for.
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope_kind: Mapped[ScopeKind] = mapped_column(
        Enum(
            ScopeKind,
            native_enum=False,
            length=8,
            name="authz_scope_kind",
            values_callable=_values,
        ),
        nullable=False,
    )
    # Org and team scopes are both team ids (the org is its root team), so one
    # column and one foreign key cover both.
    scope_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = mapped_column(
        Enum(Role, native_enum=False, length=16, name="authz_role", values_callable=_values),
        nullable=False,
    )
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
