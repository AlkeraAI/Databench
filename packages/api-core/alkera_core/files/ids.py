"""Typed ids for the Files library.

Every row in Files is keyed by a UUID, and most call sites take several of them
at once (a node, its drive, the version being written). `NewType` keeps them
distinct to mypy without paying a wrapper object at runtime, so passing a drive
id where a node id belongs is a typecheck failure rather than a runtime mystery.

Ids are opaque strings at the API boundary; these types are the internal shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, NewType
from uuid import UUID

NodeId = NewType("NodeId", UUID)
DriveId = NewType("DriveId", UUID)
VersionId = NewType("VersionId", UUID)
DomainId = NewType("DomainId", UUID)
SessionId = NewType("SessionId", UUID)
OperationId = NewType("OperationId", UUID)
TrashOpId = NewType("TrashOpId", UUID)
AclId = NewType("AclId", UUID)

#: The value every real UUID sorts after, spelled as text.
#: A batched walk keys its resume cursor on `(depth, id)` and starts below every
#: row; it lives here, beside the id types, because more than one walk needs the
#: same floor and two copies of it would be two things that can drift apart.
MIN_UUID: Final = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True, slots=True)
class OrgScope:
    """The tenant every Files query is scoped to.

    Carried as a value rather than a bare UUID so a function that needs the
    tenant says so in its signature, and so the org id cannot be swapped for
    some other id by accident.
    """

    org_team_id: UUID
