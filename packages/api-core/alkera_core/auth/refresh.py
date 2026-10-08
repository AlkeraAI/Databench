"""Refresh-token families: the revocable, rotating half of a browser session.

The access token (``tokens.py``) is a stateless JWT that lives minutes. What
keeps a browser signed in is a family of refresh tokens: an opaque 256-bit
secret the client holds only in an HTTP-only cookie scoped to the refresh
route, stored here only as a keyed hash, and ROTATED on every use — the
presented row is stamped ``used_at`` and a child row is minted in the same
family. Every row is server-side state, so a family can be ended at any
moment: logout, a password or email change, a ban, a privilege change, or
the user picking it off the sessions list.

Two expiries bound a family. The idle expiry moves forward with each
rotation and ends a session nobody has come back to; the absolute expiry is
fixed when the family begins and no activity moves it.

Rotation is single-successor (OAuth 2.0 Security BCP, RFC 9700 section
4.14.2). The presented row is read under a row lock, so two requests racing
the same token are serialized: the first stamps it used and mints the child,
the second sees it used. A used token presented again is reuse: the token was
copied, so the whole family is revoked (every access token minted in it
included) and the answer is ``refresh_token_reused``. The one exception is a
re-presentation within ``auth_refresh_reuse_grace_seconds`` of the rotation
while the child is still the family's current token: that is a second tab of
the same browser refreshing concurrently, and it is handed the SAME child,
never a second one. The child's raw value is kept on the parent row sealed
under a key derived from the parent's own raw value, so only a presenter of
the parent can open it and a database read alone yields nothing usable.

Every access token minted in a family is linked to it in the token registry
(``auth_tokens.refresh_family_id``), so ending a family ends all of them,
including the one minted for a grace re-presentation.

A family is identity-level and names the org its access tokens are minted for
(``active_org_team_id``; NULL is the home org). Rotation copies it, and refuses
with ``session_org_revoked`` when the membership in that org no longer stands:
the family's access tokens are revoked and the family forgets the org, but the
family itself lives on, because it belongs to the identity, not to the org.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.revocation import revoke_family_access_tokens
from alkera_core.auth.tenancy import (
    SESSION_ORG_REVOKED,
    MembershipRefused,
    assert_single_org,
    home_org_id,
    require_active_membership,
)
from alkera_core.auth.token_hash import hash_lookup_token, lookup_token_digests
from alkera_core.auth.tokens import SessionClaims
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.models import AuthRefreshToken, AuthToken, User
from alkera_core.observability.errors import ErrorCode

#: Where the refresh cookie is sent, and nowhere else. Scoping the cookie's
#: ``Path`` here keeps the long-lived credential off every ordinary API request.
REFRESH_ROUTE_PATH = "/api/v1/auth/refresh"

# Why a family or token was ended — the sessions list and the audit trail
# read these; they are not error codes.
REASON_LOGOUT = "logout"
REASON_USER = "user"
REASON_REUSE = "reuse"
REASON_EXPIRED = "expired"
REASON_REVOKE_ALL = "revoke_all"


class RefreshError(Exception):
    """A presented refresh token cannot be rotated. ``code`` is the 401 code the
    route answers with: ``unauthorized`` (unknown token), ``session_revoked``
    (the family was ended), ``refresh_token_reused`` (a used token came back,
    and the family was ended just now), ``session_expired`` (idle or absolute),
    ``session_org_revoked`` (the family's membership in its org no longer
    stands)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RefreshReuseError(RefreshError):
    """A used refresh token was presented again outside the concurrent-refresh
    grace. The family is already revoked when this is raised; the route records
    the security event for ``user_id`` and answers 401."""

    def __init__(self, *, family_id: UUID, user_id: UUID) -> None:
        super().__init__(ErrorCode.refresh_token_reused.value, "refresh token reuse detected")
        self.family_id = family_id
        self.user_id = user_id


@dataclass(frozen=True, slots=True)
class MintedRefresh:
    raw: str
    row: AuthRefreshToken


