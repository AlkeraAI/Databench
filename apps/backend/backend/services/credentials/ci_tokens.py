"""CI-token lifecycle -- mint, list, revoke, resolve (the ProxyToken pattern).

The raw secret exists only in the mint return value; the row stores the
HMAC-SHA256 digest under the shared lookup-token pepper, so verification reads
match with ``token_hash IN (active digest, *previous-pepper digests)`` and a
pepper rotation never strands a live token.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.auth.ci_token import mint_ci_token
from alkera_core.auth.token_hash import lookup_token_digests
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.models import CiToken
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


class CiTokenLimitError(Exception):
    """The org already holds the maximum number of live CI tokens.

    A ceiling, not a throttle. The in-process rate limiter bounds how FAST a
    caller can mint; this bounds how MANY exist at once, in Postgres, where the
    answer holds across every task and across any burst length. An external
    assessment minted 100 usable credentials in about a second precisely because
    nothing anywhere answered "how many is too many".
    """


async def count_active(db: AsyncSession, *, org_id: UUID, now: datetime | None = None) -> int:
    """How many live (non-revoked, non-expired) tokens the org holds."""
    now = now or datetime.now(UTC)
    return (
        await db.execute(
            select(func.count())
            .select_from(CiToken)
            .where(
                CiToken.org_team_id == org_id,
                CiToken.revoked_at.is_(None),
                (CiToken.expires_at.is_(None)) | (CiToken.expires_at > now),
            )
        )
    ).scalar_one()


async def mint(
    db: AsyncSession,
    *,
    org_id: UUID,
    created_by_id: UUID,
    label: str | None,
    repo: str | None = None,
    expires_at: datetime | None = None,
    max_active: int | None = None,
) -> tuple[CiToken, str]:
    """Create a token for ``org_id``. Returns ``(row, raw_secret)`` -- the raw
    secret is shown once by the route and never persisted.

    ``max_active`` caps how many live tokens the org may hold; ``None`` (the
    default) leaves the operator paths -- seeds, migrations, support tooling --
    uncapped, exactly like the password policy is a route-layer concern. The
    route passes the configured ceiling.
    """
    if max_active is not None:
        # Serialize per org for the rest of this transaction BEFORE counting.
        # Count-then-insert is a read-modify-write: without the lock, two mints
        # arriving together each read the pre-insert count, each see room, and
        # both insert -- so a ceiling meant to be exact leaks by however many
        # requests overlap. An advisory lock is the right shape here because
        # there is no single row to lock: the invariant is over a COUNT, and no
        # constraint can express "at most N". It is released on commit or
        # rollback, and it blocks only other mints for the SAME org.
        await advisory_xact_lock(db, advisory_key("ci-token-mint", org_id))
        if await count_active(db, org_id=org_id) >= max_active:
            raise CiTokenLimitError(
                f"This organization already has {max_active} active CI tokens. "
                "Revoke one before creating another."
            )
    raw, token_hash = mint_ci_token()
    token = CiToken(
        org_team_id=org_id,
        token_hash=token_hash,
        label=label,
        repo=repo.strip() if repo else None,
        created_by_id=created_by_id,
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    await db.refresh(token)
    return token, raw


async def list_for_org(
    db: AsyncSession, *, org_id: UUID, include_revoked: bool = False
) -> list[CiToken]:
    query = select(CiToken).where(CiToken.org_team_id == org_id)
    if not include_revoked:
        query = query.where(CiToken.revoked_at.is_(None))
    rows = (await db.execute(query.order_by(CiToken.created_at.desc()))).scalars()
    return list(rows)


async def has_active(db: AsyncSession, *, org_id: UUID, now: datetime | None = None) -> bool:
    """Whether the org holds at least one live (non-revoked, non-expired)
    token -- the member-visible "CI can upload" boolean; the tokens themselves
    stay admin-only."""
    now = now or datetime.now(UTC)
    query = (
        select(CiToken.id)
        .where(
            CiToken.org_team_id == org_id,
            CiToken.revoked_at.is_(None),
            (CiToken.expires_at.is_(None)) | (CiToken.expires_at > now),
        )
        .limit(1)
    )
    return (await db.execute(query)).scalar_one_or_none() is not None


async def revoke(db: AsyncSession, *, org_id: UUID, token_id: UUID) -> bool:
    """Revoke a token IN the caller's org. Idempotent; False if not found /
    belonging to another org (indistinguishable on purpose)."""
    token = await db.get(CiToken, token_id)
    if token is None or token.org_team_id != org_id:
        return False
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await db.flush()
    return True


async def revoke_where(
    db: AsyncSession, *, org_id: UUID, matches: Callable[[CiToken], bool]
) -> int:
    """Revoke every live token of the org the predicate selects; returns how
    many. The bulk path behind a lifecycle event that invalidates credentials
    wholesale -- releasing a GitHub installation, where a repo that leaves the
    org must not keep uploading with the token it already holds."""
    revoked = 0
    now = datetime.now(UTC)
    for token in await list_for_org(db, org_id=org_id):
        if matches(token):
            token.revoked_at = now
            revoked += 1
    if revoked:
        await db.flush()
    return revoked


async def resolve_active(
    db: AsyncSession, raw: str, *, now: datetime | None = None
) -> CiToken | None:
    """The live (non-revoked, non-expired) token matching ``raw``, or None."""
    now = now or datetime.now(UTC)
    token = (
        await db.execute(select(CiToken).where(CiToken.token_hash.in_(lookup_token_digests(raw))))
    ).scalar_one_or_none()
    if token is None or token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at <= now:
        return None
    return token
