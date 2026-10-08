"""Whether a request speaks for the person's whole account, not one org.

A person may belong to several orgs, and each org may sign them in through its
own IdP. That IdP is run by the org's admins, so a session it started speaks
for that org alone: it must not see the person's other orgs, end their
sessions elsewhere, or plant a second factor on the account. The controls that
reach the whole account (the sessions list, "sign out everywhere", MFA
enrollment, the orgs the person belongs to) ask this module first.

A session stands for the account when it proved the identity itself (a
password, Google or GitHub sign-in, a signup, a credential change, or an
assertion from the IdP of the person's own home org; see
``sign_in_policy.account_grant_at``). A session started before sign-ins were
recorded did: it could only have been the identity signing in to its home org.
A credential that is not a browser session (a CLI token, an access key) has no
record of how the session that minted it signed in: it may not change the
account (end sessions, plant a factor), and it keeps reading the person's orgs
as it always has, because the CLI and the editor's org switcher list them
through it.

While multi-org is off, every identity has one org and the org's IdP is the
account's own, so every session stands for the account, as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from alkera_core.auth import family_of_access_token, sign_in_policy
from alkera_core.auth.tenancy import multi_org_enabled
from alkera_core.models import User
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import refusals
from backend.auth.refusals import Reader
from backend.auth.session_issue import acting_session_jti

#: The refusal code for an account-wide control reached by a session that does
#: not stand for the account.
ACCOUNT_SIGN_IN_REQUIRED = refusals.ACCOUNT_SIGN_IN_REQUIRED.code


@dataclass(frozen=True, slots=True)
class AccountStanding:
    """What the request's credential proves about the account. ``proven``: a
    browser session that signed in as the identity itself; ``proven_at``:
    when, if that is known (a session started before sign-ins were recorded
    proves it at an unknown time); ``browser_session``: the credential is a
    browser session at all (False for a CLI token or an access key)."""

    proven: bool
    proven_at: datetime | None = None
    browser_session: bool = True

    def within(self, window: timedelta, *, now: datetime | None = None) -> bool:
        """Whether the proof is known to be no older than ``window``."""
        if self.proven_at is None:
            return False
        return (now or datetime.now(UTC)) - self.proven_at <= window


async def account_standing(db: AsyncSession, request: Request, user: User) -> AccountStanding:
    """What the session behind ``request`` proves about ``user``'s account."""
    jti = acting_session_jti(request)
    family = await family_of_access_token(db, jti) if jti is not None else None
    if family is None or family.user_id != user.id:
        return AccountStanding(proven=False, browser_session=False)
    grants = await sign_in_policy.family_grants(db, family.family_id)
    if not grants:
        return AccountStanding(proven=True)
    at = sign_in_policy.account_grant_at(grants, user=user)
    return AccountStanding(proven=at is not None, proven_at=at)


def account_wide_controls_guarded() -> bool:
    """Whether account-wide controls ask for an account-level sign-in: only
    while an identity can belong to more than one org."""
    return multi_org_enabled()


def account_sign_in_required(standing: AccountStanding) -> HTTPException:
    """The refusal, worded for the credential the standing was read from."""
    reader = Reader.of(browser_session=standing.browser_session)
    return refusals.ACCOUNT_SIGN_IN_REQUIRED.to(reader)


async def require_account_standing(db: AsyncSession, request: Request, user: User) -> None:
    """Refuse an account-wide change from a session that does not stand for
    the account (a 403 ``account_sign_in_required``)."""
    if not account_wide_controls_guarded():
        return
    standing = await account_standing(db, request, user)
    if not standing.proven:
        raise account_sign_in_required(standing)


async def speaks_for_account(db: AsyncSession, request: Request, user: User) -> bool:
    """Whether an account-wide read may show what lies outside the request's
    org: not to a browser session that an org's IdP started."""
    if not account_wide_controls_guarded():
        return True
    standing = await account_standing(db, request, user)
    return standing.proven or not standing.browser_session


__all__ = [
    "ACCOUNT_SIGN_IN_REQUIRED",
    "AccountStanding",
    "account_sign_in_required",
    "account_standing",
    "account_wide_controls_guarded",
    "require_account_standing",
    "speaks_for_account",
]
