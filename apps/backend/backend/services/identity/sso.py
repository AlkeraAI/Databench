"""Per-org enterprise SSO: config CRUD, domain→org discovery, and building a
per-request OIDC provider from a stored connection.

The provider is built ON DEMAND from the org's row (never cached in the
process-wide registry), so a config change takes effect on the next sign-in and
one org's IdP can't leak into another's. The domains staff assigned the org
(``alkera_core.auth.sso_domains``) become the ``IdpScope`` the cross-org
takeover gate enforces in ``oauth_service.resolve``.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID, uuid4

from alkera_core.auth import sign_in_policy, sso_domains
from alkera_core.auth.revocation import revoke_memberships
from alkera_core.auth.secret_box import decrypt_secret, encrypt_secret
from alkera_core.auth.sign_in_policy import sso_login_url, sso_provider_key
from alkera_core.auth.token_hash import hash_lookup_token, lookup_token_digests
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, lock_or_insert
from alkera_core.models import (
    MembershipStatus,
    OAuthIdentity,
    OrgMembership,
    SamlReplayAssertion,
    SsoConnection,
    SsoDomainClaim,
    Team,
    TeamRole,
    User,
)
from alkera_core.token_prefixes import SCIM_TOKEN_PREFIX
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.email_policy import is_personal_email
from backend.auth.oauth.oidc import OidcProvider
from backend.auth.oauth.saml import SamlIdpConfig, SamlProvider, SamlSpConfig
from backend.services import audit as audit_services
from backend.services.identity.oauth import IdpScope
from backend.services.org import active_membership_in
from backend.services.org import memberships as membership_service

#: Greppable public prefix on a raw SCIM bearer token (entropy follows it).


# Spelled once, beside the sign-in policy that reads them; re-exported here
# for the SSO routes and services that always have.
provider_key = sso_provider_key
login_url = sso_login_url


def scim_base_url() -> str:
    """The SCIM 2.0 base URL the org admin configures in their IdP's connector.
    Mounted under ``/api/v1`` so the existing reverse proxy serves it; the IdP
    appends ``/Users`` etc."""
    return f"{settings.oauth_redirect_base}/api/v1/scim/v2"


async def scope_for(db: AsyncSession, connection: SsoConnection) -> IdpScope:
    """What the org's IdP may assert: addresses in the domains assigned to
    the org. An org with none can sign nobody in."""
    return IdpScope(
        org_team_id=connection.org_team_id,
        domains=await sso_domains.domains_of(db, connection.org_team_id),
    )


def role_for_groups(connection: SsoConnection, idp_groups: tuple[str, ...]) -> TeamRole | None:
    """The org role the connection's ``groups_mapping`` assigns to a user in these
    IdP groups, or None if no mapped group matches (→ leave the role untouched).
    Group names match case-insensitively; the HIGHEST mapped role wins (admin > member)."""
    mapping = connection.groups_mapping or {}
    if not mapping or not idp_groups:
        return None
    present = {g.lower() for g in idp_groups}
    matched: set[TeamRole] = set()
    for group, role_str in mapping.items():
        # Strip + lowercase the mapping key too (IdP group names arrive stripped),
        # so a stray space in the admin's mapping entry can't silently miss.
        if group.strip().lower() in present:
            try:
                matched.add(TeamRole(str(role_str).lower()))
            except ValueError:
                continue  # a bad role string in the mapping is ignored, not fatal
    if not matched:
        return None
    return TeamRole.ADMIN if TeamRole.ADMIN in matched else TeamRole.MEMBER


async def sync_org_role(
    db: AsyncSession, *, user: User, connection: SsoConnection, idp_groups: tuple[str, ...]
) -> TeamRole | None:
    """Reconcile ``user``'s org-root role to their IdP groups via the connection's
    mapping. Returns the role newly applied (for audit), or None when nothing
    changed — including when no mapped group matched (the role is then left as-is,
    so a manually-granted admin who isn't in any mapped group is never demoted) or
    when a demotion would strip the last org admin (guarded — left as admin).

    It only ever updates a seat the person already holds in the connection's
    org. An IdP never attaches anyone to an org: with no active membership, or
    no seat on the org's root team, nothing is written."""
    target = role_for_groups(connection, idp_groups)
    if target is None:
        return None
    org_membership = await active_membership_in(
        db, user_id=user.id, org_team_id=connection.org_team_id
    )
    if org_membership is None:
        return None
    membership = await membership_service.get(db, team_id=connection.org_team_id, user_id=user.id)
    if membership is None:
        return None
    if membership.role is target:
        return None
    try:
        await membership_service.change_role(db, membership, target)
    except membership_service.MembershipError:
        return None  # never demote the last org admin via SSO
    return target


async def governed_memberships(db: AsyncSession, connection: SsoConnection) -> list[OrgMembership]:
    """The org's active, non-exempt memberships the connection governs (see
    ``sign_in_policy.governs``), read in two statements however large the org."""
    linked = set(
        (
            await db.execute(
                select(OAuthIdentity.user_id).where(
                    OAuthIdentity.provider == provider_key(connection.org_team_id)
                )
            )
        )
        .scalars()
        .all()
    )
    domains = await sso_domains.domains_of(db, connection.org_team_id)
    rows = await db.execute(
        select(OrgMembership, User.email)
        .join(User, User.id == OrgMembership.user_id)
        .where(
            OrgMembership.org_team_id == connection.org_team_id,
            OrgMembership.status == MembershipStatus.ACTIVE,
            OrgMembership.sso_exempt.is_(False),
        )
    )
    return [
        membership
        for membership, email in rows.tuples().all()
        if membership.user_id in linked
        or membership.scim_external_id is not None
        or email.rsplit("@", 1)[-1].lower().strip() in domains
    ]


async def enforce_on(db: AsyncSession, connection: SsoConnection) -> int:
    """Enforcement was just turned on. Under strict enforcement
    (``settings.sso_strict_enforcement_enabled``), end, in this org only, every
    credential of a governed member (all of them are retired by the epoch move;
    a member signs in through the IdP again), so the control holds from that
    moment. Otherwise nothing is ended: the sessions, CLI tokens and access
    keys members hold run out on their own, and every new sign-in goes through
    the IdP. Returns how many memberships were revoked."""
    if not sign_in_policy.strict_enforcement():
        return 0
    memberships = await governed_memberships(db, connection)
    await revoke_memberships(db, memberships, reason="sso_enforced")
    await audit_services.record_security_events(
        db,
        user_ids=[m.user_id for m in memberships],
        event="auth.sso_enforced_signout",
        org_team_id=connection.org_team_id,
    )
    return len(memberships)


def idp_changed(existing: SsoConnection | None, **incoming: object) -> bool:
    """Whether a save moves the connection to a different IdP: another
    protocol, issuer, client, entity id, sign-in URL or certificate. An
    unchanged value, or one left out to keep the stored one (None), is not a
    change."""
    if existing is None:
        return False
    current = {
        "protocol": existing.protocol,
        "oidc_issuer": existing.oidc_issuer,
        "oidc_client_id": existing.oidc_client_id,
        "saml_entity_id": existing.saml_entity_id,
        "saml_sso_url": existing.saml_sso_url,
        "saml_x509_cert": existing.saml_x509_cert,
    }
    return any(value is not None and value != current[field] for field, value in incoming.items())


async def _locked_connection(
    db: AsyncSession, org_team_id: UUID, *, protocol: str | None = None
) -> SsoConnection:
    """The org's connection, made if missing (with ``protocol``, or the
    column's default), locked for the save: two admins saving at once are
    taken one after the other."""
    made = {"protocol": protocol} if protocol is not None else {}
    locked, _created = await lock_or_insert(
        db,
        LockRank.ORG_SETTINGS,
        select(SsoConnection)
        .where(SsoConnection.org_team_id == org_team_id)
        .execution_options(populate_existing=True),
        insert(SsoConnection).values(id=uuid4(), org_team_id=org_team_id, **made),
    )
    return locked.scalar_one()


async def get_connection(db: AsyncSession, org_team_id: UUID) -> SsoConnection | None:
    return (
        await db.execute(select(SsoConnection).where(SsoConnection.org_team_id == org_team_id))
    ).scalar_one_or_none()


async def find_enabled_connection_for_email(db: AsyncSession, email: str) -> SsoConnection | None:
    """The enabled SSO connection of the org ``email``'s domain is assigned
    to, if any. Used by domain discovery (email → which org's SSO to start). A
    domain belongs to at most one org, so there is never a choice to make."""
    domain = email.rsplit("@", 1)[-1].lower().strip()
    if not domain:
        return None
    return (
        await db.execute(
            select(SsoConnection)
            .join(SsoDomainClaim, SsoDomainClaim.org_team_id == SsoConnection.org_team_id)
            .where(
                SsoConnection.enabled.is_(True),
                SsoDomainClaim.domain == domain,
            )
        )
    ).scalar_one_or_none()


async def enforced_login_url(db: AsyncSession) -> str | None:
    """The SSO start URL the SPA should redirect to BEFORE the user types anything
    — i.e. when this (self-hosted) instance has exactly one enabled + enforced
    connection. Returns None on SaaS, or when zero/many orgs enforce SSO (an
    instance-wide redirect would be ambiguous with multiple orgs; the per-email
    discover path still enforces those).
    """
    if not settings.is_self_hosted:
        return None
    rows = (
        (
            await db.execute(
                select(SsoConnection).where(
                    SsoConnection.enabled.is_(True), SsoConnection.enforced.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        return None
    return login_url(rows[0].org_team_id, protocol=rows[0].protocol)


def build_oidc_provider(connection: SsoConnection) -> OidcProvider:
    """Construct the per-org OIDC client. Raises ValueError if the connection is
    not a fully-configured OIDC connection."""
    if connection.protocol != "oidc":
        raise ValueError(f"connection is not OIDC (protocol={connection.protocol!r})")
    if not (
        connection.oidc_issuer
        and connection.oidc_client_id
        and connection.oidc_client_secret_encrypted
    ):
        raise ValueError("OIDC connection is missing issuer / client_id / client_secret")
    return OidcProvider(
        key=provider_key(connection.org_team_id),
        issuer=connection.oidc_issuer,
        client_id=connection.oidc_client_id,
        client_secret=decrypt_secret(connection.oidc_client_secret_encrypted),
    )


async def consume_saml_assertion(
    db: AsyncSession, *, org_team_id: UUID, assertion_id: str, expires_at: datetime
) -> bool:
    """Record a one-time-use SAML assertion. Returns True on FIRST use, False if
    the (org, assertion id) was already consumed (a replay). Atomic via the unique
    constraint + INSERT … ON CONFLICT DO NOTHING."""
    stmt = (
        pg_insert(SamlReplayAssertion)
        .values(org_team_id=org_team_id, assertion_id=assertion_id, expires_at=expires_at)
        .on_conflict_do_nothing(constraint="uq_saml_replay_org_assertion")
        .returning(SamlReplayAssertion.id)
    )
    # A row comes back only when the INSERT actually happened; a conflict (replay)
    # returns nothing.
    return (await db.execute(stmt)).first() is not None


def saml_sp_config(org_team_id: UUID) -> SamlSpConfig:
    """This SP's per-org identity, as the org admin must register it with their
    IdP. The ACS is where the IdP POSTs the signed response; the entity id is the
    Audience the IdP must assert."""
    base = settings.oauth_redirect_base
    return SamlSpConfig(
        entity_id=f"{base}/saml/metadata/{org_team_id.hex}",
        acs_url=f"{base}/api/v1/auth/sso/{org_team_id}/saml/acs",
    )


def build_saml_provider(connection: SsoConnection) -> SamlProvider:
    if connection.protocol != "saml":
        raise ValueError(f"connection is not SAML (protocol={connection.protocol!r})")
    if not (connection.saml_entity_id and connection.saml_sso_url and connection.saml_x509_cert):
        raise ValueError("SAML connection is missing entity_id / sso_url / x509_cert")
    return SamlProvider(
        key=provider_key(connection.org_team_id),
        idp=SamlIdpConfig(
            entity_id=connection.saml_entity_id,
            sso_url=connection.saml_sso_url,
            x509_cert=connection.saml_x509_cert,
        ),
        sp=saml_sp_config(connection.org_team_id),
    )


async def upsert_saml_connection(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    idp_entity_id: str,
    sso_url: str,
    x509_cert: str | None,
    enabled: bool,
    enforced: bool = False,
    groups_mapping: dict[str, str] | None = None,
) -> SsoConnection:
    """Create or update the org's SAML connection. ``x509_cert=None`` keeps the
    stored cert (so editing other fields doesn't require re-pasting it)."""
    conn = await _locked_connection(db, org_team_id, protocol="saml")
    conn.protocol = "saml"
    conn.saml_entity_id = idp_entity_id
    conn.saml_sso_url = sso_url
    if x509_cert:
        conn.saml_x509_cert = x509_cert
    conn.enabled = enabled
    conn.enforced = enforced
    conn.groups_mapping = groups_mapping or None
    await db.flush()
    await db.refresh(conn)
    return conn


async def upsert_oidc_connection(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    issuer: str,
    client_id: str,
    client_secret: str | None,
    enabled: bool,
    enforced: bool = False,
    groups_mapping: dict[str, str] | None = None,
) -> SsoConnection:
    """Create or update the org's OIDC connection. ``client_secret=None`` keeps
    the stored secret (so editing other fields doesn't require re-entering it)."""
    conn = await _locked_connection(db, org_team_id, protocol="oidc")
    conn.protocol = "oidc"
    conn.oidc_issuer = issuer
    conn.oidc_client_id = client_id
    if client_secret:
        conn.oidc_client_secret_encrypted = encrypt_secret(client_secret)
    conn.enabled = enabled
    conn.enforced = enforced
    conn.groups_mapping = groups_mapping or None
    await db.flush()
    await db.refresh(conn)
    return conn


class UnknownOrgError(LookupError):
    """No org has that id."""


async def _require_org(db: AsyncSession, org_team_id: UUID) -> None:
    org = await db.get(Team, org_team_id)
    if org is None or not org.is_root:
        raise UnknownOrgError(org_team_id)


async def sso_domains_of_org(db: AsyncSession, org_team_id: UUID) -> tuple[str, ...]:
    """The org's SSO email domains, sorted. Raises :class:`UnknownOrgError`."""
    await _require_org(db, org_team_id)
    return await sso_domains.listed(db, org_team_id)


class PublicMailboxDomainError(ValueError):
    """The domain belongs to a public mailbox provider."""

    def __init__(self, domain: str) -> None:
        super().__init__(domain)
        self.domain = domain


async def assign_sso_domains(
    db: AsyncSession, org_team_id: UUID, values: list[str], *, assigned_by: UUID
) -> sso_domains.Assignment:
    """Make ``values`` the org's whole set of SSO email domains (staff only;
    the caller has decided that).

    Raises ``sso_domains.InvalidDomainError`` for a value that is not a domain,
    :class:`PublicMailboxDomainError` for a public mailbox provider's domain
    (nobody owns ``gmail.com``, so no org's IdP may speak for it), and
    ``sso_domains.DomainHeldError`` when another org holds one, and
    :class:`UnknownOrgError` for an id that names no org. Changes nothing when
    it raises."""
    await _require_org(db, org_team_id)
    domains = sso_domains.parse_domains(values)
    for domain in domains:
        if is_personal_email(f"x@{domain}"):
            raise PublicMailboxDomainError(domain)
    return await sso_domains.assign(db, org_team_id, domains, assigned_by_id=assigned_by)


# --------------------------------------------------------------------------- #
# SCIM 2.0 bearer token (per-org, rotatable; only the HMAC is stored)
# --------------------------------------------------------------------------- #


async def mint_scim_token(db: AsyncSession, org_team_id: UUID) -> tuple[SsoConnection, str]:
    """Mint (or rotate) the org's SCIM bearer token + enable SCIM. Returns
    ``(connection, raw_secret)`` — the raw secret is shown ONCE; only its HMAC is
    stored. Minting replaces any prior token (the old one stops working at once)."""
    # SCIM can be enabled without SSO login configured: a bare connection row.
    conn = await _locked_connection(db, org_team_id)
    raw = SCIM_TOKEN_PREFIX + secrets.token_urlsafe(48)
    conn.scim_token_hash = hash_lookup_token(raw)
    conn.scim_enabled = True
    await db.flush()
    await db.refresh(conn)
    return conn, raw


async def revoke_scim_token(db: AsyncSession, org_team_id: UUID) -> SsoConnection | None:
    """Disable SCIM + drop the token. Returns the connection, or None if absent."""
    conn = await get_connection(db, org_team_id)
    if conn is None:
        return None
    conn.scim_token_hash = None
    conn.scim_enabled = False
    await db.flush()
    await db.refresh(conn)
    return conn


async def resolve_scim_connection(db: AsyncSession, raw_token: str) -> SsoConnection | None:
    """The SCIM-enabled connection whose token matches ``raw_token`` (or None).
    Matches the active + any retired pepper digest so a pepper rotation doesn't
    break a configured IdP (see token_hash.lookup_token_digests)."""
    if not raw_token or not raw_token.startswith(SCIM_TOKEN_PREFIX):
        return None
    return (
        await db.execute(
            select(SsoConnection).where(
                SsoConnection.scim_token_hash.in_(lookup_token_digests(raw_token)),
                SsoConnection.scim_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
