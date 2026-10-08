"""The thing a decision is about."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from alkera_core.authz.enums import ResourceType


@dataclass(frozen=True, slots=True)
class Resource:
    """A typed handle on the target of an action.

    ``org_id`` is the tenancy floor: when set, a caller from another org is
    told the resource does not exist before any policy runs. ``team_id`` is
    the team the resource belongs to, when it has one — policies and audit
    rows read it, the engine does not.
    """

    type: ResourceType
    id: str
    org_id: UUID | None = None
    team_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, ResourceType):
            raise ValueError(f"resource type must be a ResourceType, got {self.type!r}")
        if not self.id:
            raise ValueError("a resource needs a non-empty id")


__all__ = ["Resource"]
