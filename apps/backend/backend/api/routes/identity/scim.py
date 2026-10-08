"""SCIM 2.0 (RFC 7644) provisioning endpoints for enterprise IdPs (Okta / Entra /
Keycloak).

Authenticated by the org's SCIM **bearer token** (``Authorization: Bearer
alk_scim_…``) — NOT a user session — which resolves the org the request acts on.
Mounted under ``/api/v1/scim/v2`` so the existing reverse proxy serves it; the IdP
appends ``/Users`` etc. Excluded from the OpenAPI schema (IdPs don't use our SDK).

Every operation acts on the person's membership in the token's org, never on
their identity. Deprovisioning is ``PATCH active=false`` (and ``DELETE``): the
membership is deactivated and the person's credentials in this org end, through
the same path an org admin's offboarding takes; their identity and any other
org they belong to are untouched. A pending membership (an identity from
another org, provisioned here and not yet joined) is deleted instead. Names
an IdP pushes become the name this org shows for the person. Content type is
``application/scim+json`` on every response, including errors.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from alkera_core.models import MembershipStatus, OrgMembership, SsoConnection, User
from alkera_core.org_entitlements import org_entitlements
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from backend.auth.dependencies import DbSession
from backend.services.identity import ScimError
from backend.services.identity import scim as scim_service
from backend.services.identity import sso as sso_service

router = APIRouter(prefix="/api/v1/scim/v2", tags=["scim"], include_in_schema=False)

_MEDIA = "application/scim+json"


def _scim_json(
    body: dict[str, Any], *, status_code: int = 200, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, media_type=_MEDIA, headers=headers)


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


async def _authenticate_scim(request: Request, db: DbSession) -> SsoConnection:
    conn = await sso_service.resolve_scim_connection(db, _bearer(request) or "")
    if conn is None:
        raise ScimError(401, "invalid or missing SCIM bearer token")
    # The token was minted under an Enterprise entitlement, and the endpoints that
    # rotate or revoke it sit behind that same entitlement — so when the plan ends,
    # the credential has to end with it. Otherwise the org is left with a machine
    # credential into its own tenant that nobody (customer or Alkera) has an API to
    # kill. Always true on a self-hosted install.
    if not await org_entitlements().enterprise_features(db, conn.org_team_id):
        raise ScimError(401, "SCIM provisioning is not enabled for this organization")
    return conn


ScimConn = Annotated[SsoConnection, Depends(_authenticate_scim)]


def _user_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ScimError(404, "user not found") from exc


#: The core-schema prefix RFC 7644 §3.5.2 allows on an attribute path
#: (``urn:…:User:active`` means ``active``). Compared lower-cased.
_USER_URN_PREFIX = "urn:ietf:params:scim:schemas:core:2.0:user:"

_ACTIVE_TRUE = frozenset({"true", "t", "yes", "1"})
_ACTIVE_FALSE = frozenset({"false", "f", "no", "0"})


def _coerce_active(value: Any) -> bool:
    """The SCIM ``active`` attribute out of any shape a real IdP sends it in.

    Entra ID sends the string ``"False"``; others send a real boolean, 0/1, or the
    multi-valued ``[{"value": …}]`` form. Anything else raises — a deprovisioning
    request whose value we cannot read must fail loudly, never be dropped and
    answered 200 (the IdP would record the user as deprovisioned while their
    sessions stayed live)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _ACTIVE_TRUE:
            return True
        if text in _ACTIVE_FALSE:
            return False
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return _coerce_active(value[0].get("value"))
    raise ScimError(400, f"active is not a boolean: {value!r}", scim_type="invalidValue")


def _attr_path(raw: Any) -> str:
    """A PATCH operation's ``path``, lower-cased and stripped of the core-schema
    URN prefix, so ``urn:…:User:active`` and ``active`` are the same attribute."""
    path = str(raw or "").strip().lower()
    if path.startswith(_USER_URN_PREFIX):
        path = path[len(_USER_URN_PREFIX) :]
    return path


def _operations(body: dict[str, Any]) -> list[Any]:
    """The PATCH body's operations. SCIM attribute names are case-insensitive
    (RFC 7643 §2.1), so ``operations`` is looked up that way — a case variant used
    to fall through to an empty list and a 200 that changed nothing."""
    for key, value in body.items():
        if key.lower() == "operations":
            if not isinstance(value, list):
                raise ScimError(400, "Operations must be an array", scim_type="invalidSyntax")
            return value
    raise ScimError(400, "PATCH body must carry Operations", scim_type="invalidSyntax")


async def _set_active(db: DbSession, membership: OrgMembership, active: bool) -> None:
    if membership.status is MembershipStatus.PENDING or active != membership.is_active:
        await scim_service.set_active(db, membership=membership, active=active)


