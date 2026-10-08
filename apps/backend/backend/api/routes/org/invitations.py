"""Invitation routes.

- /api/v1/teams/{team_id}/invitations    — admin creates / lists / revokes
- /api/v1/invitations/me                       — recipient's pending invitations
- /api/v1/invitations/{invitation_id}/accept   — recipient accepts (by id)
- /api/v1/invitations/{invitation_id}/reject   — recipient rejects (by id)
- /api/v1/invitations/by-token/{token}         — public preview (no auth)
- /api/v1/invitations/by-token/{token}/accept  — signed-in recipient accepts the link
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.auth.tenancy import multi_org_enabled
from alkera_core.authz.principal import ActingContext
from alkera_core.email import send_invitation_email
from alkera_core.models import Invitation, User
from alkera_core.observability.events import EventName, emit_event
from alkera_core.schemas.identity.auth import MessageResponse
from alkera_core.schemas.identity.invitation import (
    InvitationAcceptResponse,
    InvitationCreate,
    InvitationPublicRead,
    InvitationRead,
    RecipientInvitationRead,
)
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.params import PathId
from backend.api.rate_limit import limited
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    VerifiedUser,
    require_email_verified,
    require_team_admin,
)
from backend.services.abuse import bans as ban_service
from backend.services.audit import org_audit as org_audit_service
from backend.services.org import InvitationError
from backend.services.org import invitations as invitation_service
from backend.services.org import teams as team_service

team_invitations_router = APIRouter(
    prefix="/api/v1/teams/{team_id}/invitations",
    tags=["invitations"],
)
my_invitations_router = APIRouter(prefix="/api/v1/invitations", tags=["invitations"])


# ------- team-scoped (admin) -------


@team_invitations_router.get(
    "",
    response_model=list[InvitationRead],
    dependencies=[Depends(require_team_admin("team_id"))],
)
async def list_team_invitations(team_id: UUID, db: DbSession) -> list[InvitationRead]:
    rows = await invitation_service.list_pending_for_team(db, team_id)
    # The read files the invitations it found past their expiry as expired.
    await db.commit()
    return [InvitationRead.model_validate(r) for r in rows]


@team_invitations_router.post(
    "",
    response_model=InvitationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_team_admin("team_id")),
        Depends(require_email_verified),
        # This route puts an Alkera-branded message in an inbox the caller picks.
        # The admin guard says who may send; the mutation class says how fast,
        # so one account cannot spend the deployment's sending reputation in a
        # burst.
        Depends(limited("mutation")),
    ],
)
async def create_team_invitation(
    team_id: UUID,
    payload: InvitationCreate,
    caller: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> InvitationRead:
    team = await team_service.get_by_id(db, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    # A banned address is refused as one that fails email validation, so an
    # inviter cannot use this route to learn that an address is shut out.
    if await ban_service.email_is_banned(db, payload.email):
        raise ban_service.invalid_email_refusal(payload.email, loc=("body", "email"))
    try:
        invitation, auto_accepted, raw_token = await invitation_service.create_invitation(
            db,
            team=team,
            email=payload.email,
            role=payload.role,
            invited_by=caller,
            org_team_id=org_id,
            actor=ctx.audit_dict(),
        )
    except InvitationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="invitation.created",
        target=payload.email,
        detail={
            "team_id": str(team_id),
            "role": payload.role.value,
            # An address already in the org is seated at once, with no email:
            # the row must say so, since no acceptance row will follow it.
            "auto_accepted": auto_accepted,
        },
        acting=ctx,
    )

    # Side effect: send the email if the invitation is actually pending.
    # (Auto-accepted invitations skip the email — the user is already in the org.)
    if invitation.status.value == "pending":
        org_root = await team_service.get_by_id(db, org_id)
        await send_invitation_email(
            invitation,
            token=raw_token,
            team=team,
            org_name=org_root.name if org_root else "your organization",
            inviter_display_name=caller.display_name,
        )
        emit_event(
            EventName.invitation_sent,
            user_id=caller.id,
            org_id=org_id,
            role=str(payload.role),
        )
    else:
        # In-org existing user → auto-accepted, no email.
        emit_event(
            EventName.invitation_accepted,
            user_id=caller.id,
            org_id=org_id,
            auto=True,
        )

    return InvitationRead.model_validate(invitation)


@team_invitations_router.delete(
    "/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_team_admin("team_id")), Depends(require_email_verified)],
)
async def revoke_team_invitation(
    team_id: UUID,
    invitation_id: UUID,
    caller: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    org_id: CurrentOrg,
) -> None:
    invitation = await invitation_service.get_by_id(db, invitation_id)
    if invitation is None or invitation.team_id != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    try:
        await invitation_service.revoke_invitation(db, invitation, actor=ctx.audit_dict())
    except InvitationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="invitation.revoked",
        target=invitation.email,
        detail={"team_id": str(team_id), "role": invitation.role.value},
        acting=ctx,
    )


# ------- recipient-scoped + public preview -------


@my_invitations_router.get("/me", response_model=list[RecipientInvitationRead])
async def list_my_invitations(
    caller: VerifiedUser, db: DbSession, org_id: CurrentOrg
) -> list[RecipientInvitationRead]:
    """The caller's own pending invitations, named, with the refusal an accept
    would answer for any the account cannot take (another organization's)."""
    rows = await invitation_service.list_for_recipient(db, caller, org_team_id=org_id)
    # The read files the invitations it found past their expiry as expired.
    await db.commit()
    return [
        RecipientInvitationRead.model_validate(
            {
                **InvitationRead.model_validate(r.invitation).model_dump(),
                "team_name": r.team_name,
                "org_name": r.org_name,
                "inviter_display_name": r.inviter_display_name,
                "refusal": r.refusal,
            }
        )
        for r in rows
    ]


async def _accept(
    db: AsyncSession, invitation: Invitation, *, caller: User, ctx: ActingContext
) -> InvitationAcceptResponse:
    """Accept ``invitation`` as ``caller`` and answer with the org it joined.
    A refusal is a 409, carrying the refusal's code when it has one."""
    try:
        joined = await invitation_service.accept_invitation(
            db, invitation, user=caller, actor=ctx.audit_dict()
        )
    except InvitationError as exc:
        detail: str | dict[str, str] = (
            {"code": exc.code.value, "message": str(exc)} if exc.code is not None else str(exc)
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
    # The org joined is the invitation's: the root closes the chain the
    # acceptance materialized.
    org = await team_service.get_by_id(db, joined[-1])
    emit_event(EventName.invitation_accepted, user_id=caller.id, org_id=joined[-1])
    return InvitationAcceptResponse(
        invitation=InvitationRead.model_validate(invitation),
        joined_team_ids=joined,
        org_team_id=joined[-1],
        org_name=org.name if org is not None else None,
    )


@my_invitations_router.post("/{invitation_id}/accept", response_model=InvitationAcceptResponse)
async def accept_invitation(
    invitation_id: UUID, caller: VerifiedUser, db: DbSession, ctx: CurrentPrincipal
) -> InvitationAcceptResponse:
    # Accept by id (the authed recipient has it from `/invitations/me`); the raw
    # token isn't retrievable anymore (only its hash is stored). 404 — not 403 —
    # for a non-recipient so one user can't probe another's invitations.
    invitation = await invitation_service.get_by_id(db, invitation_id)
    if invitation is None or caller.email != invitation.email.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    return await _accept(db, invitation, caller=caller, ctx=ctx)


@my_invitations_router.post(
    "/by-token/{token}/accept",
    response_model=InvitationAcceptResponse,
    dependencies=[Depends(limited("mutation"))],
)
async def accept_invitation_by_token(
    token: PathId, caller: VerifiedUser, db: DbSession, ctx: CurrentPrincipal
) -> InvitationAcceptResponse:
    """Accept an emailed invitation link while signed in.

    Served only while multi-org is on (a 404 otherwise: a signed-in account
    could only ever accept into the org it is already in, which the
    recipient's list already offers by id). The link is bound to the address
    it was mailed to: a session for any other account is told which address,
    masked, and nothing is created, so a forwarded link is never redeemed by
    whoever happens to be signed in."""
    if not multi_org_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    invitation = await invitation_service.get_by_token(db, token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    closed = invitation_service.closed_link_reason(invitation)
    if closed is not None:
        # Same answer as the public preview of this link: its holder was
        # mailed it, so a used or withdrawn link says so.
        code, message = closed
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail={"code": code, "message": message}
        )
    if caller.email != invitation.email.lower():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "invitation_other_account",
                "message": (
                    "This invitation is for "
                    f"{invitation_service.masked_address(invitation.email)}. "
                    "Sign in with that email to accept."
                ),
            },
        )
    return await _accept(db, invitation, caller=caller, ctx=ctx)


