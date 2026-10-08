"""Chat templates: save a chat as the starting point for the next one.

A template answers a question the product kept asking people to answer twice:
"do that again, but for last quarter". The conversation that worked is saved —
the brief its author writes for whoever comes next, and the files it was working
on — and a new chat is started from it with those files already in place.

Every route decides through :func:`backend.authz.enforce` against
``chat_template.access``, so a refusal is on record and a template outside the
caller's audience is the opaque not-found a stranger gets rather than a 403 that
confirms the id. The listing is the one exception, and applies the same READ
predicate without writing a decision row per row.

Saving is the only route here that does real work, and it does it in an order
chosen so a failure cannot leave a half-template on screen: the chat is decided
READ, its working directory found, the copy and the destination decided as Files
actions, the room checked before any row exists, and only then are the row, its
folder and the copy written. A copy that fails after the row exists takes the
row with it — the template is tombstoned and the caller told the copy failed,
because a template whose folder is empty looks exactly like a chat that was
working on nothing.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from uuid import UUID, uuid4

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.config import settings
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.errors import FilesError, NotFound
from alkera_core.files.ids import NodeId
from alkera_core.models import WorkspaceObject
from alkera_core.schemas.objects import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ChatTemplateList,
    ChatTemplateRead,
    ChatTemplateUpdate,
    SaveAsTemplate,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import (
    Idempotency,
    as_platform,
    idempotent_route,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_nodes import _authorized_node as _decide_node
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services.audit import org_audit as org_audit_service
from backend.services.chats import templates as chat_template_service
from backend.services.files.home import caller_home
from backend.services.objects import object_service
from backend.services.sharing import access

router = APIRouter(prefix="/api/v1/chat-templates", tags=["chat-templates"])

#: What the route answers when the chat a save names has no working directory.
#: A chat saved before the working directory existed, or one whose folder was
#: trashed: there is nothing to copy, and an empty template would be a lie about
#: the conversation it came from.
NO_WORKING_DIRECTORY = "chat.no_working_directory"

#: What the route answers when the row was written and the copy then failed.
#: The row does not survive it — see the module docstring.
COPY_FAILED = "chat_template.copy_failed"


def _refusal(code: str, message: str, *, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def _reader(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> access.Reader:
    return await access.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )


async def _load(db: AsyncSession, template_id: UUID) -> WorkspaceObject:
    """The live template with this id, or the opaque not-found.

    Narrowed to the type: the id of a chat handed to this surface must not be
    answered about as though it were a template, and the caller learns nothing
    from the refusal either way.
    """
    found = await access.load_object(db, template_id, type=chat_template_service.TEMPLATE_TYPE)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return found


async def _decide(
    request: Request,
    db: AsyncSession,
    ctx: CurrentPrincipal,
    template: WorkspaceObject,
    action: Action,
    reader: access.Reader,
) -> None:
    await enforce(
        request,
        db,
        ctx,
        action,
        access.object_resource(template, type=ResourceType.CHAT_TEMPLATE),
        await chat_template_service.template_attrs(db, template, reader),
    )


@router.post(
    "",
    response_model=ChatTemplateRead,
    status_code=201,
    dependencies=[Depends(ratelimited("items"))],
)
@idempotent_route("chat_templates.create", status=201)
async def save_as_template(
    request: Request,
    payload: SaveAsTemplate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    files: FilesCtx,
    idempotency: Idempotency,
) -> ChatTemplateRead:
    """Save a chat as a template: its brief and the files it was working on.

    Everything but the chat is optional. The title is the chat's, the brief is a
    digest of its transcript, and the destination is the caller's own
    ``Chat Templates`` folder — made on this visit if this is their first
    template. A reader who may read the chat and copy its files may save one
    into their own drive: the template is theirs, its folder is theirs, and
    nothing of the source's sharing comes with it.
    """
    if not settings.files_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # `@idempotent_route` runs this whole body inside the Files claim's
    # transaction, so the tenant role is in force: the caller's teams, the chat
    # row and the decision this reads are all the platform's, and each window
    # below is exactly the statements that need it.
    async with as_platform(db):
        reader = await _reader(request, db, ctx, user)
        chat = await access.load_object(db, payload.source_chat_id, type="chat")
        if chat is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        await enforce(
            request,
            db,
            ctx,
            Action.READ,
            access.object_resource(chat, type=ResourceType.CHAT),
            await access.chat_attrs(db, chat, reader),
        )
    chat_node = await chat_template_service.node_of(files, chat.id)
    if chat_node is None:
        raise _refusal(
            NO_WORKING_DIRECTORY,
            "This chat has no files to save; there is nothing for a template to start from",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    source = await chat_template_service.source_files(files, chat_node)
    if source is None:
        raise _refusal(
            NO_WORKING_DIRECTORY,
            "This chat has no files to save; there is nothing for a template to start from",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    # Copying the source is what a template IS, so it is decided as a copy —
    # which is where a sealed source, a source under a no-reshare folder and a
    # read that came from an expiring grant are refused.
    await _decide_node(request, files, uuid.UUID(str(source.scratch.id)), FilesAction.COPY)
    home = await caller_home(files) if payload.destination_id is None else None
    if payload.destination_id is not None:
        destination = (
            await _decide_node(request, files, payload.destination_id, FilesAction.WRITE)
        ).node
    elif home is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    else:
        destination = await chat_template_service.default_destination(files, home)
        await _decide_node(request, files, uuid.UUID(str(destination.id)), FilesAction.WRITE)
    object_id = uuid4()
    async with as_platform(db):
        await enforce(
            request,
            db,
            ctx,
            Action.CREATE,
            Resource(ResourceType.CHAT_TEMPLATE, id=str(object_id), org_id=ctx.org_id),
            chat_template_service.new_template_attrs(reader),
        )
    await chat_template_service.assert_room_for(
        files, drive=uuid.UUID(str(files.drive.id)), source=source, destination=destination
    )
    # The transcript and the object row are the platform's tables; the node the
    # service projects for the row rides the same window because the two are one
    # unit of work.
    async with as_platform(db):
        digest, last_seq = await chat_template_service.transcript_brief(db, chat)
        brief = payload.brief if payload.brief is not None else digest
        title = (payload.title or chat.title or "").strip() or "Untitled template"
        template = await chat_template_service.create_template_object(
            db,
            files.repo,
            files.ctx,
            owner=user,
            title=title,
            spec=chat_template_service.spec_for(
                brief=brief, chat=chat, source_chat_id=chat.id, saved_from_seq=last_seq
            ),
            object_id=object_id,
            logical_id=payload.client_id,
            parent_id=NodeId(uuid.UUID(str(destination.id))),
        )
    node = await chat_template_service.node_of(files, template.id)
    if node is None:  # pragma: no cover - the bridge just made it
        raise NotFound()
    landing = await chat_template_service.working_folder_of(files, node)
    if landing is None:  # pragma: no cover - the bridge just made it
        raise NotFound()
    try:
        await chat_template_service.copy_into(files, source=source.scratch, destination=landing)
    except FilesError as failed:
        # The row exists and its folder does not hold what the template promises.
        # Taking the row away is the honest end: a template whose files are half
        # a chat's would start the next conversation from the wrong place.
        async with as_platform(db):
            await object_service.tombstone(db, template)
        await db.commit()
        raise _refusal(
            COPY_FAILED,
            "The template was saved but its files could not be copied; nothing was kept",
            status_code=status.HTTP_502_BAD_GATEWAY,
        ) from failed
    async with as_platform(db):
        await object_service.announce(db, obj=template, actor=ctx.audit_dict())
        await org_audit_service.record(
            db,
            org_id=template.org_team_id,
            actor=user,
            action="chat.template_saved",
            target=str(template.id),
            detail={
                "source_chat_id": str(chat.id),
                "files_node_id": str(node.id),
                "destination_id": str(destination.id),
            },
            acting=ctx,
        )
    await db.commit()
    await db.refresh(template)
    return chat_template_service.template_read(template, files_node_id=uuid.UUID(str(node.id)))


@router.get("", response_model=ChatTemplateList, dependencies=[Depends(ratelimited("items"))])
async def list_chat_templates(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> ChatTemplateList:
    """The templates this caller may read, newest first.

    Cut by the same predicate the policy decides through, without a decision row
    per row: a page of fifty templates must not put fifty rows on the audit lane.
    """
    reader = await _reader(request, db, ctx, user)

    async def _readable(rows: list[WorkspaceObject]) -> list[WorkspaceObject]:
        return [row for row in rows if access.may_read(row, reader)]

    page = await object_service.list_objects(
        db,
        org_team_id=ctx.org_id,
        type=chat_template_service.TEMPLATE_TYPE,
        limit=limit,
        cursor=cursor,
        cut=_readable,
    )
    kept = page.items
    nodes = await chat_template_service.node_ids_for(db, ctx, kept)
    await db.commit()
    return ChatTemplateList(
        items=[
            chat_template_service.template_read(row, files_node_id=nodes.get(row.id))
            for row in kept
        ],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{template_id}", response_model=ChatTemplateRead, dependencies=[Depends(ratelimited("items"))]
)
async def get_chat_template(
    request: Request,
    template_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> ChatTemplateRead:
    template = await _load(db, template_id)
    reader = await _reader(request, db, ctx, user)
    await _decide(request, db, ctx, template, Action.READ, reader)
    node_id = await chat_template_service.node_id_for(db, ctx, template)
    await db.commit()
    return chat_template_service.template_read(template, files_node_id=node_id)


@router.put(
    "/{template_id}", response_model=ChatTemplateRead, dependencies=[Depends(ratelimited("items"))]
)
async def update_chat_template(
    request: Request,
    template_id: UUID,
    payload: ChatTemplateUpdate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> ChatTemplateRead:
    """Edit the title or the brief, naming the version you read.

    The brief is the whole reason a template is not just a folder of files, and
    changing it speaks in the author's name to every chat started from here —
    so it takes the share ladder's edit rung, not merely the right to read. The
    folder's change token moves with the row, because a client watching the
    drive and one watching the template are looking at one edit.
    """
    template = await _load(db, template_id)
    reader = await _reader(request, db, ctx, user)
    await _decide(request, db, ctx, template, Action.WRITE, reader)
    spec = dict(template.spec or {})
    if payload.brief is not None:
        spec["brief"] = payload.brief
    try:
        updated = await object_service.apply_update(
            db,
            obj=template,
            expected_version=payload.expected_version,
            title=payload.title,
            spec=spec if payload.brief is not None else None,
        )
    except object_service.VersionConflictError as stale:
        raise _refusal(
            "version_conflict",
            "This template changed since you read it; read it again and retry",
            status_code=status.HTTP_409_CONFLICT,
        ) from stale
    await object_service.announce(db, obj=updated, actor=ctx.audit_dict())
    node_id = await chat_template_service.node_id_for(db, ctx, updated)
    await db.commit()
    await db.refresh(updated)
    return chat_template_service.template_read(updated, files_node_id=node_id)


@router.delete("/{template_id}", status_code=204, dependencies=[Depends(ratelimited("items"))])
async def delete_chat_template(
    request: Request,
    template_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> None:
    """Take the template away from everyone it was shared with.

    The owner's and the org admins': a reader holding even the top rung on the
    folder may remove their own copy, not the original everybody else starts
    from.
    """
    template = await _load(db, template_id)
    reader = await _reader(request, db, ctx, user)
    await _decide(request, db, ctx, template, Action.DELETE, reader)
    await object_service.tombstone(db, template)
    await org_audit_service.record(
        db,
        org_id=template.org_team_id,
        actor=user,
        action="chat.template_deleted",
        target=str(template.id),
        detail={},
        acting=ctx,
    )
    await db.commit()


__all__ = ["COPY_FAILED", "NO_WORKING_DIRECTORY", "router"]