async def _member_or_404(
    db: DbSession, conn: SsoConnection, raw_id: str
) -> tuple[User, OrgMembership]:
    found = await scim_service.get_member(db, org_id=conn.org_team_id, user_id=_user_id(raw_id))
    if found is None:
        raise ScimError(404, "user not found")
    return found


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


@router.get("/ServiceProviderConfig")
async def service_provider_config(_conn: ScimConn) -> JSONResponse:
    return _scim_json(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": "Authentication via the org's SCIM bearer token.",
                    "primary": True,
                }
            ],
        }
    )


@router.get("/ResourceTypes")
async def resource_types(_conn: ScimConn) -> JSONResponse:
    user_type = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": scim_service.USER_SCHEMA,
        "meta": {"resourceType": "ResourceType", "location": "/scim/v2/ResourceTypes/User"},
    }
    return _scim_json(
        {
            "schemas": [scim_service.LIST_SCHEMA],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [user_type],
        }
    )


@router.get("/Schemas")
async def schemas(_conn: ScimConn) -> JSONResponse:
    user_schema = {
        "id": scim_service.USER_SCHEMA,
        "name": "User",
        "description": "SCIM core User",
        "meta": {
            "resourceType": "Schema",
            "location": f"/scim/v2/Schemas/{scim_service.USER_SCHEMA}",
        },
    }
    return _scim_json(
        {
            "schemas": [scim_service.LIST_SCHEMA],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [user_schema],
        }
    )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


@router.get("/Users")
async def list_users(request: Request, db: DbSession, conn: ScimConn) -> JSONResponse:
    qp = request.query_params
    try:
        start_index = int(qp.get("startIndex", "1"))
        count = int(qp.get("count", "100"))
    except ValueError as exc:
        raise ScimError(400, "startIndex/count must be integers", scim_type="invalidValue") from exc
    members, total = await scim_service.list_users(
        db,
        org_id=conn.org_team_id,
        filter_str=qp.get("filter"),
        start_index=max(1, start_index),
        count=count,
    )
    base = sso_service.scim_base_url()
    return _scim_json(
        {
            "schemas": [scim_service.LIST_SCHEMA],
            "totalResults": total,
            "startIndex": max(1, start_index),
            "itemsPerPage": len(members),
            "Resources": [scim_service.to_scim_user(u, m, base_url=base) for u, m in members],
        }
    )


@router.get("/Users/{raw_id}")
async def get_user(raw_id: str, db: DbSession, conn: ScimConn) -> JSONResponse:
    user, membership = await _member_or_404(db, conn, raw_id)
    return _scim_json(
        scim_service.to_scim_user(user, membership, base_url=sso_service.scim_base_url())
    )


@router.post("/Users")
async def create_user(request: Request, db: DbSession, conn: ScimConn) -> JSONResponse:
    body = await _json_body(request)
    user, membership = await scim_service.create_user(db, connection=conn, body=body)
    base = sso_service.scim_base_url()
    return _scim_json(
        scim_service.to_scim_user(user, membership, base_url=base),
        status_code=201,
        headers={"Location": f"{base}/Users/{user.id}"},
    )


@router.put("/Users/{raw_id}")
async def replace_user(
    raw_id: str, request: Request, db: DbSession, conn: ScimConn
) -> JSONResponse:
    user, membership = await _member_or_404(db, conn, raw_id)
    body = await _json_body(request)
    # userName (the email) is immutable per RFC 7643 — reject an actual rename;
    # a re-sync that resends the SAME userName is fine.
    new_username = str(body.get("userName") or "").strip().lower()
    if new_username and new_username != user.email.lower():
        raise ScimError(400, "userName is immutable", scim_type="mutability")
    if body.get("displayName"):
        membership.display_name = str(body["displayName"])
    elif "name" in body:
        name = body.get("name") or {}
        scim_service.push_names(
            user,
            membership,
            given=str(name.get("givenName") or ""),
            family=str(name.get("familyName") or ""),
        )
    # PUT replaces the whole resource: apply externalId by PRESENCE (so it can be
    # cleared), with a per-org uniqueness guard.
    if "externalId" in body:
        await _apply_external_id(db, conn, user, membership, body["externalId"])
    if "active" in body:
        await _set_active(db, membership, _coerce_active(body["active"]))
    await db.flush()
    return _scim_json(
        scim_service.to_scim_user(user, membership, base_url=sso_service.scim_base_url())
    )