def _now(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now


#: Domain separation for the key a parent token seals its successor under.
_SUCCESSOR_KEY_DOMAIN = b"alkera.refresh.successor.v1"
_NONCE_BYTES = 12


def _successor_key(parent_raw: str) -> bytes:
    # The parent's raw value is a 256-bit secret the database never holds, so a
    # key derived from it opens the seal only for someone presenting the parent.
    return hmac.new(parent_raw.encode("utf-8"), _SUCCESSOR_KEY_DOMAIN, hashlib.sha256).digest()


def seal_successor(parent_raw: str, parent_id: UUID, successor_raw: str) -> str:
    """Seal a successor's raw value for its parent row (AES-256-GCM, the parent
    row's id as associated data, so a seal cannot be moved to another row)."""
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(_successor_key(parent_raw)).encrypt(
        nonce, successor_raw.encode("utf-8"), parent_id.bytes
    )
    return base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def open_successor(parent_raw: str, parent_id: UUID, sealed: str) -> str | None:
    """The successor's raw value, or None when the seal does not open under
    ``parent_raw`` for ``parent_id``."""
    try:
        blob = base64.urlsafe_b64decode(sealed.encode("ascii"))
        nonce, body = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return (
            AESGCM(_successor_key(parent_raw)).decrypt(nonce, body, parent_id.bytes).decode("utf-8")
        )
    except (InvalidTag, ValueError):
        return None


def _new_raw() -> str:
    # 32 random bytes: a 256-bit secret, URL-safe so it rides a cookie verbatim.
    return secrets.token_urlsafe(32)


async def _link_access(db: AsyncSession, access_jti: str, family_id: UUID) -> None:
    """Record in the token registry that ``access_jti`` was minted in
    ``family_id``: the one link :func:`revoke_family` ends access tokens by."""
    await db.execute(
        update(AuthToken)
        .where(AuthToken.jti == access_jti)
        .values(refresh_family_id=family_id)
        .execution_options(synchronize_session=False)
    )


async def mint_refresh_token(
    db: AsyncSession,
    *,
    user_id: UUID,
    access_jti: str | None,
    family_id: UUID | None = None,
    family_started_at: datetime | None = None,
    absolute_expires_at: datetime | None = None,
    user_agent: str | None = None,
    ip_prefix: str | None = None,
    now: datetime | None = None,
    active_org_team_id: UUID | None = None,
) -> MintedRefresh:
    """Mint one refresh token. Without ``family_id`` this starts a new family
    (login); with one it is a rotation and inherits the family's absolute
    expiry, which no rotation may move. ``active_org_team_id`` is the org the
    family mints access tokens for (None: the home org). ``access_jti``, when
    given, must already be registered; it is linked to the family."""
    moment = _now(now)
    raw = _new_raw()
    started = family_started_at or moment
    row = AuthRefreshToken(
        family_id=family_id or uuid4(),
        user_id=user_id,
        token_hash=hash_lookup_token(raw),
        access_jti=access_jti,
        family_started_at=started,
        created_at=moment,
        last_used_at=None,
        idle_expires_at=moment + timedelta(seconds=settings.auth_refresh_idle_seconds),
        absolute_expires_at=absolute_expires_at
        or (started + timedelta(seconds=settings.auth_refresh_absolute_seconds)),
        user_agent=(user_agent or None) and user_agent[:255],
        ip_prefix=(ip_prefix or None) and ip_prefix[:64],
        active_org_team_id=active_org_team_id,
    )
    db.add(row)
    await db.flush()
    if access_jti is not None:
        await _link_access(db, access_jti, row.family_id)
    return MintedRefresh(raw=raw, row=row)


async def _lookup(db: AsyncSession, raw: str) -> AuthRefreshToken | None:
    """The row a presented token names, locked for the rest of the
    transaction: a second request presenting the same token waits here until
    the first has committed its rotation, then reads it."""
    digests = lookup_token_digests(raw)
    return (
        await lock_rows(
            db,
            LockRank.REFRESH_TOKEN,
            select(AuthRefreshToken)
            .where(AuthRefreshToken.token_hash.in_(digests))
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()


async def revoke_family(
    db: AsyncSession, family_id: UUID, *, reason: str, now: datetime | None = None
) -> list[str]:
    """End a whole family: every row, and every access token minted in it.
    Idempotent. Returns the access jtis it revoked."""
    moment = _now(now)
    await db.execute(
        update(AuthRefreshToken)
        .where(AuthRefreshToken.family_id == family_id, AuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=moment, revoked_reason=reason, successor_sealed=None)
        .execution_options(synchronize_session="fetch")
    )
    return await revoke_family_access_tokens(db, family_id)


async def revoke_family_for_user(
    db: AsyncSession, family_id: UUID, user_id: UUID, *, reason: str
) -> bool:
    """Revoke a family only if it belongs to ``user_id``. False when no such
    (owned) family exists, so a user can neither end nor probe another's."""
    owned = (
        await db.execute(
            select(AuthRefreshToken.id)
            .where(AuthRefreshToken.family_id == family_id, AuthRefreshToken.user_id == user_id)
            .limit(1)
        )
    ).first()
    if owned is None:
        return False
    await revoke_family(db, family_id, reason=reason)
    return True


async def revoke_family_of_access_token(
    db: AsyncSession, access_jti: str, *, reason: str
) -> UUID | None:
    """End the family an access token was minted in (logout). Returns the
    family id, or None when the token has no family (a CLI token, a session
    from before families existed)."""
    row = await family_of_access_token(db, access_jti)
    if row is None:
        return None
    await revoke_family(db, row.family_id, reason=reason)
    return row.family_id


def _is_expired(row: AuthRefreshToken, moment: datetime) -> bool:
    return moment >= row.idle_expires_at or moment >= row.absolute_expires_at


async def _graced_successor(
    db: AsyncSession, row: AuthRefreshToken, raw: str, moment: datetime
) -> MintedRefresh | None:
    """The successor a used ``row`` hands a concurrent re-presentation, or None
    when the re-presentation is reuse: past the grace, the successor no longer
    the family's current token, or no successor sealed for this presenter."""
    assert row.used_at is not None
    grace = timedelta(seconds=settings.auth_refresh_reuse_grace_seconds)
    if moment - row.used_at > grace:
        return None
    if row.successor_id is None or row.successor_sealed is None:
        return None
    successor = (
        await lock_rows(
            db,
            LockRank.REFRESH_TOKEN,
            select(AuthRefreshToken)
            .where(AuthRefreshToken.id == row.successor_id)
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()
    if successor is None or successor.used_at is not None or successor.revoked_at is not None:
        return None
    successor_raw = open_successor(raw, row.id, row.successor_sealed)
    if successor_raw is None:
        return None
    return MintedRefresh(raw=successor_raw, row=successor)


async def _resolve(
    db: AsyncSession, raw: str, moment: datetime
) -> tuple[AuthRefreshToken, User, MintedRefresh | None]:
    """The presented row, its identity, and (for a grace re-presentation) the
    successor it already has. Every refusal is raised here."""
    row = await _lookup(db, raw)
    if row is None:
        raise RefreshError("unauthorized", "unknown refresh token")
    if row.revoked_at is not None:
        raise RefreshError("session_revoked", "session revoked")
    replay: MintedRefresh | None = None
    if row.used_at is not None:
        replay = await _graced_successor(db, row, raw, moment)
        if replay is None:
            # The token was rotated and is being presented again: it was copied.
            # Whoever holds the successor is signed out with the thief.
            await revoke_family(db, row.family_id, reason=REASON_REUSE, now=moment)
            raise RefreshReuseError(family_id=row.family_id, user_id=row.user_id)
    current = replay.row if replay is not None else row
    if _is_expired(current, moment):
        await revoke_family(db, row.family_id, reason=REASON_EXPIRED, now=moment)
        raise RefreshError("session_expired", "session expired")
    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        await revoke_family(db, row.family_id, reason=REASON_REVOKE_ALL, now=moment)
        raise RefreshError("unauthorized", "user no longer exists")
    if row.created_at.timestamp() < int(user.token_epoch.timestamp()):
        # A revoke-all after this row was minted ends it even if the row's own
        # stamp was somehow missed — the epoch is the lever that cannot miss.
        await revoke_family(db, row.family_id, reason=REASON_REVOKE_ALL, now=moment)
        raise RefreshError("session_revoked", "session revoked")
    return row, user, replay


async def resolve_refresh_token(
    db: AsyncSession, raw: str, *, now: datetime | None = None
) -> tuple[AuthRefreshToken, User]:
    """The live family row a presented refresh token names, and its identity,
    without rotating it: every refusal the rotation would answer (unknown,
    revoked, reused, expired, a disabled identity, a revoke-all) is raised
    here the same way, reuse and expiry ending the family as they do there.

    For a route that must check something about the family before it rotates
    (switching orgs), so a refusal leaves the presented token usable. The row
    stays locked until the caller's transaction ends."""
    row, user, _ = await _resolve(db, raw, _now(now))
    return row, user


async def rotate_refresh_token(
    db: AsyncSession,
    raw: str,
    *,
    user_agent: str | None = None,
    ip_prefix: str | None = None,
    now: datetime | None = None,
    switch_to: UUID | None = None,
) -> tuple[MintedRefresh, User]:
    """Exchange a presented refresh token for its successor.

    Every refusal is a :class:`RefreshError`. The successful path stamps the
    presented row used, mints its one child in the same family, and seals the
    child for the parent's presenter; a grace re-presentation gets that same
    child back instead of a new one. The caller mints the access token and
    links it to the family (``bind_access``).

    ``switch_to`` moves the family into another org as it rotates: that org's
    membership is admitted instead of the current one's, and every row of the
    family names it from here on. The caller has already decided the person
    may enter it.
    """
    moment = _now(now)
    row, user, replay = await _resolve(db, raw, moment)
    if switch_to is None:
        await _admit_active_org(db, row, user)
    else:
        # The org being entered is admitted instead of the one being left: a
        # family whose old org just refused it may still move somewhere else.
        assert_single_org(user, org_team_id=switch_to)
        await require_active_membership(db, user_id=user.id, org_team_id=switch_to)
        await db.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.family_id == row.family_id)
            .values(active_org_team_id=switch_to)
            .execution_options(synchronize_session=False)
        )
        row.active_org_team_id = switch_to
        if replay is not None:
            replay.row.active_org_team_id = switch_to
    if replay is not None:
        return replay, user
    row.used_at = moment
    row.last_used_at = moment
    await db.flush()
    minted = await mint_refresh_token(
        db,
        user_id=row.user_id,
        access_jti=None,
        family_id=row.family_id,
        family_started_at=row.family_started_at,
        absolute_expires_at=row.absolute_expires_at,
        user_agent=user_agent or row.user_agent,
        ip_prefix=ip_prefix or row.ip_prefix,
        now=moment,
        active_org_team_id=row.active_org_team_id,
    )
    row.successor_id = minted.row.id
    row.successor_sealed = seal_successor(raw, row.id, minted.raw)
    await db.flush()
    return minted, user


def active_org_of(row: AuthRefreshToken, user: User) -> UUID:
    """The org a family mints access tokens for: its active org, or the
    identity's home org for a family that names none."""
    return row.active_org_team_id or home_org_id(user)


async def _admit_active_org(db: AsyncSession, row: AuthRefreshToken, user: User) -> None:
    """Refuse the rotation when the family's membership in its org no longer
    stands. The family's access tokens are revoked and the family forgets its
    org; the family is not revoked, because it is the identity's login session
    and the identity may still stand elsewhere."""
    org = active_org_of(row, user)
    try:
        assert_single_org(user, org_team_id=org)
        await require_active_membership(db, user_id=user.id, org_team_id=org)
    except MembershipRefused as exc:
        await _forget_org(db, row.family_id)
        raise RefreshError(
            SESSION_ORG_REVOKED, "the session's organization membership ended"
        ) from exc


async def refuse_family_org(db: AsyncSession, family_id: UUID) -> None:
    """The family's org refused it (its sign-in policy wants a step-up): every
    access token minted in the family is revoked and the family forgets its
    org. The family itself is not revoked, so the browser that holds it can
    step up (an SSO sign-in writes its grant onto it) without starting over."""
    await _forget_org(db, family_id)


async def _forget_org(db: AsyncSession, family_id: UUID) -> None:
    await revoke_family_access_tokens(db, family_id)
    await db.execute(
        update(AuthRefreshToken)
        .where(AuthRefreshToken.family_id == family_id)
        .values(active_org_team_id=None)
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()


async def bind_access(db: AsyncSession, row: AuthRefreshToken, claims: SessionClaims) -> None:
    """Record the access token minted beside a refresh row and link it to the
    row's family. A grace re-presentation binds a second access token to the
    same row; the first stays live (the other tab holds it) and stays linked,
    so ending the family still ends both."""
    row.access_jti = claims.jti
    await db.flush()
    if claims.jti is not None:
        await _link_access(db, claims.jti, row.family_id)


async def family_of_access_token(db: AsyncSession, access_jti: str) -> AuthRefreshToken | None:
    """A row of the family ``access_jti`` was minted in (its newest), or None
    when the token belongs to no family (a CLI token)."""
    family_id = await db.scalar(
        select(AuthToken.refresh_family_id).where(AuthToken.jti == access_jti)
    )
    if family_id is None:
        # An access token minted by a process that predates the registry link
        # (a rolling deploy) is still named by the row it was minted beside.
        return (
            await db.execute(
                select(AuthRefreshToken).where(AuthRefreshToken.access_jti == access_jti)
            )
        ).scalar_one_or_none()
    return (
        await db.execute(
            select(AuthRefreshToken)
            .where(AuthRefreshToken.family_id == family_id)
            .order_by(AuthRefreshToken.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def is_family_of(db: AsyncSession, family_id: UUID, user_id: UUID) -> bool:
    """Whether the login session ``family_id`` is ``user_id``'s own."""
    found = await db.scalar(
        select(AuthRefreshToken.id)
        .where(AuthRefreshToken.family_id == family_id, AuthRefreshToken.user_id == user_id)
        .limit(1)
    )
    return found is not None


async def family_alive(
    db: AsyncSession, claims: SessionClaims, *, now: datetime | None = None
) -> bool | None:
    """Whether the family behind an access token is still live: its current
    (unused, unrevoked) token exists and neither expiry has passed. ``None``
    when the token has no family at all — a CLI token, or a session from
    before families existed — so the caller falls back to the token's own
    ``exp`` instead of treating "no family" as "alive"."""
    if claims.jti is None:
        return None
    row = await family_of_access_token(db, claims.jti)
    if row is None:
        return None
    moment = _now(now)
    current = (
        (
            await db.execute(
                select(AuthRefreshToken).where(
                    AuthRefreshToken.family_id == row.family_id,
                    AuthRefreshToken.used_at.is_(None),
                    AuthRefreshToken.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return any(not _is_expired(c, moment) for c in current)


async def list_live_families(
    db: AsyncSession, user_id: UUID, *, now: datetime | None = None
) -> list[AuthRefreshToken]:
    """A user's live families, newest first — each represented by its current
    token row (the one a refresh would rotate)."""
    moment = _now(now)
    rows = (
        (
            await db.execute(
                select(AuthRefreshToken)
                .where(
                    AuthRefreshToken.user_id == user_id,
                    AuthRefreshToken.used_at.is_(None),
                    AuthRefreshToken.revoked_at.is_(None),
                )
                .order_by(AuthRefreshToken.family_started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    seen: set[UUID] = set()
    live: list[AuthRefreshToken] = []
    for row in rows:
        if row.family_id in seen or _is_expired(row, moment):
            continue
        seen.add(row.family_id)
        live.append(row)
    return live


# Don't rewrite a family's idle expiry on every keepalive tick — only once the
# slide would move it by at least this much. Bounds the write amplification of a
# live connection to ~1 write / interval / session, the same shape as the access
# token's own throttled slide.
_FAMILY_SLIDE_MIN_SECONDS = 30.0


async def slide_family_idle(
    db: AsyncSession, claims: SessionClaims, *, now: datetime | None = None
) -> bool:
    """Count what a person did on a live connection as activity on the
    session behind it.

    A browser normally keeps its family alive by rotating the refresh cookie,
    which a socket never does — the refresh cookie's ``Path`` means it only
    ever reaches the refresh route. A socket calls this on a tick after the
    person edited or moved a cursor on it; holding the connection open, its
    keepalives and what the server pushes are never activity, so an unattended
    tab does not keep its session alive.

    Only the IDLE expiry moves, and never past ``absolute_expires_at``, which
    no activity may move: a tab left open cannot outlive the session's ceiling.
    A family that is already past either expiry is left alone — a slide must
    never revive a session that has ended. Returns whether anything moved.
    """
    if claims.jti is None:
        return False
    named = await family_of_access_token(db, claims.jti)
    if named is None:
        return False
    moment = _now(now)
    idle = timedelta(seconds=settings.auth_refresh_idle_seconds)
    threshold = timedelta(seconds=_FAMILY_SLIDE_MIN_SECONDS)
    rows = (
        (
            await db.execute(
                select(AuthRefreshToken).where(
                    AuthRefreshToken.family_id == named.family_id,
                    AuthRefreshToken.used_at.is_(None),
                    AuthRefreshToken.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    moved = False
    for row in rows:
        if _is_expired(row, moment):
            continue
        target = min(moment + idle, row.absolute_expires_at)
        if target - row.idle_expires_at < threshold:
            continue
        row.idle_expires_at = target
        moved = True
    if moved:
        await db.flush()
    return moved
