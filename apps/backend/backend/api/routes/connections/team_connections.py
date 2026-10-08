"""Team/org Preconfigured Connections — admin CRUD, member sync-down, the
audited credential fetch, and the connector-forms catalog.

The save builds and test-dials a COMPLETE connection through the shared
catalog (the same function the CLI runs), then persists only the admin's raw
form input; why raw wins is on ``alkera_core.connections.models.team_connection``.

Capability rules come from the same catalog: only ``team_capable`` auth methods
may be preconfigured, who holds the credential follows from which secret inputs
were switched on, and ``auto_add`` is offered only where no member has anything
left to answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.connections import (
    CredentialState,
    Outcome,
    VerificationState,
)
from alkera_core.connections.models import TeamConnection, UserOAuthToken
from alkera_core.connections.schemas import (
    ConnectionFormsResponse,
    ConnectionMoveRequest,
    ConnectorFormDescriptor,
    MemberCredentialCounts,
    MemberTeamConnectionsResponse,
    TeamConnectionCredentialLease,
    TeamConnectionCredentialResponse,
    TeamConnectionRead,
    TeamConnectionRotateSecretRequest,
    TeamConnectionUpsertRequest,
)
from alkera_core.connectors.catalog import (
    all_descriptors,
    ask_groups,
    team_capable_methods,
    team_form_fields,
    team_form_schema,
)
from alkera_core.models import User
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select

from backend.api.deps.connection_leases import member_bundle
from backend.api.deps.connection_requests import (
    own_connection_or_404,
    personal_payload,
    team_connection_or_404,
    team_or_404,
    unprocessable,
    validated_build,
)
from backend.auth.dependencies import (
    CurrentOrg,
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    current_principal,
    require_email_verified,
    require_team_admin,
)
from backend.authz import enforce
from backend.services.audit import org_audit as org_audit_service
from backend.services.connections import (
    NO_CREATOR,
    SaveCheckRequiredError,
    badge_of,
    can_manage,
    connection_checks,
    doc_and_groups,
    has_shared_credentials,
    lease_of,
    member_connections,
    reauth_for,
)
from backend.services.connections import team_connections as team_connection_service
from backend.services.org import teams as team_service

# The bounds this route's refusals name, read off the service that enforces
# them. The secret bound has one owner in the schema, which also enforces it
# on the rotate path; the service imports it from there.
MAX_SECRET_CHARS = team_connection_service.MAX_SECRET_CHARS
MAX_SHARED_VALUE_CHARS = team_connection_service.MAX_SHARED_VALUE_CHARS

router = APIRouter(prefix="/api/v1", tags=["team-connections"])


async def _member_counts(db: DbSession, conn: TeamConnection) -> MemberCredentialCounts | None:
    """How this per-user row's members stand with their own grants. None on a
    shared row, where every member queries as the one stored credential."""
    if conn.auth_mode != "per_user":
        return None
    rows = (
        await db.execute(
            select(UserOAuthToken.state, func.count())
            .where(UserOAuthToken.team_connection_id == conn.id)
            .group_by(UserOAuthToken.state)
        )
    ).all()
    by_state = {str(state): int(count) for state, count in rows}
    return MemberCredentialCounts(
        authorized=by_state.get(CredentialState.present.value, 0),
        needs_reauth=by_state.get(CredentialState.needs_reauth.value, 0),
    )


def _read(
    conn: TeamConnection,
    team_name: str = "",
    members: MemberCredentialCounts | None = None,
    *,
    created_by_name: str = "",
    can_manage: bool = True,
) -> TeamConnectionRead:
    """One row for the portal.

    ``can_manage`` is the caller's own answer, so the same shape serves the
    admin list and the member's Connections page. A caller who cannot manage the
    row reads no consent record and no per-member tally: both are the owning
    admin's account of the credential, not something every member is shown.
    """
    doc, groups = doc_and_groups(conn)
    credential_state = CredentialState(conn.credential_state)
    badge, reason = badge_of(conn, credential_state=credential_state, now=datetime.now(UTC))
    return TeamConnectionRead(
        id=conn.id,
        team_id=conn.team_id,
        team_name=team_name,
        owner_user_id=conn.owner_user_id,
        created_by_id=conn.created_by_id,
        created_by_name=created_by_name,
        can_manage=can_manage,
        plugin=conn.plugin,
        handle=conn.handle,
        shared_values=dict(conn.shared_values or {}),
        auth_mode=conn.auth_mode,  # type: ignore[arg-type]
        auth_method=conn.auth_method,
        member_fields=list(conn.member_fields or []),
        values_doc=doc,
        ask_groups=groups,
        shared_consent=conn.shared_consent if can_manage else None,
        auto_add=conn.auto_add,
        enabled=conn.enabled,
        has_shared_secret=has_shared_credentials(conn),
        has_primary_secret=bool(conn.shared_secret_encrypted),
        credential_version=conn.credential_version,
        oauth_client_id=conn.oauth_client_id,
        has_oauth_client_secret=bool(conn.oauth_client_secret_encrypted),
        oauth_config=conn.oauth_config,
        badge=badge,
        badge_reason=reason,
        outcome=Outcome(conn.last_outcome) if conn.last_outcome else None,
        last_detail=conn.last_detail,
        last_verified_at=conn.last_verified_at,
        verification_state=(
            VerificationState(conn.verification_state) if conn.verification_state else None
        ),
        credential_state=credential_state,
        reauth=reauth_for(credential_state, per_user=conn.auth_mode == "per_user"),
        members=members if can_manage else None,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
    )


async def _admit_save(
    db: DbSession, *, team_id: UUID, requester: UUID, payload: TeamConnectionUpsertRequest
) -> object | None:
    """The registered connection check's admission for this save (409 when it
    requires one this payload has not passed)."""
    try:
        return await connection_checks().admit_save(
            db, team_id=team_id, requester=requester, payload=payload
        )
    except SaveCheckRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "verification_required", "reason": exc.reason, "message": exc.message},
        ) from None


def _consent_record(admin: User, fields: list[str]) -> dict[str, object]:
    """Who approved handing this credential to the team, when, and over which
    inputs — recorded at the one moment all three are known."""
    return {
        "approved_by": str(admin.id),
        "approved_by_email": admin.email,
        "approved_at": datetime.now(UTC).isoformat(),
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# Team-admin CRUD (permission descent via require_team_admin)
# ---------------------------------------------------------------------------


@router.get(
    "/teams/{team_id}/connections",
    response_model=list[TeamConnectionRead],
    dependencies=[Depends(require_team_admin("team_id"))],
)
async def list_team_connections(team_id: UUID, db: DbSession) -> list[TeamConnectionRead]:
    team = await team_or_404(db, team_id)
    conns = await team_connection_service.list_for_team(db, team_id)
    names = await team_connection_service.creator_names(db, conns)
    return [
        _read(
            c,
            team.name,
            await _member_counts(db, c),
            created_by_name=names.get(c.created_by_id or NO_CREATOR, ""),
        )
        for c in conns
    ]


@router.put("/teams/{team_id}/connections", response_model=TeamConnectionRead)
async def upsert_team_connection(
    team_id: UUID,
    payload: TeamConnectionUpsertRequest,
    db: DbSession,
    admin: Annotated[User, Depends(require_team_admin("team_id"))],
    ctx: CurrentPrincipal,
) -> TeamConnectionRead:
    await require_email_verified(admin)
    team = await team_or_404(db, team_id)
    # The gate: a configuration joins the table only when a settled check
    # answered for THIS payload, run by THIS admin, recently. Without the payload
    # hash the dialog could verify one password and store another.
    admitted = await _admit_save(db, team_id=team_id, requester=admin.id, payload=payload)

    built = await validated_build(db, team_id, payload)
    # Handing one warehouse login to a whole team is a decision somebody made.
    # A save that leaves the stored secret alone leaves the record alone.
    shared_consent = (
        _consent_record(admin, built.shared_secret_fields)
        if built.secret is not None or built.named_secrets is not None or built.clear_primary
        else None
    )
    conn = await team_connection_service.upsert(
        db,
        team_id=team_id,
        plugin=payload.plugin,
        handle=payload.handle,
        values_doc=built.values_doc,
        auth_mode=built.auth_mode,
        auth_method=payload.auth_method,
        shared_consent=shared_consent,
        auto_add=payload.auto_add,
        enabled=payload.enabled,
        credentials=team_connection_service.CredentialUpdate(
            primary=built.secret,
            named=built.named_secrets,
            clear_primary=built.clear_primary,
        ),
        oauth_client_id=payload.oauth_client_id,
        oauth_client_secret=payload.oauth_client_secret,
        oauth_config=payload.oauth_config,
        created_by_id=admin.id,
        actor=ctx.audit_dict(),
    )
    await org_audit_service.record(
        db,
        org_id=ctx.org_id,
        actor=admin,
        action="team_connection.upserted",
        detail={
            "team_id": str(team_id),
            "plugin": conn.plugin,
            "handle": conn.handle,
            "auth_mode": conn.auth_mode,
            "auth_method": conn.auth_method,
            "auto_add": conn.auto_add,
            "enabled": conn.enabled,
            "credentials_rotated": (
                built.secret is not None or built.named_secrets is not None or built.clear_primary
            ),
        },
    )
    # A checked row is born checked: the check that just passed is the check.
    # Nothing is dialled on the way out.
    await connection_checks().saved(db, conn, admitted)
    # Under the admin's own session, not the service's: this is the one
    # announcement of the whole save, and an audit reader following a connection
    # backwards has nothing else that names who made it.
    await team_connection_service.emit_connection_updated(
        db, conn, deleted=False, actor=ctx.audit_dict()
    )
    # Rendered from a row read back, and before the commit: the last write
    # expired the server-stamped ``updated_at``, and a lazy reload of it while
    # the response is being built is IO outside the session's own context.
    await db.refresh(conn)
    body = _read(conn, team.name, await _member_counts(db, conn))
    await db.commit()
    return body


@router.post(
    "/teams/{team_id}/connections/{connection_id}/rotate-secret",
    response_model=TeamConnectionRead,
)
async def rotate_team_connection_secret(
    team_id: UUID,
    connection_id: UUID,
    payload: TeamConnectionRotateSecretRequest,
    db: DbSession,
    admin: Annotated[User, Depends(require_team_admin("team_id"))],
    ctx: CurrentPrincipal,
) -> TeamConnectionRead:
    await require_email_verified(admin)
    team = await team_or_404(db, team_id)
    conn = await team_connection_or_404(db, team_id, connection_id)
    if conn.auth_mode != "shared" or not conn.shared_secret_encrypted:
        raise unprocessable(
            "Only an existing admin-owned primary credential on a shared connection can be rotated."
        )
    consented = list((conn.shared_consent or {}).get("fields") or [])
    conn = await team_connection_service.rotate_shared_secret(
        db,
        conn,
        shared_secret=payload.shared_secret,
        shared_consent=_consent_record(admin, consented),
        actor=ctx.audit_dict(),
    )
    await org_audit_service.record(
        db,
        org_id=ctx.org_id,
        actor=admin,
        action="team_connection.secret_rotated",
        detail={
            "team_id": str(team_id),
            "plugin": conn.plugin,
            "handle": conn.handle,
            "credential_version": conn.credential_version,
        },
    )
    recheck = await connection_checks().recheck(db, conn, requested_by=admin.id, reason="rotate")
    await db.refresh(conn)
    body = _read(conn, team.name, await _member_counts(db, conn))
    # Start the check only AFTER the row is durable: a nudge inside the open
    # transaction races the worker against an uncommitted UPDATE.
    await db.commit()
    await db.refresh(conn)
    recheck()
    return body


@router.delete("/teams/{team_id}/connections/{connection_id}", status_code=204)
async def delete_team_connection(
    team_id: UUID,
    connection_id: UUID,
    db: DbSession,
    admin: Annotated[User, Depends(require_team_admin("team_id"))],
    ctx: CurrentPrincipal,
) -> None:
    await require_email_verified(admin)
    conn = await team_connection_or_404(db, team_id, connection_id)
    detail = {"team_id": str(team_id), "plugin": conn.plugin, "handle": conn.handle}
    await team_connection_service.delete(db, conn, actor=ctx.audit_dict())
    await org_audit_service.record(
        db,
        org_id=ctx.org_id,
        actor=admin,
        action="team_connection.deleted",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Member sync-down + the audited credential fetch
# ---------------------------------------------------------------------------


@router.get("/me/team-connections", response_model=MemberTeamConnectionsResponse)
async def my_team_connections(
    user: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> MemberTeamConnectionsResponse:
    """What this member's daemon reconciles into their workspace.

    Their own personal connections ride the same route as the team's: a daemon
    that syncs one syncs the other, and neither needs a second pass nor a second
    shape to know what to materialize.

    Usage follows the same visibility the Connections page has: the rows of
    every team this person belongs to AND of every team they administer by
    descent. The org admin who configures a sub-team's connection usually holds
    no membership row on that team (joining materializes rows upward, never
    down), and a listing that followed membership alone left them unable to use
    — or test — the connection they had just verified and saved.
    """
    return await member_connections(db, user, org_id=org_id)


# ---------------------------------------------------------------------------
# The Connections page: everything one person can see, and their own rows
# ---------------------------------------------------------------------------


@router.get("/me/connections", response_model=list[TeamConnectionRead])
async def my_connections(
    user: CurrentUser, db: DbSession, org_id: CurrentOrg
) -> list[TeamConnectionRead]:
    """Every connection this person can use, in one list.

    Their own personal rows, the rows configured by every team they belong to,
    AND the rows of every team they administer — an admin who adds a sub-team's
    connection from this page has to find it on this page afterwards, and the
    sub-team is one they usually have no membership row on. Each row is stamped
    with whether THEY may change it — the owner of a personal row, an admin of
    the owning team or of any team above it. A member who can only use a row
    still gets the whole definition, because that is what the row's dialog reads
    to explain what it connects to.
    """
    admin_teams = await team_service.admin_team_ids(db, user.id, org_team_id=org_id)
    pairs = await team_connection_service.list_visible_to_member(
        db, user.id, org_team_id=org_id, also_team_ids=admin_teams
    )
    grants = await team_connection_service.member_grants(db, user.id)
    names = await team_connection_service.creator_names(db, [conn for conn, _ in pairs])
    out: list[TeamConnectionRead] = []
    for conn, team_name in pairs:
        manage = can_manage(conn, user_id=user.id, admin_teams=admin_teams)
        read = _read(
            conn,
            team_name,
            await _member_counts(db, conn) if manage else None,
            created_by_name=names.get(conn.created_by_id or NO_CREATOR, ""),
            can_manage=manage,
        )
        # A per-user row reads with this member's own grant, exactly as it does
        # on the sync route — the admin's healthy row and the member who must
        # sign in again are the same row and two different badges.
        grant = grants.get(conn.id)
        if conn.auth_mode == "per_user" and grant is not None:
            badge, reason = badge_of(conn, credential_state=grant, now=datetime.now(UTC))
            read = read.model_copy(
                update={
                    "badge": badge,
                    "badge_reason": reason,
                    "credential_state": grant,
                    "reauth": reauth_for(grant, per_user=True),
                }
            )
        out.append(read)
    return out


@router.put("/me/connections", response_model=TeamConnectionRead)
async def upsert_personal_connection(
    payload: TeamConnectionUpsertRequest,
    db: DbSession,
    user: CurrentUser,
    ctx: CurrentPrincipal,
) -> TeamConnectionRead:
    """Save one of this person's own connections, in the request's org.

    The whole pipeline the team save runs: the connection check, the one build
    over a complete answer, encrypted credential storage — with the owner
    stamped on the row so nobody else in the org can read it back.
    """
    await require_email_verified(user)
    normalized = personal_payload(payload)
    admitted = await _admit_save(db, team_id=ctx.org_id, requester=user.id, payload=normalized)

    built = await validated_build(db, ctx.org_id, normalized, owner_user_id=user.id)
    conn = await team_connection_service.upsert(
        db,
        team_id=ctx.org_id,
        plugin=normalized.plugin,
        handle=normalized.handle,
        owner_user_id=user.id,
        values_doc=built.values_doc,
        auth_mode=built.auth_mode,
        auth_method=normalized.auth_method,
        # Nobody consented to anything: the credential is the owner's own and was
        # never put in front of anyone else.
        shared_consent=None,
        auto_add=normalized.auto_add,
        enabled=normalized.enabled,
        credentials=team_connection_service.CredentialUpdate(
            primary=built.secret,
            named=built.named_secrets,
            clear_primary=built.clear_primary,
        ),
        oauth_client_id=normalized.oauth_client_id,
        oauth_client_secret=normalized.oauth_client_secret,
        oauth_config=normalized.oauth_config,
        created_by_id=user.id,
        actor=ctx.audit_dict(),
    )
    await org_audit_service.record(
        db,
        org_id=ctx.org_id,
        actor=user,
        action="connection.upserted",
        detail={
            "plugin": conn.plugin,
            "handle": conn.handle,
            "owner_user_id": str(user.id),
            "auth_mode": conn.auth_mode,
            "auth_method": conn.auth_method,
        },
    )
    await connection_checks().saved(db, conn, admitted)
    await team_connection_service.emit_connection_updated(db, conn, deleted=False, actor=None)
    await db.refresh(conn)
    body = _read(
        conn,
        "",
        await _member_counts(db, conn),
        created_by_name=user.display_name or user.email,
        can_manage=True,
    )
    await db.commit()
    return body


@router.post(
    "/me/connections/{connection_id}/rotate-secret",
    response_model=TeamConnectionRead,
)
async def rotate_personal_connection_secret(
    connection_id: UUID,
    payload: TeamConnectionRotateSecretRequest,
    db: DbSession,
    user: CurrentUser,
    ctx: CurrentPrincipal,
) -> TeamConnectionRead:
    """Replace the stored credential on one of this person's own connections."""
    await require_email_verified(user)
    conn = await own_connection_or_404(db, user, connection_id, org_id=ctx.org_id)
    if conn.auth_mode != "shared" or not conn.shared_secret_encrypted:
        raise unprocessable("Only a stored primary credential can be rotated.")
    conn = await team_connection_service.rotate_shared_secret(
        db, conn, shared_secret=payload.shared_secret, actor=ctx.audit_dict()
    )
    recheck = await connection_checks().recheck(db, conn, requested_by=user.id, reason="rotate")
    await db.refresh(conn)
    body = _read(conn, "", None, created_by_name=user.display_name or user.email, can_manage=True)
    await db.commit()
    recheck()
    return body


