"""Federated-login decision tree + registration completion.

Routes stay thin; this module owns the logic that turns a verified
`FederatedProfile` into either a logged-in user or a registration ticket, and
later turns a completed registration into a user. The provider adapters
(Google/GitHub/OIDC/SAML/mock) never appear here — only their normalized
output — so the policy is identical across providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from alkera_core.auth import (
    RegisterTicket,
    encode_register_ticket,
    revoke_all_for_user,
    sign_in_policy,
)
from alkera_core.auth.tenancy import multi_org_enabled
from alkera_core.config import settings
from alkera_core.events import actor_for_user
from alkera_core.models import MembershipStatus, OAuthIdentity, TeamRole, User
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.email_policy import is_personal_email
from backend.auth.oauth import FederatedProfile
from backend.services.identity import email_verification as email_verification_service
from backend.services.identity import org_choice
from backend.services.identity import sso_link as sso_link_service
from backend.services.identity import users as user_service
from backend.services.identity import welcome as welcome_service
from backend.services.identity.users import UserConflictError
from backend.services.infra import now as _now
from backend.services.org import InvitationError, membership_in
from backend.services.org import invitations as invitation_service
from backend.services.org import memberships as membership_service
from backend.services.org import settings as org_settings_service
from backend.services.org import teams as team_service


class OAuthLoginBlockedError(Exception):
    """Sign-in refused for a reason the user can't (or shouldn't) work around.

    `reason` is a short, non-sensitive code surfaced to the SPA as
    `?oauth_error=<reason>`. It never reveals whether an account exists.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OAuthRegistrationError(Exception):
    """Registration completion failed (bad/expired ticket, org conflict, …)."""


class OAuthAccountExistsError(OAuthRegistrationError):
    """Registration for an address that already has an account, while
    multi-org is on: the person signs in instead, keeping ``invite_token``
    (the invitation the registration carried, if any)."""

    def __init__(self, invite_token: str | None) -> None:
        super().__init__("An account with that email already exists. Sign in instead.")
        self.invite_token = invite_token


class OAuthIdentityConflictError(Exception):
    """A federated-identity row collided with the (provider, subject) or
    (provider, user_id) unique constraint — i.e. the link already exists.

    Raised by `_link_identity` so callers map it to a clean user-facing outcome
    (a returning login, "already linked", or "already registered") instead of a
    raw 500 from the DB driver."""


@dataclass(slots=True)
class LoginOutcome:
    """The profile resolved to an existing account — issue a session.

    ``org_team_id`` is the org a social sign-in lands in (None: the home org);
    ``choose_org`` asks the client to offer the org chooser, because the org
    the person used last needs a step-up first."""

    user: User
    org_team_id: UUID | None = None
    choose_org: bool = False


@dataclass(slots=True)
class RegisterOutcome:
    """No account yet — hand the SPA a signed ticket to finish registration."""

    ticket: str


@dataclass(slots=True)
class SsoLinkOutcome:
    """An org's IdP asserted an identity that exists but holds no active
    membership in the org. Nothing was linked: the assertion is parked under
    ``token`` (the browser's cookie) until the person confirms the link while
    signed in with one of the identity's own methods."""

    token: str
    org_team_id: UUID


ResolveOutcome = LoginOutcome | RegisterOutcome | SsoLinkOutcome


@dataclass(slots=True, frozen=True)
class IdpScope:
    """The authority granted to a per-org enterprise IdP (OIDC/SAML).

    An enterprise IdP is NOT globally authoritative — it speaks only for ONE org
    and only for the email domains staff assigned to that org
    (``alkera_core.auth.sso_domains``). ``resolve`` enforces both, so a
    misconfigured or malicious per-org IdP can never assert an identity outside
    its lane (the cross-org account-takeover gate)."""

    org_team_id: UUID
    domains: frozenset[str]


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower().strip()


async def _get_identity(db: AsyncSession, *, provider: str, subject: str) -> OAuthIdentity | None:
    return (
        await db.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == provider,
                OAuthIdentity.subject == subject,
            )
        )
    ).scalar_one_or_none()


