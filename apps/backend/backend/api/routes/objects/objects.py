"""Workspace objects: promoted results, their rows and their exports.

Four things this router is careful about:

* **The tenancy floor is the engine's.** An object is loaded by id alone and
  the resource names the org the row actually belongs to, so ``authorize()``
  refuses a foreign row as an opaque not-found before any policy runs — and
  leaves the denial on record. A ``WHERE org_id = …`` would give the
  caller the same answer and an auditor none.
* **Rows are always mediated.** ``GET /objects/{id}/rows`` and the CSV export
  read through the store with authorization applied; there is no storage URL to
  hand out and no request field that chooses a key.
* **The retired kinds are refused before the body is read as one.** A saved
  query and a report were replaced by chat templates, so a create naming one
  is answered ``410`` by name rather than as a shape the vocabulary happens no
  longer to spell — a client that still sends one learns why it is gone
  instead of reading a validation error about an unexpected literal.
* **Only the machine fills a result.** ``POST /objects/{id}/payload`` is the
  one place customer row data crosses into our store: its policy requires an
  agent principal acting for a user who could have written the object, and the
  route accepts it only while the object is still waiting — a receipted answer
  is not rewritable.

Every by-id route decides through ``objects.access`` with the verb it performs
— ``READ``, ``WRITE``, ``EXPORT``, ``UPLOAD_PAYLOAD`` — and the create with
``CREATE``; the listing filters with the same audience predicate the policy
decides through, without a decision row per row.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.authz.chat_scope import SCOPE_PRIVATE, team_of_scope
from alkera_core.config import settings
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE, RETIRED_OBJECT_TYPES
from alkera_core.schemas.objects import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_ROW_LIMIT,
    MAX_LIST_LIMIT,
    MAX_ROW_LIMIT,
    RESULT_IMMUTABLE_FIELDS,
    RESULT_SERVER_OWNED_FIELDS,
    BlobHandle,
    ChartSpecError,
    ObjectPayloadFailure,
    ObjectPayloadUpload,
    ObjectRowsPage,
    PromoteRelay,
    Receipt,
    ResultBlobEnvelope,
    ResultColumn,
    ResultSpec,
    WorkspaceObjectCreate,
    WorkspaceObjectList,
    WorkspaceObjectRead,
    WorkspaceObjectUpdate,
    unbound_chart_fields,
    validate_chart_spec,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession, PrincipalUser
from backend.authz import enforce, role_resolver
from backend.services.chats import chat_service
from backend.services.objects import object_service, result_store
from backend.services.org import teams as team_service
from backend.services.sharing import access

router = APIRouter(prefix="/api/v1/objects", tags=["objects"])

#: The object types whose title is a NAME somewhere else and is renamed there.
#: A chat template is a folder in the drive: its title names that folder, so
#: renaming it here would move the row and leave the folder behind. The refusal
#: is a 422 rather than a silent no-op, because a client that thought it renamed
#: something is a client whose next read disagrees with its own screen.
TITLE_ELSEWHERE_TYPES: frozenset[str] = frozenset({"chat_template", "workspace"})

#: A workspace reached through this router is decided by its own policy, the
#: one ``/workspaces/{id}`` decides by, so the two doors cannot disagree. A
#: rename through here is refused above (``TITLE_ELSEWHERE_TYPES``): a workspace
#: of one is renamed together with its chat, on its own surface.
WORKSPACE_TYPE = "workspace"
WORKSPACE_VERBS: dict[Action, Action] = {Action.READ: Action.READ, Action.WRITE: Action.RENAME}

#: The object type whose own router already answers for it.
CHAT_TYPE = "chat"
#: What this router's verbs MEAN to a chat, so the two doors onto one chat
#: cannot answer differently. A chat's WRITE here is a rename — the router edits
#: no other field of a chat, and ``WRITE`` on a chat is the publishing machine's
#: verb, not a person's. The verbs a chat has no answer for (a result's rows,
#: its export, its payload) are absent and keep deciding through
#: ``objects.access``, which refuses them on the shape of a chat anyway.
CHAT_VERBS: dict[Action, Action] = {Action.READ: Action.READ, Action.WRITE: Action.RENAME}

#: The status a promoted result waits in until its machine delivers the payload.
PENDING_UPLOAD = "pending_upload"
#: The keys of a receipt's principal chain the server writes itself. A client
#: value under either is dropped: who promoted was recorded from the promoter's
#: acting context when the result was created, and who uploaded is recorded
#: from the uploader's here.
SERVER_STAMPED_CHAIN_KEYS = frozenset({"promoted_by", "uploaded_by"})


def _version_conflict(exc: object_service.VersionConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "version_conflict",
            "message": "This object moved on; re-read it and try again",
            "current_version": exc.current.version,
        },
    )


async def _refuse_oversized_body(request: Request) -> None:
    """A 413 for a payload past the deployment's ceiling.

    Measured on the bytes that arrived rather than a declared length, so a
    chunked or mis-declared body is bounded the same way. The body has been
    read by the time a route runs, so this bounds what one request may put
    into the store, not what the process buffers.
    """
    cap = settings.objects_payload_max_bytes
    if len(await request.body()) > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"The payload is larger than this server accepts ({cap} bytes)",
        )


def _require_payload(obj: WorkspaceObject) -> None:
    """A result without its payload has no document to hand out; an empty table
    would read as "the query returned nothing" — which is the one thing it must
    not be mistaken for. Waiting and failed are told apart, because the reader's
    next move is different: wait, or go and look at what went wrong."""
    if obj.status == PENDING_UPLOAD:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "payload_pending",
            "This result is still saving; its rows arrive when the machine delivers them",
        )
    if obj.status == "failed":
        spec = obj.spec or {}
        reason = spec.get("failure_reason") if isinstance(spec, dict) else None
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "payload_failed",
            str(reason) if isinstance(reason, str) and reason else "This result could not be saved",
        )


def _digest_of(envelope: dict[str, Any]) -> str:
    """The handle's digest, derived server-side from what we stored.

    A client-supplied digest would only be a claim about content we hold;
    deriving it here makes the handle mean what it says.
    """
    return hashlib.sha256(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _load(db: DbSession, object_id: UUID, *, type: str | None = None) -> WorkspaceObject:
    obj = await access.load_object(db, object_id, type=type)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return obj


async def _reader(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: User | None
) -> access.Reader:
    """The caller's facts: a person's from their roles and memberships, a box's
    from nothing but its credential (``principal_user`` hands ``None`` for a
    machine and a user for everything else)."""
    if ctx.is_machine or user is None:
        return access.machine_reader(ctx)
    return await access.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )


async def _decide(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: User | None,
    obj: WorkspaceObject,
    action: Action,
    *,
    reader: access.Reader | None = None,
) -> None:
    reader = reader or await _reader(request, db, ctx, user)
    if obj.type == CHAT_TYPE and action in CHAT_VERBS:
        # A chat reached through this router is the same chat ``/chats/{id}``
        # answers for, so it is decided by the same policy over the same facts.
        # Deciding it here instead — under a rule about team scope and org
        # admins — is how the two doors came to disagree: a colleague holding
        # "Full access" on the chat was told it did not exist, while an admin
        # nobody shared it with read its title, its model and its spec, and
        # renamed it.
        await enforce(
            request,
            db,
            ctx,
            CHAT_VERBS[action],
            access.object_resource(obj, type=ResourceType.CHAT),
            await access.chat_attrs(db, obj, reader),
        )
        return
    if obj.type == WORKSPACE_TYPE:
        # Every verb this router has no workspace meaning for is refused by
        # the workspace policy as unsupported, never decided as an object's.
        await enforce(
            request,
            db,
            ctx,
            WORKSPACE_VERBS.get(action, action),
            access.object_resource(obj, type=ResourceType.WORKSPACE),
            await access.workspace_attrs(db, obj, reader),
        )
        return
    await enforce(
        request,
        db,
        ctx,
        action,
        access.object_resource(obj, type=ResourceType.WORKSPACE_OBJECT),
        # The one fact about the caller: whether the box asking runs the chat
        # this object came out of. Nobody who is not a box pays for the read.
        access.object_attrs(
            obj, reader, held_by_machine=await access.machine_holds_object(db, obj, reader)
        ),
    )


def _refusal(status_code: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message, **details}
    )


def _parse_result(raw: dict[str, Any]) -> ResultSpec:
    try:
        return ResultSpec.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors(include_url=False)
        ) from exc


def _admitted_chart(chart: dict[str, Any]) -> dict[str, Any]:
    """The chart walked against the allowlist and reduced to its persisted
    form — the write-time gate, so nothing that was not admitted can be read
    back later and rendered."""
    try:
        return validate_chart_spec(chart).persisted()
    except ChartSpecError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _require_chart_bound(chart: dict[str, Any] | None, keys: list[str]) -> None:
    """A chart names column KEYS; a field this result does not have is refused
    with the channel named. Nothing to bind against yet — no declared columns
    and no payload — binds trivially; the upload settles the columns."""
    if chart is None or not keys:
        return
    unbound = unbound_chart_fields(chart, keys)
    if unbound:
        raise _refusal(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "chart_field_unbound",
            f"{unbound[0]} names a field that is not a column of this result",
            field=unbound[0],
        )


def _result_spec_for_create(raw: dict[str, Any], ctx: CurrentPrincipal) -> dict[str, Any]:
    """A result the client is creating: columns, a chart, its provenance.

    The receipt and the payload's placement are the machine's to write on
    upload, so a body carrying them is refused rather than trusted; the
    promoter is stamped here from the acting context, never read from the body.
    """
    for field in sorted(RESULT_SERVER_OWNED_FIELDS):
        if field in raw:
            raise _refusal(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "server_owned_field",
                f"{field} is written when the machine delivers the payload, not by the client",
                field=field,
            )
    spec = _parse_result(raw)
    if spec.chart_spec is not None:
        spec.chart_spec = _admitted_chart(spec.chart_spec)
    spec.receipt = Receipt(principal_chain={"promoted_by": ctx.audit_dict()})
    _require_chart_bound(spec.chart_spec, [column.name for column in spec.columns])
    return spec.model_dump(mode="json")


async def _require_source_chat_readable(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: User,
    spec: dict[str, Any],
    reader: access.Reader,
) -> None:
    """A result names the chat it came out of only when its creator may read
    that chat.

    The source is not decoration. The box that runs the named chat is admitted
    to the result (``machine_holds_object``) and asked for its payload, and the
    result reads as that chat's answer. So naming a chat is a read of it,
    decided by the chat policy over the same facts ``GET /chats/{id}`` uses: a
    chat the creator cannot read, one in another org, and one that does not
    exist all get the same opaque not-found.
    """
    source = spec.get("source_chat_id")
    if source is None:
        return
    try:
        chat_id = UUID(source)
    except ValueError as exc:
        raise _refusal(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_source_chat_id",
            "source_chat_id must be a chat id",
            field="source_chat_id",
        ) from exc
    chat = await access.load_object(db, chat_id, type=CHAT_TYPE)
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    await _decide(request, db, ctx, user, chat, Action.READ, reader=reader)


def _result_spec_for_update(raw: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """An edit to a result, merged over what is stored.

    A receipted answer is not rewritable: the receipt, the payload's placement
    and the provenance are compared with what is stored, and a change to any
    of them is a conflict (an unchanged echo — GET, edit, PUT — is not a
    write). What remains is merged over the stored spec, so a partial body
    cannot drop the payload by omission; the columns named must be columns
    the payload has, and the chart must bind to the keys that result.
    """
    for field in sorted(RESULT_IMMUTABLE_FIELDS):
        if field in raw and raw[field] != stored.get(field):
            if field == "receipt":
                raise _refusal(
                    status.HTTP_409_CONFLICT,
                    "receipt_immutable",
                    "A result's receipt is written once, by the machine that ran the query",
                )
            raise _refusal(
                status.HTTP_409_CONFLICT,
                "immutable_field",
                f"{field} cannot be changed once a result exists",
                field=field,
            )
    editable = {key: value for key, value in raw.items() if key not in RESULT_IMMUTABLE_FIELDS}
    spec = _parse_result({**stored, **editable})
    if "chart_spec" in editable and spec.chart_spec is not None:
        spec.chart_spec = _admitted_chart(spec.chart_spec)
    known = result_store.envelope_keys(stored)
    if known:
        for index, column in enumerate(spec.columns):
            if column.name not in known:
                raise _refusal(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "unknown_column",
                    f"columns[{index}].name is not a column of this result's payload",
                    field=f"columns[{index}].name",
                )
    merged = spec.model_dump(mode="json")
    if "chart_spec" in editable or "columns" in editable:
        _require_chart_bound(spec.chart_spec, result_store.keys_of(merged))
    return merged


@router.get("", response_model=WorkspaceObjectList)
async def list_objects(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    type: str | None = None,
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    cursor: str | None = None,
) -> WorkspaceObjectList:
    """The org's objects, newest first, cut to what the caller may read.

    Cut by the SAME answer the by-id door gives, per row and per type: a chat is
    decided by the chat's own read predicate (its owner, its machine, a rung on
    its node, and the deployment's admin setting), everything else by its
    audience. The list is the third door onto a chat and was for a while the
    only one still answering the old question — which showed a member's private
    title, owner and spec to an org admin nobody had shared it with, and hid the
    same chat from the colleague holding "Can view".
    """
    reader = await _reader(request, db, ctx, user)

    # A cursor this listing did not mint answers 422 from the codec itself, so
    # every listing that pages inherits the refusal instead of translating it.
    async def _readable(rows: list[WorkspaceObject]) -> list[WorkspaceObject]:
        return await access.readable_objects(db, rows, reader)

    page = await object_service.list_objects(
        db, org_team_id=ctx.org_id, type=type, limit=limit, cursor=cursor, cut=_readable
    )
    return WorkspaceObjectList(
        items=[WorkspaceObjectRead.model_validate(obj) for obj in page.items],
        next_cursor=page.next_cursor,
    )


async def _refuse_a_retired_type(request: Request, user: CurrentUser) -> None:
    """A create naming a kind that no longer exists is gone, not malformed.

    A saved query and a report were replaced by chat templates, so the create
    body no longer spells either — which on its own would answer a client that
    still sends one with a validation error about an unexpected literal, and
    leave whoever reads it guessing whether the field was mistyped. Reading the
    type off the raw body first turns that into the answer it deserves: the
    kind is gone, said by name. This runs as a dependency because a dependency
    is solved before the body is validated, so the refusal is what the caller
    sees rather than what the shape would have said first — and it names the
    caller so that a stranger is still answered by the session, never by the
    catalogue of kinds this deployment used to keep.
    """
    del user
    try:
        body = await request.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return
    named = body.get("type")
    if isinstance(named, str) and named in RETIRED_OBJECT_TYPES:
        raise _refusal(
            status.HTTP_410_GONE,
            "object_type_retired",
            f"A {named} is no longer a thing this workspace saves; "
            "save the chat as a chat template instead",
            type=named,
        )


@router.post(
    "",
    response_model=WorkspaceObjectRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_refuse_a_retired_type)],
)
async def create_object(
    request: Request,
    payload: WorkspaceObjectCreate,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> WorkspaceObjectRead:
    """Save a result. Idempotent on the caller's ``client_id``.

    A ``client_id`` that already names an object is a READ of that object,
    decided as one: the row that would be returned is the row that is
    decided on, so a colliding id outside the caller's audience is the
    policy's opaque not-found, never a copy of someone else's result. A new
    object's CREATE decision is filed under the id minted for it.

    A result created here has no payload yet: it waits for its machine like
    a promoted one, and its receipt names the caller as its promoter.
    """
    reader = await _reader(request, db, ctx, user)
    existing = await object_service.find_by_logical_id(
        db, org_team_id=ctx.org_id, namespace=DEFAULT_NAMESPACE, logical_id=payload.client_id
    )
    if existing is not None:
        await _decide(request, db, ctx, user, existing, Action.READ, reader=reader)
        if existing.type != payload.type:
            raise _refusal(
                status.HTTP_409_CONFLICT,
                "client_id_in_use",
                f"This client_id already names a {existing.type}; choose another",
            )
        await db.commit()
        return WorkspaceObjectRead.model_validate(existing)
    object_id = uuid4()
    # A result is its creator's alone until they name an audience. A team
    # audience must be a team of the caller's org: the policy admits an org
    # admin into every scope of the org, so it cannot tell a foreign team from
    # one of its own, and the answer for either is the one a team that does
    # not exist gets — before any decision row names it.
    scope = payload.visibility_scope or SCOPE_PRIVATE
    team_id = team_of_scope(scope)
    if team_id is not None and not await team_service.belongs_to_org(db, team_id, ctx.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
    await enforce(
        request,
        db,
        ctx,
        Action.CREATE,
        Resource(ResourceType.WORKSPACE_OBJECT, id=str(object_id), org_id=ctx.org_id),
        access.new_object_attrs(reader, team_id=team_id, visibility_scope=scope),
    )
    spec = _result_spec_for_create(payload.spec, ctx)
    await _require_source_chat_readable(request, db, ctx, user, spec, reader)
    try:
        obj, created = await object_service.create_object(
            db,
            owner=user,
            org_id=ctx.org_id,
            type=payload.type,
            title=payload.title,
            spec=spec,
            logical_id=payload.client_id,
            object_id=object_id,
            status=PENDING_UPLOAD,
            team_id=team_id,
            visibility_scope=scope,
        )
    except object_service.LogicalIdRetiredError as exc:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "client_id_retired",
            "This client_id belonged to an object that was deleted; save under a new one",
        ) from exc
    if created:
        await object_service.announce(db, obj=obj, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(obj)
    return WorkspaceObjectRead.model_validate(obj)


@router.get("/{object_id}", response_model=WorkspaceObjectRead)
async def get_object(
    request: Request, object_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: PrincipalUser
) -> WorkspaceObjectRead:
    """One object. A box on its own machine credential reads the results of the
    chats bound to it and nothing else; everyone else reads their audience."""
    obj = await _load(db, object_id)
    await _decide(request, db, ctx, user, obj, Action.READ)
    await db.commit()
    return WorkspaceObjectRead.model_validate(obj)


@router.put("/{object_id}", response_model=WorkspaceObjectRead)
async def update_object(
    request: Request,
    object_id: UUID,
    payload: WorkspaceObjectUpdate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> WorkspaceObjectRead:
    """Edit an object, naming the version you read.

    ``expected_version`` is compared under the row's lock; ``0`` never matches
    a live row, so "I did not read this" is a conflict rather than a silent
    overwrite.
    """
    obj = await _load(db, object_id)
    await _decide(request, db, ctx, user, obj, Action.WRITE)
    spec: dict[str, Any] | None = None
    if payload.spec is not None:
        if obj.type == "result":
            spec = _result_spec_for_update(payload.spec, dict(obj.spec or {}))
        else:
            raise _refusal(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "spec_not_editable_here",
                f"A {obj.type}'s spec is not edited through this route",
            )
    if payload.title is not None and obj.type in TITLE_ELSEWHERE_TYPES:
        raise _refusal(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "title_not_editable_here",
            f"A {obj.type}'s title is renamed on its own surface, not through this route",
        )
    try:
        updated = await object_service.apply_update(
            db,
            obj=obj,
            expected_version=payload.expected_version,
            title=payload.title,
            spec=spec,
        )
    except object_service.VersionConflictError as exc:
        raise _version_conflict(exc) from exc
    await object_service.announce(db, obj=updated, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(updated)
    return WorkspaceObjectRead.model_validate(updated)


@router.get("/{object_id}/rows", response_model=ObjectRowsPage)
async def read_object_rows(
    request: Request,
    object_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT),
) -> ObjectRowsPage:
    obj = await _load(db, object_id, type="result")
    await _decide(request, db, ctx, user, obj, Action.READ)
    _require_payload(obj)
    columns, rows, total = await result_store.read_rows(db, obj=obj, offset=offset, limit=limit)
    await db.commit()
    return ObjectRowsPage(
        columns=columns, keys=result_store.keys_of(dict(obj.spec or {})), rows=rows, total=total
    )


def _csv_filename(obj: WorkspaceObject) -> str:
    """The download's name, taken from the result's own title.

    A Downloads folder full of ``3f2a9c….csv`` tells the person who exported
    nothing about what they exported. The title is folded to ASCII (a
    spreadsheet on a foreign code page is the one place a UTF-8 filename still
    goes wrong), reduced to a safe stem, and dated so two exports of the same
    result on different days do not overwrite each other. An untitled result
    falls back to its id, which is what the name used to be.
    """
    folded = unicodedata.normalize("NFKD", obj.title or "").encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-")[:80].strip("-")
    return f"{stem or obj.id}-{obj.created_at:%Y-%m-%d}.csv"


@router.get("/{object_id}/export.csv")
async def export_object_csv(
    request: Request, object_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> Response:
    """The whole result as CSV.

    Exporting is the owner's or an org admin's (``EXPORT``): a reader keeps the
    rows on screen, the export leaves the product. Every cell is escaped on the
    way out: their rows are stored LLM output, and a cell that begins like a
    formula must arrive in a spreadsheet as text.

    The document is produced as it is sent, a spill page at a time, so the
    export has no row at which it has to start refusing and costs one page of
    memory however long the result is.
    """
    obj = await _load(db, object_id, type="result")
    await _decide(request, db, ctx, user, obj, Action.EXPORT)
    _require_payload(obj)
    await db.commit()
    filename = _csv_filename(obj)

    async def document() -> AsyncIterator[str]:
        yield result_store.CSV_BOM
        async for chunk in result_store.stream_csv(db, obj=obj):
            yield chunk

    return StreamingResponse(
        document(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{object_id}/payload", response_model=WorkspaceObjectRead)
async def upload_object_payload(
    request: Request,
    object_id: UUID,
    payload: ObjectPayloadUpload,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> WorkspaceObjectRead:
    """The machine delivers the payload behind a promoted result.

    Authorised as an AGENT acting for a user who could have written the object
    (``UPLOAD_PAYLOAD``), or as the box on its own machine credential when the
    result came out of a chat bound to its machine. Accepted only while the object is still waiting:
    a second upload against a ready result is a conflict, not a rewrite — the
    first payload is the one its receipt describes. Two deliveries that race
    past that check meet at the row's lock, and the loser is told the live
    version rather than overwriting the winner.
    """
    await _refuse_oversized_body(request)
    obj = await _load(db, object_id, type="result")
    await _decide(request, db, ctx, user, obj, Action.UPLOAD_PAYLOAD)
    if obj.status != PENDING_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "payload_already_delivered",
                "message": "This result already has its payload",
            },
        )
    try:
        envelope = ResultBlobEnvelope.model_validate(payload.envelope)
        receipt = Receipt.model_validate(payload.receipt)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors(include_url=False)
        ) from exc
    handle = BlobHandle(
        sha256=_digest_of(payload.envelope),
        size=result_store.envelope_bytes(envelope),
        media_type="application/json",
    )
    spec = ResultSpec.model_validate(obj.spec or {})
    try:
        placement = await result_store.store_payload(db, obj=obj, envelope=envelope, handle=handle)
    except result_store.PayloadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    # The promoter was recorded on the receipt when the result was created and
    # the uploader is recorded now, both from the acting context the server
    # resolved; the machine's own facts join that chain and never replace it.
    offered = {
        key: value
        for key, value in receipt.principal_chain.items()
        if key not in SERVER_STAMPED_CHAIN_KEYS
    }
    receipt.principal_chain = {
        **offered,
        **spec.receipt.principal_chain,
        "uploaded_by": ctx.audit_dict(),
    }
    spec.receipt = receipt
    if not spec.columns and envelope.kind == "rows":
        spec.columns = [ResultColumn(name=name) for name in envelope.columns]
    merged = {**spec.model_dump(mode="json"), **placement}
    try:
        updated = await object_service.apply_update(
            db, obj=obj, expected_version=obj.version, spec=merged, status="ready"
        )
    except object_service.VersionConflictError as exc:
        raise _version_conflict(exc) from exc
    await object_service.announce(db, obj=updated, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(updated)
    return WorkspaceObjectRead.model_validate(updated)


@router.post("/{object_id}/payload/failed", response_model=WorkspaceObjectRead)
async def fail_object_payload(
    request: Request,
    object_id: UUID,
    payload: ObjectPayloadFailure,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> WorkspaceObjectRead:
    """The machine says the payload behind a promoted result is not coming.

    Authorised exactly as the delivery is (``UPLOAD_PAYLOAD``): whoever may
    deliver the payload may say it will not arrive. Accepted only while the
    object is still waiting — a delivered result is not retro-failed, and a
    second refusal is a conflict rather than a rewrite of the first reason.

    Without this a promote the machine cannot honour left the object in
    ``pending_upload`` for ever: its page said "Saving…", the list said "0
    rows", and the reason lived only in the chat the reader had already left.
    """
    obj = await _load(db, object_id, type="result")
    await _decide(request, db, ctx, user, obj, Action.UPLOAD_PAYLOAD)
    if obj.status != PENDING_UPLOAD:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "payload_already_settled",
            "This result is no longer waiting for a payload",
        )
    spec = ResultSpec.model_validate(obj.spec or {})
    spec.failure_reason = payload.reason
    try:
        updated = await object_service.apply_update(
            db,
            obj=obj,
            expected_version=obj.version,
            spec=spec.model_dump(mode="json"),
            status="failed",
        )
    except object_service.VersionConflictError as exc:
        raise _version_conflict(exc) from exc
    await object_service.announce(db, obj=updated, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(updated)
    return WorkspaceObjectRead.model_validate(updated)


@router.post("/{object_id}/promote/retry", response_model=WorkspaceObjectRead)
async def retry_object_promote(
    request: Request,
    object_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> WorkspaceObjectRead:
    """Ask the machine for this result's payload again.

    A promote whose machine never answered ends as ``failed`` with a reason (the
    deadline sweep), and the reader is on the object's own page by then. Without
    this they would have to go back to a chat whose card may be pages up, or
    press Save on a card that now answers with the object they already have —
    the create is idempotent on ``promote:<chat>:<event>``, so a second promote
    lands on this very row and relays nothing.

    So the retry lives here: the object already knows the chat it came out of
    and the transcript event it came from, and the machine checks both before
    honouring the relay. Authorised as the delivery is (``UPLOAD_PAYLOAD``):
    whoever may deliver the payload may ask for it again. Accepted only on a
    FAILED result — one still waiting has an outstanding relay, and one that is
    ready has its rows.
    """
    obj = await _load(db, object_id, type="result")
    await _decide(request, db, ctx, user, obj, Action.WRITE)
    if obj.status != "failed":
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "promote_not_failed",
            "This result is not waiting to be saved again",
        )
    spec = ResultSpec.model_validate(obj.spec or {})
    if not spec.source_chat_id or not spec.source_event_id:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "promote_not_bound_to_chat",
            "This result does not name the chat it came from, so it cannot be saved again",
        )
    try:
        chat_id = UUID(spec.source_chat_id)
    except ValueError as exc:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "promote_not_bound_to_chat",
            "This result names no usable chat",
        ) from exc
    chat = await access.load_object(db, chat_id, type="chat")
    if chat is None:
        raise _refusal(
            status.HTTP_409_CONFLICT,
            "chat_gone",
            "The chat this result came from no longer exists",
        )
    # The relay goes to the chat that was DECIDED on, never to an id as written:
    # the chat is client-written into the spec, so sending into it is its own
    # decision, on record.
    await enforce(
        request,
        db,
        ctx,
        Action.SEND,
        access.object_resource(chat, type=ResourceType.CHAT),
        await access.chat_attrs(db, chat, await _reader(request, db, ctx, user)),
    )
    # Back to waiting, with the old reason cleared: a page that still showed the
    # last failure under a fresh "Saving…" would be reporting two states at once.
    spec.failure_reason = None
    try:
        updated = await object_service.apply_update(
            db,
            obj=obj,
            expected_version=obj.version,
            spec=spec.model_dump(mode="json"),
            status=PENDING_UPLOAD,
        )
    except object_service.VersionConflictError as exc:
        raise _version_conflict(exc) from exc
    await chat_service.broadcast_relay(
        db,
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        events=[
            PromoteRelay(object_id=str(updated.id), event_id=spec.source_event_id).model_dump(
                mode="json"
            )
        ],
        actor=ctx.audit_dict(),
    )
    await object_service.announce(db, obj=updated, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(updated)
    return WorkspaceObjectRead.model_validate(updated)


__all__ = ["router"]
