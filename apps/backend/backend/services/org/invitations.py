"""Invitation domain operations.

An invitation is a pending offer from an Org/Team Admin to a specific email
to join a specific team. Acceptance materializes memberships up the chain
per the membership-chain rule.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from alkera_core.auth import hash_lookup_token, lookup_token_digests
from alkera_core.auth.tenancy import multi_org_enabled, stands_in
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.tenant_session import bound_org_ids
from alkera_core.events import Entity, EventType, actor_system, emit
from alkera_core.models import (
    Invitation,
    InvitationStatus,
    MembershipStatus,
    OrgMembership,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from alkera_core.models.invitation import PENDING_EXISTS_MESSAGE
from alkera_core.org_entitlements import org_entitlements
from alkera_core.schemas.identity.invitation import InvitationRefusal, InvitationRefusalCode
from alkera_core.verification import is_verified
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import audit as audit_services
from backend.services.audit import org_audit as org_audit_service
from backend.services.identity import users as user_service
from backend.services.infra import now as _now
from backend.services.org import memberships as membership_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service

DEFAULT_TTL = timedelta(days=7)

#: The actor an invitation change is recorded under when the caller names none.
SYSTEM_ACTOR = "backend:invitation_service"


class InvitationError(Exception):
    """Domain-level invitation violation (cross-org email, expired, etc).

    ``code`` is set when the violation is a standing refusal the recipient can
    read about (see :class:`InvitationRefusalCode`); routes answer it as the
    error envelope's code so a client can tell it from a transient conflict."""

    def __init__(self, message: str, *, code: InvitationRefusalCode | None = None) -> None:
        super().__init__(message)
        self.code = code


def other_org_refusal(*, own_org_name: str, target_org_name: str) -> InvitationRefusal:
    """The single-org rule, told to the one person entitled to know it: the
    owner of an address whose account already belongs to another organization.

    It says what would have to change and nothing the product refuses. Leaving
    one's own organization is not a path (a member cannot remove themself from
    the org root), and the invitation is bound to this exact address, so the
    only change that lets the person join is an invitation to an address that
    has no account yet."""
    own = own_org_name.strip() or "another organization"
    target = target_org_name.strip() or "this organization"
    return InvitationRefusal(
        code=InvitationRefusalCode.OTHER_ORG,
        message=(
            f"Your account belongs to {own}, and an account can belong to only one "
            f"organization. To join {target}, ask for an invitation to an email address "
            "you haven't used with Alkera."
        ),
    )


def unverified_refusal(*, target_org_name: str) -> InvitationRefusal:
    """An account that never proved its address may not join another org on
    the strength of that address: anyone can register an unverified account
    for an inbox they do not own and wait for an invitation to it."""
    target = target_org_name.strip() or "this organization"
    return InvitationRefusal(
        code=InvitationRefusalCode.EMAIL_VERIFICATION_REQUIRED,
        message=f"Verify your email address to join {target}.",
    )


def deactivated_refusal(*, target_org_name: str) -> InvitationRefusal:
    """The org offboarded this account. Restoring it is that org's admins'
    decision, never something an invitation does on the side."""
    target = target_org_name.strip() or "this organization"
    return InvitationRefusal(
        code=InvitationRefusalCode.MEMBERSHIP_DEACTIVATED,
        message=f"Your access to {target} was turned off. Ask an admin of {target} to restore it.",
    )


def masked_address(email: str) -> str:
    """``email`` with all but the first character of its local part hidden
    (``a***@example.com``), for telling a signed-in account which address an
    invitation is for without spelling the address out."""
    local, at, domain = email.partition("@")
    if not at:
        return "***"
    return f"{local[:1]}***@{domain}"


async def joining_refusal(
    db: AsyncSession,
    user: User,
    *,
    org: Team,
    membership: OrgMembership | None,
) -> InvitationRefusal | None:
    """The refusal an accept answers when ``user`` holds no active membership
    in ``org``, or None when the account may join it.

    While multi-org is off every such accept is the single-org rule. With it
    on, a deactivated membership stays deactivated, an unverified account must
    verify first, and anyone else may join (a pending membership, which an
    org's directory provisioned ahead of the person, is joined by accepting)."""
    if not multi_org_enabled():
        elsewhere = await org_membership_service.other_active_org(
            db, user_id=user.id, org_team_id=org.id
        )
        own_org = await db.get(Team, elsewhere.org_team_id) if elsewhere is not None else None
        return other_org_refusal(
            own_org_name=own_org.name if own_org is not None else "",
            target_org_name=org.name,
        )
    if membership is not None and membership.status is MembershipStatus.DEACTIVATED:
        return deactivated_refusal(target_org_name=org.name)
    if not is_verified(user):
        return unverified_refusal(target_org_name=org.name)
    return None


