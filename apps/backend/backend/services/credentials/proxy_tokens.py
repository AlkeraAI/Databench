"""Org-scoped proxy tokens: mint / list / revoke.

A proxy token authenticates a self-hosted gateway (in proxy mode) to Alkera's
hosted gateway. The raw secret is returned ONCE at mint and only its hash is
stored. Usage under the token meters to the org (aggregate, no per-user
identity); billing sums it for the postpaid invoice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from alkera_core.auth.proxy_token import mint_proxy_token
from alkera_core.models import ProxyToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def mint(
    db: AsyncSession,
    *,
    org_id: UUID,
    created_by_id: UUID,
    label: str | None,
    expires_at: datetime | None = None,
) -> tuple[ProxyToken, str]:
    """Create a token; return (row, raw_secret). The raw secret is shown once.
    A pure connection credential — the org's plan figures (per-seat allotment,
    spend cap) live on its EnterprisePlan, never per token."""
    raw, token_hash = mint_proxy_token()
    token = ProxyToken(
        org_team_id=org_id,
        token_hash=token_hash,
        label=label,
        created_by_id=created_by_id,
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    await db.refresh(token)
    return token, raw


async def rotate(
    db: AsyncSession, *, org_id: UUID, token_id: UUID, created_by_id: UUID
) -> tuple[ProxyToken, str] | None:
    """Mint a FRESH secret for the org, copying the source token's label + expiry.
    The OLD token stays valid until explicitly revoked, so rotation is
    zero-downtime. Returns (new_token, raw_secret), or None if the source isn't
    found / foreign."""
    old = await db.get(ProxyToken, token_id)
    if old is None or old.org_team_id != org_id:
        return None
    return await mint(
        db,
        org_id=org_id,
        created_by_id=created_by_id,
        label=old.label,
        expires_at=old.expires_at,
    )


async def list_for_org(
    db: AsyncSession, *, org_id: UUID, include_revoked: bool = False
) -> list[ProxyToken]:
    query = select(ProxyToken).where(ProxyToken.org_team_id == org_id)
    if not include_revoked:
        query = query.where(ProxyToken.revoked_at.is_(None))
    rows = (await db.execute(query.order_by(ProxyToken.created_at.desc()))).scalars()
    return list(rows)


async def revoke(db: AsyncSession, *, org_id: UUID, token_id: UUID) -> bool:
    """Revoke a token IN the caller's org. Idempotent; False if not found / foreign."""
    token = await db.get(ProxyToken, token_id)
    if token is None or token.org_team_id != org_id:
        return False
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await db.flush()
    return True
