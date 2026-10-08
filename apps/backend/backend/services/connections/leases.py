"""What a connection row looks like to one member, and handing its credential out.

Three doors hand a connection's credential out: the person's own
(``/me/team-connections/{id}/credential-lease``), a chat's on the box that
holds it, and a workspace's on the box that holds it. The two box doors also
list the connections the chat's or workspace's owner may use. One owner for
both, so every door answers the same set and the same bundle for the same
person.

The decision stays with the caller. Every function that hands a credential out
takes ``decide``: the caller's own authorization call, bound to its request
(``enforce`` in the HTTP layer), which records the decision and raises the
refusal. The facts it is asked about are resolved here. A refusal past the
decision is one of this module's exceptions, which the HTTP layer maps to its
status codes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType
from alkera_core.authz.policies.connector import (
    CHAT_ATTR,
    CHAT_BOUND_MACHINE_ATTR,
    FOR_USER_ATTR,
    WORKSPACE_ATTR,
)
from alkera_core.config import get_settings
from alkera_core.connections import (
    Badge,
    CredentialState,
    Outcome,
    Reauth,
    StatusInputs,
    VerificationState,
    derive_badge,
)
from alkera_core.connections.models import TeamConnection
from alkera_core.connections.schemas import (
    MemberTeamConnection,
    MemberTeamConnectionsResponse,
    TeamConnectionCredentialLease,
)
from alkera_core.connectors.catalog import (
    admin_answered_secret_fields,
    ask_groups,
    credential_custody,
    get_descriptor,
    team_form_fields,
    values_doc,
)
from alkera_core.connectors.connection_form import FormField
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from alkera_core.naming import is_safe_handle
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import record_org_audit
from backend.services.connections import team_connections as team_connection_service
from backend.services.connections.ports import (
    GrantOnDeviceError,
    ReauthRequiredError,
    connection_oauth,
)
from backend.services.org import admin_team_ids

#: The caller's authorization call for one decision: records it, returns on an
#: allow, raises the refusal.
Decide = Callable[[Action, Resource, Mapping[str, object]], Awaitable[object]]

#: The shortest lease the backend will hand out, whatever the deployment configures.
#: A window under a minute costs a round trip per query without shortening real
#: exposure, since a revoked row stops working at the next lease either way.
MIN_LEASE_TTL_SECONDS = 60

#: Stands in for "this row has no creator on file" when looking a name up, so a
#: row whose author's account is gone reads as blank rather than matching some
#: other row's entry.
NO_CREATOR = UUID(int=0)

CREDENTIAL_UNREADABLE_REASON = "Alkera can't open this credential; rotate it."
"""What the admin reads when the secret box cannot open the stored ciphertext.
It is Alkera's problem, not a rejected login, and the fix is a rotation."""


class ConnectionNotFoundError(Exception):
    """Nothing to hand out: answered as not found, so existence never leaks."""


class OwnerReauthRequiredError(Exception):
    """The owner's own grant needs them to sign in again."""

    def __init__(self, handle: str) -> None:
        super().__init__(f"The workspace owner must sign in to {handle} again.")
        self.handle = handle


class OwnerCredentialOnDeviceError(Exception):
    """The owner's per-user credential lives on their own computer, not here."""

    def __init__(self) -> None:
        super().__init__(
            "The workspace owner's sign-in for this connection is kept on their own computer."
        )


class CredentialUnreadableError(Exception):
    """The stored ciphertext cannot be opened; the row is already marked so."""

    def __init__(self) -> None:
        super().__init__(CREDENTIAL_UNREADABLE_REASON)


# ---------------------------------------------------------------------------
# A row as one reader sees it
# ---------------------------------------------------------------------------


def stored_specs(conn: TeamConnection) -> list[FormField]:
    """The whole team form for a stored row, or nothing when the catalog no
    longer knows the plugin (a legacy row outliving a connector removal)."""
    try:
        schema = get_descriptor(conn.plugin).form_schema()
    except KeyError:
        return []
    return team_form_fields(schema, conn.auth_method)


def has_shared_credentials(conn: TeamConnection) -> bool:
    """Whether the row owns any primary or named shared credential."""
    return bool(conn.shared_secret_encrypted or conn.named_secrets_encrypted)