@dataclass(frozen=True)
class RecipientInvitation:
    """A pending invitation as its recipient reads it: the destination by
    name, the sender, and the refusal an accept would answer with, if any."""

    invitation: Invitation
    team_name: str
    org_name: str
    inviter_display_name: str | None
    refusal: InvitationRefusal | None


def _new_token() -> str:
    return secrets.token_urlsafe(48)


async def _add_member_or_conflict(
    db: AsyncSession,
    *,
    team_id: UUID,
    user_id: UUID,
    role: TeamRole,
    actor: Mapping[str, Any] | None,
) -> None:
    """Grant the seat, reporting a membership violation as an invitation one.

    Both invitation flows end in `add_member`, and everything it refuses is
    something the caller did (already on the team, team gone) rather than a
    server fault. Routes render InvitationError as a 409.
    """
    try:
        await membership_service.add_member(
            db, team_id=team_id, user_id=user_id, role=role, actor=actor
        )
    except membership_service.MembershipError as exc:
        raise InvitationError(str(exc)) from exc


async def _emit_changed(
    db: AsyncSession,
    invitation: Invitation,
    *,
    org_id: UUID,
    actor: Mapping[str, Any] | None,
) -> None:
    """Announce an invitation's status moving, inside the caller's transaction.
    The row names its team so the stream hands it to that team's members and
    the org's admins — the people whose pending list it changes."""
    await emit(
        db,
        org_id=org_id,
        type=EventType.INVITATION_CHANGED,
        entity=Entity.INVITATION,
        entity_id=str(invitation.id),
        payload={"team_id": str(invitation.team_id), "status": invitation.status.value},
        actor=actor if actor is not None else actor_system(SYSTEM_ACTOR),
    )


async def get_by_id(db: AsyncSession, invitation_id: UUID) -> Invitation | None:
    return (
        await db.execute(select(Invitation).where(Invitation.id == invitation_id))
    ).scalar_one_or_none()


#: What an invitation link says once it can no longer be used, by status.
#: Keyed by status so a new terminal status is a new entry, not a new branch.
_CLOSED_LINK: dict[InvitationStatus, tuple[str, str]] = {
    InvitationStatus.ACCEPTED: (
        "invitation_accepted",
        "This invitation has already been accepted. Sign in to continue.",
    ),
    InvitationStatus.REJECTED: (
        "invitation_declined",
        "This invitation was declined. Ask your organization for a new link.",
    ),
    InvitationStatus.REVOKED: (
        "invitation_revoked",
        "This invitation was withdrawn. Ask your organization for a new link.",
    ),
    InvitationStatus.EXPIRED: (
        "invitation_expired",
        "This invitation has expired. Ask your organization for a new link.",
    ),
}


def closed_link_reason(invitation: Invitation) -> tuple[str, str] | None:
    """``(code, message)`` when an invitation link can no longer be used, or
    None while it is live. A pending row past its expiry reads as expired."""
    if invitation.status is InvitationStatus.PENDING:
        if invitation.expires_at < _now():
            return _CLOSED_LINK[InvitationStatus.EXPIRED]
        return None
    return _CLOSED_LINK.get(
        invitation.status, ("invitation_closed", "This invitation can no longer be used.")
    )


async def get_by_token(db: AsyncSession, token: str) -> Invitation | None:
    # Only the keyed hash is stored; match the active OR any retired pepper's
    # digest so a pepper rotation doesn't void an outstanding invitation.
    return (
        await db.execute(
            select(Invitation).where(Invitation.token.in_(lookup_token_digests(token)))
        )
    ).scalar_one_or_none()


async def _without_expired(db: AsyncSession, rows: list[Invitation]) -> list[Invitation]:
    """Mark every row past its ``expires_at`` expired and leave it out: an
    invitation nobody can accept any more is not pending, whoever asks."""
    now = _now()
    expired = [inv for inv in rows if inv.expires_at < now]
    for inv in expired:
        inv.status = InvitationStatus.EXPIRED
        inv.resolved_at = now
    if expired:
        await db.flush()
    return [inv for inv in rows if inv not in expired]


