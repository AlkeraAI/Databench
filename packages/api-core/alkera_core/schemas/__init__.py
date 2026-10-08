"""Pydantic schemas — request/response shapes shared between backend and clients.

Organized by domain subpackage:
- `identity/` — auth, user, invitation
- `tenancy/`  — org, team, team_membership
- `system/`   — health, dashboard
- `chat/`     — versioned chat models (events, parts, manifest)

Consumers SHOULD import from these subpackages directly when reasonable.
However, this top-level module re-exports every symbol so existing
imports like `from alkera_core.schemas import UserRead` keep working —
the reorg is back-compat by design.
"""

from __future__ import annotations

from alkera_core.schemas.identity.auth import (
    LoginRequest,
    LoginResponse,
    MessageResponse,
)
from alkera_core.schemas.identity.invitation import (
    InvitationAcceptResponse,
    InvitationCreate,
    InvitationPublicRead,
    InvitationRead,
)
from alkera_core.schemas.identity.user import UserBase, UserCreate, UserRead
from alkera_core.schemas.kafka_contracts import KafkaContract, KafkaContracts, KafkaFieldMap
from alkera_core.schemas.preferences import Preferences
from alkera_core.schemas.system.dashboard import DashboardResponse
from alkera_core.schemas.system.health import InfoResponse, LiveStatus, ReadyStatus
from alkera_core.schemas.tenancy.org import (
    OrgCreate,
    OrgCreateResponse,
    OrgRead,
    PlatformRoleUpdate,
)
from alkera_core.schemas.tenancy.team import TeamBase, TeamCreate, TeamRead
from alkera_core.schemas.tenancy.team_membership import (
    TeamMembershipBase,
    TeamMembershipCreate,
    TeamMembershipRead,
)

__all__ = [
    "DashboardResponse",
    "InfoResponse",
    "InvitationAcceptResponse",
    "InvitationCreate",
    "InvitationPublicRead",
    "InvitationRead",
    "KafkaContract",
    "KafkaContracts",
    "KafkaFieldMap",
    "LiveStatus",
    "LoginRequest",
    "LoginResponse",
    "MessageResponse",
    "OrgCreate",
    "OrgCreateResponse",
    "OrgRead",
    "PlatformRoleUpdate",
    "Preferences",
    "ReadyStatus",
    "TeamBase",
    "TeamCreate",
    "TeamMembershipBase",
    "TeamMembershipCreate",
    "TeamMembershipRead",
    "TeamRead",
    "UserBase",
    "UserCreate",
    "UserRead",
]