def _stored_secret_fields(
    conn: TeamConnection, specs: list[FormField], member: set[str]
) -> set[str]:
    """Return admin-owned fields backed by primary or named encrypted storage."""
    stored_roles = {
        name for name in (conn.named_secrets_encrypted or {}) if is_safe_handle(name)
    } | ({"primary"} if conn.shared_secret_encrypted else set())
    custody = credential_custody(
        get_descriptor(conn.plugin),
        conn.auth_method,
        [field.name for field in specs if field.name not in member],
        stored_roles,
    )
    return admin_answered_secret_fields(specs, stored_roles, custody)


def doc_and_groups(conn: TeamConnection) -> tuple[list[dict[str, object]], list[list[str]]]:
    """Synthesize the values document + compiled ask groups from the stored
    columns, so every row — however old — serves the one shape clients read.
    A stored shared secret answers its slot as ``deferred``; it never rides."""
    specs = stored_specs(conn)
    member = set(conn.member_fields or [])
    deferred = _stored_secret_fields(conn, specs, member) if specs else set()
    doc = values_doc(
        specs,
        values=dict(conn.shared_values or {}),
        member_names=member,
        deferred=deferred,
    )
    return [slot.model_dump(mode="json") for slot in doc], ask_groups(specs)


_NEEDS_ATTENTION = frozenset(
    {
        CredentialState.needs_reauth,
        CredentialState.unreadable,
        CredentialState.revoked,
        CredentialState.expired,
    }
)


def reauth_for(credential_state: CredentialState, *, per_user: bool) -> Reauth | None:
    """How a credential that needs attention gets fixed. A member's own OAuth
    grant is fixed in a browser; a shared credential only an admin can replace."""
    if credential_state not in _NEEDS_ATTENTION:
        return None
    return Reauth.browser if per_user else Reauth.admin


def badge_of(
    conn: TeamConnection, *, credential_state: CredentialState, now: datetime
) -> tuple[Badge, str]:
    """The derived badge and the sentence under it.

    The reason follows whichever axis decided the badge: a credential problem
    explains itself, and anything else is the last check's own detail. Showing
    the connector's stale sentence next to "Sign in again" is how the two used
    to contradict each other on the same row.
    """
    badge = derive_badge(
        StatusInputs(
            enabled=conn.enabled,
            credential_state=credential_state,
            verification_state=(
                VerificationState(conn.verification_state) if conn.verification_state else None
            ),
            last_outcome=Outcome(conn.last_outcome) if conn.last_outcome else None,
            last_verified_at=conn.last_verified_at,
        ),
        now=now,
    )
    credential_decided = badge is Badge.needs_reauth and credential_state in _NEEDS_ATTENTION
    return badge, (conn.credential_state_reason if credential_decided else conn.last_detail)


def can_manage(conn: TeamConnection, *, user_id: UUID, admin_teams: set[UUID]) -> bool:
    """Whether this caller may edit, rotate or remove the row.

    A personal row answers to its owner and to nobody else — not to the org
    admin above them, because it is that member's own credential. A team row
    answers to an admin of the owning team or of any team above it, which is
    what ``admin_teams`` already closes over.
    """
    if conn.owner_user_id is not None:
        return conn.owner_user_id == user_id
    return conn.team_id in admin_teams


#: Why a lease of a connection's credential is refused, by the state the
#: credential is in. The lease itself (``shared_bundle``, ``owner_grant_lease``)
#: is the authority; :func:`lease_refusal` answers the same in advance.
LEASE_REFUSALS: Final[dict[CredentialState, str]] = {
    CredentialState.needs_reauth: "Its sign-in needs to be renewed.",
    CredentialState.expired: "Its sign-in has expired.",
    CredentialState.revoked: "Its access was revoked.",
    CredentialState.unreadable: "Its saved credential can't be read.",
    CredentialState.absent: "No credential is saved for it.",
}
#: A per-user connection the person never signed in to on Alkera.
NOT_SIGNED_IN: Final = "Not signed in on Alkera yet."


