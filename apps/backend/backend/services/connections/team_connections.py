"""Save and persist team connections under explicit credential custody.

``validate_and_build`` validates the admin's complete answer. Secrets remain
write-only and encrypted at rest. Materialized memberships reduce member
visibility to one ``team_id IN (my memberships)`` query.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from alkera_core.auth.secret_box import decrypt_secret, encrypt_secret
from alkera_core.connections import (
    Badge,
    CredentialState,
    Outcome,
    StatusInputs,
    VerificationState,
    derive_badge,
)
from alkera_core.connections.models import ConnectionVerification, TeamConnection, UserOAuthToken
from alkera_core.connections.schemas import (
    MAX_SECRET_CHARS as MAX_SECRET_CHARS,
)
from alkera_core.connections.schemas import (
    TeamConnectionUpsertRequest,
)
from alkera_core.connectors.catalog import (
    ConnectorDescriptor,
    admin_answered_secret_fields,
    auth_mode_for,
    build_result,
    credential_custody,
    distribution_refusal,
    doc_member_fields,
    doc_shared_values,
    get_descriptor,
    is_auto_add_eligible,
    length_error,
    missing_required,
    overlong_values,
    personal_capable_methods,
    required_error,
    sanitized_shared_values,
    team_capable_methods,
    team_form_fields,
    values_doc,
)
from alkera_core.connectors.connection import (
    ConnectionBuildResult,
    Credential,
    credential_secret_value,
)
from alkera_core.connectors.connection_form import (
    AuthMethodSchema,
    ConnectionFormSchema,
    FormField,
    ValueSlot,
)
from alkera_core.db.locking import LockRank, lock_or_insert
from alkera_core.events import (
    VISIBILITY_ORG,
    Entity,
    EventType,
    actor_system,
    emit,
    org_root_for_team,
    user_visibility,
)
from alkera_core.models import Team, TeamMembership, User
from alkera_core.naming import is_safe_handle
from sqlalchemy import and_, or_, select
from sqlalchemy import delete as sql_delete
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.connections.ports import connection_oauth
from backend.services.org import teams as team_service

#: The actor a connection change is recorded under when the caller names none.
SYSTEM_ACTOR = "backend:team_connection_service"

#: Keys that must NEVER appear in a stored ``shared_values`` dict — the
#: anti-leak invariant (secrets ride the encrypted columns, never JSON).
FORBIDDEN_ATTRIBUTE_KEYS = frozenset(
    {
        "password",
        "pwd",
        "passwd",
        "secret",
        "token",
        "authtoken",
        "private_key",
        "passphrase",
        "shared_secret",
        "client_secret",
        "oauth_client_secret",
        "access_token",
        "refresh_token",
        "api_key",
    }
)


def forbidden_attribute_keys(values: Mapping[str, object]) -> list[str]:
    """The subset of ``values`` keys that look like secret material."""
    return sorted(k for k in values if k.lower() in FORBIDDEN_ATTRIBUTE_KEYS)


# ---------------------------------------------------------------------------
# The save rule — one build over the admin's complete answer
# ---------------------------------------------------------------------------


#: The most one connection may hand every member. These values are re-served on
#: every member's sync pass and written to their disk, so an unbounded one is an
#: admin-shaped way to make every workstation re-download megabytes. Generous
#: against real inputs: the longest thing here is a SQLAlchemy URL or an account
#: URL, both far under a kilobyte.
MAX_SHARED_VALUE_CHARS = 4096


class MoveRefusedError(Exception):
    """A move the row itself refuses, whoever asked for it.

    ``code`` is what the dialog keys off; ``message`` is what it prints.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SaveRefusedError(Exception):
    """A refusal of the save (or its probe-before-live), carrying the sentence
    the admin reads. The route serves it as a 422."""


class ProbeDial(NamedTuple):
    """What the worker dials: the built attributes and resolved credentials."""

    attributes: dict[str, object]
    secret: str | None
    named_secrets: Mapping[str, str] | None = None


class BuiltUpsert(NamedTuple):
    values_doc: list[ValueSlot]
    """The one document this save splits into: every form input as a slot
    carrying who answers it and the admin's raw value where they distribute
    one. ``upsert`` flattens it to the stored columns."""
    auth_mode: str
    """Read off the switches: who ends up holding the credential."""
    dial: ProbeDial
    """The complete build and resolved credentials for the ephemeral probe."""
    secret: str | None
    """The shared secret to STORE — ``None`` means keep/absent."""
    clear_primary: bool
    """Whether the primary role stopped being admin-owned and must be removed."""
    named_secrets: dict[str, str] | None
    """Exact named-secret replacement, or ``None`` for a legacy unmanaged build."""
    shared_secret_fields: list[str]
    """Which secret inputs this row hands to the whole team. Empty unless a
    shared credential is being stored."""


