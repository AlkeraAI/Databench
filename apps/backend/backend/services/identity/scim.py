"""SCIM 2.0 (RFC 7643/7644) user provisioning — the service layer.

A SCIM User here is one person's MEMBERSHIP in the connection's org, shown with
their identity's email and name. Every operation is scoped to that one org by
the caller (the SCIM bearer token resolves it) and acts on the membership:

* ``active=false`` and ``DELETE`` deactivate the membership
  (``org_memberships.deactivate``): the person's credentials in this org end,
  and their identity and every other org they belong to are untouched;
* names an IdP pushes become the name this org shows for the person
  (``org_memberships.display_name``); SCIM never writes an identity's names,
  email, password or MFA;
* ``externalId`` is the org's own id for the person
  (``org_memberships.scim_external_id``), unique within the org.

Creating a user whose email has no identity anywhere creates the identity (its
home membership comes with it). An email whose identity already exists in
another org is refused as a SCIM uniqueness conflict while multi-org is off.
With it on, the org gets a PENDING membership for that identity instead: it
grants nothing (no seat, no credential, no role) until the person joins, and
the IdP sees it as an active user (``active`` is the IdP's administrative
intent, which a pending membership honours). ``active=false`` on a pending
membership deletes it. Provisioning never touches the identity's names,
password, second factor or other memberships, and a pending membership shows
the org no name but the one its IdP pushed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC
from typing import Any
from uuid import UUID

from alkera_core.auth import sso_domains
from alkera_core.events import actor_system
from alkera_core.models import MembershipStatus, OrgMembership, SsoConnection, TeamRole, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.identity import users as user_service
from backend.services.identity.users import UserConflictError
from backend.services.org import (
    LastActiveAdminError,
    create_org_membership,
    deactivate_membership,
    membership_in,
    reactivate_membership,
    withdraw_pending_membership,
)
from backend.services.org import memberships as membership_service

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

#: The actor a provisioning change is recorded under: the identity provider,
#: which authenticates with a bearer token and has no user session behind it.
SYSTEM_ACTOR = "backend:scim_service"

# `userName eq "value"` / `externalId eq "value"` — the filter Okta/Entra use to
# dedup on sync. Quotes may be double; value is captured verbatim.
_FILTER_RE = re.compile(r'^\s*(userName|externalId)\s+eq\s+"(?P<value>[^"]*)"\s*$', re.IGNORECASE)


class ScimError(Exception):
    """A SCIM-shaped failure. The route layer renders it as the RFC error body."""

    def __init__(self, status_code: int, detail: str, *, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type


def error_body(status_code: int, detail: str, *, scim_type: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "detail": detail, "status": str(status_code)}
    if scim_type:
        body["scimType"] = scim_type
    return body


def to_scim_user(user: User, membership: OrgMembership, *, base_url: str) -> dict[str, Any]:
    """The SCIM User for ``user`` as the membership's org sees them. A pending
    membership reads as active (the IdP provisioned it and has not withdrawn
    it). The names are the ones the org's IdP pushed, and the identity's own
    only for a part it never pushed on a membership the person has joined: an
    address that already has an account elsewhere reads exactly like a new
    one, so provisioning tells an org nothing about other orgs."""
    created = membership.joined_at.astimezone(UTC).isoformat()
    given, family = _shown_names(user, membership)
    name = {"givenName": given, "familyName": family}
    display = membership.display_name or f"{given} {family}".strip()
    return {
        "schemas": [USER_SCHEMA],
        "id": str(user.id),
        "externalId": membership.scim_external_id,
        "userName": user.email,
        "name": name,
        "displayName": display,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": membership.status is not MembershipStatus.DEACTIVATED,
        "meta": {
            "resourceType": "User",
            "created": created,
            "lastModified": created,  # no separate updated_at is tracked
            "location": f"{base_url}/Users/{user.id}",
        },
    }


def _shown_names(user: User, membership: OrgMembership) -> tuple[str, str]:
    pending = membership.status is MembershipStatus.PENDING
    given = membership.scim_given_name
    family = membership.scim_family_name
    if given is None:
        given = "" if pending else user.first_name
    if family is None:
        family = "" if pending else user.last_name
    return given, family


def push_names(
    user: User, membership: OrgMembership, *, given: str | None, family: str | None
) -> None:
    """Record the name parts an IdP pushed (``None``: not pushed) and the
    org's display name composed from them."""
    if given is not None:
        membership.scim_given_name = given
    if family is not None:
        membership.scim_family_name = family
    membership.display_name = display_name_from(user, membership, given=given, family=family)


def parse_filter(filter_str: str | None) -> tuple[str, str] | None:
    """``(attr, value)`` for a supported ``eq`` filter, or None for no filter.
    Raises ScimError(400, invalidFilter) for a present-but-unsupported filter."""
    if not filter_str:
        return None
    m = _FILTER_RE.match(filter_str)
    if m is None:
        raise ScimError(400, f"unsupported filter: {filter_str}", scim_type="invalidFilter")
    return m.group(1).lower(), m.group("value")