def lease_refusal(conn: TeamConnection, grant_state: CredentialState | None) -> str:
    """Why leasing ``conn``'s credential for a person whose own grant on it is
    ``grant_state`` (``None``: they hold none) is refused; empty when a lease
    would be handed out.

    A shared row leases its stored bundle: none stored, or one Alkera cannot
    open, is refused. A per-user row leases the person's own server-held
    grant: none at all (never signed in on Alkera, or a credential kept on
    their own computer) is refused, and so is one that needs a new sign-in."""
    if conn.auth_mode != "per_user":
        if CredentialState(conn.credential_state) is CredentialState.unreadable:
            return LEASE_REFUSALS[CredentialState.unreadable]
        if not has_shared_credentials(conn):
            return LEASE_REFUSALS[CredentialState.absent]
        return ""
    if grant_state is None:
        return NOT_SIGNED_IN
    if grant_state in _NEEDS_ATTENTION:
        return LEASE_REFUSALS[grant_state]
    return ""


def member_view(
    conn: TeamConnection,
    team_name: str,
    grant_state: CredentialState | None = None,
    *,
    created_by_name: str = "",
    manageable: bool = False,
) -> MemberTeamConnection:
    """The row from ONE member's vantage.

    On a per-user row the credential axis is that member's own grant, so the
    admin whose connection is healthy and the member who must sign in again read
    different badges off the same row — which is the distinction the single
    shared status word could not make.
    """
    doc, groups = doc_and_groups(conn)
    credential_state = (
        grant_state
        if conn.auth_mode == "per_user" and grant_state is not None
        else CredentialState(conn.credential_state)
    )
    badge, reason = badge_of(conn, credential_state=credential_state, now=datetime.now(UTC))
    return MemberTeamConnection(
        id=conn.id,
        team_id=conn.team_id,
        team_name=team_name,
        owner_user_id=conn.owner_user_id,
        created_by_id=conn.created_by_id,
        created_by_name=created_by_name,
        can_manage=manageable,
        plugin=conn.plugin,
        handle=conn.handle,
        shared_values=dict(conn.shared_values or {}),
        auth_mode=conn.auth_mode,  # type: ignore[arg-type]
        auth_method=conn.auth_method,
        member_fields=list(conn.member_fields or []),
        values_doc=doc,
        ask_groups=groups,
        auto_add=conn.auto_add,
        enabled=conn.enabled,
        has_shared_secret=has_shared_credentials(conn),
        has_primary_secret=bool(conn.shared_secret_encrypted),
        credential_version=conn.credential_version,
        # Leases let revocation take effect without writing team secrets to disk.
        shared_custody="lease",
        oauth_client_id=conn.oauth_client_id,
        badge=badge,
        badge_reason=reason,
        outcome=Outcome(conn.last_outcome) if conn.last_outcome else None,
        last_verified_at=conn.last_verified_at,
        credential_state=credential_state,
        reauth=reauth_for(credential_state, per_user=conn.auth_mode == "per_user"),
        lease_refusal=lease_refusal(conn, grant_state),
        updated_at=conn.updated_at,
    )


async def member_connections(
    db: AsyncSession, user: User, *, org_id: UUID
) -> MemberTeamConnectionsResponse:
    """The set a daemon materializes for ``user`` in ``org_id`` and nowhere else:
    their own rows, their teams' and the teams they administer. One builder, so
    the person's door and the box's (a chat's or a workspace's, in its org)
    answer the same set for the same person."""
    admin_teams = await admin_team_ids(db, user.id, org_team_id=org_id)
    pairs = await team_connection_service.list_visible_to_member(
        db, user.id, org_team_id=org_id, also_team_ids=admin_teams
    )
    grants = await team_connection_service.member_grants(db, user.id)
    names = await team_connection_service.creator_names(db, [conn for conn, _ in pairs])
    return MemberTeamConnectionsResponse(
        connections=[
            member_view(
                conn,
                team_name,
                grants.get(conn.id),
                created_by_name=names.get(conn.created_by_id or NO_CREATOR, ""),
                manageable=can_manage(conn, user_id=user.id, admin_teams=admin_teams),
            )
            for conn, team_name in pairs
        ]
    )