async def list_pending_for_team(db: AsyncSession, team_id: UUID) -> Sequence[Invitation]:
    result = await db.execute(
        select(Invitation)
        .where(
            Invitation.team_id == team_id,
            Invitation.status == InvitationStatus.PENDING,
        )
        .order_by(Invitation.created_at)
    )
    return await _without_expired(db, list(result.scalars().all()))


async def _pending_for_address(db: AsyncSession, email: str) -> list[Invitation]:
    """Every live pending invitation addressed to `email`, oldest first."""
    result = await db.execute(
        select(Invitation)
        .where(
            Invitation.email == email.lower(),
            Invitation.status == InvitationStatus.PENDING,
        )
        .order_by(Invitation.created_at)
    )
    return await _without_expired(db, list(result.scalars().all()))


async def list_pending_for_email(
    db: AsyncSession, email: str, *, org_team_id: UUID
) -> Sequence[Invitation]:
    """Pending invitations addressed to `email` into a team of `org_team_id`,
    the org of the request asking. An invitation into any other org is not this
    org's to show. The recipient's own list, which also shows the refused ones
    with their reason, is :func:`list_for_recipient`.
    """
    rows = await _pending_for_address(db, email.lower())
    # One downward walk of the org tree, not one recursive ancestor walk per
    # row: the question is the same for every invitation ("is that team inside
    # this org?"), so the answer is computed once.
    org_team_ids = {org_team_id, *await team_service.descendant_ids(db, org_team_id)}
    return [inv for inv in rows if inv.team_id in org_team_ids]


async def list_for_recipient(
    db: AsyncSession, user: User, *, org_team_id: UUID
) -> list[RecipientInvitation]:
    """The caller's own pending invitations, each named and judged, read from a
    request in `org_team_id`.

    An invitation into an organization the account does not belong to is
    listed with the refusal `accept_invitation` would give, instead of being
    hidden: hiding it left the owner of the address with an emailed link that
    led nowhere and no way to learn why. The inviting admin is still told
    nothing (see `create_invitation`) — this list is only ever the address
    owner's.

    The in-org question is answered once for the whole list (one walk per org
    the account belongs to); only an invitation into another organization
    costs a walk up its own tree, to name that organization.
    """
    rows = await _pending_for_address(db, user.email)
    if not rows:
        return []
    own_org = await db.get(Team, org_team_id)
    own_org_name = own_org.name if own_org is not None else ""
    # Each team of an org the account belongs to, keyed to that org: the
    # account may belong to several, and an invitation into one of them is
    # named by its own org, not by the org the request happens to be in.
    own_team_org: dict[UUID, UUID] = {}
    for membership in await org_membership_service.list_active_for_user(db, user.id):
        for team_id in (
            membership.org_team_id,
            *await team_service.descendant_ids(db, membership.org_team_id),
        ):
            own_team_org[team_id] = membership.org_team_id
    wanted = {inv.team_id for inv in rows} | set(own_team_org.values())
    teams = {
        t.id: t for t in (await db.execute(select(Team).where(Team.id.in_(wanted)))).scalars().all()
    }
    inviter_ids = {inv.invited_by_id for inv in rows if inv.invited_by_id is not None}
    inviters = (
        {
            u.id: u
            for u in (await db.execute(select(User).where(User.id.in_(inviter_ids))))
            .scalars()
            .all()
        }
        if inviter_ids
        else {}
    )

    out: list[RecipientInvitation] = []
    for inv in rows:
        team = teams.get(inv.team_id)
        if team is None:
            continue
        refusal: InvitationRefusal | None = None
        if inv.team_id in own_team_org:
            own = teams.get(own_team_org[inv.team_id])
            org_name = own.name if own is not None else own_org_name
        else:
            chain = await team_service.ancestor_chain(db, inv.team_id)
            if not chain:
                continue
            org_name = chain[-1].name
            if multi_org_enabled():
                refusal = await joining_refusal(
                    db,
                    user,
                    org=chain[-1],
                    membership=await org_membership_service.get(
                        db, user_id=user.id, org_team_id=chain[-1].id
                    ),
                )
            else:
                refusal = other_org_refusal(own_org_name=own_org_name, target_org_name=org_name)
        inviter = inviters.get(inv.invited_by_id) if inv.invited_by_id is not None else None
        out.append(
            RecipientInvitation(
                invitation=inv,
                team_name=team.name,
                org_name=org_name,
                inviter_display_name=inviter.display_name if inviter is not None else None,
                refusal=refusal,
            )
        )
    return out


