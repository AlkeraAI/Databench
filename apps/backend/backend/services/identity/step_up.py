"""Proof of a current factor before a credential-grade self-service action.

A stolen session must not convert straight into permanent account control or
the loss of the account: changing the email or password, and deleting the
account, all demand the current password (and the authenticator code when MFA
is on) on top of the session.

A verification that is not throttled is an oracle: the attacker who already
holds the session this control exists to defend against would otherwise get
unlimited guesses at the real credential, each one an argon2 hash on a pooled
connection. So a WRONG factor goes through the same ``lockout_service`` counter
``/auth/login`` uses, a locked account is refused before any hashing, and the
counter is committed even though the request itself rolls back. A factor that
is merely ABSENT is not a guess and is not counted: the SPA learns it needs one
from the response and prompts.

Every refusal carries a machine-readable ``code`` so the SPA can tell "collect
a password", "collect a code", "that was wrong" and "you have no password"
apart.
"""

from __future__ import annotations

import contextlib

from alkera_core.logging import get_logger
from alkera_core.models import User
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.password import verify_password
from backend.services.identity import lockout as lockout_service
from backend.services.identity import mfa as mfa_service

log = get_logger(__name__)


async def _commit_accounting(db: AsyncSession) -> None:
    """Persist the lockout counter written for a refused step-up, before raising
    the 4xx that rolls the request back. Reuses the request's own session (a
    second pooled one lets concurrent failures deadlock the pool). Never raises:
    accounting must not turn a 403 into a 500."""
    try:
        await db.commit()
    except Exception:  # pragma: no cover — accounting must never break the route
        log.warning("step_up.accounting_commit_failed", exc_info=True)
        with contextlib.suppress(Exception):
            await db.rollback()


class StepUpRefusedError(Exception):
    """A factor is absent, or the account has none to prove. The route answers
    403 with ``code`` and ``message``; a WRONG factor or a locked account is
    the lockout service's own refusal and propagates as that."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str) -> StepUpRefusedError:
    return StepUpRefusedError(code, message)


async def require_current_factors(
    db: AsyncSession,
    target: User,
    *,
    current_password: str | None,
    mfa_code: str | None,
    federated_message: str,
    missing_factor_message: str,
) -> None:
    """Refuse unless ``current_password`` (and ``mfa_code`` when MFA is on) are
    ``target``'s. An account with no password is refused with
    ``password_not_set`` and ``federated_message``; the caller decides what
    such an account proves instead."""
    lock_expiry = lockout_service.lock_expiry(target)
    if lock_expiry is not None:
        raise lockout_service.locked_out(lock_expiry)
    if target.password_hash is None:
        raise _refuse("password_not_set", federated_message)
    if not current_password:
        raise _refuse("current_password_required", missing_factor_message)
    if not verify_password(current_password, target.password_hash):
        await lockout_service.record_failure(db, target.email)
        await _commit_accounting(db)
        raise await lockout_service.wrong_factor(
            db, target, code="current_password_invalid", wrong="That password isn't correct."
        )
    await require_mfa_code(db, target, mfa_code=mfa_code)
    # Every factor proved — clear the streak, exactly as a successful login
    # does. Deliberately after the second factor: a correct password alone
    # must not reset it.
    lockout_service.reset(target)


async def require_mfa_code(db: AsyncSession, target: User, *, mfa_code: str | None) -> None:
    """When MFA is on, refuse unless ``mfa_code`` is a current code. A wrong
    code counts against the lockout."""
    if not target.mfa_enabled:
        return
    lock_expiry = lockout_service.lock_expiry(target)
    if lock_expiry is not None:
        raise lockout_service.locked_out(lock_expiry)
    if not mfa_code:
        raise _refuse("mfa_required", "Enter your authenticator code.")
    if not await mfa_service.verify_code_locked(db, target, mfa_code):
        await lockout_service.record_failure(db, target.email)
        await _commit_accounting(db)
        raise await lockout_service.wrong_factor(
            db, target, code="mfa_invalid", wrong="That code isn't correct."
        )


__all__ = ["StepUpRefusedError", "require_current_factors", "require_mfa_code"]
