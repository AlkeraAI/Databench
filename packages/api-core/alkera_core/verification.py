"""Email-verification grace-period policy.

Single source of truth shared by the backend API gate and the model-gateway —
both import `alkera_core` and neither can import the other's services, so the
rule cannot live in `apps/backend/backend/services`. Pure functions over a
User-shaped object (only `.email_verified_at`, `.created_at`, `.platform_role`
are read) keep them trivially testable and identical across services.

Policy: a freshly-created account may use the product for a grace window after
signup; once the window lapses an unverified account is *blocked*. Already
verified accounts and platform staff are never subject to the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from alkera_core.config import settings

if TYPE_CHECKING:
    from alkera_core.models import User

VerificationState = Literal["verified", "grace", "blocked"]


def _grace_period() -> timedelta:
    return timedelta(days=settings.email_verification_grace_period_days)


def is_exempt(user: User) -> bool:
    """Platform staff (ALKERA_ADMIN / ALKERA_SUPPORT) are provisioned without a
    real inbox (dev seed / ops bootstrap), so they are never gated."""
    return user.platform_role is not None


def requires_verification(user: User) -> bool:
    """True while the account still owes us a verified email — drives the SPA's
    persistent verify banner. False for verified or exempt accounts.

    Also False deployment-wide when email is disabled (`EMAIL_ENABLED=false`): with
    no way to deliver a verification link, gating on verification would permanently
    lock everyone out, so a no-email deployment treats accounts as verified (signup
    is gated by SSO / admin provisioning instead). This is the single seam — the
    block gate, the durable-mutation gate, and the SPA banner all derive from it.
    """
    if not settings.email_enabled:
        return False
    return user.email_verified_at is None and not is_exempt(user)


def is_verified(user: User) -> bool:
    """The account's email is PROVEN (or the account is staff-exempt).

    The strict counterpart to `is_blocked`: True for verified + exempt accounts,
    False while the account still owes a verified email — even *inside* the grace
    window. The `require_email_verified` gate uses this to refuse durable
    org-structure / security mutations (creating invitations, adding members,
    editing org login settings) to an account that has never proven it owns its
    email — see that gate's docstring for the account-squatter threat it closes.
    The model gateway applies the same strict predicate to every model request,
    so an unverified signup cannot spend credits during its grace window.
    """
    return not requires_verification(user)


def deadline(user: User) -> datetime | None:
    """The instant an unverified account flips from grace to blocked, or ``None``
    when the account is not subject to verification (already verified / exempt)."""
    if not requires_verification(user):
        return None
    return user.created_at + _grace_period()


def state(user: User, *, now: datetime | None = None) -> VerificationState:
    """``verified`` (or exempt), ``grace`` (unverified, still inside the window),
    or ``blocked`` (unverified, window lapsed)."""
    due = deadline(user)
    if due is None:
        return "verified"
    now = now or datetime.now(UTC)
    return "grace" if now < due else "blocked"


def is_blocked(user: User, *, now: datetime | None = None) -> bool:
    """The account is unverified AND its grace window has lapsed → deny access.

    The backend's product-router dependency (`require_verified_or_grace`) calls
    this to refuse a request. The model gateway does NOT — paid inference gates
    on the stricter `requires_verification`, with no grace window.
    """
    return state(user, now=now) == "blocked"
