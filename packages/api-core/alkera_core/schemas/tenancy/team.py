"""Pydantic schemas for Team."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from alkera_core.validation.display_name import DisplayNameStr


class TeamBase(BaseModel):
    # Name is deliberately UNCONSTRAINED on the read base: a minimal (email +
    # password) signup creates an UNNAMED org ("") that the admin names on the
    # complete-profile step, so a *read* of any team/org must not blow up before
    # then. Write schemas below re-add `min_length=1`. Mirrors `UserBase`.
    name: str = Field(max_length=255)


class TeamCreate(TeamBase):
    # DisplayNameStr, not a bare str: a team name is embedded in the invitation
    # email's prose and subject, where a mail client turns URL-shaped text into a
    # live link sent under our own domain. See alkera_core.validation.display_name.
    name: DisplayNameStr = Field(min_length=1, max_length=255)
    parent_team_id: UUID | None = None


class TeamRead(TeamBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    parent_team_id: UUID | None
    is_root: bool
    created_at: datetime
    # Direct membership-row count (includes inherited/materialized rows, so an
    # intermediate team's count equals everyone in its subtree). Populated by
    # the route; defaults to 0 when read straight off a Team model.
    member_count: int = 0
