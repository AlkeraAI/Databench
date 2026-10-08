"""Minting + bookkeeping for signed entitlement tokens (SaaS side).

Minting needs the Ed25519 signing seed (``ALKERA_ENTITLEMENTS_SIGNING_KEY``,
SaaS backend only). The serial embedded in the token is DB-assigned — insert
first, flush to get it, then sign. Minting a new grant auto-supersedes the
org's prior active grants (bookkeeping only; offline verification means an old
token still verifies until its expiry — the real enforcement).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from alkera_core.config import settings
from alkera_core.entitlements import Feature, mint_entitlement_token
from alkera_core.models import EntitlementGrant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def mint(
    db: AsyncSession,
    *,
    org_id: UUID,
    customer_slug: str,
    features: Feature,
    expires_on: date,
    issued_by_id: UUID,
) -> EntitlementGrant:
    """Mint + record a grant, superseding the org's prior active grants.

    Caller guarantees ``settings.entitlements_minting_configured`` (the route
    503s otherwise); a missing seed here is a programming error.
    """
    signing_key = settings.alkera_entitlements_signing_key
    if not signing_key:  # pragma: no cover - guarded by the route
        raise RuntimeError("entitlement minting requires ALKERA_ENTITLEMENTS_SIGNING_KEY")

    grant = EntitlementGrant(
        org_team_id=org_id,
        customer_slug=customer_slug,
        features=int(features),
        expires_on=expires_on,
        token="",  # filled after flush assigns the serial
        issued_by_id=issued_by_id,
    )
    db.add(grant)
    await db.flush()
    await db.refresh(grant)
    grant.token = mint_entitlement_token(
        customer=customer_slug,
        features=features,
        expires_on=expires_on,
        serial=grant.serial,
        signing_key_b64=signing_key,
    )

    now = datetime.now(UTC)
    prior = (
        (
            await db.execute(
                select(EntitlementGrant).where(
                    EntitlementGrant.org_team_id == org_id,
                    EntitlementGrant.superseded_at.is_(None),
                    EntitlementGrant.id != grant.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for old in prior:
        old.superseded_at = now
    await db.flush()
    return grant


async def list_for_org(db: AsyncSession, *, org_id: UUID) -> list[EntitlementGrant]:
    """Every grant ever issued to the org, newest first (superseded included —
    the list IS the audit trail)."""
    result = await db.execute(
        select(EntitlementGrant)
        .where(EntitlementGrant.org_team_id == org_id)
        .order_by(EntitlementGrant.serial.desc())
    )
    return list(result.scalars().all())


async def supersede(db: AsyncSession, *, org_id: UUID, grant_id: UUID) -> bool:
    """Mark a grant superseded (idempotent). False if missing / foreign to the
    org. Bookkeeping only — the token keeps verifying offline until expiry."""
    grant = await db.get(EntitlementGrant, grant_id)
    if grant is None or grant.org_team_id != org_id:
        return False
    if grant.superseded_at is None:
        grant.superseded_at = datetime.now(UTC)
        await db.flush()
    return True
