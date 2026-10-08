"""Per-org enterprise SSO (OIDC / SAML) IdP configuration.

One connection per org (v1). The client secret is stored ENCRYPTED at rest (see
``alkera_core.auth.secret_box``). ``allowed_domains`` is the comma-separated
list of email domains the org's admin says this IdP speaks for. It is only what
was asked for: each entry is a claim in ``sso_domain_claims`` that counts once
the org proves it, and ``alkera_core.auth.sso_domains`` is the one reader that
decides anything from it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base

#: The bounds an org may set on how long an SSO sign-in stands before the next
#: refresh sends the person back to the IdP: one hour to thirty days.
SSO_MAX_AGE_MIN_SECONDS = 3600
SSO_MAX_AGE_MAX_SECONDS = 2_592_000
SSO_MAX_AGE_DEFAULT_SECONDS = 86_400


class SsoConnection(Base):
    __tablename__ = "sso_connections"
    __table_args__ = (
        CheckConstraint(
            f"session_max_age_seconds BETWEEN {SSO_MAX_AGE_MIN_SECONDS} "
            f"AND {SSO_MAX_AGE_MAX_SECONDS}",
            name="ck_sso_connections_session_max_age",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # SSO-only: when True (and enabled), entering this org requires a sign-in
    # through its IdP for every identity the connection governs. The server
    # enforces it (``alkera_core.auth.sign_in_policy``) at sign-in, at every
    # refresh and at device approval; the login screen's redirect is only the
    # UX on top. A membership's ``sso_exempt`` and platform staff pass, so a
    # broken IdP can never lock the org out. It governs this org and no other.
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # How long an SSO sign-in into this org stands: a browser session older than
    # this is sent back to the IdP at its next refresh.
    session_max_age_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=SSO_MAX_AGE_DEFAULT_SECONDS,
        server_default=text(str(SSO_MAX_AGE_DEFAULT_SECONDS)),
    )
    # "oidc" | "saml" — the wire the IdP speaks.
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, server_default="oidc")
    # Comma-separated email domains the admin listed (e.g. "acme.com,acme.io"),
    # written by ``sso_domains.sync_claims``. Listing proves nothing: read the
    # org's verified domains from ``alkera_core.auth.sso_domains`` instead.
    allowed_domains: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")

    # --- OIDC ---
    oidc_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oidc_client_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Encrypted at rest (never returned by any read API).
    oidc_client_secret_encrypted: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # --- SAML (seam; populated when the SAML provider lands) ---
    saml_entity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    saml_sso_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    saml_x509_cert: Mapped[str | None] = mapped_column(String(8192), nullable=True)

    # --- IdP group → org role mapping ---
    # {idp_group_name: "admin" | "member"}. On SSO login the user's mapped groups
    # set their org-root role (highest wins); an unmapped set leaves the role
    # untouched. NULL/empty = no mapping (everyone provisions as member).
    groups_mapping: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)

    # --- SCIM 2.0 provisioning (IdP-driven user lifecycle) ---
    scim_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # HMAC of the org's SCIM bearer token (the raw secret is shown once at mint).
    # NULL = no token provisioned. UNIQUE (a token resolves to exactly one org;
    # the resolver's scalar_one_or_none relies on it) — Postgres treats multiple
    # NULLs as distinct, so many token-less orgs are fine. Indexed for the lookup.
    scim_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
