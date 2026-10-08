"""DB-backed brute-force account lockout.

Self-hosted deployments often have no edge WAF, so the password login path needs
its own throttle. Consecutive failures within a window lock the account for a
cool-off period; the counter resets on the first success. DB-backed so it holds
across replicas without external infrastructure. Disabled when the threshold is 0.

Tradeoff: per-email lockout lets an attacker who knows an address cause a
temporary denial for that user. The cool-off is deliberately short, and a
break-glass admin / SSO path remains; this is the standard, compliance-expected
behavior. Tune via AUTH_LOCKOUT_* settings.

An address that resolves to NO usable account — never registered, or banned and
therefore answered as if it never existed — is counted the same way, in
``login_lockouts``. Without that the lockout was an existence oracle: the same
six wrong passwords produced a 429 for a real account and an endless 401 for
everything else, which is exactly the fact the decoy KDF on this route already
goes out of its way not to leak.

Both halves run the same threshold, the same window and the same cool-off, and
the parity is UNCONDITIONAL over every legal combination of the three — which
took two deliberate choices, because the obvious implementation breaks at two
of them:

* ``duration`` may exceed ``window``. A prune keyed on ``last_failed_at`` alone
  would drop a row that is still inside its cool-off, while the ``users`` half
  is never pruned — so after one window the unknown address answers 401 and the
  real one still answers 429. :func:`prune_expired` therefore deletes only a row
  past BOTH its window and its lock.
* ``threshold`` may be 1. The upsert's INSERT half runs on the very first
  failure, so if only ``ON CONFLICT`` stamped ``locked_until`` a real address
  would lock on attempt one and an unknown one never would. Both halves stamp it.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import UTC, datetime, timedelta

from alkera_core.auth import prune_login_lockouts
from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.models import LoginLockout, User
from fastapi import HTTPException, status
from sqlalchemy import case, delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import record_security_event

log = get_logger(__name__)


def _digest(email: str) -> str:
    """The key an unknown identifier is counted under.

    Peppered so a stolen dump can't be scanned for "did <address> ever try to
    sign in" — the table's purpose is a schedule, not a record of who knocked."""
    return hmac.new(
        settings.effective_token_hash_pepper.encode(),
        email.lower().strip().encode(),
        hashlib.sha256,
    ).hexdigest()


def lock_expiry(user: User, *, now: datetime | None = None) -> datetime | None:
    """When this account's cool-off ends, or None when no lock is in force.

    The instant rather than a yes/no, because every caller that refuses on a
    lock also has to say how long it lasts — see :func:`locked_out`."""
    if settings.auth_lockout_threshold <= 0:
        return None
    if user.locked_until is None or user.locked_until <= (now or datetime.now(UTC)):
        return None
    return user.locked_until


def locked_out(expires_at: datetime, *, now: datetime | None = None) -> HTTPException:
    """The single refusal a lock produces, wherever it is enforced.

    Carries ``Retry-After``. The person behind a lockout is usually the account
    owner who mistyped, and a refusal with no number leaves them re-trying the
    one endpoint the lock exists to protect — the wait has to be something a
    client can render rather than something it has to discover by knocking. The
    number discloses nothing about whether the account exists: an address with
    no usable account is locked from the same duration at the same point in its
    streak, so both halves answer the same seconds at the same attempt."""
    seconds = max(1, math.ceil((expires_at - (now or datetime.now(UTC))).total_seconds()))
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "account_locked",
            "message": f"Too many failed attempts. Try again in {span(seconds)}.",
            "retry_after": seconds,
        },
        headers={"Retry-After": str(seconds)},
    )