async def create_invitation(
    db: AsyncSession,
    *,
    team: Team,
    email: str,
    role: TeamRole,
    invited_by: User,
    org_team_id: UUID,
    actor: Mapping[str, Any] | None = None,
) -> tuple[Invitation, bool, str]:
    """Create a pending invitation. Returns (invitation, auto_accepted, raw_token).
    Either outcome is announced on the event outbox under ``actor``.

    `raw_token` is the un-hashed token that goes into the emailed link; only its
    keyed HMAC hash is persisted on the row (so a DB read can't replay the link).
    For an auto-accepted invitation no email is sent, so the caller ignores it.

    - If a user with `email` already exists IN THIS ORG: synthesizes an
      already-accepted invitation, materializes the membership chain, and
      returns auto_accepted=True.
    - If the user is already a member of THIS team: raises InvitationError.
    - Otherwise: writes a pending invitation row.

    An address that already has an account in ANOTHER org takes that last path,
    exactly like an address with no account at all. The single-org rule still
    holds — `accept_invitation` refuses it, and says why to the one person
    entitled to know, the owner of the address. Refusing HERE instead would
    answer a question the caller has no standing to ask: any org admin could
    type an arbitrary address and read, from the refusal alone, whether it has
    an Alkera account in someone else's tenant. Every other credential surface
    in this codebase keeps a constant answer for exactly that reason, and the
    invitation surface must not be the hole in it.

    The partial unique index prevents two pending invitations for the same
    (email, team) pair; we check explicitly first for a clearer error.
    """
    normalized = email.lower().strip()

    existing_user = await user_service.get_by_email(db, normalized)
    if (
        existing_user is not None
        and await org_membership_service.active(
            db, user_id=existing_user.id, org_team_id=org_team_id
        )
        is not None
    ):
        # Already in this org: directly add membership chain.
        await _add_member_or_conflict(
            db, team_id=team.id, user_id=existing_user.id, role=role, actor=actor
        )
        # Synthesize an audit-trail invitation row marked accepted.
        raw_token = _new_token()
        invitation = Invitation(
            team_id=team.id,
            email=normalized,
            role=role,
            token=hash_lookup_token(raw_token),
            status=InvitationStatus.ACCEPTED,
            invited_by_id=invited_by.id,
            expires_at=_now() + DEFAULT_TTL,
            resolved_at=_now(),
        )
        db.add(invitation)
        await db.flush()
        await db.refresh(invitation)
        await _emit_changed(db, invitation, org_id=org_team_id, actor=actor)
        return invitation, True, raw_token

    # Reject if a pending invitation already exists for the pair.
    existing_pending = await db.execute(
        select(Invitation).where(
            Invitation.email == normalized,
            Invitation.team_id == team.id,
            Invitation.status == InvitationStatus.PENDING,
        )
    )
    if existing_pending.scalar_one_or_none() is not None:
        raise InvitationError(PENDING_EXISTS_MESSAGE)

    raw_token = _new_token()
    invitation = Invitation(
        team_id=team.id,
        email=normalized,
        role=role,
        token=hash_lookup_token(raw_token),
        status=InvitationStatus.PENDING,
        invited_by_id=invited_by.id,
        expires_at=_now() + DEFAULT_TTL,
    )
    db.add(invitation)
    await db.flush()
    await db.refresh(invitation)
    await _emit_changed(db, invitation, org_id=org_team_id, actor=actor)
    return invitation, False, raw_token


async def _inviter_still_authorized(
    db: AsyncSession, invitation: Invitation, chain: list[Team]
) -> bool:
    """True iff the account that issued `invitation` could still issue it today.

    An invitation is a standing grant of a caller-chosen role with a 7-day TTL,
    so it must not outlive the authority that minted it: deactivation, org
    removal, SCIM deprovisioning and hard deletion all leave the row PENDING
    and redeemable otherwise. Fails CLOSED — a NULL `invited_by_id` (the FK is
    `ON DELETE SET NULL`, so that is exactly what a deleted inviter leaves
    behind) is refused.
    """
    if invitation.invited_by_id is None:
        return False
    inviter = await user_service.get_by_id(db, invitation.invited_by_id)
    # The inviter must still stand in the invitation's org: an identity the
    # platform disabled, or a membership the org deactivated or removed, ends
    # the authority the invitation was minted under.
    if inviter is None or not chain:
        return False
    org_id = chain[-1].id
    if not await stands_in(db, inviter, org_id):
        return False
    # Permission descent: an ADMIN row on the team or any of its ancestors.
    admin_row = (
        await db.execute(
            select(TeamMembership.id)
            .where(
                TeamMembership.user_id == inviter.id,
                TeamMembership.org_team_id == org_id,
                TeamMembership.team_id.in_([t.id for t in chain]),
                TeamMembership.role == TeamRole.ADMIN,
            )
            .limit(1)
        )
    ).first()
    return admin_row is not None


