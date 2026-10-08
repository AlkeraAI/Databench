"""Whether a sign-in may enter an org: the one place that decides it.

An identity authenticates (password, Google, GitHub, an SSO assertion) and a
login session (a refresh family) remembers how, in ``auth_session_org_grants``.
An org decides whether that is good enough to enter it, by its own rules and
nobody else's:

1. **Platform staff** pass, so a broken IdP or a strict toggle can never lock
   the platform out of an org it supports.
2. **SSO required.** When the org's connection is enabled and ``enforced``, an
   identity it governs (an email in a domain assigned to the org, or an
   identity already federated by the org's IdP) enters only through THAT org's
   IdP: a password, Google or GitHub sign-in is refused. A session the org
   admitted stays in for its normal lifetime, and a session already in another
   org enters this one only if it signed in through this org's IdP at some
   point. The membership's ``sso_exempt`` (the org's break-glass flag) passes.

   With ``settings.sso_strict_enforcement_enabled`` on, enforcement is
   stricter: a membership the org's SCIM provisioned is governed too, and
   every entry, every refresh included, needs a grant from the org's IdP no
   older than the connection's ``session_max_age_seconds``.
3. **Allowed methods.** The org may switch Google or GitHub sign-in off
   (``org_settings.allow_login_google`` / ``allow_login_github``); a session
   whose only sign-ins are switched-off methods does not enter.

Every rule reads the org being entered and that person's membership in it, so
one org's enforcement, exemption or toggle never reaches another org.

An SSO assertion is a grant for its own org only (``org_team_id`` set on the
grant): an org's IdP speaks for that org, so a session it started enters a
different org only with a sign-in of the identity's own (a password, Google,
GitHub) or that org's IdP.

Every sign-in path asks :func:`evaluate` (password, OAuth and SSO callbacks,
every refresh, device approval, a switch between orgs) and acts on the answer,
so a new session-minting path cannot skip the policy. The answer is
:class:`Allowed` or :class:`StepUp`, never an exception: what to do with a
step-up (a 403 on the password route, a login error on an OAuth callback, a
401 on a refresh) belongs to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.sso_domains import domains_of
from alkera_core.auth.tenancy import home_org_id, multi_org_enabled
from alkera_core.config import settings
from alkera_core.models import (
    AuthRefreshToken,
    AuthSessionOrgGrant,
    OAuthIdentity,
    OrgMembership,
    OrgSettings,
    SsoConnection,
    User,
)
from alkera_core.utils.email import email_domain

#: The sign-in method an org's SSO connection itself is.
SSO_METHOD = "sso"

#: The login methods an org may switch off, and the org setting that does it.
_TOGGLED_METHODS: dict[str, str] = {
    "google": "allow_login_google",
    "github": "allow_login_github",
}

#: Grant methods that record a session the server re-issued without the person
#: proving anything new ("sign out everywhere else"). They carry the grants of
#: the session they replace and are not themselves a sign-in.
_NOT_A_SIGN_IN: frozenset[str] = frozenset({"renewal"})

StepUpKind = Literal["sso_required", "login_method_not_allowed"]


@dataclass(frozen=True)
class Allowed:
    """The sign-in may enter the org."""


@dataclass(frozen=True)
class StepUp:
    """The sign-in may not enter the org as it is. ``kind`` names why;
    ``login_url`` is where the org's own sign-in starts, when there is one."""

    kind: StepUpKind
    login_url: str | None = None


@dataclass(frozen=True)
class Grant:
    """One way a login session authenticated: identity-level (``org_team_id``
    None) or for one org (an SSO assertion), and when."""

    org_team_id: UUID | None
    method: str
    authenticated_at: datetime


def sso_provider_key(org_team_id: UUID) -> str:
    """The stable ``oauth_identities.provider`` value for an org's SSO IdP."""
    return f"sso:{org_team_id.hex}"


def sso_login_url(org_team_id: UUID, *, protocol: str) -> str:
    """The browser-facing URL that starts this org's SSO sign-in (OIDC authz or
    SAML AuthnRequest)."""
    base = f"{settings.oauth_redirect_base}/api/v1/auth/sso/{org_team_id}"
    return f"{base}/saml/login" if protocol == "saml" else f"{base}/login"