# ---------------------------------------------------------------------------
# Handing a credential out
# ---------------------------------------------------------------------------


async def mark_credential_unreadable(conn: TeamConnection) -> None:
    """Record that Alkera cannot open this credential.

    Every read path that decrypts funnels here, because an unopenable ciphertext
    used to be an unhandled 500 on the member routes and a "credential rejected"
    on the worker — two different lies about the same Alkera-side problem.

    The flag is written in a session of its own because the request is about to
    be refused and its transaction rolls back with the refusal. Written in that
    transaction the fact would vanish, every later read would rediscover the
    same failure, and the badge would go on claiming the connection is healthy
    while nothing can open its credential.
    """
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(TeamConnection).where(TeamConnection.id == conn.id))
        ).scalar_one_or_none()
        if row is not None:
            row.credential_state = CredentialState.unreadable.value
            row.credential_state_reason = CREDENTIAL_UNREADABLE_REASON
            await db.commit()


async def decide_credential_fetch(
    db: AsyncSession,
    decide: Decide,
    connection_id: UUID,
    *,
    for_user: User,
    org_id: UUID,
    attrs: Mapping[str, object] | None = None,
) -> TeamConnection:
    """Decide handing ``for_user``'s credential for one connection out and
    return the live row.

    ``for_user`` is the person the credential is handed out for: the caller on
    the person's door, the owner of a held chat or workspace on the box's.
    Every gate is re-checked on every call: this is the point at which a
    disabled row, a flip to per-user, or a lost membership stops working. The
    facts are resolved here; the policy decides through ``decide``; ``attrs``
    are the door's own facts for the policy."""
    conn = await team_connection_service.get_by_id(db, connection_id)
    entitled = await team_connection_service.is_member_entitled(
        db, user_id=for_user.id, connection_id=connection_id, org_team_id=org_id
    )
    await decide(
        Action.FETCH_CREDENTIAL,
        Resource(
            ResourceType.CONNECTOR,
            id=str(connection_id),
            org_id=org_id,
            team_id=conn.team_id if conn else None,
        ),
        {
            "member_entitled": entitled,
            "enabled": bool(conn and conn.enabled),
            "auth_mode": conn.auth_mode if conn else "",
            # A bundle of only named roles is still a shared credential to hand out.
            "has_shared_secret": bool(conn and has_shared_credentials(conn)),
            "team_id": str(conn.team_id) if conn else "",
            # Empty on a team row; a personal row names its one owner, and the
            # policy refuses anybody else even if entitlement said otherwise.
            "owner_user_id": str(conn.owner_user_id) if conn and conn.owner_user_id else "",
            **(attrs or {}),
        },
    )
    if conn is None:
        # Unreachable past an allow (the policy admits only a live row); kept as
        # the fail-closed floor rather than an assertion.
        raise ConnectionNotFoundError
    return conn


async def shared_bundle(
    db: AsyncSession,
    ctx: ActingContext,
    conn: TeamConnection,
    *,
    for_user: User,
    org_id: UUID,
    action: str,
    detail: Mapping[str, object] | None = None,
) -> tuple[str, dict[str, str]]:
    """The decrypted shared bundle of an admitted row, on the org's audit chain
    in ``for_user``'s name; ``detail`` is the door's own words for the row."""
    try:
        secret = team_connection_service.decrypt_shared_secret(conn)
        named_secrets = team_connection_service.decrypt_named_secrets(conn)
    except InvalidToken:
        await mark_credential_unreadable(conn)
        raise CredentialUnreadableError from None
    if secret is None and not named_secrets:
        # A storage failure after authorization, not a decision.
        raise ConnectionNotFoundError
    await record_org_audit(
        db,
        org_id=org_id,
        actor=for_user,
        action=action,
        acting=ctx,
        detail={
            "connection_id": str(conn.id),
            "team_id": str(conn.team_id),
            "plugin": conn.plugin,
            "handle": conn.handle,
            "credential_version": conn.credential_version,
            # Which roles left the server this time — their names, never their values.
            "roles": [*(["primary"] if secret is not None else []), *sorted(named_secrets)],
            **(detail or {}),
        },
    )
    return secret or "", named_secrets