@router.delete("/me/connections/{connection_id}", status_code=204)
async def delete_personal_connection(
    connection_id: UUID, db: DbSession, user: CurrentUser, ctx: CurrentPrincipal
) -> None:
    """Remove one of this person's own connections. A team row is not theirs to
    delete here, and is the same 404 as one that never existed."""
    await require_email_verified(user)
    conn = await own_connection_or_404(db, user, connection_id, org_id=ctx.org_id)
    detail = {"plugin": conn.plugin, "handle": conn.handle, "owner_user_id": str(user.id)}
    await team_connection_service.delete(db, conn, actor=ctx.audit_dict())
    await org_audit_service.record(
        db, org_id=ctx.org_id, actor=user, action="connection.deleted", detail=detail
    )


# ---------------------------------------------------------------------------
# Moving a connection between owners
# ---------------------------------------------------------------------------


@router.post("/connections/{connection_id}/owner", response_model=TeamConnectionRead)
async def move_connection_owner(
    request: Request,
    connection_id: UUID,
    payload: ConnectionMoveRequest,
    db: DbSession,
    user: CurrentUser,
    ctx: CurrentPrincipal,
) -> TeamConnectionRead:
    """Re-address one connection: to a team, or back to the person asking.

    One route for every direction, because the invariants are the same in all of
    them and a per-direction route would have to repeat each one. Both ends of
    the move are the policy's decision, made before anything is written; what
    the move does to the row and to what the old address derived from it is the
    service's, which is where a future mover — a team that merges, a member who
    leaves — will find it already done.
    """
    await require_email_verified(user)
    conn = await team_connection_service.get_by_id(db, connection_id)
    destination = await team_or_404(db, payload.team_id) if payload.team_id is not None else None
    admin_teams = await team_service.admin_team_ids(db, user.id, org_team_id=ctx.org_id)
    await enforce(
        request,
        db,
        ctx,
        Action.MOVE,
        Resource(
            ResourceType.CONNECTOR,
            id=str(connection_id),
            org_id=ctx.org_id,
            team_id=conn.team_id if conn else None,
        ),
        {
            "source_visible": conn is not None
            and await team_connection_service.is_member_entitled(
                db, user_id=user.id, connection_id=connection_id, org_team_id=ctx.org_id
            ),
            "source_managed": conn is not None
            and can_manage(conn, user_id=user.id, admin_teams=admin_teams),
            "dest_personal": destination is None,
            "dest_admin": destination is not None and destination.id in admin_teams,
            "dest_team_id": str(destination.id) if destination else "",
        },
    )
    if conn is None:
        # Unreachable past an allow (the policy admits only a visible row); kept
        # as the fail-closed floor rather than an assertion.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    was = {"team_id": str(conn.team_id), "owner_user_id": str(conn.owner_user_id or "")}
    try:
        stranded = await team_connection_service.move_owner(
            db,
            conn,
            team_id=destination.id if destination else ctx.org_id,
            owner_user_id=None if destination else user.id,
            actor=ctx.audit_dict(),
        )
    except team_connection_service.MoveRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message},
        ) from None
    await org_audit_service.record(
        db,
        org_id=ctx.org_id,
        actor=user,
        action="connection.owner_moved",
        acting=ctx,
        detail={
            "connection_id": str(conn.id),
            "plugin": conn.plugin,
            "handle": conn.handle,
            "from": was,
            "to": {
                "team_id": str(conn.team_id),
                "owner_user_id": str(conn.owner_user_id or ""),
            },
            # Whose own sign-in the move took away with it — names the people an
            # admin has to tell, which is the one thing the row itself forgets.
            "grants_retired": [str(uid) for uid in stranded],
        },
    )
    team = await team_or_404(db, conn.team_id)
    body = _read(
        conn,
        team.name,
        await _member_counts(db, conn),
        created_by_name="",
        can_manage=True,
    )
    await db.commit()
    return body