async def external_id_taken(
    db: AsyncSession, *, org_id: UUID, external_id: str, exclude_user_id: UUID | None = None
) -> bool:
    """Whether another membership in the org already holds ``external_id`` (SCIM
    dedup — externalId is unique per org; the filter relies on it returning ≤1)."""
    stmt = select(OrgMembership.id).where(
        OrgMembership.org_team_id == org_id, OrgMembership.scim_external_id == external_id
    )
    if exclude_user_id is not None:
        stmt = stmt.where(OrgMembership.user_id != exclude_user_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def list_users(
    db: AsyncSession, *, org_id: UUID, filter_str: str | None, start_index: int, count: int
) -> tuple[Sequence[tuple[User, OrgMembership]], int]:
    """A page of the org's members (oldest membership first; stable for SCIM
    pagination), honoring a userName/externalId ``eq`` filter. ``start_index``
    is 1-based (SCIM); ``count`` is clamped [0, 200]."""
    parsed = parse_filter(filter_str)
    where = [OrgMembership.org_team_id == org_id]
    if parsed is not None:
        attr, value = parsed
        if attr == "username":
            where.append(func.lower(User.email) == value.lower())
        else:  # externalid
            where.append(OrgMembership.scim_external_id == value)
    joined = select(User, OrgMembership).join(OrgMembership, OrgMembership.user_id == User.id)
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(OrgMembership)
            .join(User, User.id == OrgMembership.user_id)
            .where(*where)
        )
        or 0
    )
    offset = max(0, start_index - 1)
    count = max(0, min(count, 200))
    rows = await db.execute(
        joined.where(*where)
        .order_by(OrgMembership.joined_at, OrgMembership.id)
        .offset(offset)
        .limit(count)
    )
    return list(rows.tuples().all()), total


async def get_member(
    db: AsyncSession, *, org_id: UUID, user_id: UUID
) -> tuple[User, OrgMembership] | None:
    """The person and their membership in the org, or None when they hold no
    membership there."""
    membership = await membership_in(db, user_id=user_id, org_team_id=org_id)
    if membership is None:
        return None
    user = await user_service.get_by_id(db, user_id)
    return None if user is None else (user, membership)


async def _member_by_email(db: AsyncSession, *, org_id: UUID, email: str) -> bool:
    row = await db.execute(
        select(OrgMembership.id)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_team_id == org_id, func.lower(User.email) == email.lower())
        .limit(1)
    )
    return row.first() is not None


async def assert_domain_allowed(db: AsyncSession, connection: SsoConnection, email: str) -> None:
    """Provisioning is the org's IdP speaking for an address, the same
    authority an SSO sign-in exercises, so it is held to the same thing: the
    domains platform staff assigned the org (``alkera_core.auth.sso_domains``).

    The platform's email namespace is unique. Without this, one tenant's SCIM
    token could mint a ``users`` row at another company's address, which would
    block the real owner's signup and could later receive that person's
    social sign-in. A domain no one assigned the org provisions nothing."""
    domains = await sso_domains.domains_of(db, connection.org_team_id)
    if not domains:
        raise ScimError(
            400,
            "no email domain is assigned to this organization; SCIM cannot provision users",
            scim_type="invalidValue",
        )
    if email.rsplit("@", 1)[-1].lower().strip() not in domains:
        raise ScimError(
            400,
            f"userName domain is not assigned to this organization: {email}",
            scim_type="invalidValue",
        )


async def create_user(
    db: AsyncSession, *, connection: SsoConnection, body: dict[str, Any]
) -> tuple[User, OrgMembership]:
    """JIT-provision a SCIM user in the connection's org (role: member; no
    password, they sign in via SSO). Raises ScimError(409, uniqueness) when the
    userName is already in the org, and ScimError(400) when its domain is not
    one the connection is authoritative for.

    An address that already has an account in another org is answered exactly
    like a new one, whatever that account's standing and whether or not
    multi-org is on: the org gets a pending membership carrying what its IdP
    pushed (see :func:`_offer`), so provisioning never tells an org which
    addresses have accounts elsewhere. Only the person can turn that
    membership into a seat, and only while multi-org is on."""
    org_id = connection.org_team_id
    email = _primary_email(body)
    if not email:
        raise ScimError(400, "userName / emails is required", scim_type="invalidValue")
    await assert_domain_allowed(db, connection, email)
    if await _member_by_email(db, org_id=org_id, email=email):
        raise ScimError(409, f"user already exists: {email}", scim_type="uniqueness")
    external_id = str(body["externalId"]) if body.get("externalId") else None
    if external_id and await external_id_taken(db, org_id=org_id, external_id=external_id):
        raise ScimError(409, f"externalId already exists: {external_id}", scim_type="uniqueness")
    name = body.get("name") or {}
    existing = await user_service.get_by_email(db, email)
    if existing is not None:
        return existing, await _offer(
            db, org_id=org_id, user=existing, body=body, external_id=external_id
        )
    try:
        user = await user_service.create_user(
            db,
            org_team_id=org_id,
            email=email,
            first_name=str(name.get("givenName") or ""),
            last_name=str(name.get("familyName") or ""),
            password=None,
        )
    except UserConflictError as exc:
        # The address gained an account between the read above and the insert.
        existing = await user_service.get_by_email(db, email)
        if existing is None:
            raise ScimError(409, f"user already exists: {email}", scim_type="uniqueness") from exc
        return existing, await _offer(
            db, org_id=org_id, user=existing, body=body, external_id=external_id
        )
    membership = await membership_in(db, user_id=user.id, org_team_id=org_id)
    if membership is None:  # pragma: no cover - the users trigger writes it with the row
        raise RuntimeError(f"user {user.id} was created without its home membership")
    _record_pushed(membership, body, external_id=external_id)
    user.provisioned_by = "scim"
    await membership_service.add_member(db, team_id=org_id, user_id=user.id, role=TeamRole.MEMBER)
    await db.flush()
    # SCIM-active defaults true; an IdP can create-then-deactivate.
    if body.get("active") is False:
        await set_active(db, membership=membership, active=False)
    return user, membership