@router.patch("/Users/{raw_id}")
async def patch_user(raw_id: str, request: Request, db: DbSession, conn: ScimConn) -> JSONResponse:
    """Apply a SCIM PATCH.

    Deprovisioning (``active`` → false) is the operation an enterprise buys SCIM
    for, so every operation touching ``active`` either APPLIES or 400s — it is
    never skipped into a 200 that says the user is still active. Attributes this
    resource does not model (displayName, title, department, …) stay tolerated so a
    routine IdP attribute sync doesn't fail."""
    user, membership = await _member_or_404(db, conn, raw_id)
    body = await _json_body(request)
    for op in _operations(body):
        if not isinstance(op, dict):
            raise ScimError(400, "each operation must be an object", scim_type="invalidSyntax")
        verb = str(op.get("op") or "").strip().lower()
        if verb not in ("replace", "add", "remove"):
            raise ScimError(400, f"unsupported op: {op.get('op')!r}", scim_type="invalidSyntax")
        path = _attr_path(op.get("path"))
        value = op.get("value")
        if not path:
            # A pathless operation carries an object of attributes to merge.
            if not isinstance(value, dict):
                raise ScimError(
                    400, "a pathless operation needs an object value", scim_type="invalidValue"
                )
            await _apply_attrs(db, conn, user, membership, value)
        elif path == "active":
            # `remove active` is how some IdPs express deprovisioning; the safe
            # reading of "this user no longer has an active attribute" is inactive.
            await _set_active(db, membership, False if verb == "remove" else _coerce_active(value))
        elif path == "displayname":
            membership.display_name = None if verb == "remove" else (str(value or "") or None)
        elif path == "name.givenname":
            scim_service.push_names(
                user, membership, given="" if verb == "remove" else str(value or ""), family=None
            )
        elif path == "name.familyname":
            scim_service.push_names(
                user, membership, given=None, family="" if verb == "remove" else str(value or "")
            )
        elif path == "externalid":
            await _apply_external_id(
                db, conn, user, membership, None if verb == "remove" else value
            )
    await db.flush()
    return _scim_json(
        scim_service.to_scim_user(user, membership, base_url=sso_service.scim_base_url())
    )


@router.delete("/Users/{raw_id}", status_code=204)
async def delete_user(raw_id: str, db: DbSession, conn: ScimConn) -> Response:
    _user, membership = await _member_or_404(db, conn, raw_id)
    # Deprovisioning deactivates the membership; the IdP never deletes an
    # identity, which may belong to other orgs, nor anyone's data.
    await scim_service.set_active(db, membership=membership, active=False)
    return Response(status_code=204, media_type=_MEDIA)


# --------------------------------------------------------------------------- #
# Groups (read-only — group→role is configured in Alkera, not pushed via SCIM)
# --------------------------------------------------------------------------- #


@router.get("/Groups")
async def list_groups(_conn: ScimConn) -> JSONResponse:
    return _scim_json(
        {
            "schemas": [scim_service.LIST_SCHEMA],
            "totalResults": 0,
            "startIndex": 1,
            "itemsPerPage": 0,
            "Resources": [],
        }
    )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise ScimError(400, "request body is not valid JSON", scim_type="invalidSyntax") from exc
    if not isinstance(body, dict):
        raise ScimError(400, "request body must be a JSON object", scim_type="invalidSyntax")
    return body


async def _apply_attrs(
    db: DbSession,
    conn: SsoConnection,
    user: User,
    membership: OrgMembership,
    attrs: dict[str, Any],
) -> None:
    if attrs.get("displayName"):
        membership.display_name = str(attrs["displayName"])
    else:
        name = attrs.get("name") or {}
        given, family = name.get("givenName"), name.get("familyName")
        if given is not None or family is not None:
            scim_service.push_names(
                user,
                membership,
                given=None if given is None else str(given),
                family=None if family is None else str(family),
            )
    if "externalId" in attrs:
        await _apply_external_id(db, conn, user, membership, attrs["externalId"])
    if "active" in attrs:
        await _set_active(db, membership, _coerce_active(attrs["active"]))


async def _apply_external_id(
    db: DbSession, conn: SsoConnection, user: User, membership: OrgMembership, value: Any
) -> None:
    """Set the membership's ``scim_external_id`` (clearing on falsy), refusing a
    value already held by another member of the org (SCIM dedup keeps the
    externalId filter ≤1 result)."""
    ext = str(value) if value else None
    if ext and await scim_service.external_id_taken(
        db, org_id=conn.org_team_id, external_id=ext, exclude_user_id=user.id
    ):
        raise ScimError(409, f"externalId already exists: {ext}", scim_type="uniqueness")
    membership.scim_external_id = ext


def scim_error_response(exc: ScimError) -> JSONResponse:
    """Render a ScimError as the RFC 7644 error body (used by the app handler)."""
    return _scim_json(
        scim_service.error_body(exc.status_code, exc.detail, scim_type=exc.scim_type),
        status_code=exc.status_code,
    )


__all__ = ["router", "scim_error_response"]
