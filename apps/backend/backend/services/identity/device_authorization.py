"""Device authorization grant (RFC 8628) — issue, approve/deny, and redeem.

The token endpoint's state machine lives in `redeem`: it maps a polled
`device_code` to one of the RFC 8628 §3.5 outcomes (`authorization_pending`,
`slow_down`, `access_denied`, `expired_token`, `invalid_grant`) or, once the user
has approved, to the approver `User` (the route then mints the JWT).

Security notes:
- The raw `device_code` is high-entropy (`secrets.token_urlsafe(32)`, ~256 bits)
  and persisted only as a keyed HMAC (`hash_lookup_token`), so a DB read can't
  replay a poll.
- The `user_code` is short (8 chars over a 32-symbol alphabet ≈ 40 bits) and
  stored verbatim for the SPA lookup. It's defended by a short TTL, status-scoped
  uniqueness, and a per-session attempt cap (`record_user_code_failure` +
  `assert_user_code_attempts`), not by hashing.
- Transitions are strictly forward (guards on `status == PENDING`), so no
  re-approve, approve-after-deny, or redeem-after-consume is possible.
"""

from __future__ import annotations

import secrets
import time
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from alkera_core.auth import hash_lookup_token, lookup_token_digests
from alkera_core.config import settings
from alkera_core.models import DeviceAuthorization, DeviceAuthStatus, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.identity import users as user_service
from backend.services.infra import now as _now

# RFC 8628 §6.1-friendly charset: uppercase, no ambiguous 0/O/1/I. 32 symbols.
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_USER_CODE_MAX_GEN_ATTEMPTS = 5

# Friendly labels for the consent screen, keyed by the client_id the CLI/daemon
# sends. Unknown clients fall back to the generic label.
_CLIENT_NAMES = {
    "alkera-cli": "Alkera CLI",
    "alkera-vscode": "Alkera for VS Code",
    "alkera-box": "Alkera personal box",
}

#: The client a person's own box signs in as. Approving it does not mint a
#: session: it mints a machine credential bound to the approver's org that
#: serves only the approver's own private chats (see ``mint_personal``).
PERSONAL_BOX_CLIENT_ID = "alkera-box"
_DEFAULT_CLIENT_NAME = "Alkera Client"


class DeviceAuthError(Exception):
    """Approve/deny attempted on a non-pending or expired authorization."""


class RedeemError(StrEnum):
    """A token-endpoint poll that did not (yet) yield a token. The value is the
    exact RFC 8628 §3.5 / §5.2 error string returned to the client."""

    AUTHORIZATION_PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"
    ACCESS_DENIED = "access_denied"
    EXPIRED_TOKEN = "expired_token"  # noqa: S105 — RFC error string, not a secret
    INVALID_GRANT = "invalid_grant"


def client_name_for(client_id: str) -> str:
    """Human label shown on the consent screen for a given client_id."""
    return _CLIENT_NAMES.get(client_id, _DEFAULT_CLIENT_NAME)


def is_known_client(client_id: str) -> bool:
    """True for a client this deployment issues device codes to.

    The consent screen names the requesting client, so an unrecognized id would
    render the generic label and let anyone drive a real, approvable flow under a
    name of their choosing. Refuse at the door instead; ``client_name_for``'s
    fallback stays as a display-only backstop for already-stored rows."""
    return client_id in _CLIENT_NAMES


def _gen_user_code() -> str:
    n = settings.auth_device_user_code_length
    chars = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(n))
    half = n // 2
    return f"{chars[:half]}-{chars[half:]}"


def _gen_device_code() -> str:
    return secrets.token_urlsafe(32)