async def _link_identity(
    db: AsyncSession,
    *,
    user_id: UUID,
    provider: str,
    subject: str,
    email: str | None,
    email_verified: bool,
    raw: dict[str, object] | None,
) -> OAuthIdentity:
    identity = OAuthIdentity(
        user_id=user_id,
        provider=provider,
        subject=subject,
        email_at_link=email,
        email_verified=email_verified,
        raw_profile=raw,
        last_login_at=_now(),
    )
    db.add(identity)
    try:
        await db.flush()
    except IntegrityError as exc:
        # (provider, subject) already linked, or this user already has this
        # provider linked (e.g. a second Google account whose email matches).
        # The failed flush leaves the session rollback-only. Reset it before
        # translating, or a caller that maps this to an error redirect exits
        # cleanly, `get_db` commits the poisoned session, and the user sees a
        # 500 instead of the redirect.
        await db.rollback()
        raise OAuthIdentityConflictError(str(exc)) from exc
    return identity


async def resolve(
    db: AsyncSession,
    profile: FederatedProfile,
    *,
    invite_token: str | None,
    idp_scope: IdpScope | None = None,
) -> ResolveOutcome:
    """Map a verified profile to a login or a registration ticket.

    When ``idp_scope`` is set the profile came from a per-org enterprise IdP, and
    the CROSS-ORG TAKEOVER GATE applies: the email's domain must be one the IdP is
    authoritative for, and the resolution may only ever touch a user in the IdP's
    own org — a match in any other org is refused (``sso_org_mismatch``). With no
    account, SSO JIT-provisions into the IdP's org instead of minting a ticket.

    With multi-org on, an identity that holds no active membership in the
    IdP's org (none, or one the org's SCIM provisioned as pending) is not
    refused but parked for a confirmation (``SsoLinkOutcome``); nothing is
    linked and nobody joins anything here.

    Raises `OAuthLoginBlockedError` when sign-in is refused (provider disabled for
    the org, unverified provider email matching an existing account, …).
    """
    if not profile.email:
        raise OAuthLoginBlockedError("no_email")

    if idp_scope is not None and _email_domain(profile.email) not in idp_scope.domains:
        # The IdP asserted an address outside the domains its org owns. Fail
        # closed — this is the first line of the takeover gate.
        raise OAuthLoginBlockedError("sso_domain_mismatch")

    # (a) Known identity → log that user in.
    identity = await _get_identity(db, provider=profile.provider, subject=profile.subject)
    if identity is not None:
        user, banned = await user_service.get_by_id_with_ban(db, identity.user_id)
        if user is None:  # dangling link (user deleted) — treat as fresh
            await db.delete(identity)
            await db.flush()
        elif not banned:
            if not user.is_active:
                raise OAuthLoginBlockedError("account_deactivated")
            if idp_scope is None and not profile.email_verified:
                # A social provider that does not vouch for the address proves
                # only that someone holds this provider account, which is how a
                # squatter's link was made. Such a link is never a way in.
                raise OAuthLoginBlockedError("email_unverified")
            if idp_scope is not None:
                parked = await _park_for_link(db, user, profile, idp_scope)
                if parked is not None:
                    return parked
                await _assert_member_of(db, user, idp_scope.org_team_id)
            landing = await _assert_sign_in_allowed(
                db, user, provider=profile.provider, idp_scope=idp_scope
            )
            identity.last_login_at = _now()
            identity.email_at_link = profile.email
            identity.email_verified = profile.email_verified
            identity.raw_profile = profile.raw
            await db.flush()
            return LoginOutcome(
                user=user, org_team_id=landing.org_team_id, choose_org=landing.choose_org
            )
        # A banned owner is resolved as if the link were nobody's: the row stays
        # (lifting the ban restores the link) and the profile falls through to
        # the branches an unknown person takes.

    # (b) Email matches an existing account → auto-link (verified emails only).
    #
    # TRUST BOUNDARY (read before adding a new provider): auto-linking by email
    # is only safe when the provider is authoritative for the address it asserts
    # AND honestly reports `email_verified`. Google + GitHub are (Google verifies
    # the address; GitHub reports per address whether it is verified). A *generic* per-org
    # OIDC/SAML IdP is NOT globally authoritative — it can assert
    # `verified: victim@other-org.com` and would otherwise take over that
    # account. For a per-org IdP the `idp_scope` org + domain gate below IS that
    # authority check; Google/GitHub (idp_scope=None) keep the verified-email gate.
    # A verified email is authority over the ADDRESS, not consent to add a new
    # credential to an account already protected by a second factor — see
    # `_assert_new_link_allowed`.
    existing, existing_banned = await user_service.get_by_email_with_ban(db, profile.email)
    if existing is not None and not existing_banned:
        if not existing.is_active:
            raise OAuthLoginBlockedError("account_deactivated")
        if idp_scope is not None:
            # THE cross-org takeover refusal: the IdP asserted an email that
            # resolves to an identity with no active membership in the IdP's
            # org. The domain check above can pass (an org may legitimately own
            # a domain) yet the matched identity belong elsewhere — refuse,
            # never link across the org boundary. An org's IdP never attaches
            # an existing identity to its org by itself: with multi-org on the
            # assertion waits for the identity's own confirmation instead.
            parked = await _park_for_link(db, existing, profile, idp_scope)
            if parked is not None:
                return parked
            await _assert_member_of(db, existing, idp_scope.org_team_id)
        elif not profile.email_verified:
            # Non-SSO: never bind a provider account to an existing user on the
            # strength of an unverified email — that's an account-takeover vector.
            raise OAuthLoginBlockedError("email_unverified")
        elif existing.provisioned_by is not None:
            # An org's IdP or SCIM made this account. A social provider vouching
            # for the address says nothing about which org's account it is: an
            # org that provisioned an address it does not own would otherwise
            # receive the real owner the first time they sign in with Google,
            # and its IdP would keep access. The account opens through the org's
            # own sign-in; a social account is added to it by its owner only.
            raise OAuthLoginBlockedError("sso_sign_in_required")
        # Checked BEFORE the link + the pre-hijack claim below, so a refused login
        # leaves no side effects.
        landing = await _assert_sign_in_allowed(
            db, existing, provider=profile.provider, idp_scope=idp_scope
        )
        _assert_new_link_allowed(existing, idp_scope=idp_scope)
        try:
            await _link_identity(
                db,
                user_id=existing.id,
                provider=profile.provider,
                subject=profile.subject,
                email=profile.email,
                email_verified=profile.email_verified,
                raw=profile.raw,
            )
        except OAuthIdentityConflictError as exc:
            # The user already has this provider linked to a *different* account
            # (e.g. signing in with a second Google account whose email matches).
            # Bail BEFORE any pre-hijack cleanup below so a refused login leaves
            # no side effects.
            raise OAuthLoginBlockedError("already_linked") from exc
        if existing.email_verified_at is None:
            # ACCOUNT-PRE-HIJACK DEFENSE. The matched account's email was never
            # verified, so it may have been *squatted*: an attacker can create
            # an account at the victim's address before the victim ever signs
            # in (e.g. a bare password signup, which issues a usable session
            # without proving the email). Linking a genuinely-verified provider
            # into that account silently merges the victim into the squatter's
            # account, leaving the squatter co-resident (they still know the
            # password they set). The provider has *verified* this address, so
            # it is the authoritative owner and claims the account — and we evict
            # whoever set it up. Runs only after the link above succeeds, so the
            # `already_linked` refusal never triggers credential/session wipes.
            await _claim_unverified_account(db, existing, proved_by=profile)
        return LoginOutcome(
            user=existing, org_team_id=landing.org_team_id, choose_org=landing.choose_org
        )

    # (c) No account.
    if idp_scope is not None:
        # SSO JIT-provisions directly into the IdP's org — no ticket/org-choice
        # step (the org is fixed by the connection; the IdP has vouched for the
        # email, which is in an allowed domain by the gate above).
        user = await _jit_provision(db, profile, org_team_id=idp_scope.org_team_id)
        return LoginOutcome(user=user)

    # Non-SSO: mint a registration ticket. We can't persist a user yet (every
    # user needs an org, chosen during registration). Only for an address the
    # provider verified: an account made on an unproven address is a squat on
    # whoever owns it.
    if not profile.email_verified:
        raise OAuthLoginBlockedError("email_unverified")
    ticket = encode_register_ticket(
        provider=profile.provider,
        subject=profile.subject,
        email=profile.email,
        email_verified=profile.email_verified,
        first_name=profile.first_name,
        last_name=profile.last_name,
        invite_token=invite_token,
    )
    return RegisterOutcome(ticket=ticket)


