"""Personal-access-token lifecycle: mint, resolve, revoke, list.

The user-bound counterpart of the CI and proxy token services. The raw secret
exists only in the mint return value; the row stores its HMAC-SHA256 digest
under the shared lookup-token pepper, so a presented token is matched with
``token_hash IN (active digest, *previous-pepper digests)`` and a pepper
rotation never strands a live token.

Resolution is the whole point of this version (no route mints a token yet):
a token authenticates only while it is unrevoked and unexpired, its owner is
active, and the owner's membership in the token's org still stands. A token
names the membership it was minted through and that membership's credential
epoch; deactivating the membership or bumping its epoch refuses the token. A
row minted before tokens named a membership resolves by (owner, org) and counts
as epoch 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.auth.pat_token import mint_pat_token
from alkera_core.auth.tenancy import (
    MembershipRefused,
    assert_single_org,
    require_active_membership,
    verify_membership,
)
from alkera_core.auth.token_hash import lookup_token_digests
from alkera_core.models import PersonalAccessToken, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.identity import users as user_service


@dataclass(frozen=True, slots=True)
class ResolvedPat:
    """A live token and the active owner it stands in for."""

    token: PersonalAccessToken
    owner: User


async def mint(
    db: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    label: str | None,
    scopes: Sequence[str] = (),
    expires_at: datetime | None = None,
) -> tuple[PersonalAccessToken, str]:
    """Create a token for ``user_id`` in ``org_id``. Returns ``(row, raw_secret)``;
    the raw secret is never persisted.

    Raises ``ValueError`` when the user does not exist, is inactive, or holds no
    active membership in ``org_id`` (a token must never be minted across a
    tenancy boundary; which org is the person's home has no bearing on it),
    and when ``expires_at`` is already in the past (such a token could never
    authenticate, so minting it can only be a mistake).
    """
    owner = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if owner is None:
        raise ValueError("the token's owner must be a member of the org it is minted in")
    if not owner.is_active:
        raise ValueError("a deactivated user cannot hold a personal access token")
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ValueError("a personal access token cannot be minted already expired")
    try:
        membership = await require_active_membership(db, user_id=owner.id, org_team_id=org_id)
    except MembershipRefused as exc:
        raise ValueError("the token's owner must be a member of the org it is minted in") from exc
    raw, token_hash = mint_pat_token()
    token = PersonalAccessToken(
        org_team_id=org_id,
        user_id=owner.id,
        membership_id=membership.id,
        membership_epoch=membership.credential_epoch,
        token_hash=token_hash,
        label=label,
        scopes=list(scopes),
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    await db.refresh(token)
    return token, raw


async def resolve_active(
    db: AsyncSession, raw: str, *, now: datetime | None = None
) -> ResolvedPat | None:
    """The live token matching ``raw`` together with its owner, or ``None``.

    ``None`` when no row matches, the token is revoked or expired, or the owner
    is gone, banned or deactivated. Raises
    :class:`~alkera_core.auth.tenancy.MembershipRefused` when the owner's
    membership in the token's org no longer stands (or, while multi-org is
    off, the token's org is not the owner's home org).
    """
    now = now or datetime.now(UTC)
    token = (
        await db.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.token_hash.in_(lookup_token_digests(raw))
            )
        )
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at <= now:
        return None
    owner, banned = await user_service.get_by_id_with_ban(db, token.user_id)
    if owner is None or banned or not owner.is_active:
        return None
    assert_single_org(owner, org_team_id=token.org_team_id)
    await verify_membership(
        db,
        user_id=owner.id,
        org_team_id=token.org_team_id,
        membership_id=token.membership_id,
        membership_epoch=token.membership_epoch,
    )
    return ResolvedPat(token=token, owner=owner)


async def revoke(db: AsyncSession, *, org_id: UUID, token_id: UUID) -> bool:
    """Revoke a token IN the caller's org. Idempotent; ``False`` when not found
    or belonging to another org (indistinguishable on purpose)."""
    token = await db.get(PersonalAccessToken, token_id)
    if token is None or token.org_team_id != org_id:
        return False
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await db.flush()
    return True


async def list_for_user(
    db: AsyncSession, *, user_id: UUID, include_revoked: bool = False
) -> list[PersonalAccessToken]:
    """The user's tokens, newest first; live ones only unless asked."""
    query = select(PersonalAccessToken).where(PersonalAccessToken.user_id == user_id)
    if not include_revoked:
        query = query.where(PersonalAccessToken.revoked_at.is_(None))
    rows = (await db.execute(query.order_by(PersonalAccessToken.created_at.desc()))).scalars()
    return list(rows)


__all__ = ["ResolvedPat", "list_for_user", "mint", "resolve_active", "revoke"]