def sso_max_age(connection: SsoConnection) -> timedelta:
    """How long an SSO grant for the connection's org stands."""
    return timedelta(seconds=connection.session_max_age_seconds)


async def family_grants(db: AsyncSession, family_id: UUID) -> list[Grant]:
    """Every grant the login session ``family_id`` holds."""
    rows = await db.execute(
        select(AuthSessionOrgGrant).where(AuthSessionOrgGrant.family_id == family_id)
    )
    return [
        Grant(org_team_id=row.org_team_id, method=row.method, authenticated_at=row.authenticated_at)
        for row in rows.scalars()
    ]


async def connection_for(db: AsyncSession, org_team_id: UUID) -> SsoConnection | None:
    """The org's SSO connection, whatever its state."""
    return (
        await db.execute(select(SsoConnection).where(SsoConnection.org_team_id == org_team_id))
    ).scalar_one_or_none()


def holds_fresh_sso(
    grants: list[Grant], *, org_team_id: UUID, connection: SsoConnection, now: datetime
) -> bool:
    """Whether ``grants`` include an assertion from the org's IdP young enough
    for the connection's max age."""
    oldest = now - sso_max_age(connection)
    return any(
        g.org_team_id == org_team_id and g.method == SSO_METHOD and g.authenticated_at >= oldest
        for g in grants
    )


async def has_fresh_sso_grant(
    db: AsyncSession, *, family_id: UUID | None, org_team_id: UUID
) -> bool:
    """Whether the login session ``family_id`` signed in through the org's IdP
    within the connection's max age. False with no session or no connection."""
    if family_id is None:
        return False
    connection = await connection_for(db, org_team_id)
    if connection is None:
        return False
    return holds_fresh_sso(
        await family_grants(db, family_id),
        org_team_id=org_team_id,
        connection=connection,
        now=datetime.now(UTC),
    )


def strict_enforcement() -> bool:
    """Whether an enforced org holds its members to the stricter rules (SCIM
    governs, a max age on every refresh). Read per call so a test can turn it
    on for itself alone."""
    return settings.sso_strict_enforcement_enabled


async def governs(
    db: AsyncSession,
    connection: SsoConnection,
    user: User,
    membership: OrgMembership | None,
) -> bool:
    """Whether the org's connection speaks for ``user``: their email is in a
    domain assigned to the org, or they hold an identity federated by the org's IdP;
    under strict enforcement, also when the org's SCIM provisioned their
    membership. Read for the connection's own org and no other."""
    if email_domain(user.email) in await domains_of(db, connection.org_team_id):
        return True
    if strict_enforcement() and membership is not None and membership.scim_external_id is not None:
        return True
    linked = await db.execute(
        select(OAuthIdentity.id)
        .where(
            OAuthIdentity.user_id == user.id,
            OAuthIdentity.provider == sso_provider_key(connection.org_team_id),
        )
        .limit(1)
    )
    return linked.first() is not None


async def _method_allowed(db: AsyncSession, org_team_id: UUID, method: str) -> bool:
    column = _TOGGLED_METHODS.get(method)
    if column is None:
        return True
    row = (
        await db.execute(select(OrgSettings).where(OrgSettings.org_team_id == org_team_id))
    ).scalar_one_or_none()
    # No row: the org never changed a default, and every method is allowed.
    return True if row is None else bool(getattr(row, column))


async def _membership(db: AsyncSession, user: User, org_team_id: UUID) -> OrgMembership | None:
    return (
        await db.execute(
            select(OrgMembership).where(
                OrgMembership.user_id == user.id, OrgMembership.org_team_id == org_team_id
            )
        )
    ).scalar_one_or_none()


async def _family_is_in(
    db: AsyncSession, *, family_id: UUID, user: User, org_team_id: UUID, predates_grants: bool
) -> bool:
    """Whether the login session ``family_id`` is already signed in to
    ``org_team_id``: the org its newest refresh token names. A family that
    names none is in the identity's home org while multi-org is off (the only
    org it can be in), and so is one that predates grants (it was started
    before an identity could belong to a second org); otherwise it is in no
    org."""
    named = (
        await db.execute(
            select(AuthRefreshToken.active_org_team_id)
            .where(AuthRefreshToken.family_id == family_id)
            .order_by(AuthRefreshToken.created_at.desc())
            .limit(1)
        )
    ).first()
    if named is None:
        return False
    current = named[0]
    if current is None:
        in_home = predates_grants or not multi_org_enabled()
        return in_home and home_org_id(user) == org_team_id
    return bool(current == org_team_id)