async def _park_for_link(
    db: AsyncSession, user: User, profile: FederatedProfile, idp_scope: IdpScope
) -> SsoLinkOutcome | None:
    """Park the assertion for the identity's confirmation when multi-org is on
    and ``user`` holds no active membership in the IdP's org: none at all, or
    a pending one the org's SCIM provisioned (the confirmation activates it).
    None otherwise: an active member signs in as before, and a deactivated
    one is refused by `_assert_member_of`."""
    if not multi_org_enabled():
        return None
    membership = await membership_in(db, user_id=user.id, org_team_id=idp_scope.org_team_id)
    if membership is not None and membership.status is not MembershipStatus.PENDING:
        return None
    # Staff assigned the org the email's domain, which says the address is its
    # to assert. It does not say this person agreed to join, so every
    # attachment still waits for the person.
    token = await sso_link_service.park(db, org_team_id=idp_scope.org_team_id, profile=profile)
    return SsoLinkOutcome(token=token, org_team_id=idp_scope.org_team_id)


async def _assert_member_of(db: AsyncSession, user: User, org_team_id: UUID) -> None:
    """Refuse an SSO sign-in for an identity that is not an active member of
    the IdP's org: ``sso_org_mismatch`` with no membership there (the IdP has
    no authority over this identity), ``account_deactivated`` when the org
    deactivated it."""
    membership = await membership_in(db, user_id=user.id, org_team_id=org_team_id)
    if membership is None:
        raise OAuthLoginBlockedError("sso_org_mismatch")
    if not membership.is_active:
        raise OAuthLoginBlockedError("account_deactivated")


