"""Identity-domain schemas: auth, user, invitation."""

from __future__ import annotations

from alkera_core.schemas.identity.auth import (
    CompleteProfileRequest,
    DeviceApprovalRequest,
    DeviceInfoResponse,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    OAuthProvidersResponse,
    OAuthRegisterContext,
    OAuthRegisterRequest,
    SessionListResponse,
    SessionRead,
)
from alkera_core.schemas.identity.invitation import (
    InvitationAcceptResponse,
    InvitationCreate,
    InvitationPublicRead,
    InvitationRead,
)
from alkera_core.schemas.identity.user import (
    LinkedIdentity,
    LinkedIdentityList,
    UserBase,
    UserCreate,
    UserRead,
)

__all__ = [
    "CompleteProfileRequest",
    "DeviceApprovalRequest",
    "DeviceInfoResponse",
    "InvitationAcceptResponse",
    "InvitationCreate",
    "InvitationPublicRead",
    "InvitationRead",
    "LinkedIdentity",
    "LinkedIdentityList",
    "LoginRequest",
    "LoginResponse",
    "MessageResponse",
    "OAuthProvidersResponse",
    "OAuthRegisterContext",
    "OAuthRegisterRequest",
    "SessionListResponse",
    "SessionRead",
    "UserBase",
    "UserCreate",
    "UserRead",
]