def span(seconds: int) -> str:
    """A wait as a person reads it, rounded UP so nobody retries early:
    "40 seconds", "1 minute", "15 minutes", "2 hours"."""
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    if seconds < 3600:
        minutes = math.ceil(seconds / 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = math.ceil(seconds / 3600)
    return f"{hours} hour{'' if hours == 1 else 's'}"


async def wrong_factor(db: AsyncSession, user: User, *, code: str, wrong: str) -> HTTPException:
    """The refusal for a wrong password or code on a signed-in step-up, once
    :func:`record_failure` has counted it and the count is committed.

    The caller is the account's own session, so the budget is not an oracle
    here: the person needs to know that the next tries count toward a lock,
    and how many are left, before they spend them. The try that crosses the
    threshold keeps its own code (it was still a wrong factor) and says the
    account is now locked and for how long, so the lock is never first learned
    from a later, unexplained refusal. Lockout disabled: the plain sentence."""
    if settings.auth_lockout_threshold <= 0:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, "message": wrong}
        )
    await db.refresh(user, attribute_names=["failed_login_count", "locked_until"])
    expiry = lock_expiry(user)
    if expiry is not None:
        seconds = max(1, math.ceil((expiry - datetime.now(UTC)).total_seconds()))
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": code,
                "message": f"{wrong} The account is now locked for {span(seconds)}.",
                "attempts_left": 0,
                "retry_after": seconds,
            },
        )
    left = max(1, settings.auth_lockout_threshold - (user.failed_login_count or 0))
    lock_for = span(settings.auth_lockout_duration_seconds)
    tries = "try" if left == 1 else "tries"
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": code,
            "message": f"{wrong} {left} more {tries} before the account is locked for {lock_for}.",
            "attempts_left": left,
        },
    )


async def record_failure(db: AsyncSession, email: str) -> None:
    """Count a failed attempt + lock past the threshold, in ONE atomic UPDATE.

    The count is computed SQL-side from the row Postgres locks for the write, so
    every attempt is counted exactly once. A read-modify-write would silently
    lose concurrent guesses — N parallel attempts all read the same count and all
    store the same value, so the counter tracks rounds of guessing rather than
    guesses and the account never locks.

    Runs on the CALLER's session (the caller commits before returning its
    401/403 — the request's own rollback would otherwise discard the counter).
    Best-effort: wrapped in a SAVEPOINT so a failure here can neither raise into
    the auth path nor poison the surrounding transaction.
    """
    if settings.auth_lockout_threshold <= 0:
        return
    now = datetime.now(UTC)
    window_start = now - timedelta(seconds=settings.auth_lockout_window_seconds)
    # Evaluated against the pre-UPDATE row: a first failure, or one that lands
    # past the window, restarts the streak at 1.
    next_count = case(
        (User.last_failed_login_at.is_(None), 1),
        (User.last_failed_login_at < window_start, 1),
        else_=User.failed_login_count + 1,
    )
    stmt = (
        update(User)
        .where(User.email == email.lower().strip())
        .values(
            failed_login_count=next_count,
            last_failed_login_at=now,
            locked_until=case(
                (
                    next_count >= settings.auth_lockout_threshold,
                    now + timedelta(seconds=settings.auth_lockout_duration_seconds),
                ),
                else_=User.locked_until,
            ),
        )
        .returning(User.id, User.failed_login_count)
        .execution_options(synchronize_session=False)
    )
    try:
        async with db.begin_nested():
            row = (await db.execute(stmt)).first()
            # The attempt that reached the threshold is the one that locked the
            # account: the identity's own log records the lockout once.
            if row is not None and row[1] == settings.auth_lockout_threshold:
                await record_security_event(db, user_id=row[0], event="auth.locked_out")
    except Exception:  # pragma: no cover — lockout accounting must never break login
        log.warning("lockout.record_failure_failed", exc_info=True)


def reset(user: User) -> None:
    """Clear the failure counter + lock on a successful authentication."""
    user.failed_login_count = 0
    user.last_failed_login_at = None
    user.locked_until = None


