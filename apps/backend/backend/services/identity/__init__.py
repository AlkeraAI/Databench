"""Who a person is and how they sign in: accounts, OAuth and SSO, SCIM, MFA,
lockout, password reset, email verification, the device grant and the welcome
mail.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.services.identity.account_lifecycle import (
        deletion_read as deletion_read,
    )
    from backend.services.identity.account_lifecycle import (
        plan_for as plan_for,
    )
    from backend.services.identity.account_lifecycle import (
        present_plan as present_plan,
    )
    from backend.services.identity.admin_create import (
        AdminAdded as AdminAdded,
    )
    from backend.services.identity.admin_create import (
        add_by_email as add_by_email,
    )
    from backend.services.identity.device_authorization import (
        DeviceAuthError as DeviceAuthError,
    )
    from backend.services.identity.device_authorization import (
        RedeemError as RedeemError,
    )
    from backend.services.identity.email_verification import (
        VerificationError as VerificationError,
    )
    from backend.services.identity.email_verification import (
        verification_resend_available_at as verification_resend_available_at,
    )
    from backend.services.identity.mfa import (
        MfaError as MfaError,
    )
    from backend.services.identity.oauth import (
        LoginOutcome as LoginOutcome,
    )
    from backend.services.identity.oauth import (
        OAuthLoginBlockedError as OAuthLoginBlockedError,
    )
    from backend.services.identity.oauth import (
        OAuthRegistrationError as OAuthRegistrationError,
    )
    from backend.services.identity.oauth import (
        RegisterOutcome as RegisterOutcome,
    )
    from backend.services.identity.oauth import (
        SsoLinkOutcome as SsoLinkOutcome,
    )
    from backend.services.identity.org_choice import (
        Landing as Landing,
    )
    from backend.services.identity.org_choice import (
        account_exists_detail as account_exists_detail,
    )
    from backend.services.identity.org_choice import (
        enterable_org as enterable_org,
    )
    from backend.services.identity.org_choice import (
        joinable_org as joinable_org,
    )
    from backend.services.identity.org_choice import (
        org_landing as org_landing,
    )
    from backend.services.identity.org_choice import (
        org_membership_count as org_membership_count,
    )
    from backend.services.identity.org_choice import (
        org_role_in as org_role_in,
    )
    from backend.services.identity.org_choice import (
        org_sso_required_for as org_sso_required_for,
    )
    from backend.services.identity.org_choice import (
        switchable_orgs as switchable_orgs,
    )
    from backend.services.identity.password_reset import (
        PasswordResetError as PasswordResetError,
    )
    from backend.services.identity.scim import (
        ScimError as ScimError,
    )
    from backend.services.identity.signup_hooks import (
        SIGNUP_HOOKS as SIGNUP_HOOKS,
    )
    from backend.services.identity.signup_hooks import (
        run_signup_hooks as run_signup_hooks,
    )
    from backend.services.identity.sso import (
        PublicMailboxDomainError as PublicMailboxDomainError,
    )
    from backend.services.identity.sso import (
        UnknownOrgError as UnknownOrgError,
    )
    from backend.services.identity.sso import (
        assign_sso_domains as assign_sso_domains,
    )
    from backend.services.identity.sso import (
        sso_domains_of_org as sso_domains_of_org,
    )
    from backend.services.identity.sso_link import (
        SSO_LINK_TTL as SSO_LINK_TTL,
    )
    from backend.services.identity.sso_link import (
        SsoLinkError as SsoLinkError,
    )
    from backend.services.identity.sso_link import (
        sso_link_cancel as sso_link_cancel,
    )
    from backend.services.identity.sso_link import (
        sso_link_confirm as sso_link_confirm,
    )
    from backend.services.identity.sso_link import (
        sso_link_park as sso_link_park,
    )
    from backend.services.identity.sso_link import (
        sso_link_view as sso_link_view,
    )
    from backend.services.identity.step_up import (
        StepUpRefusedError as StepUpRefusedError,
    )
    from backend.services.identity.step_up import (
        require_current_factors as require_current_factors,
    )
    from backend.services.identity.step_up import (
        require_mfa_code as require_mfa_code,
    )
    from backend.services.identity.users import (
        UserConflictError as UserConflictError,
    )
    from backend.services.identity.users import (
        user_with_ban as user_with_ban,
    )

#: Each public name and the submodule that owns it. A name resolves on first
#: use, so importing one submodule of this package never imports its siblings.
_OWNERS: dict[str, str] = {
    "deletion_read": "backend.services.identity.account_lifecycle",
    "plan_for": "backend.services.identity.account_lifecycle",
    "present_plan": "backend.services.identity.account_lifecycle",
    "StepUpRefusedError": "backend.services.identity.step_up",
    "require_current_factors": "backend.services.identity.step_up",
    "require_mfa_code": "backend.services.identity.step_up",
    "verification_resend_available_at": "backend.services.identity.email_verification",
    "user_with_ban": "backend.services.identity.users",
    "account_exists_detail": "backend.services.identity.org_choice",
    "SsoLinkOutcome": "backend.services.identity.oauth",
    "SSO_LINK_TTL": "backend.services.identity.sso_link",
    "SsoLinkError": "backend.services.identity.sso_link",
    "sso_link_cancel": "backend.services.identity.sso_link",
    "sso_link_confirm": "backend.services.identity.sso_link",
    "sso_link_view": "backend.services.identity.sso_link",
    "sso_link_park": "backend.services.identity.sso_link",
    "joinable_org": "backend.services.identity.org_choice",
    "org_sso_required_for": "backend.services.identity.org_choice",
    "AdminAdded": "backend.services.identity.admin_create",
    "add_by_email": "backend.services.identity.admin_create",
    "DeviceAuthError": "backend.services.identity.device_authorization",
    "Landing": "backend.services.identity.org_choice",
    "LoginOutcome": "backend.services.identity.oauth",
    "MfaError": "backend.services.identity.mfa",
    "OAuthLoginBlockedError": "backend.services.identity.oauth",
    "OAuthRegistrationError": "backend.services.identity.oauth",
    "PasswordResetError": "backend.services.identity.password_reset",
    "RedeemError": "backend.services.identity.device_authorization",
    "RegisterOutcome": "backend.services.identity.oauth",
    "ScimError": "backend.services.identity.scim",
    "PublicMailboxDomainError": "backend.services.identity.sso",
    "UnknownOrgError": "backend.services.identity.sso",
    "assign_sso_domains": "backend.services.identity.sso",
    "sso_domains_of_org": "backend.services.identity.sso",
    "SIGNUP_HOOKS": "backend.services.identity.signup_hooks",
    "run_signup_hooks": "backend.services.identity.signup_hooks",
    "UserConflictError": "backend.services.identity.users",
    "VerificationError": "backend.services.identity.email_verification",
    "enterable_org": "backend.services.identity.org_choice",
    "org_landing": "backend.services.identity.org_choice",
    "org_membership_count": "backend.services.identity.org_choice",
    "org_role_in": "backend.services.identity.org_choice",
    "switchable_orgs": "backend.services.identity.org_choice",
}

__all__ = [
    "SIGNUP_HOOKS",
    "SSO_LINK_TTL",
    "AdminAdded",
    "DeviceAuthError",
    "Landing",
    "LoginOutcome",
    "MfaError",
    "OAuthLoginBlockedError",
    "OAuthRegistrationError",
    "PasswordResetError",
    "PublicMailboxDomainError",
    "RedeemError",
    "RegisterOutcome",
    "ScimError",
    "SsoLinkError",
    "SsoLinkOutcome",
    "StepUpRefusedError",
    "UnknownOrgError",
    "UserConflictError",
    "VerificationError",
    "account_exists_detail",
    "add_by_email",
    "assign_sso_domains",
    "deletion_read",
    "enterable_org",
    "joinable_org",
    "org_landing",
    "org_membership_count",
    "org_role_in",
    "org_sso_required_for",
    "plan_for",
    "present_plan",
    "require_current_factors",
    "require_mfa_code",
    "run_signup_hooks",
    "sso_domains_of_org",
    "sso_link_cancel",
    "sso_link_confirm",
    "sso_link_park",
    "sso_link_view",
    "switchable_orgs",
    "user_with_ban",
    "verification_resend_available_at",
]


def __getattr__(name: str) -> Any:
    owner = _OWNERS.get(name)
    if owner is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner_module(owner), name)


def _owner_module(owner: str) -> ModuleType:
    """Import ``owner`` through a literal import. The compiled CLI build follows
    only literal imports, so a name resolved by string would be missing there."""
    if owner == "backend.services.identity.account_lifecycle":
        from backend.services.identity import account_lifecycle

        return account_lifecycle
    if owner == "backend.services.identity.step_up":
        from backend.services.identity import step_up

        return step_up
    if owner == "backend.services.identity.admin_create":
        from backend.services.identity import admin_create

        return admin_create
    if owner == "backend.services.identity.device_authorization":
        from backend.services.identity import device_authorization

        return device_authorization
    if owner == "backend.services.identity.email_verification":
        from backend.services.identity import email_verification

        return email_verification
    if owner == "backend.services.identity.mfa":
        from backend.services.identity import mfa

        return mfa
    if owner == "backend.services.identity.oauth":
        from backend.services.identity import oauth

        return oauth
    if owner == "backend.services.identity.org_choice":
        from backend.services.identity import org_choice

        return org_choice
    if owner == "backend.services.identity.password_reset":
        from backend.services.identity import password_reset

        return password_reset
    if owner == "backend.services.identity.scim":
        from backend.services.identity import scim

        return scim
    if owner == "backend.services.identity.users":
        from backend.services.identity import users

        return users
    if owner == "backend.services.identity.sso_link":
        from backend.services.identity import sso_link

        return sso_link
    if owner == "backend.services.identity.signup_hooks":
        from backend.services.identity import signup_hooks

        return signup_hooks
    if owner == "backend.services.identity.sso":
        from backend.services.identity import sso

        return sso
    raise AssertionError(f"no import for {owner}")
