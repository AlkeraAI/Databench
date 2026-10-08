"""The open platform's ORM models. Re-exported so Alembic autogenerate picks them up.

Only open models live in this barrel. An extension domain's models (billing's,
the schema gate's, the knowledge base's) are imported by that domain's code and
extensions, which is what registers
their tables on the shared ``Base.metadata``; importing this package never
loads them."""

from __future__ import annotations

from alkera_core.models import files, model_catalog
from alkera_core.models._enums import (
    DeviceAuthStatus,
    InvitationStatus,
    MembershipStatus,
    PlatformRole,
    TeamRole,
    TokenType,
)
from alkera_core.models.account_lifecycle import AccountDeletionRequest, AccountExportRequest
from alkera_core.models.allocations import TeamAllocation
from alkera_core.models.audit_log import AuditLog
from alkera_core.models.auth_refresh_token import AuthRefreshToken
from alkera_core.models.auth_session_org_grant import AuthSessionOrgGrant
from alkera_core.models.auth_token import AuthToken
from alkera_core.models.ban import EmailDomainBan, UserBan
from alkera_core.models.chat_attachment import ChatAttachment
from alkera_core.models.chat_read_mark import ChatReadMark
from alkera_core.models.chat_workspace_state import ChatWorkspaceState
from alkera_core.models.ci_token import CiToken
from alkera_core.models.compute import (
    ComputeAllocation,
    ComputeAllocationEvent,
    ComputeGrant,
    ComputeMachineType,
)
from alkera_core.models.compute_offerings import ComputeOffering, ComputeOfferingOrg
from alkera_core.models.crash_report import CrashReport
from alkera_core.models.crdt_doc import CrdtDoc, CrdtPeer, CrdtUpdate
from alkera_core.models.deployment_health import DeploymentHealthCheck, DeploymentHealthRun
from alkera_core.models.device_authorization import DeviceAuthorization
from alkera_core.models.entitlement_grant import EntitlementGrant
from alkera_core.models.event_outbox import EventOutbox
from alkera_core.models.files import (
    DedupDomain,
    FileAcl,
    FileAclMember,
    FileConflict,
    FileContentGrant,
    FileDirStats,
    FileDirStatsDelta,
    FileDrive,
    FileErasureLog,
    FileHistory,
    FileHold,
    FileIdempotencyKey,
    FileKeyChunk,
    FileLease,
    FileLeaseEpochHwm,
    FileLeaseLiveEntry,
    FileLink,
    FileLock,
    FileManifest,
    FileNode,
    FileOp,
    FilePack,
    FilePageGrant,
    FilePlatform,
    FileQuarantine,
    FileRetentionLabel,
    FileShare,
    FileStageJob,
    FileStar,
    FileStore,
    FileSweepShard,
    FileTrashOp,
    FileUploadPart,
    FileUploadSession,
    FileVersion,
    ManifestTerm,
    StorageUsageSnapshot,
)
from alkera_core.models.identity_org_creation import IdentityOrgCreation
from alkera_core.models.identity_security_event import IdentitySecurityEvent
from alkera_core.models.invitation import Invitation
from alkera_core.models.login_lockout import LoginLockout
from alkera_core.models.machine_credential import MachineCredential, OrgComputeAssignment
from alkera_core.models.model_provider_config import ModelProviderConfig
from alkera_core.models.oauth_identity import OAuthIdentity
from alkera_core.models.org_audit_event import OrgAuditEvent
from alkera_core.models.org_machines import (
    OrgComputeSettings,
    OrgMachine,
    OrgMachineAudience,
    WorkspaceMachineMove,
)
from alkera_core.models.org_membership import OrgMembership
from alkera_core.models.org_settings import OrgSettings
from alkera_core.models.org_sync_settings import OrgSyncSettings
from alkera_core.models.personal_access_token import PersonalAccessToken
from alkera_core.models.proxy_token import ProxyToken
from alkera_core.models.rate_limit_window import RateLimitWindow
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.models.realtime_presence import RealtimePresence
from alkera_core.models.role_assignment import RoleAssignment
from alkera_core.models.saml_replay import SamlReplayAssertion
from alkera_core.models.signup_funnel import SignupClick
from alkera_core.models.ssh_machines import SshMachineEndpoint
from alkera_core.models.sso_connection import SsoConnection
from alkera_core.models.sso_domain_claim import SsoDomainClaim
from alkera_core.models.sso_link_request import SsoLinkRequest
from alkera_core.models.storage_limits import OrgStorageLimit, UserStorageLimit
from alkera_core.models.team import Team
from alkera_core.models.team_membership import TeamMembership
from alkera_core.models.user import User
from alkera_core.models.user_org_preference import UserOrgPreference
from alkera_core.models.user_preference import UserPreference
from alkera_core.models.workspace_object import (
    ChatMessage,
    ObjectPayloadRow,
    WorkspaceObject,
)
from alkera_core.models.ws_ticket_use import WsTicketUse
from alkera_core.notebooks.models import (
    NotebookEdit,
    NotebookEpochTail,
    NotebookKernel,
    NotebookPeer,
    NotebookRun,
)

