"""Files authorization: the ladder, the grant source, the decider, capabilities.

Role names live here and nowhere else. Routes, services, the CLI and the UI
consume :class:`~alkera_core.files.authz.capabilities.Capabilities` and typed
``Authorized`` handles; the ``files.access`` policy consumes
``allowed_actions``. The two protocols — :class:`GrantSource` and
:class:`AccessDecider` — are the socket the platform RBAC/ABAC engine plugs
into later, with one implementation each today.
"""

from __future__ import annotations

from alkera_core.files.authz.actions import READ_ONLY_ACTIONS, FilesAction
from alkera_core.files.authz.capabilities import Capabilities, capabilities
from alkera_core.files.authz.decider import (
    AccessDecider,
    AccessFacts,
    EffectiveAccess,
    LadderDecider,
    NodeFlag,
    effective_role,
    node_flags,
)
from alkera_core.files.authz.defaults import DEFAULT_ACLS, DriveFolder, default_acl
from alkera_core.files.authz.grants import (
    FilesGrantSource,
    Grant,
    GrantOrigin,
    GrantSource,
    Principal,
)
from alkera_core.files.authz.ladder import (
    LADDER,
    ROLE_COMMENTER,
    ROLE_LABELS,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_READER,
    ROLE_WRITER,
    RoleLadder,
    role_label,
)
from alkera_core.files.authz.policy_attrs import policy_attrs

__all__ = [
    "DEFAULT_ACLS",
    "LADDER",
    "READ_ONLY_ACTIONS",
    "ROLE_COMMENTER",
    "ROLE_LABELS",
    "ROLE_MANAGER",
    "ROLE_OWNER",
    "ROLE_READER",
    "ROLE_WRITER",
    "AccessDecider",
    "AccessFacts",
    "Capabilities",
    "DriveFolder",
    "EffectiveAccess",
    "FilesAction",
    "FilesGrantSource",
    "Grant",
    "GrantOrigin",
    "GrantSource",
    "LadderDecider",
    "NodeFlag",
    "Principal",
    "RoleLadder",
    "capabilities",
    "default_acl",
    "effective_role",
    "node_flags",
    "policy_attrs",
    "role_label",
]