@router.get(
    "/me/team-connections/{connection_id}/credential",
    response_model=TeamConnectionCredentialResponse,
)
async def fetch_team_connection_credential(
    request: Request, connection_id: UUID, user: CurrentUser, db: DbSession
) -> TeamConnectionCredentialResponse:
    """Hand the shared credential bundle to an entitled member over TLS.

    Legacy daemons write each value to its own chmod-600 file. Current daemons
    use the role-addressed lease route for the complete bundle.
    """
    conn, secret, named_secrets = await member_bundle(
        request,
        db,
        await current_principal(request, db),
        connection_id,
        user=user,
        action="team_connection.credential_fetched",
    )
    return TeamConnectionCredentialResponse(
        secret=secret,
        named_secrets=named_secrets,
        credential_version=conn.credential_version,
    )


@router.post(
    "/me/team-connections/{connection_id}/credential-lease",
    response_model=TeamConnectionCredentialLease,
)
async def lease_team_connection_credential(
    request: Request, connection_id: UUID, user: CurrentUser, db: DbSession
) -> TeamConnectionCredentialLease:
    """Lease the complete role-addressed shared credential bundle.

    The member's daemon keeps it in memory and comes back when it expires, so a
    connection that is disabled, flipped to per-user, or deleted stops working
    within one lease instead of living on as a file on every laptop that ever
    synced. That bound is the point of the route; rotating a role is still what
    revokes access this second."""
    conn, secret, named_secrets = await member_bundle(
        request,
        db,
        await current_principal(request, db),
        connection_id,
        user=user,
        action="team_connection.credential_leased",
    )
    return lease_of(conn, secret, named_secrets)