async def _offer(
    db: AsyncSession,
    *,
    org_id: UUID,
    user: User,
    body: dict[str, Any],
    external_id: str | None,
) -> OrgMembership:
    """What a create leaves for an identity that exists outside the org: a
    pending membership, withdrawn again at once when the IdP created the user
    inactive (the answer an IdP gets from creating and then deactivating). An
    org's IdP never attaches an existing identity by itself, and nothing here
    depends on the identity's standing, so the answer cannot describe it."""
    membership = await _provision_pending(
        db, org_id=org_id, user=user, body=body, external_id=external_id
    )
    if body.get("active") is False:
        await set_active(db, membership=membership, active=False)
    return membership


async def _provision_pending(
    db: AsyncSession,
    *,
    org_id: UUID,
    user: User,
    body: dict[str, Any],
    external_id: str | None,
) -> OrgMembership:
    """A pending membership in the org for an identity that exists elsewhere,
    carrying the org's id and name for the person. Nothing else is written:
    no seat, no role, and nothing on the identity."""
    membership = await create_org_membership(
        db, user_id=user.id, org_team_id=org_id, status=MembershipStatus.PENDING
    )
    _record_pushed(membership, body, external_id=external_id)
    await db.flush()
    return membership


def _record_pushed(
    membership: OrgMembership, body: dict[str, Any], *, external_id: str | None
) -> None:
    """What a create carries onto the membership, the same for a new address
    and one that already has an account: the org's id for the person, the
    names its IdP sent, and its display name."""
    membership.scim_external_id = external_id
    name = body.get("name") or {}
    given, family = name.get("givenName"), name.get("familyName")
    membership.scim_given_name = None if given is None else str(given)
    membership.scim_family_name = None if family is None else str(family)
    if body.get("displayName"):
        membership.display_name = str(body["displayName"])


async def set_active(db: AsyncSession, *, membership: OrgMembership, active: bool) -> None:
    """Deactivate or reactivate the person's membership in the org. A
    deactivation ends their credentials in the org at once, exactly as an org
    admin's would, and is REFUSED when it would leave the org with no active
    admin (an IdP must not be able to lock an org out of its own administration;
    the same guard the org-admin path enforces).

    A pending membership is the person's to activate, never the IdP's:
    ``active=true`` leaves it as it is, and ``active=false`` deletes it. The
    object then answers the rest of the request as inactive."""
    actor = actor_system(SYSTEM_ACTOR)
    if membership.status is MembershipStatus.PENDING:
        if not active:
            await withdraw_pending_membership(db, membership, actor=actor)
            membership.status = MembershipStatus.DEACTIVATED
        return
    if active:
        await reactivate_membership(db, membership, actor=actor)
        return
    try:
        await deactivate_membership(db, membership, actor=actor)
    except LastActiveAdminError as exc:
        raise ScimError(
            400, "cannot deactivate the last active org admin", scim_type="mutability"
        ) from exc


def display_name_from(
    user: User, membership: OrgMembership, *, given: str | None, family: str | None
) -> str:
    """The org's name for the person after an IdP pushes a given and/or family
    name: the part pushed, and the identity's own for the part not pushed
    (nothing for it on a pending membership, whose person has not joined)."""
    pending = membership.status is MembershipStatus.PENDING
    first = given if given is not None else ("" if pending else user.first_name)
    last = family if family is not None else ("" if pending else user.last_name)
    composed = f"{first} {last}".strip()
    return composed or (membership.display_name or "")


def _primary_email(body: dict[str, Any]) -> str:
    """The user's email: the primary `emails` entry, else the first, else
    `userName` if it looks like an email."""
    emails = body.get("emails") or []
    if isinstance(emails, list) and emails:
        primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), None)
        chosen = primary or (emails[0] if isinstance(emails[0], dict) else None)
        if chosen and chosen.get("value"):
            return str(chosen["value"]).strip().lower()
    username = str(body.get("userName") or "").strip().lower()
    return username if "@" in username else ""