class CredentialUpdate(NamedTuple):
    """Primary and named secret changes for one atomic credential generation."""

    primary: str | None
    named: Mapping[str, str] | None
    clear_primary: bool = False


class _FieldSplit(NamedTuple):
    """The admin form resolved once against the catalog fields and method."""

    descriptor: ConnectorDescriptor
    specs: list[FormField]
    by_name: dict[str, FormField]
    shared_names: list[str]
    member_fields: list[str]
    method: AuthMethodSchema | None


class _CredentialContext(NamedTuple):
    """The validated build and resolved role ownership."""

    built: ConnectionBuildResult
    custody: dict[str, str]
    shared_roles: set[str]
    probe_secret: str | None


def org_client_rule(provider: str) -> bool | None:
    """Whether this provider obliges the org to register its own OAuth client, or
    ``None`` when no adapter is wired for it.

    Read off the adapter, which declares it: a client's type is fixed when it is
    registered, not discoverable by watching a resolve fail. The connector-forms
    route reads the same declaration, so the form and the save's refusal cannot
    disagree about who needs an app. With no browser sign-in registered
    (:func:`~backend.services.connections.ports.connection_oauth`), none is wired."""
    return connection_oauth().org_client_rule(provider)


def _needs_org_oauth_client(provider: str) -> bool:
    """The save's reading. A provider with no adapter is a miswired connector, and
    the admin gets that sentence rather than a save that could never authorize."""
    rule = org_client_rule(provider)
    if rule is None:
        raise SaveRefusedError(
            f"No browser sign-in is wired for {provider!r}, so this connector cannot be "
            "preconfigured with one."
        )
    return rule


class _ResolvedMethod(NamedTuple):
    """The payload's connector and auth method resolved against the catalog:
    the descriptor, the method's team form, and the method's schema entry."""

    descriptor: ConnectorDescriptor
    schema: ConnectionFormSchema
    specs: list[FormField]
    by_name: dict[str, FormField]
    method: AuthMethodSchema | None


def _resolved_method(
    payload: TeamConnectionUpsertRequest, *, personal: bool = False
) -> _ResolvedMethod:
    """Refuse an unknown connector, or a method this form cannot carry.

    Which methods a form can carry depends on who the row belongs to: a team row
    is filled in by an admin for other people, a personal row by the only person
    who will ever use it."""
    try:
        descriptor = get_descriptor(payload.plugin)
    except KeyError:
        raise SaveRefusedError(f"Connector {payload.plugin!r} cannot be preconfigured.") from None
    schema = descriptor.form_schema()
    allowed = personal_capable_methods(schema) if personal else team_capable_methods(schema)
    if payload.auth_method not in allowed:
        if personal:
            raise SaveRefusedError(
                f"Auth method {payload.auth_method!r} is not one {payload.plugin!r} offers."
            )
        raise SaveRefusedError(
            f"Auth method {payload.auth_method!r} cannot preconfigure {payload.plugin!r}."
        )
    specs = team_form_fields(schema, payload.auth_method)
    method = next((m for m in schema.auth_methods if m.name == payload.auth_method), None)
    return _ResolvedMethod(descriptor, schema, specs, {f.name: f for f in specs}, method)


def _resolve_field_split(
    payload: TeamConnectionUpsertRequest, *, personal: bool = False
) -> _FieldSplit:
    """Resolve the admin's switches against the catalog's team form, refusing an
    unknown field name — and an auto-add that would leave a member something to
    answer. Connector and method resolution is ``_resolved_method``'s.

    A personal row has no members, so there are no switches to resolve: every
    field on the form is the owner's, and one they left blank is blank rather
    than a slot somebody else is expected to answer."""
    resolved = _resolved_method(payload, personal=personal)
    if personal:
        return _FieldSplit(
            resolved.descriptor,
            resolved.specs,
            resolved.by_name,
            [spec.name for spec in resolved.specs],
            [],
            resolved.method,
        )
    shared_names = [name.strip() for name in payload.shared_fields if name.strip()]
    unknown = sorted(set(shared_names) - set(resolved.by_name))
    if unknown:
        raise SaveRefusedError(
            f"{payload.plugin!r} has no form field named {', '.join(repr(n) for n in unknown)}."
        )
    shared = set(shared_names)
    member_fields = [f.name for f in resolved.specs if f.name not in shared]
    if payload.auto_add and not is_auto_add_eligible(
        resolved.schema, payload.auth_method, member_fields
    ):
        raise SaveRefusedError(
            "Auto-add needs a connection with nothing left for a member to answer. "
            "Prefill every field, or let members add it themselves."
        )

    return _FieldSplit(
        resolved.descriptor,
        resolved.specs,
        resolved.by_name,
        shared_names,
        member_fields,
        resolved.method,
    )