async def create_device_code(
    db: AsyncSession, *, client_id: str, scope: str | None
) -> tuple[str, DeviceAuthorization]:
    """Create a pending authorization. Returns ``(raw_device_code, row)``.

    Only the caller (the route) ever sees the raw device_code — the row stores
    only its hash. A pre-flight SELECT avoids the (astronomically rare) pending
    ``user_code`` collision without touching the request transaction; the
    partial-unique index is the ultimate backstop against a genuine race.
    """
    raw_device_code = _gen_device_code()
    device_code_hash = hash_lookup_token(raw_device_code)
    expires_at = _now() + timedelta(seconds=settings.auth_device_code_ttl_seconds)

    user_code = _gen_user_code()
    for _ in range(_USER_CODE_MAX_GEN_ATTEMPTS):
        clash = (
            await db.execute(
                select(DeviceAuthorization.id).where(
                    DeviceAuthorization.user_code == user_code,
                    DeviceAuthorization.status == DeviceAuthStatus.PENDING,
                )
            )
        ).first()
        if clash is None:
            break
        user_code = _gen_user_code()

    row = DeviceAuthorization(
        device_code_hash=device_code_hash,
        user_code=user_code,
        client_id=client_id,
        scope=scope,
        interval_seconds=settings.auth_device_poll_interval_seconds,
        status=DeviceAuthStatus.PENDING,
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return raw_device_code, row


async def get_for_approval(db: AsyncSession, user_code: str) -> DeviceAuthorization | None:
    """The pending, unexpired authorization for ``user_code`` (the SPA lookup).

    Returns None for a missing, expired, or already-resolved code — the route
    maps all of those to an indistinguishable 404 so there's no enumeration
    oracle distinguishing "wrong code" from "already used".
    """
    row = (
        await db.execute(
            select(DeviceAuthorization).where(
                DeviceAuthorization.user_code == user_code,
                DeviceAuthorization.status == DeviceAuthStatus.PENDING,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at <= _now():
        return None
    return row


async def approve(
    db: AsyncSession, row: DeviceAuthorization, *, user: User, org_team_id: UUID
) -> None:
    """Bind ``row`` to the approving user and to ``org_team_id``, the org of the
    browser session that approved it (the CLI token is minted for that org).
    Raises if it's no longer pending."""
    _assert_pending(row)
    row.status = DeviceAuthStatus.APPROVED
    row.user_id = user.id
    row.org_team_id = org_team_id
    row.approved_at = _now()
    await db.flush()


async def deny(db: AsyncSession, row: DeviceAuthorization) -> None:
    """Mark ``row`` denied. Raises if it's no longer pending."""
    _assert_pending(row)
    row.status = DeviceAuthStatus.DENIED
    row.denied_at = _now()
    await db.flush()


def _assert_pending(row: DeviceAuthorization) -> None:
    if row.status != DeviceAuthStatus.PENDING:
        raise DeviceAuthError(f"Device authorization is {row.status.value}, not pending")
    if row.expires_at <= _now():
        raise DeviceAuthError("Device authorization has expired")


async def redeem(
    db: AsyncSession, *, raw_device_code: str, client_id: str
) -> tuple[User, DeviceAuthorization] | RedeemError:
    """Run one token-endpoint poll. Returns ``(user, row)`` once the user has
    approved (the route mints + records the JWT, the row is marked consumed), or
    a :class:`RedeemError` describing the current state.

    Locks the row ``FOR UPDATE`` so two concurrent polls of the same approved
    device_code can't both observe APPROVED and each mint a token — the second
    blocks until the first commits, then re-reads CONSUMED and gets invalid_grant.
    This is the same single-use-mutation idiom the billing engine uses.
    """
    row = (
        await db.execute(
            select(DeviceAuthorization)
            .where(DeviceAuthorization.device_code_hash.in_(lookup_token_digests(raw_device_code)))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.client_id != client_id:
        return RedeemError.INVALID_GRANT

    now = _now()

    # A code that has reached an end answers with that end on every poll, at
    # any pace (RFC 8628 section 3.5): expired, consumed and denied are final,
    # and none of them is ``slow_down``, which would tell the client to keep
    # polling. Expiry first, so an expired-but-approved code reports expired.
    if row.status == DeviceAuthStatus.EXPIRED or row.expires_at <= now:
        if row.status != DeviceAuthStatus.EXPIRED:
            row.status = DeviceAuthStatus.EXPIRED
            await db.flush()
        return RedeemError.EXPIRED_TOKEN
    if row.status == DeviceAuthStatus.CONSUMED:
        # Single-use: the token was already handed out once.
        return RedeemError.INVALID_GRANT
    if row.status == DeviceAuthStatus.DENIED:
        return RedeemError.ACCESS_DENIED

    # Only a live code (pending or approved) is paced and counted from here.
    # Hard poll cap: bound an attacker hammering the endpoint.
    row.poll_count += 1
    if row.poll_count > settings.auth_device_max_poll_attempts:
        row.status = DeviceAuthStatus.EXPIRED
        await db.flush()
        return RedeemError.EXPIRED_TOKEN

    # slow_down: a poll sooner than `interval_seconds` after the previous one.
    # Advance last_polled_at even on a too-fast poll so the client must back off
    # from its latest attempt (RFC 8628 §3.5: add ≥5s to interval on slow_down).
    if (
        row.last_polled_at is not None
        and (now - row.last_polled_at).total_seconds() < row.interval_seconds
    ):
        row.last_polled_at = now
        await db.flush()
        return RedeemError.SLOW_DOWN
    row.last_polled_at = now

    if row.status == DeviceAuthStatus.PENDING:
        await db.flush()
        return RedeemError.AUTHORIZATION_PENDING

    # APPROVED → mint + consume (single-use).
    user, banned = (
        (None, False)
        if row.user_id is None
        else await user_service.get_by_id_with_ban(db, row.user_id)
    )
    if user is None or banned or not user.is_active:
        # The approver was deleted, banned or deprovisioned between approve and
        # redeem — fail closed, and the same way for all three. Every other
        # token-minting path checks `is_active`; an approved-but-unredeemed
        # grant must not outlive the account it belongs to and hand out a fresh
        # 90-day CLI token after offboarding.
        row.status = DeviceAuthStatus.EXPIRED
        await db.flush()
        return RedeemError.EXPIRED_TOKEN
    row.status = DeviceAuthStatus.CONSUMED
    row.consumed_at = now
    await db.flush()
    return user, row


class _UserCodeAttemptLimiter:
    """Per-user failed-lookup counter with a fixed time window.

    A speed bump against an authenticated user brute-forcing another device's
    short `user_code`. In-process (module singleton), mirroring the revocation
    cache: the deployment carries no shared cache tier, and a per-process cap
    on a 40-bit code behind a 10-minute TTL is sufficient. The window is anchored
    at a user's FIRST failure and lasts `_WINDOW_SECONDS`; once it elapses the
    count resets on the next failure (a fixed, not sliding, window — adequate for
    a brute-force speed bump and cheap to reason about).
    """

    _WINDOW_SECONDS = 600.0

    def __init__(self) -> None:
        # user_id -> (failure_count, window_started_at_monotonic)
        self._failures: dict[UUID, tuple[int, float]] = {}

    def over_limit(self, user_id: UUID) -> bool:
        entry = self._failures.get(user_id)
        if entry is None:
            return False
        count, started = entry
        if (time.monotonic() - started) >= self._WINDOW_SECONDS:
            del self._failures[user_id]
            return False
        return count >= settings.auth_device_max_user_code_attempts

    def record_failure(self, user_id: UUID) -> None:
        now = time.monotonic()
        entry = self._failures.get(user_id)
        if entry is None or (now - entry[1]) >= self._WINDOW_SECONDS:
            self._failures[user_id] = (1, now)
        else:
            self._failures[user_id] = (entry[0] + 1, entry[1])

    def reset(self) -> None:
        """Drop all state. For tests."""
        self._failures.clear()


_user_code_limiter = _UserCodeAttemptLimiter()


def assert_user_code_attempts(user_id: UUID) -> bool:
    """True if the user is still under their failed-lookup budget."""
    return not _user_code_limiter.over_limit(user_id)


def record_user_code_failure(user_id: UUID) -> None:
    """Count a failed (404) user_code lookup against the user's budget."""
    _user_code_limiter.record_failure(user_id)
