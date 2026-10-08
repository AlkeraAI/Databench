"""Enterprise SSO: domain discovery + per-org IdP config (no secret ever read back)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from alkera_core.models.sso_connection import (
    SSO_MAX_AGE_DEFAULT_SECONDS,
    SSO_MAX_AGE_MAX_SECONDS,
    SSO_MAX_AGE_MIN_SECONDS,
)


class SsoExemptMember(BaseModel):
    """A break-glass member of this org: may sign in without the org's IdP even
    where the org enforces SSO. The flag is on their membership in this org and
    has no effect anywhere else."""

    user_id: UUID
    email: str
    sso_exempt: bool


class SsoExemptUpdateRequest(BaseModel):
    exempt: bool


class SsoDiscoverResponse(BaseModel):
    sso: bool
    login_url: str | None
    # SSO-only for this domain: the SPA should send the user straight to the IdP
    # and not offer the password form (only meaningful when sso is True).
    enforced: bool = False


class SsoDomainsRead(BaseModel):
    """The email domains assigned to an org's SSO connection."""

    org_id: UUID
    domains: list[str]


class SsoDomainsUpdate(BaseModel):
    """The org's whole set of SSO email domains, replacing what it had. Set by
    platform staff only."""

    domains: list[str] = Field(max_length=50)


class SsoConnectionRead(BaseModel):
    """The org's SSO config for the admin screen. The OIDC client secret + SAML
    cert are NEVER returned — only whether they're stored. For SAML, the SP
    values the org admin must register with their IdP are surfaced."""

    configured: bool
    enabled: bool
    # SSO-only: entering this org requires its IdP for every identity it governs.
    enforced: bool
    # How long an SSO sign-in into this org stands before the next refresh sends
    # the person back to the IdP.
    session_max_age_seconds: int = SSO_MAX_AGE_DEFAULT_SECONDS
    protocol: str
    # The email domains assigned to the org, comma-separated. Read-only here:
    # platform staff assign them.
    allowed_domains: str
    # OIDC
    oidc_issuer: str | None
    oidc_client_id: str | None
    has_client_secret: bool
    # SAML (IdP side)
    saml_idp_entity_id: str | None
    saml_sso_url: str | None
    has_saml_cert: bool
    # SAML (our SP side, for the admin to register with the IdP)
    saml_sp_entity_id: str | None = None
    saml_acs_url: str | None = None
    # IdP group → org role mapping: {idp_group_name: "admin" | "member"}.
    groups_mapping: dict[str, str] = Field(default_factory=dict)
    # SCIM 2.0 provisioning status (the token itself is never returned).
    scim_enabled: bool = False
    has_scim_token: bool = False
    # The base URL the admin configures in their IdP's SCIM connector.
    scim_base_url: str | None = None


class ScimTokenResponse(BaseModel):
    """The freshly-minted SCIM bearer token — shown ONCE; only its hash is stored."""

    token: str
    scim_base_url: str


class SsoConnectionUpdateRequest(BaseModel):
    protocol: Literal["oidc", "saml"] = "oidc"
    enabled: bool = False
    # SSO-only: entering this org requires its IdP for every identity it governs.
    # Turning it on requires the admin's own session to have signed in through
    # the org's IdP recently, and ends every credential in the org that was not.
    enforced: bool = False
    # Omit / null to keep the stored value (24 hours on a new connection).
    session_max_age_seconds: int | None = Field(
        default=None, ge=SSO_MAX_AGE_MIN_SECONDS, le=SSO_MAX_AGE_MAX_SECONDS
    )
    # IdP group → org role mapping: {idp_group_name: "admin" | "member"}. The full
    # set is replaced on each save (an empty object clears it).
    groups_mapping: dict[str, Literal["admin", "member"]] = Field(default_factory=dict)

    # --- OIDC (required when protocol='oidc') ---
    oidc_issuer: str | None = Field(default=None, max_length=512)
    oidc_client_id: str | None = Field(default=None, max_length=512)
    # Omit / null to KEEP the stored secret. Required on first OIDC configure.
    oidc_client_secret: str | None = Field(default=None, max_length=2048)

    # --- SAML (required when protocol='saml') ---
    saml_idp_entity_id: str | None = Field(default=None, max_length=512)
    saml_sso_url: str | None = Field(default=None, max_length=1024)
    # Omit / null to KEEP the stored cert. Required on first SAML configure.
    saml_x509_cert: str | None = Field(default=None, max_length=8192)


class SsoLinkRead(BaseModel):
    """`GET /auth/sso-link`: the org whose single sign-on is waiting to be
    linked to the signed-in identity, and the address its IdP asserted,
    masked."""

    org_team_id: UUID
    org_name: str
    email_masked: str


class SsoLinkConfirmResponse(BaseModel):
    """`POST /auth/sso-link/confirm`. ``joined`` is whether the person is now
    an active member of the org; when not, ``message`` says what to do next."""

    linked: bool
    joined: bool
    org_team_id: UUID
    message: str | None = None


class SsoLinkCancelResponse(BaseModel):
    """`POST /auth/sso-link/cancel`."""

    cancelled: bool