async def _jit_provision(db: AsyncSession, profile: FederatedProfile, *, org_team_id: UUID) -> User:
    """Create + link a brand-new SSO user in the IdP's org, as a member. The IdP
    has vouched for the email (and it's in an allowed domain), so it's marked
    verified. Caller has already enforced the cross-org gate."""
    try:
        user = await user_service.create_user(
            db,
            org_team_id=org_team_id,
            email=profile.email,
            first_name=profile.first_name,
            last_name=profile.last_name,
            password=None,
        )
    except UserConflictError as exc:
        # Lost a race with another concurrent JIT for the same email — treat as
        # "already registered, log in".
        raise OAuthLoginBlockedError("already_linked") from exc
    user.provisioned_by = "sso"
    await membership_service.add_member(
        db,
        team_id=org_team_id,
        user_id=user.id,
        role=TeamRole.MEMBER,
        actor=actor_for_user(user, org_id=org_team_id),
    )
    await _link_identity(
        db,
        user_id=user.id,
        provider=profile.provider,
        subject=profile.subject,
        email=profile.email,
        email_verified=True,
        raw=profile.raw,
    )
    await email_verification_service.mark_verified(db, user, by_user=True)
    return user


async def _assert_sign_in_allowed(
    db: AsyncSession, user: User, *, provider: str, idp_scope: IdpScope | None
) -> org_choice.Landing:
    """Hold a federated sign-in to the org's sign-in policy
    (``alkera_core.auth.sign_in_policy``): a provider the org switched off is
    refused (``provider_disabled``), and a social provider is never a second
    front door around the org's own IdP (``sso_required``). An SSO login
    (``idp_scope`` set) IS the org's IdP asserting the identity, so enforcement
    is satisfied by definition.

    Whether a brand-new link may be CREATED into the matched account is a
    separate question, answered by `_assert_new_link_allowed`.

    On an identity the account ALREADY holds, its own TOTP is deliberately NOT
    demanded on this leg. App-level MFA is the second factor on the LOCAL
    PASSWORD credential — `auth.login` asks for it because a password alone is
    weak. A federated sign-in is a different credential chain that the provider
    has already authenticated, with whatever factors the user keeps there; that
    is the trust model any product accepts by offering a "Sign in with Google"
    button at all. A tenant that wants a hard guarantee over every sign-in turns
    on SSO enforcement — that is the control for it, and unlike a post-redirect
    code prompt it also covers the accounts whose only credential is the
    provider.

    The org is the one the session is minted for: the IdP's org for an SSO
    sign-in; for Google and GitHub (a sign-in that names no org), the most
    recently used org whose policy admits the provider, else the home org.
    Returns where the sign-in lands.
    """
    if idp_scope is not None:
        landing = org_choice.Landing(org_team_id=idp_scope.org_team_id)
    else:
        landing = await org_choice.landing_org(db, user, method=provider)
    entering = landing.org_team_id
    # A person the org being entered has offboarded is refused as a
    # deactivated account, whichever provider vouches for them.
    standing = await membership_in(db, user_id=user.id, org_team_id=entering)
    if standing is not None and not standing.is_active:
        raise OAuthLoginBlockedError("account_deactivated")
    if idp_scope is not None:
        outcome = await sign_in_policy.evaluate(
            db,
            user=user,
            org_team_id=entering,
            family_id=None,
            method=sign_in_policy.SSO_METHOD,
        )
        refusal = outcome if isinstance(outcome, sign_in_policy.StepUp) else None
    else:
        refusal = landing.refusal
    if refusal is not None:
        raise OAuthLoginBlockedError(
            "provider_disabled" if refusal.kind == "login_method_not_allowed" else "sso_required"
        )
    return landing


