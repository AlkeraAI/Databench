"""Users routes, scoped to the org of the caller's credential.

A person's identity (email, password, MFA, their own name) is theirs: only they
change it, on the self path. An org admin acts on the person's MEMBERSHIP in
the org and on nothing else: the name the org shows for them
(``org_memberships.display_name``) and removing them from the org. An org can
never delete an identity, set its password or read another org's facts about
it, because the identity may belong to other orgs.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from alkera_core.email import send_email_verification
from alkera_core.logging import get_logger
from alkera_core.models import MembershipStatus, OrgMembership, User
from alkera_core.schemas.identity.user import UserCreate, UserRead
from alkera_core.utils import email_domain
from alkera_core.validation.display_name import OptionalDisplayNameStr
from alkera_core.validation.password import USER_PASSWORD_SCHEMA
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    OrgAdmin,
    _is_admin_of_team,
    require_email_verified,
)
from backend.auth.password_policy import enforce_password
from backend.auth.session_issue import (
    METHOD_PASSWORD_CHANGE,
    METHOD_PROFILE,
    client_hint,
    reissue_session,
)
from backend.services.audit import org_audit as org_audit_service
from backend.services.audit import record_security_event
from backend.services.identity import (
    StepUpRefusedError,
    UserConflictError,
    add_by_email,
    require_current_factors,
)
from backend.services.identity import email_verification as email_verification_service
from backend.services.identity import users as user_service
from backend.services.org import (
    InvitationError,
    LastActiveAdminError,
    members_of,
    membership_in,
    remove_from_org,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["users"])

#: Ceiling on one directory page. The underlying query has no LIMIT of its own,
#: so this is what keeps a single call from serializing a fifty-thousand-seat
#: tenant's entire roster into one response.
MAX_DIRECTORY_PAGE = 200


class OrgUserRead(BaseModel):
    """One colleague as the organization directory shows them.

    Deliberately narrower than ``UserRead``. A read of SOMEONE ELSE must not
    carry their security posture: ``mfa_enabled`` and ``has_password`` together
    name exactly which colleagues have no second factor and which sign in with a
    password — a ranked target list for a phishing or credential-stuffing run —
    while ``platform_role`` marks who is Alkera staff inside the tenant and
    ``email_verified_at`` who never proved their address. A caller's own posture
    is still theirs to read, on ``GET /api/v1/auth/me``.

    ``display_name`` is the name this org shows for the person: the one its
    admin, IdP or SCIM set on their membership, else their own name. No field
    names an org or carries anything another org knows about them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    display_name: str
    created_at: datetime


def org_user_read(user: User, membership: OrgMembership) -> OrgUserRead:
    """One person as the org whose membership this is shows them."""
    return OrgUserRead(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        display_name=membership.display_name or user.display_name,
        created_at=user.created_at,
    )


#: The note a deprecated create answers with when the request carried a
#: password: the account was created, and the password was not used.
PASSWORD_IGNORED = "password_ignored"  # noqa: S105 - a note's name, not a credential
#: The note when the address belongs to an identity in another org: an
#: invitation into this org is pending, and no account was created.
INVITATION_PENDING = "invitation_pending"


class AdminCreatedUserRead(UserRead):
    """What the deprecated ``POST /api/v1/users`` answers: the ``UserRead`` it
    always answered, plus what happened that the caller did not ask for.

    ``org_team_id`` is the caller's org. ``notes`` names each departure from
    what the request asked (``password_ignored``, ``invitation_pending``).
    ``invitation_id`` is set (and ``id`` is the invitation's) when the address
    belongs to an identity elsewhere and an invitation was sent instead."""

    notes: list[str] = Field(default_factory=list)
    invitation_id: UUID | None = None