def _require_org_oauth_client(
    payload: TeamConnectionUpsertRequest,
    method: AuthMethodSchema | None,
    stored_oauth_secret: bool,
) -> None:
    """A per-user OAuth method on a CONFIDENTIAL provider (BigQuery) needs the
    org's client configured — its id + secret ride the request's dedicated
    (encrypted) slots, NOT the form fields, and a member never pastes them. The
    connector form carries no client fields; validated here so a misconfigured
    connection is caught at save, not at the member's authorize."""
    if method is None or method.oauth is None or not _needs_org_oauth_client(method.oauth.provider):
        return
    if not (payload.oauth_client_id or "").strip():
        raise SaveRefusedError(
            "Browser sign-in for this connector needs the org's OAuth client ID."
        )
    if not (payload.oauth_client_secret or "").strip() and not stored_oauth_secret:
        raise SaveRefusedError(
            "Browser sign-in for this connector needs the org's OAuth client secret."
        )


def _build_complete(
    split: _FieldSplit,
    payload: TeamConnectionUpsertRequest,
    fields: dict[str, str],
    answered_fields: set[str],
) -> ConnectionBuildResult:
    """Build the admin's complete form, including fields held back from members."""
    missing = missing_required(split.specs, fields | dict.fromkeys(answered_fields, "stored"))
    if missing:
        raise SaveRefusedError(
            f"{required_error(missing)}. Fill the connection in completely so Alkera can "
            f"verify it, then switch off whatever each member should answer themselves."
        )
    overlong = overlong_values(split.specs, fields)
    if overlong:
        raise SaveRefusedError(f"{length_error(overlong)}.")

    try:
        result = build_result(split.descriptor, payload.handle, payload.auth_method, fields)
    except ValueError as exc:
        detail = str(exc).strip()
        sentence = detail[:1].upper() + detail[1:]
        raise SaveRefusedError(f"{sentence.rstrip('.')}.") from None

    # A saved row is opened by other machines (members' laptops, cloud boxes that serve
    # many orgs), so a connection that would reach the machine opening it instead of a
    # server is refused here, before anyone can be handed it.
    refusal = distribution_refusal(split.descriptor, result.connection.attributes)
    if refusal is not None:
        sentence = refusal[:1].upper() + refusal[1:]
        raise SaveRefusedError(f"{sentence.rstrip('.')}.")
    return result


def _shared_values_for(split: _FieldSplit, clean: dict[str, str]) -> dict[str, str]:
    """What descends is what the admin typed, minus anything they embedded in a
    non-secret value. A secret never rides here — it has an encrypted column."""
    return {
        name: clean.get(name, "").strip()
        for name in split.shared_names
        if name in split.by_name and not split.by_name[name].secret
    }


def _refuse_member_fields_on_oauth(split: _FieldSplit) -> None:
    """Refuse held-back fields that a browser sign-in cannot collect."""
    if split.method is None or split.method.oauth is None or not split.member_fields:
        return
    held = [split.by_name[name].label for name in split.member_fields if name in split.by_name]
    raise SaveRefusedError(
        f"{', '.join(held)} cannot be left to members on a browser sign-in, because a "
        "member only supplies their identity in the browser. Prefill everything, or "
        "choose an auth method that collects a credential from each member."
    )


def _decide_stored_secret(
    shared: bool, requested: str | None, stored: str | None
) -> tuple[str | None, bool, str | None]:
    """Resolve the primary storage mutation and probe value together.

    The switches own custody because a generic SQL URL can carry the credential
    while its password field stays empty. ``None`` means keep or absent.
    """
    if not shared:
        return None, stored is not None, requested
    if requested is None:
        return None, False, stored
    if requested == stored:
        return None, False, stored
    # Refused here rather than by the database. The credential is encrypted
    # before it is stored and Fernet output runs about a third longer, so a
    # value past this point came back as a truncated insert and a 500 instead
    # of the named refusal every other bad input on this route gets.
    if len(requested) > MAX_SECRET_CHARS:
        raise SaveRefusedError(
            f"That credential is too long to store. Keep it under {MAX_SECRET_CHARS} characters."
        )
    return requested, False, requested


