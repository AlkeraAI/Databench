"""User domain operations.

Routes stay thin and call these. Password hashing happens here, not in
routes — it's a domain concern.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from alkera_core.auth import revoke_all_for_user
from alkera_core.auth.proxy_token import is_proxy_machine_email
from alkera_core.bans import banned_predicate
from alkera_core.db.errors import is_unique_violation
from alkera_core.events import Entity, EventType, actor_system, emit
from alkera_core.models import OrgMembership, PlatformRole, User
from alkera_core.objects import chat_end
from alkera_core.org_entitlements import org_entitlements
from alkera_core.utils import email_domain as derive_email_domain
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.password import hash_password

#: The actor an activation change is recorded under when the caller names none.
SYSTEM_ACTOR = "backend:user_service"


class UserConflictError(Exception):
    """Raised when a uniqueness constraint would be violated (e.g. duplicate email)."""


def normalize_email(email: str) -> str:
    """The single spelling an address is stored + compared under."""
    return email.lower().strip()


def assert_email_assignable(email: str) -> None:
    """Refuse an address no human account may ever hold.

    The per-org proxy MACHINE identity lives on a reserved synthetic domain
    (svc.alkera.proxy). A human must never hold an address there: the gateway
    trusts the forwarded usage-meter header ONLY from a proxy-machine principal
    (identified by that domain), and resolves its funding pool under the
    machine rule that drops the per-user postpaid cap — so a user holding such
    an address could forge the meter, dodge invoicing, and draw an uncapped
    postpaid pool. This is the chokepoint EVERY write of ``User.email`` goes
    through (creation and self-service rename alike); the machine user itself is
    built by direct construction, never through this module.
    """
    if is_proxy_machine_email(email):
        raise UserConflictError("That email domain is reserved for internal use")


async def get_by_id(db: AsyncSession, user_id: UUID) -> User | None:
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def _one_with_ban(
    db: AsyncSession, stmt: Select[tuple[User, bool]]
) -> tuple[User | None, bool]:
    row = (await db.execute(stmt)).first()
    if row is None:
        return None, False
    return row[0], bool(row[1])


async def get_by_id_with_ban(db: AsyncSession, user_id: UUID) -> tuple[User | None, bool]:
    """The user and whether they are banned, in one statement. Every path that
    authenticates a user reads through here (or ``get_by_email_with_ban``) and
    answers a banned user exactly as it answers ``None``."""
    return await _one_with_ban(db, select(User, banned_predicate()).where(User.id == user_id))


async def get_by_email_with_ban(db: AsyncSession, email: str) -> tuple[User | None, bool]:
    return await _one_with_ban(
        db, select(User, banned_predicate()).where(User.email == normalize_email(email))
    )


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    # Normalize identically to how emails are stored (`create_user` lowercases +
    # strips), so a lookup with stray case/whitespace still resolves the row.
    return (
        await db.execute(select(User).where(User.email == email.lower().strip()))
    ).scalar_one_or_none()


async def list_all(db: AsyncSession) -> Sequence[User]:
    """Cross-tenant — admin-only routes."""
    result = await db.execute(select(User).order_by(User.created_at))
    return result.scalars().all()


async def create_user(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    email: str,
    first_name: str,
    last_name: str,
    password: str | None,
    platform_role: PlatformRole | None = None,
) -> User:
    normalized = normalize_email(email)
    assert_email_assignable(normalized)
    if await get_by_email(db, normalized) is not None:
        raise UserConflictError(f"User with email {normalized!r} already exists")

    user = User(
        home_org_team_id=org_team_id,
        email=normalized,
        email_domain=derive_email_domain(normalized),
        first_name=first_name,
        last_name=last_name,
        password_hash=hash_password(password) if password else None,
        platform_role=platform_role,
    )
    try:
        # In a savepoint, so the loser of two concurrent creates for one address
        # gets the same refusal the existence check above gives, with its
        # transaction still usable, rather than the unique index's raw error.
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError as exc:
        if is_unique_violation(exc):
            raise UserConflictError(f"User with email {normalized!r} already exists") from exc
        raise
    await db.refresh(user)
    # Every new seat is provisioned onto Free with its first cycle's credits, in the
    # same transaction — so a user is never created without a usable balance. This is
    # THE convergence for bare signup, invitation-accept, OAuth signup, and the dev
    # seed; idempotent, so re-running any of those paths is safe.
    await org_entitlements().open_seat(db, user_id=user.id, org_id=org_team_id)
    return user


async def update_profile(
    db: AsyncSession,
    user: User,
    *,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    password: str | None = None,
) -> User:
    """Update non-privilege fields. Does NOT modify platform_role.

    A change to either credential-grade field is a SECURITY EVENT, handled here
    rather than at a route so no caller can forget it:

    - a new address is unproven, so the verification stamp (and any outstanding
      token for the old address) is cleared. Carrying it over would leave the
      account "verified" on an address nobody owns, which disarms the OAuth
      account-pre-hijack eviction in ``oauth_service.resolve`` (it fires only on
      ``email_verified_at is None``) and satisfies ``require_email_verified``
      with a stolen identity;
    - a new password or address revokes every live session/CLI token, matching
      ``password_reset_service.consume_token``. Otherwise the victim's own
      "change my password" does not evict an intruder, and an intruder's change
      does not evict the owner;
    - a new password or address also retires any outstanding password-reset
      link. An emailed link stays spendable for its whole TTL, and spending it
      both sets a password the account holder did not choose and revokes the
      sessions they just established — so the link has to die with the
      credential it was minted against. ``clear_password`` and a consumed reset
      already do this; this is the third writer of the same invariant.
    """
    credentials_changed = password is not None
    if email is not None:
        normalized = normalize_email(email)
        if normalized != user.email:
            assert_email_assignable(normalized)
            existing = await get_by_email(db, normalized)
            if existing is not None and existing.id != user.id:
                raise UserConflictError(f"Email {normalized!r} already taken")
            user.email = normalized
            user.email_domain = derive_email_domain(normalized)
            user.email_verified_at = None
            user.email_verification_token = None
            user.email_verification_expires_at = None
            credentials_changed = True
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if password is not None:
        user.password_hash = hash_password(password)
    if credentials_changed:
        user.password_reset_token = None
        user.password_reset_expires_at = None
    await db.flush()
    if credentials_changed:
        await revoke_all_for_user(db, user.id)
    return user


async def clear_password(db: AsyncSession, user: User) -> User:
    """Wipe the local password and any pending reset token.

    Used when a verified federated login claims an account whose email was
    never proven (the account-pre-hijack defense in `oauth_service`): whoever
    set the password did so without proving they own the address, so the
    credential — and any stale reset link that could re-set it — is removed.
    The account becomes OAuth-only until the rightful owner sets a new password.
    """
    user.password_hash = None
    user.password_reset_token = None
    user.password_reset_expires_at = None
    await db.flush()
    return user


async def set_active(
    db: AsyncSession, user: User, active: bool, *, actor: Mapping[str, Any] | None = None
) -> User:
    """The identity-level platform disable: flip ``users.is_active``. A disabled
    identity signs in nowhere and every credential it holds is refused.

    Platform staff only; an org offboards a person on their membership
    (``org_memberships.deactivate``) and never here, because the identity may
    belong to other orgs. Disabling ends the person's live chats in every org
    and every session (``revoke_all_for_user``), and the change is announced
    in each org the person belongs to. The caller owns the audit record. A
    call that changes nothing writes nothing and announces nothing.
    """
    if user.is_active == active:
        return user
    user.is_active = active
    await db.flush()
    if not active:
        # No box keeps running a conversation for an identity the platform has
        # just disabled, in any org.
        await chat_end.end_chats(
            db,
            await chat_end.live_chats_owned_by(db, user.id),
            chat_end.ChatEndReason.ACCESS_REMOVED,
            actor=actor,
        )
        await revoke_all_for_user(db, user.id)
    orgs = (
        await db.execute(select(OrgMembership.org_team_id).where(OrgMembership.user_id == user.id))
    ).scalars()
    for org_id in orgs.all():
        await emit(
            db,
            org_id=org_id,
            type=EventType.MEMBERSHIP_CHANGED,
            entity=Entity.MEMBERSHIP,
            entity_id=f"{org_id}:{user.id}",
            payload={"team_id": str(org_id), "user_id": str(user.id), "active": active},
            actor=actor if actor is not None else actor_system(SYSTEM_ACTOR),
        )
    return user


async def set_platform_role(db: AsyncSession, user: User, role: PlatformRole | None) -> User:
    """Privileged operation — caller must enforce ALKERA_ADMIN at the route.

    A change ends every live session of the account: the role rides the
    access token as a claim, and a grant or a revocation must not wait for
    the credentials minted under the old one to lapse."""
    if user.platform_role is role:
        return user
    user.platform_role = role
    await db.flush()
    await revoke_all_for_user(db, user.id)
    return user


#: The name the ``backend.services.identity`` package exports for this.
user_with_ban = get_by_id_with_ban
