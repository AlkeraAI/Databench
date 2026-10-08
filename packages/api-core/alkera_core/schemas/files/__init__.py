"""The persisted Files shapes: node kinds, attributes, principals, ACLs, history.

Every model here is a ``VersionedModel`` with a fixture under
``packages/api-core/tests/fixtures/files/v<SCHEMA_VERSION>/``, so a shape that
changes without a version bump fails the lineage test rather than a customer's
rolling deploy.
"""

from __future__ import annotations

from alkera_core.schemas.files.acl import (
    Ace,
    AceBase,
    AclBody,
    OrgAce,
    RawAce,
    TeamAce,
    UserAce,
    canonical_json,
)
from alkera_core.schemas.files.attrs import (
    MAX_XATTR_NAME_BYTES,
    MAX_XATTR_TOTAL_BYTES,
    MAX_XATTR_VALUE_BYTES,
    AttrsPatch,
    Base64Bytes,
    NodeAttrs,
    validate_xattrs,
)
from alkera_core.schemas.files.history import HistoryKind, HistorySnapshot
from alkera_core.schemas.files.kinds import (
    MimeClass,
    NodeKind,
    NodeState,
    RawKind,
    SymlinkKind,
    Trust,
    parse_kind,
)
from alkera_core.schemas.files.principal import (
    ROLE_LADDER,
    DirectGrant,
    DriveDefaultGrant,
    Grant,
    GrantOrigin,
    InheritedGrant,
    Principal,
    RawGrantOrigin,
    validate_role,
)

__all__ = [
    "MAX_XATTR_NAME_BYTES",
    "MAX_XATTR_TOTAL_BYTES",
    "MAX_XATTR_VALUE_BYTES",
    "ROLE_LADDER",
    "Ace",
    "AceBase",
    "AclBody",
    "AttrsPatch",
    "Base64Bytes",
    "DirectGrant",
    "DriveDefaultGrant",
    "Grant",
    "GrantOrigin",
    "HistoryKind",
    "HistorySnapshot",
    "InheritedGrant",
    "MimeClass",
    "NodeAttrs",
    "NodeKind",
    "NodeState",
    "OrgAce",
    "Principal",
    "RawAce",
    "RawGrantOrigin",
    "RawKind",
    "SymlinkKind",
    "TeamAce",
    "Trust",
    "UserAce",
    "canonical_json",
    "parse_kind",
    "validate_role",
    "validate_xattrs",
]