def _decide_named_secrets(
    shared_roles: set[str],
    requested: Mapping[str, Credential | None] | None,
    stored: Mapping[str, str],
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """Resolve named storage replacement and probe values role by role."""
    if requested is None:
        return None, dict(stored)
    decided: dict[str, str] = {}
    dial: dict[str, str] = {}
    for name, credential in requested.items():
        if not is_safe_handle(name):
            raise SaveRefusedError(f"Unsafe credential name {name!r}.")
        if credential is None:
            if name in shared_roles and name in stored:
                decided[name] = stored[name]
                dial[name] = stored[name]
            continue
        secret = credential_secret_value(credential)
        if secret is None:
            raise SaveRefusedError(f"The {name} credential cannot be stored.")
        if len(secret) > MAX_SECRET_CHARS:
            raise SaveRefusedError(
                f"The {name} credential is too long to store. "
                f"Keep it under {MAX_SECRET_CHARS} characters."
            )
        dial[name] = secret
        if name in shared_roles:
            decided[name] = secret
    replacement = decided if decided != dict(stored) else None
    return replacement, dial


def _consented_fields(
    split: _FieldSplit, fields: dict[str, str], clean: dict[str, str]
) -> list[str]:
    """For the consent record: which inputs the credential actually came out of.
    A field the sanitizer rewrote was carrying secret material of its own."""
    return [
        name
        for name in split.shared_names
        if (split.by_name[name].secret and (fields.get(name) or ""))
        or clean.get(name) != fields.get(name)
    ]


def _guard_served_values(split: _FieldSplit, shared_values: dict[str, str]) -> None:
    """Keep re-served member values bounded and free of secret material."""
    leftover = forbidden_attribute_keys(shared_values)
    if leftover:
        raise SaveRefusedError(f"Shared values must not contain secret material: {leftover}.")
    oversized = [
        split.by_name[name].label
        for name, value in shared_values.items()
        if len(value) > MAX_SHARED_VALUE_CHARS
    ]
    if oversized:
        raise SaveRefusedError(
            f"{', '.join(oversized)} is too long to hand to every member. "
            f"Keep each value under {MAX_SHARED_VALUE_CHARS} characters."
        )


def _decide_stored_credentials(
    split: _FieldSplit,
    payload: TeamConnectionUpsertRequest,
    *,
    stored_secret: str | None,
    stored_named_secrets: Mapping[str, str] | None,
    stored_oauth_secret: bool,
) -> _CredentialContext:
    """Build the complete answer and resolve each credential role's owner."""
    stored_named = stored_named_secrets or {}
    stored_roles = set(stored_named)
    if stored_secret:
        stored_roles.add("primary")
    custody = credential_custody(
        split.descriptor, payload.auth_method, split.shared_names, stored_roles
    )
    answered_fields = admin_answered_secret_fields(split.specs, stored_roles, custody)
    _require_org_oauth_client(payload, split.method, stored_oauth_secret)
    built = _build_complete(split, payload, dict(payload.fields), answered_fields)
    probe_secret = (
        credential_secret_value(built.credential) if built.credential is not None else None
    )

    credential_roles = stored_roles | set(built.named_credentials or {})
    if probe_secret:
        credential_roles.add("primary")
    custody.update(dict.fromkeys(credential_roles - custody.keys(), "admin"))
    shared_roles = {role for role, owner in custody.items() if owner == "admin"}
    missing_source = next(
        (
            spec
            for spec in split.specs
            if not spec.secret and spec.credential_role in shared_roles - credential_roles
        ),
        None,
    )
    if missing_source is not None:
        raise SaveRefusedError(f"{missing_source.label} must contain a credential.")
    return _CredentialContext(built, custody, shared_roles, probe_secret)


def validate_and_build(
    payload: TeamConnectionUpsertRequest,
    *,
    stored_secret: str | None,
    stored_named_secrets: Mapping[str, str] | None = None,
    stored_oauth_secret: bool = False,
    personal: bool = False,
) -> BuiltUpsert:
    """Validate one complete build, then assemble its stored and dialed halves.

    ``personal`` says the row belongs to one person rather than to a team, which
    is the whole of the difference: nobody else holds a slot in it."""
    split = _resolve_field_split(payload, personal=personal)
    fields = dict(payload.fields)
    context = _decide_stored_credentials(
        split,
        payload,
        stored_secret=stored_secret,
        stored_named_secrets=stored_named_secrets,
        stored_oauth_secret=stored_oauth_secret,
    )

    clean = sanitized_shared_values(split.descriptor, fields)
    shared_values = _shared_values_for(split, clean)
    _refuse_member_fields_on_oauth(split)
    secret, clear_primary, dial_secret = _decide_stored_secret(
        "primary" in context.shared_roles, context.probe_secret, stored_secret
    )
    named_secrets, dial_named_secrets = _decide_named_secrets(
        context.shared_roles, context.built.named_credentials, stored_named_secrets or {}
    )
    consent_fields = _consented_fields(split, fields, clean)

    probe_attributes = {
        k: v
        for k, v in dict(context.built.connection.attributes).items()
        if k.lower() not in FORBIDDEN_ATTRIBUTE_KEYS
    }
    _guard_served_values(split, shared_values)

    # The one split site: the whole form becomes the values document, each slot
    # owned by whoever answers it. Everything stored or served flattens from it.
    doc = values_doc(split.specs, values=shared_values, member_names=set(split.member_fields))
    return BuiltUpsert(
        values_doc=doc,
        auth_mode=auth_mode_for(
            split.descriptor.form_schema(), payload.auth_method, context.custody
        ),
        dial=ProbeDial(probe_attributes, dial_secret, dial_named_secrets),
        secret=secret,
        clear_primary=clear_primary,
        named_secrets=named_secrets,
        shared_secret_fields=consent_fields,
    )


async def get_by_id(db: AsyncSession, connection_id: UUID) -> TeamConnection | None:
    row = await db.execute(select(TeamConnection).where(TeamConnection.id == connection_id))
    return row.scalar_one_or_none()


async def get_by_identity(
    db: AsyncSession, *, team_id: UUID, plugin: str, handle: str, owner_user_id: UUID | None = None
) -> TeamConnection | None:
    """The row at one identity. ``owner_user_id`` selects between the team's row
    and that person's own — they are different rows under the same names, which
    is exactly what the identity index admits."""
    row = await db.execute(
        select(TeamConnection).where(
            TeamConnection.team_id == team_id,
            TeamConnection.plugin == plugin,
            TeamConnection.handle == handle,
            TeamConnection.owner_user_id.is_(None)
            if owner_user_id is None
            else TeamConnection.owner_user_id == owner_user_id,
        )
    )
    return row.scalar_one_or_none()


async def list_for_team(db: AsyncSession, team_id: UUID) -> list[TeamConnection]:
    """The team's own connections. A member's personal row hangs off the org
    root team so the org's tenancy checks reach it, and it must not turn up in
    the admin list of what the team has configured."""
    rows = await db.execute(
        select(TeamConnection)
        .where(TeamConnection.team_id == team_id, TeamConnection.owner_user_id.is_(None))
        .order_by(TeamConnection.plugin, TeamConnection.handle)
    )
    return list(rows.scalars().all())


async def team_delete_refusal(db: AsyncSession, team_id: UUID) -> str | None:
    """Why ``team_id`` cannot be deleted while it holds a connection, or
    ``None``. A connection's stored credential sits on an ``ON DELETE CASCADE``
    key, so deleting the team would destroy encrypted secrets with no trace."""
    held = (
        await db.execute(
            select(TeamConnection.id).where(TeamConnection.team_id == team_id).limit(1)
        )
    ).first()
    if held is None:
        return None
    return "Cannot delete a team that still has connections; delete them first"


async def list_visible_to_member(
    db: AsyncSession,
    user_id: UUID,
    *,
    org_team_id: UUID,
    also_team_ids: Collection[UUID] = (),
) -> list[tuple[TeamConnection, str]]:
    """Every connection the member is entitled to in ``org_team_id``, with its
    team name: the team rows of every team they belong to there, plus their OWN
    personal rows there. Membership materialization makes the team half one
    indexed IN-query.

    One org, always the request's. A person in several orgs holds memberships
    and personal rows in each, and what one org configured must never reach a
    session, or a workspace, of another: the daemon's sync-down would write it
    to disk there.

    ``also_team_ids`` adds teams the caller does not belong to but ADMINISTERS —
    the Connections page and the daemon's sync-down both pass the administered
    set, so an admin who configures a sub-team's connection finds it on the page
    afterwards AND their workspace materializes it: what they configured is
    theirs to use, not only to manage.

    Somebody else's personal row is never here, however far up the tree the
    caller's admin rights reach — it is that person's credential, not the org's.
    """
    member_teams = select(TeamMembership.team_id).where(
        TeamMembership.user_id == user_id, TeamMembership.org_team_id == org_team_id
    )
    administered = await _teams_in_org(db, also_team_ids, org_team_id=org_team_id)
    reachable = or_(
        TeamConnection.team_id.in_(member_teams),
        TeamConnection.team_id.in_(administered),
    )
    rows = await db.execute(
        select(TeamConnection, Team.name)
        .join(Team, Team.id == TeamConnection.team_id)
        .where(
            or_(
                and_(TeamConnection.owner_user_id.is_(None), reachable),
                and_(
                    TeamConnection.owner_user_id == user_id,
                    TeamConnection.team_id == org_team_id,
                ),
            )
        )
        .order_by(TeamConnection.plugin, TeamConnection.handle)
    )
    return [(conn, team_name) for conn, team_name in rows.all()]


async def _teams_in_org(
    db: AsyncSession, team_ids: Collection[UUID], *, org_team_id: UUID
) -> list[UUID]:
    """The ids among ``team_ids`` that sit in the org tree of ``org_team_id``.
    A caller's set is normally already one org's; this keeps a wider one from
    reaching across."""
    if not team_ids:
        return []
    in_org = {org_team_id, *await team_service.descendant_ids(db, org_team_id)}
    return [team_id for team_id in team_ids if team_id in in_org]


async def is_member_entitled(
    db: AsyncSession, *, user_id: UUID, connection_id: UUID, org_team_id: UUID
) -> bool:
    """Whether this caller may use the connection from a session in
    ``org_team_id``. A connection of another org is never theirs here, whatever
    they hold there.

    On a team row: whether they hold a membership row on its team (any ancestor
    membership materializes down, so that is the descent check for members) OR
    administer it by descent — the admin above a sub-team holds no membership
    row on it, and what they configured is theirs to use as well as to manage.
    On a PERSONAL row: whether they are its owner, and nothing else — a team
    admin's descent reaches the rows their team configured, never a member's
    own credential.
    """
    member_teams = select(TeamMembership.team_id).where(
        TeamMembership.user_id == user_id, TeamMembership.org_team_id == org_team_id
    )
    row = await db.execute(
        select(TeamConnection.id).where(
            TeamConnection.id == connection_id,
            or_(
                and_(
                    TeamConnection.owner_user_id.is_(None),
                    TeamConnection.team_id.in_(member_teams),
                ),
                and_(
                    TeamConnection.owner_user_id == user_id,
                    TeamConnection.team_id == org_team_id,
                ),
            ),
        )
    )
    if row.scalar_one_or_none() is not None:
        return True
    team_row = await db.execute(
        select(TeamConnection.team_id).where(
            TeamConnection.id == connection_id, TeamConnection.owner_user_id.is_(None)
        )
    )
    team_id = team_row.scalar_one_or_none()
    if team_id is None:
        return False
    return team_id in await team_service.admin_team_ids(db, user_id, org_team_id=org_team_id)


async def creator_names(db: AsyncSession, conns: Collection[TeamConnection]) -> dict[UUID, str]:
    """ "Added by" for a page of rows, in one query.

    The name is read now rather than stored with the row, so somebody who
    changes their name is not still printed under the old one next to every
    connection they ever added. Falls back to the email, which every account
    has; an account that has since been deleted leaves no name at all.
    """
    ids = {c.created_by_id for c in conns if c.created_by_id is not None}
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {u.id: (u.display_name or u.email) for u in rows}


async def member_grants(db: AsyncSession, user_id: UUID) -> dict[UUID, CredentialState]:
    """This member's own OAuth grants, so a per-user row reads with THEIR
    credential state rather than the admin's view of the shared half."""
    return {
        conn_id: CredentialState(state)
        for conn_id, state in (
            await db.execute(
                select(UserOAuthToken.team_connection_id, UserOAuthToken.state).where(
                    UserOAuthToken.user_id == user_id
                )
            )
        ).all()
    }


def _retire_shared_credentials(conn: TeamConnection) -> bool:
    """Remove every admin-owned role and report whether custody changed."""
    changed = bool(conn.shared_secret_encrypted or conn.named_secrets_encrypted)
    conn.shared_secret_encrypted = None
    conn.named_secrets_encrypted = {}
    conn.shared_consent = None
    return changed


def _update_shared_credentials(
    conn: TeamConnection,
    credentials: CredentialUpdate,
    shared_consent: dict[str, object] | None,
) -> bool:
    """Apply one primary-and-named role update to an existing shared bundle."""
    changed = any(
        (credentials.primary is not None, credentials.named is not None, credentials.clear_primary)
    )
    if credentials.clear_primary:
        conn.shared_secret_encrypted = None
    if credentials.primary is not None:
        conn.shared_secret_encrypted = encrypt_secret(credentials.primary)
    if credentials.named is not None:
        conn.named_secrets_encrypted = {
            name: encrypt_secret(secret) for name, secret in credentials.named.items()
        }
    if changed:
        conn.shared_consent = dict(shared_consent) if shared_consent else None
    return changed


def connection_badge(conn: TeamConnection, *, now: datetime | None = None) -> Badge:
    """The one derived status of a stored row, from the admin's vantage.

    Derived here and carried on the wire so no surface re-derives it; a client
    that computed its own would be a second opinion nobody reconciled.
    """
    return derive_badge(
        StatusInputs(
            enabled=conn.enabled,
            credential_state=CredentialState(conn.credential_state),
            verification_state=(
                VerificationState(conn.verification_state) if conn.verification_state else None
            ),
            last_outcome=Outcome(conn.last_outcome) if conn.last_outcome else None,
            last_verified_at=conn.last_verified_at,
        ),
        now=now or datetime.now(UTC),
    )


async def _emit_updated(
    db: AsyncSession,
    *,
    connection_id: UUID,
    team_id: UUID,
    owner_user_id: UUID | None,
    badge: Badge,
    credential_version: int | None,
    deleted: bool,
    actor: Mapping[str, Any] | None,
) -> None:
    """Announce a connection write on the event outbox, inside the caller's
    transaction. A team row is org-visible and carries its team, so the stream
    hands it only to that team's members (and the org's admins); a personal row
    is addressed to the one person who can see it, which is also the one daemon
    that must re-reconcile."""
    await emit(
        db,
        org_id=await org_root_for_team(db, team_id),
        type=EventType.TEAM_CONNECTION_UPDATED,
        entity=Entity.TEAM_CONNECTION,
        entity_id=str(connection_id),
        version=credential_version or 0,
        payload={
            "team_id": str(team_id),
            "owner_user_id": str(owner_user_id) if owner_user_id else None,
            "badge": badge.value,
            "deleted": deleted,
        },
        visibility=(user_visibility(owner_user_id) if owner_user_id else VISIBILITY_ORG),
        actor=actor if actor is not None else actor_system(SYSTEM_ACTOR),
    )


async def emit_connection_updated(
    db: AsyncSession,
    conn: TeamConnection,
    *,
    deleted: bool,
    actor: Mapping[str, Any] | None,
) -> None:
    """The same announcement for a caller that already holds the row."""
    await _emit_updated(
        db,
        connection_id=conn.id,
        team_id=conn.team_id,
        owner_user_id=conn.owner_user_id,
        badge=connection_badge(conn),
        credential_version=conn.credential_version,
        deleted=deleted,
        actor=actor,
    )


async def upsert(
    db: AsyncSession,
    *,
    team_id: UUID,
    plugin: str,
    handle: str,
    owner_user_id: UUID | None = None,
    values_doc: list[ValueSlot],
    auth_mode: str,
    auth_method: str,
    shared_consent: dict[str, object] | None,
    auto_add: bool,
    enabled: bool,
    credentials: CredentialUpdate,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    oauth_config: dict[str, object] | None,
    created_by_id: UUID | None,
    actor: Mapping[str, Any] | None = None,
) -> TeamConnection:
    """Persist one validated row by ``(team_id, plugin, handle, owner)`` identity.

    ``owner_user_id`` set makes the row that person's own; the team keeps its
    separate row under the same names.

    Secrets are write-only. ``None`` keeps the stored ciphertext.
    """
    locked, _created = await lock_or_insert(
        db,
        LockRank.ORG_SETTINGS,
        select(TeamConnection)
        .where(
            TeamConnection.team_id == team_id,
            TeamConnection.plugin == plugin,
            TeamConnection.handle == handle,
            TeamConnection.owner_user_id.is_(None)
            if owner_user_id is None
            else TeamConnection.owner_user_id == owner_user_id,
        )
        .execution_options(populate_existing=True),
        insert(TeamConnection).values(
            id=uuid4(),
            team_id=team_id,
            plugin=plugin,
            handle=handle,
            owner_user_id=owner_user_id,
            created_by_id=created_by_id,
        ),
    )
    conn = locked.scalar_one()
    conn.shared_values = doc_shared_values(values_doc)
    conn.auth_mode = auth_mode
    conn.auth_method = auth_method
    conn.member_fields = doc_member_fields(values_doc)
    conn.auto_add = auto_add
    conn.enabled = enabled
    credentials_changed = (
        _retire_shared_credentials(conn)
        if auth_mode != "shared"
        else _update_shared_credentials(conn, credentials, shared_consent)
    )
    if credentials_changed:
        conn.credential_version = (conn.credential_version or 0) + 1
    if oauth_client_id is not None:
        conn.oauth_client_id = oauth_client_id or None
    if oauth_client_secret:
        conn.oauth_client_secret_encrypted = encrypt_secret(oauth_client_secret)
    if oauth_config is not None:
        conn.oauth_config = dict(oauth_config) or None
    # The verdict this save was built on is stamped by the caller, which holds
    # the verification record. Nothing is dialled again on the way out: the
    # check that just passed IS the check, and re-dialling is what made a
    # successful add flicker through "Validating…" and back.
    await db.flush()
    await db.refresh(conn)
    return conn


async def rotate_shared_secret(
    db: AsyncSession,
    conn: TeamConnection,
    *,
    shared_secret: str,
    shared_consent: dict[str, object] | None = None,
    actor: Mapping[str, Any] | None = None,
) -> TeamConnection:
    """Replace the primary shared credential through a distinct audited verb.

    Named roles rotate through connection edit. This bumps
    ``credential_version`` so members lease the new bundle.
    The consent record follows the credential: whoever rotated is who the team
    is querying as now."""
    conn.shared_secret_encrypted = encrypt_secret(shared_secret)
    conn.credential_version = (conn.credential_version or 0) + 1
    if shared_consent is not None:
        conn.shared_consent = dict(shared_consent)
    await db.flush()
    await db.refresh(conn)
    return conn


async def delete(
    db: AsyncSession, conn: TeamConnection, *, actor: Mapping[str, Any] | None = None
) -> None:
    connection_id, team_id = conn.id, conn.team_id
    owner_user_id = conn.owner_user_id
    badge, credential_version = connection_badge(conn), conn.credential_version
    await db.delete(conn)
    await db.flush()
    await _emit_updated(
        db,
        connection_id=connection_id,
        team_id=team_id,
        owner_user_id=owner_user_id,
        badge=badge,
        credential_version=credential_version,
        deleted=True,
        actor=actor,
    )


#: The verification states a record is in while nobody has stamped a verdict on
#: it yet. A move carries exactly these along; a settled record keeps the team
#: it actually ran for, because that is the fact it records.
_IN_FLIGHT_VERIFICATIONS = (VerificationState.queued.value, VerificationState.running.value)


async def _retire_stranded_grants(
    db: AsyncSession, conn: TeamConnection, *, entitled: Collection[UUID]
) -> list[UUID]:
    """Drop the per-user credentials of everyone the move leaves behind.

    A per-user row's members each hold their own grant against it. After a move
    those people no longer see the connection, and a grant for a connection that
    is not on your screen is a credential nobody can revoke, refresh or reason
    about — so it goes with the connection. The people who follow the row to its
    new owner keep theirs, which is the whole reason this is a set difference
    rather than a clean sweep.
    """
    holders = set(
        (
            await db.execute(
                select(UserOAuthToken.user_id).where(UserOAuthToken.team_connection_id == conn.id)
            )
        )
        .scalars()
        .all()
    )
    stranded = sorted(holders - set(entitled), key=str)
    if stranded:
        await db.execute(
            sql_delete(UserOAuthToken).where(
                UserOAuthToken.team_connection_id == conn.id,
                UserOAuthToken.user_id.in_(stranded),
            )
        )
    return stranded


async def _entitled_after(
    db: AsyncSession, *, team_id: UUID, owner_user_id: UUID | None
) -> set[UUID]:
    """Who can see the row once it has moved. A personal row: exactly its owner.
    A team row: everyone holding a materialized membership on the destination."""
    if owner_user_id is not None:
        return {owner_user_id}
    org_id = await team_service.org_root_id(db, team_id)
    return set(
        (
            await db.execute(
                select(TeamMembership.user_id).where(
                    TeamMembership.org_team_id == org_id, TeamMembership.team_id == team_id
                )
            )
        )
        .scalars()
        .all()
    )


async def move_owner(
    db: AsyncSession,
    conn: TeamConnection,
    *,
    team_id: UUID,
    owner_user_id: UUID | None,
    actor: Mapping[str, Any] | None = None,
) -> list[UUID]:
    """Re-address one connection, keeping the row.

    The row keeps its id, its configuration, its stored credential and its
    version, so nothing has to be re-entered and no member's lease is broken by
    the move itself. What changes is who the row is addressed to, and everything
    that was derived from the old address goes with it:

    * a name already taken at the destination refuses the move rather than
      colliding on the identity index, which would surface as a 500;
    * per-user grants held by people the move leaves behind are deleted, so no
      member is left holding a credential for a connection they cannot see;
    * verifications still in flight are re-stamped to the destination, so the
      dialog polling one keeps resolving and a verdict that lands after the move
      cannot recreate the row's status under its old team;
    * the old audience is told the connection is gone and the new audience is
      told it is here, which is what makes both sides' workspaces reconcile.

    Returns the users whose grants were dropped.
    """
    if conn.team_id == team_id and conn.owner_user_id == owner_user_id:
        raise MoveRefusedError("already_there", "This connection is already for that owner.")
    clash = await get_by_identity(
        db, team_id=team_id, plugin=conn.plugin, handle=conn.handle, owner_user_id=owner_user_id
    )
    if clash is not None and clash.id != conn.id:
        raise MoveRefusedError(
            "handle_taken",
            f"A connection named {conn.handle} is already there. "
            "Rename this one, or remove the existing one first.",
        )
    old_team_id, old_owner_id = conn.team_id, conn.owner_user_id
    old_badge, old_version = connection_badge(conn), conn.credential_version
    stranded = await _retire_stranded_grants(
        db, conn, entitled=await _entitled_after(db, team_id=team_id, owner_user_id=owner_user_id)
    )
    await db.execute(
        sql_update(ConnectionVerification)
        .where(
            ConnectionVerification.connection_id == conn.id,
            ConnectionVerification.state.in_(_IN_FLIGHT_VERIFICATIONS),
        )
        .values(team_id=team_id)
    )
    conn.team_id = team_id
    conn.owner_user_id = owner_user_id
    await db.flush()
    await db.refresh(conn)
    # Two announcements, not one: the payload carries the address, so a single
    # event at the new address never reaches the workspaces that must drop it.
    await _emit_updated(
        db,
        connection_id=conn.id,
        team_id=old_team_id,
        owner_user_id=old_owner_id,
        badge=old_badge,
        credential_version=old_version,
        deleted=True,
        actor=actor,
    )
    await emit_connection_updated(db, conn, deleted=False, actor=actor)
    return stranded


def decrypt_shared_secret(conn: TeamConnection) -> str | None:
    """Decrypt the primary shared credential after authorization."""
    if not conn.shared_secret_encrypted:
        return None
    return decrypt_secret(conn.shared_secret_encrypted)


def decrypt_named_secrets(conn: TeamConnection) -> dict[str, str]:
    """Decrypt each independently stored named secret after authorization."""
    return {
        name: decrypt_secret(ciphertext)
        for name, ciphertext in dict(conn.named_secrets_encrypted or {}).items()
    }
