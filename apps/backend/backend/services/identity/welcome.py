"""Welcome email — sent exactly once, the first time a user's email is verified.

The `users.welcome_email_sent_at` stamp is the once-ever guard: it makes the send
idempotent across every verification entry point (the verify-email route, invite
signup, OAuth registration), so a user is welcomed at most once regardless of path.

Deliberately kept OUT of `email_verification_service.mark_verified` — that primitive
is also used by the dev seed, which must not send mail. The user-facing paths call
this guard explicitly instead.
"""

from __future__ import annotations

from typing import Any, cast

from alkera_core.email import send_welcome_email
from alkera_core.models import User
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.infra import now as _now


async def send_welcome_if_unsent(db: AsyncSession, user: User) -> bool:
    """Send the welcome email once. Return True if sent now, False if already sent.

    The atomic conditional UPDATE — NOT a Python read-check-write — is the real
    guard. Concurrent or duplicate verifies (a double-fired SPA verify request is
    the common case) race here, and only the transaction whose UPDATE actually
    flips the NULL sentinel sends: under READ COMMITTED the loser's
    `welcome_email_sent_at IS NULL` predicate re-evaluates against the winner's
    committed row, matches nothing, and reports rowcount 0. A plain
    `if user.welcome_email_sent_at is not None` check is NOT enough — both racers
    load a stale NULL before either commits and both send (the double-send bug).

    The send swallows SMTP errors (see `alkera_core.email._send`), so a transient mail
    failure still leaves the user marked welcomed rather than re-sending on every
    later login.
    """
    # Fast path: obviously already welcomed (e.g. the guard invoked twice within
    # one transaction). The atomic claim below is what makes it correct.
    if user.welcome_email_sent_at is not None:
        return False
    now = _now()
    result = await db.execute(
        update(User)
        .where(User.id == user.id, User.welcome_email_sent_at.is_(None))
        .values(welcome_email_sent_at=now)
        .execution_options(synchronize_session=False)
    )
    # execute() is typed as Result, but an UPDATE returns a CursorResult (rowcount).
    if cast("CursorResult[Any]", result).rowcount == 0:
        # Lost the race (or already sent) — another transaction has the claim.
        return False
    user.welcome_email_sent_at = now  # keep the in-memory object consistent
    await send_welcome_email(user)
    return True
