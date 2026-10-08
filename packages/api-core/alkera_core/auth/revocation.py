"""Server-side token revocation.

JWT decode/verify is stateless (`tokens.py`); revocation is the stateful layer:

- a registry row per issued token (`AuthToken`), keyed by its `jti`, so a
  specific token can be revoked and a user's active sessions listed;
- a per-user `token_epoch` "revoke-all" lever — any token whose `iat` predates
  it is rejected. It is the lever for IDENTITY events (a password change, a
  platform ban or disable, "sign out everywhere", a platform role change);
- a per-membership lever, :func:`revoke_membership`, for everything an ORG
  decides (deactivating or removing a member, turning SSO enforcement on). It
  ends the person's credentials in that one org and nowhere else, because the
  identity may belong to other orgs.

The per-request check (`assert_token_active`) is hot, so:
- the `token_epoch` comparison is free (the caller already loaded the `User`);
- the revoked-`jti` lookup is served from a small in-process cache (≤5s stale,
  written-through on revoke) instead of querying Postgres every request.

The cache is keyed per `jti` and BOUNDED. It must never hold "every revoked
token on the deployment": that set is attacker-growable (any account can mint
and revoke tokens, and a revoked row stays live until its natural expiry — 90
days for a CLI token), it is cross-tenant, and it was rebuilt wholesale on the
caller's own request connection. A miss costs one indexed lookup instead.

No shared store by design (the deployment carries no cache tier). The cache is
a module-level singleton; each process/worker keeps its own — independent ≤5s
staleness across workers is acceptable, and a revoke is always durable in
Postgres regardless. Reusable as-is by the model-gateway.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.last_used import stamp_last_used
from alkera_core.auth.tenancy import users_homed_in
from alkera_core.auth.tokens import InvalidTokenError, SessionClaims
from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.models import (
    AuthRefreshToken,
    AuthToken,
    MachineCredential,
    OrgMembership,
    PersonalAccessToken,
    TokenType,
    User,
)
from alkera_core.models.compute import PERSONAL_TENANCY

_log = get_logger(__name__)

# How long a "this jti is NOT revoked" answer is trusted before it is re-checked
# against Postgres — i.e. the worst-case cross-process propagation delay of a
# revoke. A "revoked" answer never expires: revocation is one-way.
_CACHE_TTL_SECONDS = 5.0
# Hard ceiling on each of the cache's two maps. The DB is authoritative, so an
# eviction costs one indexed lookup and can never produce a wrong answer — which
# is what lets the bound be enforced unconditionally.
_CACHE_MAX_ENTRIES = 8192
# Don't write `last_used_at` on EVERY request — only once it's this stale. Bounds
# the idle-timeout write amplification to ~1 write / interval / active session.
_IDLE_UPDATE_INTERVAL_SECONDS = 30.0


class TokenRevokedError(InvalidTokenError):
    """The token's jti is revoked, or it predates the user's token_epoch.

    Subclasses `InvalidTokenError` so existing 401 handling covers it.
    """


class _RevocationCache:
    """Bounded, per-`jti` revocation answers backed by an indexed lookup.

    Two LRU maps, both capped at `_CACHE_MAX_ENTRIES`:
    - revoked jtis, kept for the life of the process (a revoke is one-way, so
      the answer can never go stale) and written through on revoke, which blocks
      a just-revoked token immediately within this process;
    - jtis last seen live, trusted for `_CACHE_TTL_SECONDS` so a revoke issued by
      another worker is picked up within that window.

    A miss runs a single `jti`-indexed query on the caller's session. That is the
    point: it costs O(1) per request regardless of how many tokens the whole
    deployment has revoked, whereas materialising the full revoked set scaled
    with a number any single tenant can inflate at will.
    """

    def __init__(self) -> None:
        self._revoked: OrderedDict[str, None] = OrderedDict()
        self._live: OrderedDict[str, float] = OrderedDict()

    async def is_revoked(self, db: AsyncSession, jti: str) -> bool:
        if jti in self._revoked:
            self._revoked.move_to_end(jti)
            return True
        checked_at = self._live.get(jti)
        if checked_at is not None and (time.monotonic() - checked_at) < _CACHE_TTL_SECONDS:
            self._live.move_to_end(jti)
            return False
        revoked = (
            await db.execute(
                select(AuthToken.id)
                .where(AuthToken.jti == jti, AuthToken.revoked_at.is_not(None))
                .limit(1)
            )
        ).first() is not None
        if revoked:
            self.mark_revoked(jti)
            return True
        self._remember_live(jti)
        return False

    def mark_revoked(self, jti: str) -> None:
        """Block `jti` immediately in this process (write-through on revoke)."""
        self._live.pop(jti, None)
        self._revoked[jti] = None
        self._revoked.move_to_end(jti)
        _trim(self._revoked)

    def _remember_live(self, jti: str) -> None:
        self._live[jti] = time.monotonic()
        self._live.move_to_end(jti)
        _trim(self._live)

    def reset(self) -> None:
        """Drop all cached state. For tests + a hard refresh."""
        self._revoked.clear()
        self._live.clear()


def _trim(entries: OrderedDict[str, None] | OrderedDict[str, float]) -> None:
    """Evict least-recently-used entries down to the cap."""
    while len(entries) > _CACHE_MAX_ENTRIES:
        entries.popitem(last=False)


_cache = _RevocationCache()


async def register_token(
    db: AsyncSession,
    *,
    claims: SessionClaims,
    token_type: TokenType,
    label: str | None = None,
) -> None:
    """Record a freshly-minted token in the registry.

    No-op for jti-less (legacy) tokens — nothing to register or later revoke.
    A token minted for a membership records it and its org, so the sessions
    list can name the org and a membership's tokens can be found.
    """
    if claims.jti is None:
        return
    bound = claims.membership_id is not None
    db.add(
        AuthToken(
            jti=claims.jti,
            user_id=claims.user_id,
            token_type=token_type,
            issued_at=datetime.fromtimestamp(claims.issued_at, tz=UTC),
            expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC),
            label=label,
            org_team_id=claims.org_team_id if bound else None,
            membership_id=claims.membership_id,
        )
    )
    await db.flush()


async def revoke_jti(db: AsyncSession, jti: str) -> None:
    """Revoke a single token. Idempotent. Written through to the cache."""
    await db.execute(
        update(AuthToken)
        .where(AuthToken.jti == jti, AuthToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    await db.flush()
    _cache.mark_revoked(jti)


async def revoke_family_access_tokens(db: AsyncSession, family_id: UUID) -> list[str]:
    """Revoke every access token minted in the refresh family ``family_id``, set-wise.
    Idempotent. Returns the jtis this call revoked.

    A token belongs to the family when the registry links it
    (``refresh_family_id``) or a row of the family was minted beside it
    (``access_jti``, which a process from before the link still writes)."""
    beside = select(AuthRefreshToken.access_jti).where(
        AuthRefreshToken.family_id == family_id, AuthRefreshToken.access_jti.is_not(None)
    )
    result = await db.execute(
        update(AuthToken)
        .where(
            or_(AuthToken.refresh_family_id == family_id, AuthToken.jti.in_(beside)),
            AuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(AuthToken.jti)
        .execution_options(synchronize_session=False)
    )
    jtis = list(result.scalars().all())
    await db.flush()
    for jti in jtis:
        _cache.mark_revoked(jti)
    return jtis


def _personal_boxes_of(user_ids: Sequence[UUID]) -> ColumnElement[bool]:
    """A live machine credential of a box on one of ``user_ids``' own hardware.

    A personal box is approved on the person's session through the device
    flow, so it is a credential the person's sign-in made: whoever held that
    session for a moment could have approved one. It ends wherever their
    sessions end."""
    return and_(
        MachineCredential.tenancy == PERSONAL_TENANCY,
        MachineCredential.created_by.in_(user_ids),
        MachineCredential.revoked_at.is_(None),
    )


async def revoke_all_for_user(db: AsyncSession, user_id: UUID) -> None:
    """Revoke every token for a user (logout-all / password reset / ban).

    Bumps `token_epoch` (catches even jti-less/unregistered tokens) AND marks
    the registry rows revoked (keeps the session list + cache accurate) AND
    ends every refresh-token family, so no browser can mint a fresh access
    token afterwards — a revoke-all that left the refresh half alive would
    only have signed the user out for as long as one access token lasts —
    AND revokes the machine credential of every box on the person's own
    hardware, which never expires and would otherwise outlive all of it.
    """
    await db.execute(update(User).where(User.id == user_id).values(token_epoch=func.now()))
    await db.execute(
        update(AuthRefreshToken)
        .where(AuthRefreshToken.user_id == user_id, AuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now(), revoked_reason="revoke_all")
    )
    await db.execute(
        update(MachineCredential)
        .where(_personal_boxes_of([user_id]))
        .values(revoked_at=func.now())
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(
        update(AuthToken)
        .where(AuthToken.user_id == user_id, AuthToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(AuthToken.jti)
    )
    await db.flush()
    for jti in result.scalars().all():
        _cache.mark_revoked(jti)


async def bump_membership_epoch(db: AsyncSession, membership_id: UUID) -> int:
    """Move a membership's credential epoch on: every credential minted for it
    carries the epoch it was minted at, so each one is refused from now on.
    Returns the new epoch."""
    result = await db.execute(
        update(OrgMembership)
        .where(OrgMembership.id == membership_id)
        .values(credential_epoch=OrgMembership.credential_epoch + 1)
        .returning(OrgMembership.credential_epoch)
        .execution_options(synchronize_session=False)
    )
    epoch = int(result.scalar_one())
    await db.flush()
    return epoch


#: How many memberships one statement of :func:`revoke_memberships` names.
_REVOKE_BATCH = 1000


def _legacy_in_home_org(user_ids: Sequence[UUID], org_id: UUID) -> ColumnElement[bool]:
    """A registry row minted before tokens named a membership, for one of
    ``user_ids`` whose home org is ``org_id``. Every org was the home org
    then, so such a token is a credential into ``org_id`` and ends with the
    person's membership there; a person removed from another org keeps it."""
    return and_(
        AuthToken.membership_id.is_(None),
        AuthToken.org_team_id.is_(None),
        AuthToken.user_id.in_(
            users_homed_in(org_id)
            .with_only_columns(User.id)
            .where(User.id.in_(user_ids))
            .order_by(None)
        ),
    )


