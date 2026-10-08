"""Pydantic schemas for TeamMembership."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from alkera_core.models._enums import TeamRole


class TeamMembershipBase(BaseModel):
    user_id: UUID
    team_id: UUID
    role: TeamRole = TeamRole.MEMBER


class TeamMembershipCreate(TeamMembershipBase):
    pass


class TeamMembershipRead(TeamMembershipBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    # Friendly label for `role` (e.g. 'Admin'); filled from `role` below.
    role_display: str = ""

    @model_validator(mode="after")
    def _fill_role_display(self) -> Self:
        self.role_display = self.role.display_name
        return self


class TeamMemberRead(BaseModel):
    """One person's standing on one team, with their identity and the team's
    name, so the member table needs no client-side join.

    A person stands on a team two ways, and the two are not exclusive: a
    membership row written on the team itself (``direct_role``), and admin
    reaching the team from a team above it (``descent_role``, carried from
    ``descent_from_team_*`` — the nearest ancestor holding the admin row). A
    person can hold both; a person can hold only one. ``role`` is what they
    hold here once descent is applied — admin when either side is admin, else
    the direct row's role — and ``effective_role`` names the same value for
    what it is. Descent is decided server-side so no client recomputes it.
    """

    user_id: UUID
    display_name: str
    email: str
    first_name: str
    last_name: str
    # The standing held on this team: descent applied.
    role: TeamRole
    team_id: UUID
    team_name: str
    # When the direct row was written; for a person reaching the team by
    # descent alone, when the admin row above was.
    created_at: datetime
    # The row written on this team itself; None when the person reaches the
    # team by descent only.
    direct_role: TeamRole | None = None
    # Admin when a team above holds an admin row for the person; None otherwise
    # (a member row above grants nothing below).
    descent_role: TeamRole | None = None
    descent_from_team_id: UUID | None = None
    descent_from_team_name: str | None = None
    # Friendly label for `role` (e.g. 'Admin'); filled from `role` below.
    role_display: str = ""
    # Same value as `role`, under the name that says what it is.
    effective_role: TeamRole = TeamRole.MEMBER

    @model_validator(mode="after")
    def _fill_derived(self) -> Self:
        self.role_display = self.role.display_name
        self.effective_role = self.role
        return self
