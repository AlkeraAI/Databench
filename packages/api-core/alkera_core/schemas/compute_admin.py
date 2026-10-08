"""Platform-staff compute shapes — the grants an org holds and the act of
changing one.

Deliberately a module of its own rather than a section of
:mod:`alkera_core.schemas.compute`: that module is the TENANT-visible surface
and a guard test fails it on any cost-shaped field. A grant is staff-facing —
the ceiling, the billed rate and the expiry are exactly what an operator must
see — so it lives here, behind ``/admin/v1``.

In-flight HTTP shapes only: plain ``BaseModel`` (nothing here is persisted).
Money is integer nano-USD; ids are UUID strings.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

#: The ``machine_type`` value of a grant that admits every machine type. A code
#: rather than an empty string so a reader never has to guess whether a blank
#: means "any type" or "the type failed to load" — the two are opposite facts and
#: an operator must be able to tell a wildcard grant from a broken row.
ANY_MACHINE_TYPE = "any"
#: What that wildcard is called on screen. The API sends the label so every
#: surface reading a grant spells it the same way.
ANY_MACHINE_TYPE_LABEL = "Any machine type"


class ComputeGrantRead(BaseModel):
    """One live compute grant held by an org (or by a team inside it)."""

    id: str
    org_team_id: str
    # The team the grant was written against — the org root, or a team below it.
    team_name: str = ""
    # NULL = the grant admits every machine type. Never "" — an id is either a
    # real id or absent.
    machine_type_id: str | None = None
    # The provider's own code for the granted type, or ``ANY_MACHINE_TYPE`` for a
    # wildcard grant. This is the field to branch on.
    machine_type: str = ANY_MACHINE_TYPE
    # Always populated — the granted type's display name, or the wildcard label.
    machine_type_display_name: str = ANY_MACHINE_TYPE_LABEL
    ceiling: int
    per_user_max: int | None = None
    rate_per_minute_nanos: int
    expires_at: datetime
    note: str = ""
    created_at: datetime


class ComputeGrantUpsertRequest(BaseModel):
    """Grant an org compute, or bring its live grant up to date.

    Idempotent on ``(team, machine type)``: a second call with the same target
    updates the live grant instead of stacking a second one.
    """

    # Defaults to the org root when omitted — the usual "grant the whole org" act.
    org_team_id: UUID | None = None
    # Omit for a wildcard grant that admits every machine type.
    machine_type_id: UUID | None = None
    ceiling: int = Field(ge=0)
    per_user_max: int | None = Field(default=None, ge=1)
    rate_per_minute_nanos: int = Field(default=0, ge=0)
    expires_at: datetime
    note: str = Field(default="", max_length=512)


__all__ = [
    "ANY_MACHINE_TYPE",
    "ANY_MACHINE_TYPE_LABEL",
    "ComputeGrantRead",
    "ComputeGrantUpsertRequest",
]