__all__ = [
    "AccountDeletionRequest",
    "AccountExportRequest",
    "AuditLog",
    "AuthRefreshToken",
    "AuthSessionOrgGrant",
    "AuthToken",
    "ChatAttachment",
    "ChatMessage",
    "ChatReadMark",
    "ChatWorkspaceState",
    "CiToken",
    "ComputeAllocation",
    "ComputeAllocationEvent",
    "ComputeGrant",
    "ComputeMachineType",
    "ComputeOffering",
    "ComputeOfferingOrg",
    "CrashReport",
    "CrdtDoc",
    "CrdtPeer",
    "CrdtUpdate",
    "DedupDomain",
    "DeploymentHealthCheck",
    "DeploymentHealthRun",
    "DeviceAuthStatus",
    "DeviceAuthorization",
    "EmailDomainBan",
    "EntitlementGrant",
    "EventOutbox",
    "FileAcl",
    "FileAclMember",
    "FileConflict",
    "FileContentGrant",
    "FileDirStats",
    "FileDirStatsDelta",
    "FileDrive",
    "FileErasureLog",
    "FileHistory",
    "FileHold",
    "FileIdempotencyKey",
    "FileKeyChunk",
    "FileLease",
    "FileLeaseEpochHwm",
    "FileLeaseLiveEntry",
    "FileLink",
    "FileLock",
    "FileManifest",
    "FileNode",
    "FileOp",
    "FilePack",
    "FilePageGrant",
    "FilePlatform",
    "FileQuarantine",
    "FileRetentionLabel",
    "FileShare",
    "FileStageJob",
    "FileStar",
    "FileStore",
    "FileSweepShard",
    "FileTrashOp",
    "FileUploadPart",
    "FileUploadSession",
    "FileVersion",
    "IdentityOrgCreation",
    "IdentitySecurityEvent",
    "Invitation",
    "InvitationStatus",
    "LoginLockout",
    "MachineCredential",
    "ManifestTerm",
    "MembershipStatus",
    "ModelProviderConfig",
    "NotebookEdit",
    "NotebookEpochTail",
    "NotebookKernel",
    "NotebookPeer",
    "NotebookRun",
    "OAuthIdentity",
    "ObjectPayloadRow",
    "OrgAuditEvent",
    "OrgComputeAssignment",
    "OrgComputeSettings",
    "OrgMachine",
    "OrgMachineAudience",
    "OrgMembership",
    "OrgSettings",
    "OrgStorageLimit",
    "OrgSyncSettings",
    "PersonalAccessToken",
    "PlatformRole",
    "ProxyToken",
    "RateLimitWindow",
    "RealtimeDoc",
    "RealtimePresence",
    "RoleAssignment",
    "SamlReplayAssertion",
    "SignupClick",
    "SshMachineEndpoint",
    "SsoConnection",
    "SsoDomainClaim",
    "SsoLinkRequest",
    "StorageUsageSnapshot",
    "Team",
    "TeamAllocation",
    "TeamMembership",
    "TeamRole",
    "TokenType",
    "User",
    "UserBan",
    "UserOrgPreference",
    "UserPreference",
    "UserStorageLimit",
    "WorkspaceMachineMove",
    "WorkspaceObject",
    "WsTicketUse",
    "files",
    "model_catalog",
]