async def revoke_memberships(
    db: AsyncSession, memberships: Sequence[OrgMembership], *, reason: str
) -> None:
    """:func:`revoke_membership` for many memberships of ONE org, set-wise: a
    handful of statements per thousand memberships instead of four per
    person, inside the caller's transaction."""
    for start in range(0, len(memberships), _REVOKE_BATCH):
        chunk = memberships[start : start + _REVOKE_BATCH]
        orgs = {m.org_team_id for m in chunk}
        if len(orgs) != 1:
            raise ValueError("revoke_memberships takes the memberships of one org")
        (org_id,) = orgs
        ids = [m.id for m in chunk]
        user_ids = [m.user_id for m in chunk]
        epochs = await db.execute(
            update(OrgMembership)
            .where(OrgMembership.id.in_(ids))
            .values(credential_epoch=OrgMembership.credential_epoch + 1)
            .returning(OrgMembership.id, OrgMembership.credential_epoch)
            .execution_options(synchronize_session=False)
        )
        moved = dict(epochs.tuples().all())
        for membership in chunk:
            membership.credential_epoch = moved.get(membership.id, membership.credential_epoch)
        result = await db.execute(
            update(AuthToken)
            .where(
                or_(AuthToken.membership_id.in_(ids), _legacy_in_home_org(user_ids, org_id)),
                AuthToken.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
            .returning(AuthToken.jti)
            .execution_options(synchronize_session=False)
        )
        jtis = list(result.scalars().all())
        await db.execute(
            update(AuthRefreshToken)
            .where(
                AuthRefreshToken.user_id.in_(user_ids),
                AuthRefreshToken.active_org_team_id == org_id,
            )
            .values(active_org_team_id=None)
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(PersonalAccessToken)
            .where(
                PersonalAccessToken.user_id.in_(user_ids),
                PersonalAccessToken.org_team_id == org_id,
                PersonalAccessToken.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(MachineCredential)
            .where(_personal_boxes_of(user_ids), MachineCredential.org_team_id == org_id)
            .values(revoked_at=func.now())
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        for jti in jtis:
            _cache.mark_revoked(jti)
        _log.info(
            "auth.memberships_revoked",
            org_id=str(org_id),
            reason=reason,
            memberships=len(chunk),
            tokens_revoked=len(jtis),
        )


async def revoke_membership(db: AsyncSession, membership: OrgMembership, *, reason: str) -> None:
    """End every credential ``membership``'s person holds in its org, and only there.

    * the membership's credential epoch moves on, which refuses every session,
      CLI and gateway token, socket and event stream minted for it (each carries
      the epoch it was minted at), including the ones nothing registered;
    * the registry rows of tokens minted for it are marked revoked, so the
      sessions list and the revocation cache agree at once, and so are the
      person's rows from before tokens named a membership when the org is
      their home org (every such token is a credential into it);
    * every refresh family whose active org is this org forgets it, so its next
      refresh answers ``session_org_revoked`` instead of minting into the org
      again; the family itself lives on, because it is the identity's;
    * the person's personal access tokens for the org are revoked for good,
      and so is the machine credential of every box on their own hardware
      bound to the org.

    ``users.token_epoch`` and every other org's rows are untouched. ``reason``
    names why (``deactivated``, ``removed``, ``sso_enforced``, ``org_deleted``),
    for the log line; the caller writes the audit row.
    """
    await revoke_memberships(db, [membership], reason=reason)


def _in_org(org_team_id: UUID) -> ColumnElement[bool]:
    """A registry row the org ``org_team_id`` may see: one minted for a
    membership of that org, or a legacy row minted before tokens named an org
    (it names none, so it is the identity's, not any one org's)."""
    return or_(AuthToken.org_team_id == org_team_id, AuthToken.org_team_id.is_(None))


async def revoke_jti_for_user(
    db: AsyncSession, jti: str, user_id: UUID, *, org_team_id: UUID
) -> bool:
    """Revoke a token only if it belongs to `user_id` and to the org
    ``org_team_id`` (the request's org).

    Returns False when no such token exists — the caller maps that to 404 so a
    user can neither revoke nor probe another user's tokens, and a credential
    for one org can neither revoke nor probe the same person's tokens for
    another.
    """
    result = await db.execute(
        update(AuthToken)
        .where(AuthToken.jti == jti, AuthToken.user_id == user_id, _in_org(org_team_id))
        .values(revoked_at=func.now())
        .returning(AuthToken.jti)
    )
    if not result.scalars().all():
        return False
    await db.flush()
    _cache.mark_revoked(jti)
    return True


async def list_active_tokens(
    db: AsyncSession, user_id: UUID, *, org_team_id: UUID
) -> list[AuthToken]:
    """A user's live (unrevoked, unexpired) tokens in the org ``org_team_id``,
    newest first. A token minted for the person's membership of another org
    is that org's and is not listed."""
    rows = await db.execute(
        select(AuthToken)
        .where(
            AuthToken.user_id == user_id,
            _in_org(org_team_id),
            AuthToken.revoked_at.is_(None),
            AuthToken.expires_at > func.now(),
        )
        .order_by(AuthToken.issued_at.desc())
    )
    return list(rows.scalars().all())


async def assert_token_active(
    db: AsyncSession, claims: SessionClaims, user: User, *, slide_idle: bool = True
) -> None:
    """Raise `TokenRevokedError` if the token has been revoked.

    Two checks: (1) `iat` predates the user's `token_epoch` (revoke-all);
    (2) the `jti` is in the revoked-cache. Floors `token_epoch` to whole
    seconds so a token minted in the same second as a revoke-all (e.g. an
    immediate re-login after a password reset) is NOT falsely rejected — the
    registry's jti revocation still catches the genuinely-old tokens exactly.

    ``slide_idle=False`` is for re-checking a long-lived connection (an open
    event stream, a socket) on a timer: the idle window is still enforced, but
    the check does not count as activity, so a tab that is merely open cannot
    keep its session alive forever. A socket passes ``True`` only on a tick
    after the person did something on it.
    """
    if claims.issued_at < int(user.token_epoch.timestamp()):
        raise TokenRevokedError("token predates token_epoch")
    if claims.jti is not None and await _cache.is_revoked(db, claims.jti):
        raise TokenRevokedError("token revoked")
    if settings.auth_idle_timeout_seconds > 0 and claims.jti is not None:
        await _enforce_idle_timeout(db, claims.jti, slide=slide_idle)


async def _enforce_idle_timeout(db: AsyncSession, jti: str, *, slide: bool = True) -> None:
    """Reject (and, when ``slide`` is set, otherwise slide) a SESSION token that's
    been idle past ``auth_idle_timeout_seconds``. CLI tokens are exempt — they're
    used intermittently by design. The first request after mint uses
    ``issued_at`` as the baseline (``last_used_at`` is NULL until the first slide)."""
    row = (
        await db.execute(
            select(
                AuthToken.token_type, AuthToken.last_used_at, AuthToken.issued_at, AuthToken.id
            ).where(AuthToken.jti == jti)
        )
    ).first()
    if row is None or row[0] != TokenType.SESSION:
        return  # unregistered/legacy or a CLI token — no idle policy
    last_used, issued = row[1], row[2]
    now = datetime.now(UTC)
    baseline = last_used or issued
    if (now - baseline).total_seconds() > settings.auth_idle_timeout_seconds:
        raise TokenRevokedError("session idle timeout")
    if not slide:
        return
    # Throttled, and never waiting: every request of one session decides to
    # slide at the same moment, and the row's write lasts to the end of the
    # request that made it, so a plain UPDATE queued all of the session's
    # concurrent requests behind each other's commits. One of them records the
    # moment; the others skip it.
    await stamp_last_used(
        db, AuthToken, row[3], now, resolution=timedelta(seconds=_IDLE_UPDATE_INTERVAL_SECONDS)
    )
    await db.flush()


async def prune_expired(db: AsyncSession) -> int:
    """Delete registry rows whose token has expired. Returns the count removed.

    ONLY natural expiry. A revoked row must survive for the whole life of the
    token it revokes: `is_revoked` decides purely on the row's presence, and
    `revoke_jti` / `revoke_jti_for_user` (logout, "revoke this session") do not
    bump `token_epoch`, so deleting a revoked-but-unexpired row would hand the
    holder of that exact token — a stolen one, typically — a working credential
    again for the remainder of its natural life (up to 90 days for a CLI token).
    Retention here is therefore a correctness constraint, not housekeeping
    policy. The registry's size is not what the auth hot path costs: the
    revocation cache is a bounded per-jti LRU over an indexed lookup.
    """
    result = await db.execute(delete(AuthToken).where(AuthToken.expires_at < func.now()))
    await db.flush()
    # `execute` is typed as Result; a DELETE always yields a CursorResult at
    # runtime (where `rowcount` lives). Narrow rather than `# type: ignore`.
    return result.rowcount if isinstance(result, CursorResult) else 0