class UserUpdate(BaseModel):
    """Self-service or admin-driven update. Cannot change platform_role here
    — that lives behind /admin/v1/users/{id}/platform_role.

    On your own account the names, email and password are your identity's.
    ``current_password`` / ``mfa_code`` are the step-up proof required to change
    either credential-grade field (``email``, ``password``); they are ignored for
    a name-only edit.

    An org admin editing a member sets the name the org shows for them
    (``display_name``, or ``first_name`` + ``last_name`` combined), on their
    membership in the org; the person's own name is never written.
    """

    email: EmailStr | None = None
    # OptionalDisplayNameStr: the inviter's display name is rendered in every
    # invitation email this account sends, so it carries the same link/markup
    # policy as the org and team names beside it.
    first_name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)
    last_name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(
        default=None, min_length=8, max_length=255, json_schema_extra=USER_PASSWORD_SCHEMA
    )
    current_password: str | None = Field(default=None, max_length=255)
    mfa_code: str | None = Field(default=None, max_length=32)
    display_name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


async def _person_here(db: AsyncSession, user_id: UUID, org_id: UUID) -> tuple[User, OrgMembership]:
    """The person and their membership in the request's org, active or
    deactivated; a 404 when they hold none there (an identity in another org
    is not "here", whatever else is true of it), or only a pending one (the
    org provisioned them and they have not joined)."""
    membership = await membership_in(db, user_id=user_id, org_team_id=org_id)
    if membership is not None and membership.status is MembershipStatus.PENDING:
        membership = None
    target = await user_service.get_by_id(db, user_id) if membership is not None else None
    if membership is None or target is None:
        raise _not_found()
    return target, membership


async def _caller_is_org_admin(db: AsyncSession, caller_id: UUID, org_id: UUID) -> bool:
    return await _is_admin_of_team(db, user_id=caller_id, team_id=org_id, org_team_id=org_id)


async def _require_step_up(db: AsyncSession, target: User, payload: UserUpdate) -> None:
    """Demand proof of a CURRENT factor before a credential-grade self-edit.

    A new password locks the owner out, and a repointed email redirects the
    self-service reset channel to an attacker, so the two fields that ARE the
    account are held to the standard ``/auth/mfa/disable`` already applies. A
    federated (OAuth/SSO/JIT) account has no current password to prove and is
    pointed at ``POST /auth/password/send-reset``: delivery to the address on
    file IS the proof of possession. See ``backend.services.identity.step_up``."""
    try:
        await require_current_factors(
            db,
            target,
            current_password=payload.current_password,
            mfa_code=payload.mfa_code,
            federated_message=(
                "Set a password first — we'll email you a link — before "
                "changing your email address or password here."
            ),
            missing_factor_message=(
                "Your current password is required to change your email or password."
            ),
        )
    except StepUpRefusedError as refused:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": refused.code, "message": refused.message},
        ) from refused


@router.get("", response_model=list[OrgUserRead])
async def list_users(
    db: DbSession,
    admin: OrgAdmin,
    org_id: CurrentOrg,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=MAX_DIRECTORY_PAGE, ge=1, le=MAX_DIRECTORY_PAGE),
) -> list[OrgUserRead]:
    """The organization's member directory, one bounded page at a time.

    Org-admin only, matching the roster reads beside it: listing a single team's
    members already requires a team admin because it exposes member emails, and
    this listing exposes every email in the organization.
    """
    rows = await members_of(db, org_id, offset=offset, limit=limit)
    return [org_user_read(user, membership) for user, membership in rows]