# ---------------------------------------------------------------------------
# The connector-forms catalog (portal renders the same generic form)
# ---------------------------------------------------------------------------


def _stamped_method(method: dict[str, object]) -> dict[str, object] | None:
    """Stamp a browser sign-in with whether the org must register its own OAuth
    client, so the admin form knows to collect the id and secret. Read from the
    same declaration the save reads, so the form and the refusal cannot disagree
    about who needs an app.

    A method whose provider has no adapter returns ``None`` and is dropped
    instead of raising: one miswired connector must not empty the whole picker
    and leave an admin unable to configure anything. Offering it would only lead
    to a save the route refuses by name."""
    oauth = method.get("oauth")
    if not (isinstance(oauth, dict) and oauth):
        return method
    rule = team_connection_service.org_client_rule(str(oauth["provider"]))
    if rule is None:
        return None
    oauth["needs_org_client"] = rule
    return method


@router.get("/plugins/connection-forms", response_model=ConnectionFormsResponse)
async def connection_forms(user: CurrentUser) -> ConnectionFormsResponse:
    connectors = []
    for d in all_descriptors():
        schema = d.form_schema()
        # The TEAM view of the form: the connector's own inputs plus the
        # deployment tier, which is one more value to distribute or hold back.
        form = team_form_schema(schema).model_dump(mode="json")
        stamped = (_stamped_method(m) for m in form.get("auth_methods", []))
        brokerable = [m for m in stamped if m is not None]
        form["auth_methods"] = brokerable
        served = {m["name"] for m in brokerable}
        capable = [n for n in team_capable_methods(schema) if n == "" or n in served]
        connectors.append(
            ConnectorFormDescriptor(
                name=d.name,
                title=d.title,
                form=form,
                team_capable_methods=capable,
                # The compiled completeness rule per method, so the portal's
                # gate checks structure and never re-derives the answer rule.
                ask_groups={n: ask_groups(team_form_fields(schema, n)) for n in capable},
            )
        )
    return ConnectionFormsResponse(
        connectors=connectors, checked_before_save=connection_checks().checks_before_save
    )