async def accept_invitation(
    db: AsyncSession,
    invitation: Invitation,
    *,
    user: User,
    actor: Mapping[str, Any] | None = None,
) -> list[UUID]:
    """Accept by adding the user to the team chain. Returns the team_ids
    where membership was materialized (whole ancestor chain, the org root
    last). The new membership and the accepted invitation are both announced
    under ``actor``.

    Guards, all fail-closed: the invitation is pending and unexpired, it was
    issued to this exact address, the accepter may hold a seat in the
    invitation's org (:func:`joining_refusal`), and the inviter still holds
    the authority they used to mint it.

    An account already active in the invitation's org is seated on the team.
    One that is not joins the org first: while multi-org is off that is the
    single-org rule's refusal; with it on, a verified account gets an active
    membership in the org (a pending one is activated), its seat on the org's
    billing, and an entry in its own security log.
    """
    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationError(f"Invitation status is {invitation.status.value}")
    if invitation.expires_at < _now():
        invitation.status = InvitationStatus.EXPIRED
        invitation.resolved_at = _now()
        await db.flush()
        raise InvitationError("Invitation has expired")
    if user.email != invitation.email.lower():
        raise InvitationError("Invitation was issued to a different email address")

    chain = await team_service.ancestor_chain(db, invitation.team_id)
    if not chain:
        raise InvitationError("Invitation target no longer exists")
    bound = bound_org_ids(db)
    if bound is not None and chain[-1].id not in bound:
        # A signed-in request is held to its own org's rows, and this one
        # accepts into another: the invitation itself is the authority to read
        # the inviting org's seats and to write the new ones, so that part runs
        # outside the request's tenant binding, in the same transaction.
        async with cross_tenant_write(db, reason="invitation.accept_into_another_org"):
            return await _accept_into(db, invitation, chain, user=user, actor=actor)
    return await _accept_into(db, invitation, chain, user=user, actor=actor)


async def _accept_into(
    db: AsyncSession,
    invitation: Invitation,
    chain: list[Team],
    *,
    user: User,
    actor: Mapping[str, Any] | None,
) -> list[UUID]:
    org = chain[-1]
    # Enforced at ACCEPTANCE, not only at creation: an address invited before
    # it had an account can sign up into its own org instead of clicking
    # through, and the invitation would otherwise still materialize memberships
    # (and the entitlements keyed off them) inside an org the account never
    # joined. The refusal may be spelled out here, because the person reading
    # it owns the address. The creating admin is told nothing.
    membership = await org_membership_service.get(db, user_id=user.id, org_team_id=org.id)
    joining = membership is None or membership.status is not MembershipStatus.ACTIVE
    if joining:
        refusal = await joining_refusal(db, user, org=org, membership=membership)
        if refusal is not None:
            raise InvitationError(refusal.message, code=refusal.code)
    if not await _inviter_still_authorized(db, invitation, chain):
        raise InvitationError("The account that issued this invitation can no longer grant access")

    if joining:
        await _join_org(db, user=user, org_id=org.id, membership=membership, actor=actor)
    await _add_member_or_conflict(
        db, team_id=invitation.team_id, user_id=user.id, role=invitation.role, actor=actor
    )
    invitation.status = InvitationStatus.ACCEPTED
    invitation.resolved_at = _now()
    await db.flush()
    await _emit_changed(db, invitation, org_id=org.id, actor=actor)
    await _audit_accepted(db, invitation, user=user, org_id=org.id, actor=actor)
    if joining:
        await audit_services.record_security_event(
            db,
            user_id=user.id,
            event="auth.org_joined",
            org_team_id=org.id,
            detail={"via": "invitation"},
        )
    return [t.id for t in chain]