@router.post(
    "",
    response_model=AdminCreatedUserRead,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    summary="Add a person to this organization by email (deprecated: use invitations)",
    responses={202: {"model": AdminCreatedUserRead, "description": "An invitation was sent"}},
    dependencies=[Depends(require_email_verified)],
)
async def create_user(
    payload: UserCreate,
    response: Response,
    db: DbSession,
    admin: OrgAdmin,
    org_id: CurrentOrg,
    ctx: CurrentPrincipal,
) -> AdminCreatedUserRead:
    """Deprecated: kept for the clients that script it; invitations are the way
    to add people.

    It no longer lets an admin choose a person's password or reach an identity
    that belongs elsewhere. An address with no account gets one WITHOUT a
    password (201) and the email that lets the person set their own; a password
    in the request is not used and the answer says so (``password_ignored``).
    An address whose identity belongs to another org gets a pending invitation
    into this org instead (202, ``invitation_pending``). An address already in
    this org is a 409, as it always was."""
    if payload.org_team_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The team must belong to your organization",
        )
    notes = [PASSWORD_IGNORED] if payload.password is not None else []
    try:
        added = await add_by_email(
            db,
            admin=admin,
            org_team_id=org_id,
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            actor=ctx.audit_dict(),
        )
    except (UserConflictError, InvitationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.headers["Deprecation"] = "true"
    if added.user is not None:
        await org_audit_service.record(
            db,
            org_id=org_id,
            actor=admin,
            action="member.created",
            target=added.user.email,
            detail={"password_ignored": payload.password is not None},
            acting=ctx,
        )
        read = AdminCreatedUserRead.model_validate(added.user)
        read.org_team_id = org_id
        read.notes = notes
        return read
    assert added.invitation is not None
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=admin,
        action="invitation.created",
        target=added.invitation.email,
        detail={"team_id": str(org_id), "role": "member", "auto_accepted": False},
        acting=ctx,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return AdminCreatedUserRead(
        id=added.invitation.id,
        invitation_id=added.invitation.id,
        org_team_id=org_id,
        email=added.invitation.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
        display_name=f"{payload.first_name} {payload.last_name}".strip(),
        created_at=added.invitation.created_at,
        notes=[*notes, INVITATION_PENDING],
    )


@router.get("/{user_id}", response_model=OrgUserRead)
async def get_user(
    user_id: UUID, _caller: CurrentUser, org_id: CurrentOrg, db: DbSession
) -> OrgUserRead:
    """One colleague's directory entry — the same narrow shape as the listing.

    Looking a teammate up yields a name and an address, never their second-factor
    or password state. Your own account's posture is on ``/api/v1/auth/me``.
    """
    target, membership = await _person_here(db, user_id, org_id)
    return org_user_read(target, membership)


async def _set_member_name(
    db: AsyncSession,
    *,
    admin: User,
    target: User,
    membership: OrgMembership,
    payload: UserUpdate,
) -> OrgUserRead:
    """An org admin names a member for their org: the membership's
    ``display_name``, from ``display_name`` or the first and last names given.
    The person's own identity is not written."""
    if payload.password is not None or payload.email is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can't change another member's email or password. "
            "They must do that themselves",
        )
    if payload.display_name is not None:
        name = payload.display_name
    elif payload.first_name is not None or payload.last_name is not None:
        first = payload.first_name if payload.first_name is not None else target.first_name
        last = payload.last_name if payload.last_name is not None else target.last_name
        name = f"{first} {last}".strip()
    else:
        name = None
    if name is not None and name != membership.display_name:
        previous = membership.display_name
        membership.display_name = name
        await db.flush()
        await org_audit_service.record(
            db,
            org_id=membership.org_team_id,
            actor=admin,
            action="member.display_name_changed",
            target=target.email,
            detail={"from": previous, "to": name},
        )
    return org_user_read(target, membership)