def _holds_any_sso(grants: list[Grant], *, org_team_id: UUID) -> bool:
    return any(g.org_team_id == org_team_id and g.method == SSO_METHOD for g in grants)


def account_grant_at(grants: list[Grant], *, user: User) -> datetime | None:
    """When the session last proved the identity itself, or None if it never
    did: a sign-in that names no org (a password, Google, GitHub, a signup, a
    credential change), or an assertion from the IdP of the identity's own home
    org. An assertion from any other org's IdP speaks for that org alone, so it
    never stands for the account: the org's admins run that IdP."""
    home = home_org_id(user)
    times = [
        g.authenticated_at
        for g in grants
        if (g.org_team_id is None and g.method not in _NOT_A_SIGN_IN and g.method != SSO_METHOD)
        or (g.org_team_id == home and g.method == SSO_METHOD)
    ]
    return max(times) if times else None


async def evaluate(
    db: AsyncSession,
    *,
    user: User,
    org_team_id: UUID,
    family_id: UUID | None,
    method: str | None,
) -> Allowed | StepUp:
    """Whether ``user`` may enter ``org_team_id``.

    ``family_id`` is the login session whose grants are weighed, when there is
    one; ``method`` is a sign-in happening right now (``password``, ``sso``, or
    an OAuth provider's key), weighed as a grant made this instant (an ``sso``
    sign-in is a grant for ``org_team_id``, the org whose IdP is asserting). A
    family with no grants at all predates grants and counts as signed in at an
    unknown time by a method no org switched off. An org that enforces SSO
    admits it by the org it is already in (see the module docstring); under
    strict enforcement it needs a fresh grant like any other session.
    """
    if user.platform_role is not None:
        return Allowed()
    now = datetime.now(UTC)
    grants = await family_grants(db, family_id) if family_id is not None else []
    predates_grants = family_id is not None and not grants
    if method is not None:
        grants.append(
            Grant(
                org_team_id=org_team_id if method == SSO_METHOD else None,
                method=method,
                authenticated_at=now,
            )
        )

    connection = await connection_for(db, org_team_id)
    membership = await _membership(db, user, org_team_id)
    if (
        connection is not None
        and connection.enabled
        and connection.enforced
        and not (membership is not None and membership.sso_exempt)
        and await governs(db, connection, user, membership)
    ):
        if strict_enforcement():
            if holds_fresh_sso(grants, org_team_id=org_team_id, connection=connection, now=now):
                return Allowed()
        elif _holds_any_sso(grants, org_team_id=org_team_id):
            return Allowed()
        elif (
            method is None
            and family_id is not None
            and await _family_is_in(
                db,
                family_id=family_id,
                user=user,
                org_team_id=org_team_id,
                predates_grants=predates_grants,
            )
        ):
            # A session the org already admitted carries on: enforcement is
            # checked at each sign-in and each entry, not re-checked by age.
            return Allowed()
        return StepUp(
            kind="sso_required",
            login_url=sso_login_url(org_team_id, protocol=connection.protocol),
        )

    if predates_grants:
        return Allowed()
    sign_ins = [
        g.method
        for g in grants
        if (g.org_team_id is None and g.method not in _NOT_A_SIGN_IN and g.method != SSO_METHOD)
        or (g.org_team_id == org_team_id and g.method == SSO_METHOD)
    ]
    for candidate in sign_ins:
        if await _method_allowed(db, org_team_id, candidate):
            return Allowed()
    return StepUp(kind="login_method_not_allowed")


__all__ = [
    "SSO_METHOD",
    "Allowed",
    "Grant",
    "StepUp",
    "StepUpKind",
    "account_grant_at",
    "connection_for",
    "evaluate",
    "family_grants",
    "governs",
    "has_fresh_sso_grant",
    "holds_fresh_sso",
    "sso_login_url",
    "sso_max_age",
    "sso_provider_key",
    "strict_enforcement",
]