async def _join_org(
    db: AsyncSession,
    *,
    user: User,
    org_id: UUID,
    membership: OrgMembership | None,
    actor: Mapping[str, Any] | None,
) -> None:
    """Make ``user`` an active member of the org ``org_id`` and open their seat
    there. The membership row comes before any team seat (the team rows'
    foreign key names it). The seat goes through the one convergence every new
    seat takes, which grants the Free allowance only to an identity's first
    seat, so joining a second org never mints a second one."""
    if membership is None:
        await org_membership_service.create(db, user_id=user.id, org_team_id=org_id)
    else:
        # Only a pending membership reaches here: a deactivated one was refused.
        await org_membership_service.activate_pending(
            db, membership, actor=actor, seat_on_root=False
        )
    await org_entitlements().open_seat(db, user_id=user.id, org_id=org_id)


async def _audit_accepted(
    db: AsyncSession,
    invitation: Invitation,
    *,
    user: User,
    org_id: UUID,
    actor: Mapping[str, Any] | None,
) -> None:
    """Put the acceptance on the org's audit chain, in the accepting transaction.

    Recorded here rather than in a route because an invitation is accepted from
    three places -- the recipient's accept button, a password signup carrying
    the invite, and a provider signup carrying it -- and a new member joining
    the org is on the record whichever way they came in. ``actor`` is the same
    acting-chain document ``org_audit_service.record(acting=...)`` would stamp.
    """
    detail: dict[str, Any] = {"team_id": str(invitation.team_id), "role": invitation.role.value}
    if actor is not None:
        detail["actor"] = dict(actor)
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=user,
        action="invitation.accepted",
        target=invitation.email,
        detail=detail,
    )


async def reject_invitation(
    db: AsyncSession, invitation: Invitation, *, actor: Mapping[str, Any] | None = None
) -> None:
    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationError(f"Invitation status is {invitation.status.value}")
    invitation.status = InvitationStatus.REJECTED
    invitation.resolved_at = _now()
    await db.flush()
    await _emit_changed(
        db,
        invitation,
        org_id=await team_service.org_root_id(db, invitation.team_id),
        actor=actor,
    )


async def revoke_invitation(
    db: AsyncSession, invitation: Invitation, *, actor: Mapping[str, Any] | None = None
) -> None:
    """Admin-side cancel of a pending invitation."""
    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationError(f"Invitation status is {invitation.status.value}")
    invitation.status = InvitationStatus.REVOKED
    invitation.resolved_at = _now()
    await db.flush()
    await _emit_changed(
        db,
        invitation,
        org_id=await team_service.org_root_id(db, invitation.team_id),
        actor=actor,
    )


async def revoke_pending_by_inviter(
    db: AsyncSession,
    user_id: UUID,
    *,
    actor: Mapping[str, Any] | None = None,
    org_team_id: UUID | None = None,
) -> int:
    """Revoke every PENDING invitation minted by `user_id` (only those into
    ``org_team_id``'s teams when an org is named). Returns the count.

    Deprovisioning has to sweep these: an invitation is a 7-day standing grant
    of an attacker-chosen role, addressed to an inbox the inviter controls, and
    the row survives deactivation, removal, SCIM deprovisioning and even a hard
    delete (`invited_by_id` is `ON DELETE SET NULL`). Call it BEFORE deleting
    the user — afterwards the link is already gone.
    """
    rows = (
        (
            await db.execute(
                select(Invitation).where(
                    Invitation.invited_by_id == user_id,
                    Invitation.status == InvitationStatus.PENDING,
                )
            )
        )
        .scalars()
        .all()
    )
    if org_team_id is not None:
        org_teams = {org_team_id, *await team_service.descendant_ids(db, org_team_id)}
        rows = [invitation for invitation in rows if invitation.team_id in org_teams]
    now = _now()
    for invitation in rows:
        invitation.status = InvitationStatus.REVOKED
        invitation.resolved_at = now
    if rows:
        await db.flush()
    org_by_team: dict[UUID, UUID] = {}
    for invitation in rows:
        if invitation.team_id not in org_by_team:
            org_by_team[invitation.team_id] = await team_service.org_root_id(db, invitation.team_id)
        await _emit_changed(db, invitation, org_id=org_by_team[invitation.team_id], actor=actor)
    return len(rows)


#: The names the ``backend.services.org`` package exports for these.
invitation_by_token = get_by_token
invitation_link_closed_reason = closed_link_reason
masked_invited_address = masked_address