@my_invitations_router.post("/{invitation_id}/reject", response_model=MessageResponse)
async def reject_invitation(
    invitation_id: UUID, caller: VerifiedUser, db: DbSession, ctx: CurrentPrincipal
) -> MessageResponse:
    invitation = await invitation_service.get_by_id(db, invitation_id)
    if invitation is None or caller.email != invitation.email.lower():
        # Pretend it doesn't exist for cross-account requests / unknown ids.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    try:
        await invitation_service.reject_invitation(db, invitation, actor=ctx.audit_dict())
    except InvitationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Recorded on the INVITING org's chain: it is that org's invitation that
    # was declined, whichever org (if any) the recipient belongs to.
    await org_audit_service.record(
        db,
        org_id=await team_service.org_root_id(db, invitation.team_id),
        actor=caller,
        action="invitation.rejected",
        target=invitation.email,
        detail={"team_id": str(invitation.team_id), "role": invitation.role.value},
        acting=ctx,
    )
    return MessageResponse(message="Rejected")


@my_invitations_router.get("/by-token/{token}", response_model=InvitationPublicRead)
async def preview_invitation_by_token(token: PathId, db: DbSession) -> InvitationPublicRead:
    """Public preview — used by the signup page when a user lands on
    /signup?invite=<token>. Returns the destination org/team and the invited
    address the token is bound to; nothing about any other invitation or user."""
    invitation = await invitation_service.get_by_token(db, token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    closed = invitation_service.closed_link_reason(invitation)
    if closed is not None:
        # The link's holder was mailed it, so telling them it was already used
        # (rather than "invalid or expired") reveals nothing they don't hold,
        # and it keeps someone who already joined from thinking they did not.
        code, message = closed
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail={"code": code, "message": message}
        )
    team = await team_service.get_by_id(db, invitation.team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invitation target missing"
        )
    chain = await team_service.ancestor_chain(db, team.id)
    org = chain[-1]  # root team
    inviter_display_name: str | None = None
    if invitation.invited_by_id is not None:
        from backend.services.identity import users as user_service

        inviter = await user_service.get_by_id(db, invitation.invited_by_id)
        if inviter is not None:
            inviter_display_name = inviter.display_name
    return InvitationPublicRead(
        email=invitation.email,
        team_id=team.id,
        team_name=team.name,
        org_team_id=org.id,
        org_name=org.name,
        role=invitation.role,
        inviter_display_name=inviter_display_name,
        expires_at=invitation.expires_at,
    )