@router.patch("/{user_id}", response_model=UserRead | OrgUserRead)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    caller: CurrentUser,
    org_id: CurrentOrg,
    db: DbSession,
    request: Request,
    response: Response,
) -> UserRead | OrgUserRead:
    """Edit a person. Your own account: your identity's names, email and
    password, answered with your full ``UserRead``. Another member, as an org
    admin: the name this org shows for them, answered with ``OrgUserRead``."""
    target, membership = await _person_here(db, user_id, org_id)

    is_self = caller.id == target.id
    is_admin = await _caller_is_org_admin(db, caller.id, org_id)
    if payload.display_name is not None and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an org admin sets the name the organization shows",
        )
    if not is_self and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot update another user without org admin",
        )
    if not is_self:
        # An admin mutating ANOTHER member is a durable, security-relevant action,
        # so require a PROVEN email (not just the grace window), mirroring the gate
        # on the sibling org-structure routes. Self-profile edits during grace
        # stay open.
        await require_email_verified(caller)
        # ...and an admin must NEVER change another member's credentials/identity:
        # setting their password is a direct account-takeover, and changing their
        # email is an indirect one (point it at a controlled address, then trigger
        # the self-service reset). A member manages their own identity (the self
        # path + the emailed reset); an admin names the member for the org only.
        return await _set_member_name(
            db, admin=caller, target=target, membership=membership, payload=payload
        )
    if payload.display_name is not None:
        await _set_member_name(
            db,
            admin=caller,
            target=target,
            membership=membership,
            payload=UserUpdate(display_name=payload.display_name),
        )
    previous_email = target.email
    email_changing = (
        payload.email is not None and user_service.normalize_email(payload.email) != previous_email
    )
    if is_self and (email_changing or payload.password is not None):
        await _require_step_up(db, target, payload)
    # Against the identity the credential will PROTECT after this edit: a caller
    # changing address and password in one request must not be able to set the
    # new password to the new address.
    enforce_password(
        payload.password,
        email=payload.email or target.email,
        names=(
            payload.first_name or target.first_name,
            payload.last_name or target.last_name,
        ),
    )
    try:
        updated = await user_service.update_profile(
            db,
            target,
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            password=payload.password,
        )
    except UserConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    email_changed = updated.email != previous_email
    # `is_self` is redundant today (the not-self branch above refuses both fields)
    # but load-bearing: the cookie minted below is for `updated`, so re-minting on
    # someone else's edit would hand the ACTOR a session as their TARGET.
    if is_self and (email_changed or payload.password is not None):
        # `update_profile` killed every live session/CLI token — that is the point.
        # Re-mint one for the tab the caller is acting from so the remediation
        # ("change my password") doesn't sign them out of the browser they did it
        # in, while every OTHER credential stays dead.
        # The re-mint is in the org this request is in, and only when that
        # org's sign-in policy still admits the session it replaces; when it
        # does not, this tab is signed out too and the change still stands.
        await reissue_session(
            db,
            updated,
            request=request,
            response=response,
            method=METHOD_PASSWORD_CHANGE if payload.password is not None else METHOD_PROFILE,
            org_team_id=org_id,
        )
        if email_changed:
            await record_security_event(
                db,
                user_id=updated.id,
                event="auth.email_changed",
                org_team_id=org_id,
                client=client_hint(request),
                detail={"previous_email_domain": email_domain(previous_email)},
            )
        if payload.password is not None:
            await record_security_event(
                db,
                user_id=updated.id,
                event="auth.password_changed",
                org_team_id=org_id,
                client=client_hint(request),
            )
    read = UserRead.model_validate(updated)
    read.org_team_id = org_id
    if is_self and email_changed:
        # The new address is unproven (``update_profile`` cleared the stamp and
        # the old address's token), so mail it a link now, through the same path
        # the resend button uses. A refused send does not undo the change: it
        # leaves no pending token, so the response's
        # ``verification_resend_available_at`` is null — the SPA reads that as
        # "nothing was sent" and offers the send instead of claiming it.
        await email_verification_service.issue_and_send(db, updated, send=send_email_verification)
    # The SPA seeds its /auth/me cache from this response, so it carries the
    # same resend state /auth/me does.
    read.verification_resend_available_at = email_verification_service.resend_available_at(updated)
    return read


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_email_verified)],
    summary="Remove a member from this organization",
)
async def delete_user(
    user_id: UUID, db: DbSession, caller: OrgAdmin, org_id: CurrentOrg, ctx: CurrentPrincipal
) -> None:
    """Remove the person from the caller's organization: their membership, and
    with it every team seat they hold here, their credentials here, their
    pending invitations into it and their live chats in it. Their identity and
    any other organization they belong to are untouched; an organization never
    deletes a person's account."""
    target, membership = await _person_here(db, user_id, org_id)
    if target.id == caller.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove yourself from the organization",
        )
    try:
        await remove_from_org(db, membership, actor=ctx.audit_dict())
        await record_security_event(
            db, user_id=target.id, event="auth.org_removed", org_team_id=org_id
        )
    except LastActiveAdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the last active admin",
        ) from exc
    await org_audit_service.record(
        db,
        org_id=org_id,
        actor=caller,
        action="member.removed",
        target=target.email,
        acting=ctx,
    )