def _assert_new_link_allowed(user: User, *, idp_scope: IdpScope | None) -> None:
    """Refuse to CREATE a brand-new provider link into an account that already
    carries its own second factor.

    Auto-linking by verified email is done *for* the account holder — they never
    asked for this provider to become a credential on their account. Doing it to
    an account that deliberately enrolled TOTP downgrades that account to a single
    factor: whoever controls the provider identity gets a full session without the
    code the owner's other sign-in demands, and the link appears without them
    approving it. An identity the account ALREADY holds is a different case (it was
    accepted once and keeps working); this gate is only about minting a new one.

    Two carve-outs keep the refusal from stranding anyone:

    - an account whose email was never verified is about to be claimed by the
      address's proven owner, and the claim evicts the factor a squatter planted —
      that MFA is not evidence of the rightful owner's intent;
    - an account with no local password has no other way in, so refusing would lock
      its owner out rather than protect them.

    An enterprise IdP (``idp_scope``) is the org's own authority asserting the
    identity under whatever factors the org enforces, so it is not gated here.
    """
    if idp_scope is not None:
        return
    if not user.mfa_enabled:
        return
    if user.email_verified_at is None or user.password_hash is None:
        return
    raise OAuthLoginBlockedError("mfa_link_required")


async def _claim_unverified_account(
    db: AsyncSession, user: User, *, proved_by: FederatedProfile
) -> None:
    """A verified federated login is taking ownership of a never-verified
    account (see the pre-hijack note in `resolve`). Called only after the new
    identity has been linked successfully.

    Evict any prior occupant of the claimed account:
    - wipe the local password (and pending reset token) so a squatter who set
      one loses email/password access,
    - clear any enrolled second factor — a squatter can enroll TOTP before the
      claim, and a factor left standing is one the attacker still holds (backup
      codes never expire) while the rightful owner cannot remove it: `/mfa/disable`
      demands a code only the attacker has, and there is no admin reset — and
    - unlink every provider identity but the one that just proved the
      address: a squatter's own provider link is a way back in that no
      password wipe touches, and
    - revoke every existing session (bumps `token_epoch` + the registry) so any
      live squatter session dies.

    Then stamp the email verified — the provider has now vouched for it. The
    fresh session minted for the rightful owner *after* `resolve` returns is
    issued past the new `token_epoch`, so it survives the revoke-all.
    """
    await user_service.clear_password(db, user)
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_backup_codes = None
    await db.execute(
        delete(OAuthIdentity).where(
            OAuthIdentity.user_id == user.id,
            ~(
                (OAuthIdentity.provider == proved_by.provider)
                & (OAuthIdentity.subject == proved_by.subject)
            ),
        )
    )
    await revoke_all_for_user(db, user.id)
    await email_verification_service.mark_verified(db, user, by_user=True)


