"""Platform bans — the register, and the refusal every public path shares.

Banning shuts a person (or every address at a domain) out of the product and
answers them everywhere as if the account did not exist: the session
dependency, a personal access token, a password login, a device-code
redemption and a Slack mention all fold :func:`alkera_core.bans.banned_predicate`
into the user fetch they already make, and take the branch they take for an
unknown user. The two public routes that would otherwise *confirm* an address
— signup and invitation — answer a banned address exactly as one that fails
email validation (:func:`invalid_email_refusal`), so the ban reads as "enter a
valid email".

A ban also revokes the account's sessions through the ordinary revocation
seam, the way deactivation does, so an open socket or event stream re-checking
its token drops at its next check rather than at its next request. Lifting a
ban restores whatever credential the seam did not touch (a personal access
token, a device grant) at once; a revoked session stays revoked, as after a
logout-all.

Platform staff cannot be banned by account, and a domain ban never covers a
staff account either (the predicate exempts them) — a ban on the operators'
own domain must not lock every admin out with nobody left to lift it.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from alkera_core.auth import revoke_all_for_user
from alkera_core.bans import active_domain_ban_exists, active_user_ban_exists
from alkera_core.models import EmailDomainBan, User, UserBan
from alkera_core.schemas.identity.bans import DomainBanRead, UserBanRead
from alkera_core.utils.email import email_domain
from fastapi.exceptions import RequestValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from backend.services.identity import users as user_service
from backend.services.infra import now as _now


class BanConflictError(Exception):
    """An active ban already covers the target."""


class NoActiveBanError(Exception):
    """There is no active ban to lift."""


class BanRefusedError(Exception):
    """The target may not be banned: the caller themselves, or a staff account."""


#: The reason ``email-validator`` gives an address whose domain has no dot.
#: A banned address is refused with this exact text so the response is the
#: one pydantic produces for a malformed address, byte for byte.
_INVALID_EMAIL_REASON = "The part after the @-sign is not valid. It should have a period."


def invalid_email_refusal(email: str, *, loc: tuple[str, ...]) -> RequestValidationError:
    """The 422 a request body gets when ``EmailStr`` rejects the address —
    raised for a banned address so the two are indistinguishable. ``loc`` is
    the field's location in the body, e.g. ``("body", "email")``."""
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": loc,
                "msg": f"value is not a valid email address: {_INVALID_EMAIL_REASON}",
                "input": email,
                "ctx": {"reason": _INVALID_EMAIL_REASON},
            }
        ]
    )


# --------------------------------------------------------------------------- #
# the question every path asks
# --------------------------------------------------------------------------- #


async def email_is_banned(db: AsyncSession, email: str) -> bool:
    """Whether an address — whether or not it has an account — is shut out:
    its account carries an active ban, or its domain does. One statement."""
    normalized = user_service.normalize_email(email)
    account_hit = (
        select(UserBan.id)
        .join(User, User.id == UserBan.user_id)
        .where(User.email == normalized, UserBan.lifted_at.is_(None))
        .exists()
    )
    domain_hit = (
        select(EmailDomainBan.id)
        .where(
            EmailDomainBan.domain == email_domain(normalized),
            EmailDomainBan.lifted_at.is_(None),
        )
        .exists()
    )
    return bool((await db.execute(select(or_(account_hit, domain_hit)))).scalar_one())


async def ban_reasons(db: AsyncSession) -> dict[UUID, str]:
    """Every banned user id with the reason that bans them — the account ban's
    when there is one, else the domain ban's. One statement for the whole
    register, so the users list costs no query per row."""
    stmt = (
        select(User.id, func.coalesce(UserBan.reason, EmailDomainBan.reason))
        .select_from(User)
        .outerjoin(UserBan, and_(UserBan.user_id == User.id, UserBan.lifted_at.is_(None)))
        .outerjoin(
            EmailDomainBan,
            and_(
                EmailDomainBan.domain == User.email_domain,
                EmailDomainBan.lifted_at.is_(None),
                User.platform_role.is_(None),
            ),
        )
        .where(or_(UserBan.id.is_not(None), EmailDomainBan.id.is_not(None)))
    )
    return {user_id: reason for user_id, reason in (await db.execute(stmt)).all()}


# --------------------------------------------------------------------------- #
# user bans
# --------------------------------------------------------------------------- #


async def active_user_ban(db: AsyncSession, user_id: UUID) -> UserBan | None:
    return (
        await db.execute(
            select(UserBan).where(UserBan.user_id == user_id, UserBan.lifted_at.is_(None))
        )
    ).scalar_one_or_none()