async def unknown_lock_expiry(
    db: AsyncSession, email: str, *, now: datetime | None = None
) -> datetime | None:
    """When the cool-off ends for an identifier with no usable account.

    The login route calls this on EVERY attempt and then picks which answer to
    act on, rather than only when the address turned out not to exist: the work
    a refusal costs — one primary-key lookup — is then the same either way, and
    cannot become the timing oracle the decoy KDF two lines later exists to
    close. ``db.get`` would serve the second call of a request from the identity
    map, so the read is issued explicitly."""
    if settings.auth_lockout_threshold <= 0:
        return None
    row = (
        await db.execute(
            LoginLockout.__table__.select().where(LoginLockout.identifier_digest == _digest(email))
        )
    ).first()
    if row is None or row.locked_until is None:
        return None
    expires_at: datetime = row.locked_until
    return expires_at if expires_at > (now or datetime.now(UTC)) else None


async def record_unknown_failure(db: AsyncSession, email: str) -> None:
    """Count a failed attempt against an identifier with no usable account.

    The same single-statement discipline as :func:`record_failure`: the streak
    is computed SQL-side from the row Postgres locks for the write, so parallel
    guesses are all counted and the ladder — restart past the window, lock at
    the threshold — matches the `users` one exactly. Best-effort and wrapped in
    a SAVEPOINT for the same reason: accounting must never break a login."""
    if settings.auth_lockout_threshold <= 0:
        return
    now = datetime.now(UTC)
    window_start = now - timedelta(seconds=settings.auth_lockout_window_seconds)
    locked_until = now + timedelta(seconds=settings.auth_lockout_duration_seconds)
    # Evaluated against the row already stored: a failure past the window
    # restarts the streak at 1, exactly as the `users` ladder does.
    next_count = case(
        (LoginLockout.last_failed_at < window_start, 1),
        else_=LoginLockout.failed_count + 1,
    )
    # The INSERT half IS the first failure, so it must lock when one failure is
    # the threshold — otherwise a real address locks at threshold 1 and an
    # unknown one never does, which is the oracle back again.
    first_failure_locks = locked_until if settings.auth_lockout_threshold <= 1 else None
    stmt = (
        insert(LoginLockout)
        .values(
            identifier_digest=_digest(email),
            failed_count=1,
            last_failed_at=now,
            locked_until=first_failure_locks,
        )
        .on_conflict_do_update(
            constraint="pk_login_lockouts",
            set_={
                "failed_count": next_count,
                "last_failed_at": now,
                "locked_until": case(
                    (next_count >= settings.auth_lockout_threshold, locked_until),
                    else_=LoginLockout.locked_until,
                ),
            },
        )
    )
    try:
        async with db.begin_nested():
            await db.execute(stmt)
    except Exception:  # pragma: no cover — lockout accounting must never break login
        log.warning("lockout.record_unknown_failure_failed", exc_info=True)


async def prune_expired(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete the counters that can no longer change an answer. Returns the count.

    The predicate lives in ``alkera_core.auth.lockout_prune`` because the caller
    that matters is the WORKER's daily prune, beside the token and device-code
    ones, and the worker does not depend on the backend. Retention is not
    housekeeping here: releasing a row early re-opens the existence oracle, so
    the reasoning is on that function.

    Scheduled rather than amortized onto the login path — a writer-driven purge
    is process-local state that fires N times over with N replicas, and its
    cadence is the one thing on this path an operator might want to move."""
    return await prune_login_lockouts(
        db, window_seconds=settings.auth_lockout_window_seconds, now=now
    )


async def clear_unknown(db: AsyncSession, email: str) -> None:
    """Drop the unknown-identifier counter once the address authenticates.

    An address sprayed before anyone registered it must not arrive pre-locked
    on the day it becomes a real account — from then on its own `users` row is
    the counter that governs it."""
    try:
        async with db.begin_nested():
            await db.execute(
                delete(LoginLockout).where(LoginLockout.identifier_digest == _digest(email))
            )
    except Exception:  # pragma: no cover — accounting must never break login
        log.warning("lockout.clear_unknown_failed", exc_info=True)
