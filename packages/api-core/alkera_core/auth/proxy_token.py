"""Minting + hashing of org-scoped gateway proxy tokens.

A proxy token is a high-entropy bearer secret a self-hosted gateway presents to
Alkera's hosted gateway. Like the single-use email tokens, only its keyed HMAC
hash is stored (so a DB leak can't replay it); the raw secret is shown ONCE at
mint. The ``alk_proxy_`` prefix makes a leaked token greppable + lets the gateway
cheaply tell a proxy token apart from a JWT before any DB work.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.token_hash import hash_lookup_token, lookup_token_digests
from alkera_core.models import ProxyToken
from alkera_core.token_prefixes import PROXY_TOKEN_PREFIX

#: Human-recognizable prefix on the raw secret — a PUBLIC, greppable marker (the
#: secret entropy follows it), not itself a credential, so S105 is a false alarm.


def mint_proxy_token() -> tuple[str, str]:
    """Return ``(raw_secret, token_hash)``. Persist only the hash; surface the raw
    secret to the operator exactly once."""
    raw = PROXY_TOKEN_PREFIX + secrets.token_urlsafe(48)
    return raw, hash_lookup_token(raw)


def looks_like_proxy_token(raw: str) -> bool:
    """Cheap pre-check so the gateway can route a credential to the proxy-token
    path without attempting a JWT decode first."""
    return raw.startswith(PROXY_TOKEN_PREFIX)


#: Machine (billing-only) users live on this synthetic domain so org-member
#: queries can exclude them — no human account can hold an address here.
PROXY_MACHINE_EMAIL_DOMAIN = "svc.alkera.proxy"


def proxy_machine_email(org_team_id: object) -> str:
    return f"proxy-{getattr(org_team_id, 'hex', org_team_id)}@{PROXY_MACHINE_EMAIL_DOMAIN}"


def is_proxy_machine_email(email: str) -> bool:
    return email.endswith(f"@{PROXY_MACHINE_EMAIL_DOMAIN}")


async def resolve_active_proxy_token(
    db: AsyncSession, raw: str, *, now: datetime | None = None
) -> ProxyToken | None:
    """The live (non-revoked, non-expired) token matching ``raw``, or None."""
    now = now or datetime.now(UTC)
    # Match the active pepper's digest OR any retired one (proxy tokens are
    # long-lived, so a pepper rotation must not break a deployment's billing).
    token = (
        await db.execute(
            select(ProxyToken).where(ProxyToken.token_hash.in_(lookup_token_digests(raw)))
        )
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at <= now:
        return None
    return token