def _lease_bound() -> datetime:
    """The deployment's lease TTL from now, floored so a misconfigured zero can
    never hand out a lease that is already dead."""
    ttl = max(MIN_LEASE_TTL_SECONDS, get_settings().team_connection_lease_ttl_seconds)
    return datetime.now(UTC) + timedelta(seconds=ttl)


def lease_of(
    conn: TeamConnection, secret: str, named_secrets: dict[str, str]
) -> TeamConnectionCredentialLease:
    """One time-bounded copy of the bundle."""
    return TeamConnectionCredentialLease(
        secret=secret,
        named_secrets=named_secrets,
        credential_version=conn.credential_version,
        expires_at=_lease_bound(),
    )


async def owner_grant_lease(
    db: AsyncSession,
    ctx: ActingContext,
    conn: TeamConnection,
    *,
    owner: User,
    org_id: UUID,
    detail: Mapping[str, object],
) -> TeamConnectionCredentialLease:
    """The owner's own per-user grant as a lease: the live access token the
    server holds for them, as the primary role, until it expires. Raises
    :class:`OwnerReauthRequiredError` when the owner must sign in again,
    :class:`OwnerCredentialOnDeviceError` when their credential lives on their
    own computer, and ``ProviderUnreachableError`` when the
    provider could not be reached."""
    try:
        access, expires_at, _scope = await connection_oauth().lease(db, conn, user_id=owner.id)
    except ReauthRequiredError as exc:
        raise OwnerReauthRequiredError(conn.handle) from exc
    except GrantOnDeviceError as exc:
        raise OwnerCredentialOnDeviceError from exc
    await record_org_audit(
        db,
        org_id=org_id,
        actor=owner,
        action="team_connection.credential_leased",
        acting=ctx,
        detail={
            "connection_id": str(conn.id),
            "team_id": str(conn.team_id),
            "plugin": conn.plugin,
            "handle": conn.handle,
            "credential": "owner_grant",
            "roles": ["primary"],
            **detail,
        },
    )
    bound = _lease_bound()
    return TeamConnectionCredentialLease(
        secret=access,
        named_secrets={},
        credential_version=conn.credential_version,
        expires_at=min(bound, expires_at) if expires_at is not None else bound,
    )


async def lease_for_owner(
    db: AsyncSession,
    decide: Decide,
    ctx: ActingContext,
    *,
    org_id: UUID,
    bound_machine_id: str,
    owner: User,
    connection_id: UUID,
    chat_id: str = "",
    workspace_id: str = "",
) -> TeamConnectionCredentialLease:
    """The box's door, once the chat or workspace policy has admitted the box:
    lease one connection the OWNER may use in ``org_id``, decided by the
    connector policy on the owner's entitlement with the binding compared
    against the box a second time, and recorded on the org's audit chain in
    the owner's name with the machine's chain and the chat or workspace it was
    leased for.

    Sharing a workspace shares every connection its owner may use, the
    owner's personal rows and per-user rows included. A shared row leases its
    bundle; a per-user row leases the OWNER's own grant, the access token the
    server keeps for them (refreshed when needed). A per-user credential the
    owner keeps on their own computer has nothing here to lease."""
    scope = {"chat_id": chat_id} if chat_id else {"workspace_id": workspace_id}
    detail = {**scope, "leased_for": "chat" if chat_id else "workspace"}
    conn = await decide_credential_fetch(
        db,
        decide,
        connection_id,
        for_user=owner,
        org_id=org_id,
        attrs={
            FOR_USER_ATTR: str(owner.id),
            CHAT_BOUND_MACHINE_ATTR: bound_machine_id,
            CHAT_ATTR: chat_id,
            WORKSPACE_ATTR: workspace_id,
        },
    )
    if conn.auth_mode == "per_user":
        return await owner_grant_lease(db, ctx, conn, owner=owner, org_id=org_id, detail=detail)
    secret, named_secrets = await shared_bundle(
        db,
        ctx,
        conn,
        for_user=owner,
        org_id=org_id,
        action="team_connection.credential_leased",
        detail=detail,
    )
    return lease_of(conn, secret, named_secrets)