async def ban_user(db: AsyncSession, *, target: User, actor: User, reason: str) -> UserBan:
    """Ban ``target`` and revoke every session they hold.

    Refused for the caller themselves and for any staff account (a ban on the
    people who lift bans is a lockout with nobody left to undo it). A second
    ban while one is active is a conflict — decided by the partial unique
    index, so two concurrent bans cannot both land.
    """
    if target.id == actor.id:
        raise BanRefusedError("You cannot ban yourself")
    if target.platform_role is not None:
        raise BanRefusedError("Platform staff accounts cannot be banned")
    if await active_user_ban(db, target.id) is not None:
        raise BanConflictError("This user is already banned")
    ban = UserBan(user_id=target.id, reason=reason, created_by_id=actor.id)
    db.add(ban)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise BanConflictError("This user is already banned") from exc
    await db.refresh(ban)
    await revoke_all_for_user(db, target.id)
    return ban


async def lift_user_ban(db: AsyncSession, *, user_id: UUID, actor: User) -> UserBan:
    ban = await active_user_ban(db, user_id)
    if ban is None:
        raise NoActiveBanError("This user is not banned")
    ban.lifted_at = _now()
    ban.lifted_by_id = actor.id
    await db.flush()
    return ban


async def list_user_bans(db: AsyncSession) -> list[UserBanRead]:
    """The whole register, active and lifted, newest first, with the banned
    user's identity and the admins on either end."""
    banned = aliased(User)
    creator = aliased(User)
    lifter = aliased(User)
    stmt = (
        select(
            UserBan,
            banned.email,
            banned.first_name,
            banned.last_name,
            creator.email,
            lifter.email,
        )
        .join(banned, banned.id == UserBan.user_id)
        .outerjoin(creator, creator.id == UserBan.created_by_id)
        .outerjoin(lifter, lifter.id == UserBan.lifted_by_id)
        .order_by(UserBan.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        UserBanRead(
            id=ban.id,
            user_id=ban.user_id,
            user_email=email,
            user_display_name=f"{first} {last}".strip(),
            reason=ban.reason,
            created_at=ban.created_at,
            created_by_id=ban.created_by_id,
            created_by_email=created_by_email,
            lifted_at=ban.lifted_at,
            lifted_by_id=ban.lifted_by_id,
            lifted_by_email=lifted_by_email,
            active=ban.lifted_at is None,
        )
        for ban, email, first, last, created_by_email, lifted_by_email in rows
    ]


# --------------------------------------------------------------------------- #
# domain bans
# --------------------------------------------------------------------------- #


async def active_domain_ban(db: AsyncSession, domain: str) -> EmailDomainBan | None:
    return (
        await db.execute(
            select(EmailDomainBan).where(
                EmailDomainBan.domain == domain, EmailDomainBan.lifted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def ban_domain(db: AsyncSession, *, domain: str, actor: User, reason: str) -> EmailDomainBan:
    """Ban every address at ``domain`` (already normalized) and revoke the
    sessions of every non-staff account there."""
    if await active_domain_ban(db, domain) is not None:
        raise BanConflictError("This domain is already banned")
    ban = EmailDomainBan(domain=domain, reason=reason, created_by_id=actor.id)
    db.add(ban)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise BanConflictError("This domain is already banned") from exc
    await db.refresh(ban)
    covered: Sequence[UUID] = (
        (
            await db.execute(
                select(User.id).where(User.email_domain == domain, User.platform_role.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for user_id in covered:
        await revoke_all_for_user(db, user_id)
    return ban


async def lift_domain_ban(db: AsyncSession, *, domain: str, actor: User) -> EmailDomainBan:
    ban = await active_domain_ban(db, domain)
    if ban is None:
        raise NoActiveBanError("This domain is not banned")
    ban.lifted_at = _now()
    ban.lifted_by_id = actor.id
    await db.flush()
    return ban


async def list_domain_bans(db: AsyncSession) -> list[DomainBanRead]:
    creator = aliased(User)
    lifter = aliased(User)
    stmt = (
        select(EmailDomainBan, creator.email, lifter.email)
        .outerjoin(creator, creator.id == EmailDomainBan.created_by_id)
        .outerjoin(lifter, lifter.id == EmailDomainBan.lifted_by_id)
        .order_by(EmailDomainBan.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    return [
        DomainBanRead(
            id=ban.id,
            domain=ban.domain,
            reason=ban.reason,
            created_at=ban.created_at,
            created_by_id=ban.created_by_id,
            created_by_email=created_by_email,
            lifted_at=ban.lifted_at,
            lifted_by_id=ban.lifted_by_id,
            lifted_by_email=lifted_by_email,
            active=ban.lifted_at is None,
        )
        for ban, created_by_email, lifted_by_email in rows
    ]


__all__ = [
    "BanConflictError",
    "BanRefusedError",
    "NoActiveBanError",
    "active_domain_ban",
    "active_domain_ban_exists",
    "active_user_ban",
    "active_user_ban_exists",
    "ban_domain",
    "ban_reasons",
    "ban_user",
    "email_is_banned",
    "invalid_email_refusal",
    "lift_domain_ban",
    "lift_user_ban",
    "list_domain_bans",
    "list_user_bans",
]
