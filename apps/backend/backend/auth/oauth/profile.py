"""The normalized result every identity provider returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FederatedProfile:
    """Provider-agnostic identity, the only thing the decision tree consumes.

    Adapters (Google/GitHub/OIDC/SAML) translate their wire shapes into this.
    """

    provider: str
    subject: str
    email: str
    email_verified: bool
    first_name: str = ""
    last_name: str = ""
    # IdP group / role memberships (OIDC `groups` claim or SAML group attribute),
    # normalized to a list of names. Drives group→org-role mapping; empty when the
    # IdP sends none.
    idp_groups: tuple[str, ...] = ()
    # Full claims / userinfo as seen, for audit + future fields.
    raw: dict[str, Any] = field(default_factory=dict)