async def complete_registration(
    db: AsyncSession,
    ticket: RegisterTicket,
    *,
    first_name: str,
    last_name: str,
    org_name: str | None,
    invite_token: str | None,
    allow_personal_email: bool = False,
) -> User:
    """Create the account behind a registration ticket and link the identity.

    The email/provider/subject come ONLY from the (server-signed) ticket. The
    org choice mirrors email/password signup: exactly one of `org_name` (new
    org) or an invite. An invite carried on the ticket itself wins.
    """
    if not ticket.email_verified:
        # Tickets are no longer minted for an unverified address; one minted
        # before that is refused here rather than redeemed.
        raise OAuthLoginBlockedError("email_unverified")
    effective_invite = ticket.invite_token or invite_token
    if (org_name is None) == (effective_invite is None):
        raise OAuthRegistrationError(
            "Provide exactly one: an organization name or an invitation token"
        )

    # Replay / race guard: if the email is already taken, the ticket was already
    # consumed (or the user signed up another way). Tell them to log in.
    if await user_service.get_by_email(db, ticket.email) is not None:
        if multi_org_enabled():
            raise OAuthAccountExistsError(effective_invite)
        raise OAuthRegistrationError("An account with that email already exists. Sign in instead.")

    # Business-email gate (defense in depth — the dedicated SPA page is the real
    # gate; the "continue anyway" path sends `allow_personal_email=true`).
    if not allow_personal_email and is_personal_email(ticket.email):
        raise OAuthLoginBlockedError("personal_email")

    if effective_invite is not None:
        return await _register_via_invite(
            db,
            ticket,
            first_name=first_name,
            last_name=last_name,
            invite_token=effective_invite,
        )
    assert org_name is not None  # narrowed by the XOR check
    return await _register_new_org(
        db, ticket, first_name=first_name, last_name=last_name, org_name=org_name
    )


async def _register_via_invite(
    db: AsyncSession,
    ticket: RegisterTicket,
    *,
    first_name: str,
    last_name: str,
    invite_token: str,
) -> User:
    invitation = await invitation_service.get_by_token(db, invite_token)
    if invitation is None or invitation.status.value != "pending":
        raise OAuthRegistrationError("Invitation not found")
    if invitation.email != ticket.email.lower().strip():
        raise OAuthRegistrationError("Email does not match the invited address")
    chain = await team_service.ancestor_chain(db, invitation.team_id)
    if not chain:
        raise OAuthRegistrationError("Invitation target missing")
    org_root = chain[-1]
    # An org may forbid a provider for its joiners.
    if not await org_settings_service.provider_allowed(db, org_root.id, ticket.provider):
        raise OAuthLoginBlockedError("provider_disabled")

    try:
        user = await user_service.create_user(
            db,
            org_team_id=org_root.id,
            email=ticket.email,
            first_name=first_name,
            last_name=last_name,
            password=None,
        )
    except UserConflictError as exc:
        raise OAuthRegistrationError(str(exc)) from exc
    try:
        await _link_identity(
            db,
            user_id=user.id,
            provider=ticket.provider,
            subject=ticket.subject,
            email=ticket.email,
            email_verified=ticket.email_verified,
            raw=None,
        )
    except OAuthIdentityConflictError as exc:
        # The provider identity is already registered (replay/race).
        raise OAuthRegistrationError(
            "An account with that email already exists. Sign in instead."
        ) from exc
    try:
        await invitation_service.accept_invitation(
            db, invitation, user=user, actor=actor_for_user(user, org_id=org_root.id)
        )
    except InvitationError as exc:
        raise OAuthRegistrationError(str(exc)) from exc
    await _finish_signup(db, user)
    return user


async def _register_new_org(
    db: AsyncSession,
    ticket: RegisterTicket,
    *,
    first_name: str,
    last_name: str,
    org_name: str,
) -> User:
    # New-org creation is implicitly allowed regardless of org settings — the
    # org has none yet and the creator chose the provider. The deployment's
    # signup mode still decides whether a stranger may found one at all.
    if not settings.public_signup_open:
        raise OAuthRegistrationError("Sign-up is by invitation only. Ask an admin to invite you.")
    try:
        _org, user = await team_service.create_org_with_admin(
            db,
            org_name=org_name,
            admin_email=ticket.email,
            admin_first_name=first_name,
            admin_last_name=last_name,
            admin_password=None,
        )
    except UserConflictError as exc:
        raise OAuthRegistrationError(str(exc)) from exc
    try:
        await _link_identity(
            db,
            user_id=user.id,
            provider=ticket.provider,
            subject=ticket.subject,
            email=ticket.email,
            email_verified=ticket.email_verified,
            raw=None,
        )
    except OAuthIdentityConflictError as exc:
        raise OAuthRegistrationError(
            "An account with that email already exists. Sign in instead."
        ) from exc
    await _finish_signup(db, user)
    return user


async def _finish_signup(db: AsyncSession, user: User) -> None:
    """The provider vouched for the email (an unverified ticket is refused
    before an account exists), so the account starts verified."""
    await email_verification_service.mark_verified(db, user, by_user=True)
    # OAuth signups never hit the verify-email route, so welcome them here.
    # Once-ever guarded.
    await welcome_service.send_welcome_if_unsent(db, user)
