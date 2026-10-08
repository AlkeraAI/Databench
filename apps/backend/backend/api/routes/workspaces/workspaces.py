"""``/api/v1/workspaces``: start, list, open, rename and delete workspaces, and
list or start the chats in one.

Every route that decides access decides through :func:`backend.authz.enforce`
against ``workspace.access``: ``CREATE`` to start one, ``READ`` to open it or
list its chats, ``RENAME``, ``WRITE`` to start a chat in it, ``DELETE`` to end
it. A workspace in another org, or one nobody shared with the caller, is the
same opaque 404 a workspace that does not exist is. The listing applies the
policy's READ answer per row without writing a decision row per row.

Sharing a workspace is sharing its folder: :attr:`WorkspaceRead.files_node_id`
names it, and the Files permissions routes grant on it like on any folder. The
chats in the workspace sit under that folder (or, for a workspace of one, ARE
it), so the grant reaches them through the drive's own inheritance.

Starting a chat in an existing workspace waits on ``workspaces_multi_chat``:
the box still runs one sandbox per chat, so with the flag off a workspace holds
exactly one chat and a new chat gets a workspace of its own. A chat is started
in a workspace through ``POST /api/v1/chats`` naming its ``workspace_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid4

from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.authz.engine import authorize as decide_policy
from alkera_core.authz.policies import org_machine as org_machine_policy
from alkera_core.config import settings
from alkera_core.files.objects_bridge import (
    WORKSPACE_TYPE,
)
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.objects import chat_spares, workspace_end
from alkera_core.objects.workspaces import effective_binding, workspace_spec_of
from alkera_core.schemas.objects import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    ChatSessionList,
    ChatSpec,
    WorkspaceCreate,
    WorkspaceDeletion,
    WorkspaceList,
    WorkspaceRead,
    WorkspaceUpdate,
)
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status

from backend.api.deps.compute import refused
from backend.api.deps.workspace_access import decide_workspace as _decide
from backend.api.deps.workspace_access import workspace_reader as _reader
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services import audit, objects, sharing, workspaces
from backend.services import chats as chat_svc
from backend.services.compute import (
    ComputeRefusedError,
    chat_is_writable,
    load_org_machine,
    new_workspace_pin,
    org_machine_attrs,
    org_machine_audiences,
    org_machine_resource,
    org_machine_viewer,
)
from backend.services.infra import nudge_workspace_deletions
from backend.services.workspaces import (
    PROJECT_CAP_REACHED,
    ChatsSummary,
    MoveTarget,
    WorkspaceProjectCapError,
    admit_move,
    summarize,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

#: The refusal for a project workspace while a workspace holds one chat; the
#: same code the chat route gives a chat named into a workspace then.
MULTI_CHAT_OFF = "workspaces_multi_chat_disabled"


async def _load(db: DbSession, workspace_id: UUID) -> WorkspaceObject:
    workspace = await workspaces.load(db, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return workspace


def _may(
    ctx: CurrentPrincipal, workspace: WorkspaceObject, attrs: Mapping[str, object], action: Action
) -> bool:
    """The policy's answer for ``action`` without a decision row: a hint for
    drawing a control, never a grant."""
    return decide_policy(
        ctx, action, sharing.object_resource(workspace, type=ResourceType.WORKSPACE), attrs
    ).allowed


def _read(
    ctx: CurrentPrincipal,
    workspace: WorkspaceObject,
    attrs: Mapping[str, object],
    chats: ChatsSummary,
    nodes: Mapping[UUID, workspaces.WorkspaceNodes],
    progress: workspaces.WorkspaceProgress,
    unread: Mapping[UUID, int],
) -> WorkspaceRead:
    spec = workspace_spec_of(workspace.spec)
    binding = effective_binding(chats.specs, is_writable=chat_is_writable, spec=spec)
    node = nodes.get(workspace.id)
    if binding.machine_id:
        # The chats' stored status is the binding as it looked when it was
        # made; the machine has moved on since, and its own row says where to.
        binding = replace(
            binding,
            machine_status=progress.machines.status_of(
                ChatSpec(machine_id=binding.machine_id, mirror_state=binding.mirror_state)
            ),
        )
    title = workspace.title
    if spec.layout == "adopted" and chats.only_title is not None:
        # A workspace of one IS its chat to the person looking at it: the
        # chat's name is the one it answers to, however it was renamed.
        title = chats.only_title
    return WorkspaceRead(
        id=workspace.id,
        title=title,
        kind=spec.kind,
        layout=spec.layout,
        owner_user_id=workspace.owner_user_id,
        version=workspace.version,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        files_node_id=node.folder if node else None,
        files_drive_id=node.drive if node else None,
        working_node_id=node.working if node else None,
        adopted_chat_id=UUID(spec.adopted_chat_id) if spec.adopted_chat_id else None,
        chat_count=chats.count,
        unread_count=unread.get(workspace.id, 0),
        machine_id=binding.machine_id,
        machine_status=binding.machine_status,
        mirror_state=binding.mirror_state,
        wake_requested_at=chat_svc.instant(binding.wake_requested_at),
        writable=binding.writable,
        sandbox_state=binding.sandbox_state,
        sandbox_memory_used_mb=spec.sandbox_memory_used_mb,
        status=workspaces.workspace_status_fact(
            workspace.id, binding, chat_count=chats.count, node=node, progress=progress
        ),
        can_rename=_may(ctx, workspace, attrs, Action.RENAME),
        can_delete=_may(ctx, workspace, attrs, Action.DELETE),
        can_add_chat=settings.workspaces_multi_chat
        and spec.layout == "native"
        and _may(ctx, workspace, attrs, Action.WRITE),
    )


async def _one(
    db: DbSession, ctx: CurrentPrincipal, workspace: WorkspaceObject, attrs: Mapping[str, object]
) -> WorkspaceRead:
    chats = (await summarize(db, [workspace.id]))[workspace.id]
    nodes = await workspaces.nodes_for(db, ctx, [workspace])
    progress = await workspaces.gather_progress(
        db, org_id=workspace.org_team_id, workspace_ids=[workspace.id]
    )
    unread = await chat_svc.workspace_unread_counts(db, ctx, [workspace.id])
    return _read(ctx, workspace, attrs, chats, nodes, progress, unread)


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    request: Request,
    payload: WorkspaceCreate,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> WorkspaceRead:
    """Start a project workspace with a folder of its own. Creating with the
    same ``client_id`` twice returns the first one, decided as a READ. With no
    ``machine_pin`` sent it runs on the org's default machine for new
    workspaces when the caller may use it; a ``machine_pin`` sent, null
    included, is the caller's choice and wins."""
    reader = await _reader(request, db, ctx, user)
    if payload.client_id:
        existing = await objects.object_service.find_by_logical_id(
            db,
            org_team_id=ctx.org_id,
            namespace=DEFAULT_NAMESPACE,
            logical_id=payload.client_id,
        )
        if existing is not None and existing.type == WORKSPACE_TYPE:
            _refuse_a_different_replay(existing, payload, user)
            attrs = await _decide(request, db, ctx, reader, existing, Action.READ)
            await db.commit()
            return await _one(db, ctx, existing, attrs)
    if not settings.workspaces_multi_chat:
        # A project workspace exists to hold several chats, which no box serves
        # yet; a member's main workspace is still made on demand.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": MULTI_CHAT_OFF, "message": "Project workspaces aren't available yet."},
        )
    object_id = uuid4()
    await enforce(
        request,
        db,
        ctx,
        Action.CREATE,
        Resource(ResourceType.WORKSPACE, id=str(object_id), org_id=ctx.org_id),
        sharing.new_workspace_attrs(reader),
    )
    if "machine_pin" in payload.model_fields_set:
        # "Run on" was answered: a machine, or null for the default placement.
        pin = await _run_on(request, db, ctx, user, payload.machine_pin)
    else:
        pin = await new_workspace_pin(db, org_id=ctx.org_id, user_id=user.id)
    try:
        workspace, created = await workspaces.create_project(
            db,
            owner=user,
            org_id=ctx.org_id,
            title=payload.title,
            client_id=payload.client_id,
            object_id=object_id,
        )
    except workspaces.WorkspaceClientIdTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "client_id_in_use", "message": str(exc)},
        ) from exc
    except WorkspaceProjectCapError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": PROJECT_CAP_REACHED, "message": str(exc)},
        ) from exc
    except objects.object_service.LogicalIdRetiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "client_id_retired",
                "message": "This client_id belonged to a workspace that was deleted",
            },
        ) from exc
    if created:
        if pin is not None:
            # Born pinned: the first chat lands on the machine chosen.
            workspace.spec = (
                workspace_spec_of(workspace.spec)
                .model_copy(update={"machine_pin": str(pin)})
                .model_dump(mode="json")
            )
            await db.flush()
        await objects.object_service.announce(db, obj=workspace, actor=ctx.audit_dict())
    else:
        # A concurrent retry of the same client_id made it first.
        _refuse_a_different_replay(workspace, payload, user)
    await db.commit()
    await db.refresh(workspace)
    return await _one(db, ctx, workspace, await sharing.workspace_attrs(db, workspace, reader))


async def _run_on(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    machine_id: UUID | None,
) -> UUID | None:
    """The org machine a new workspace is to run on, decided like a move onto
    it: the caller may use it (``org_machine`` use, on record; a machine of
    another org, deleted or missing is its not-found) and, when it is not
    running, its funding can start it."""
    if machine_id is None:
        return None
    viewer = await org_machine_viewer(
        db, ctx=ctx, user=user, resolver=role_resolver(request, db, ctx)
    )
    row = await load_org_machine(db, org_id=viewer.org_id, machine_id=machine_id)
    grants = (
        await org_machine_audiences(db, org_id=viewer.org_id, machine_ids=[row.machine.id])
        if row is not None
        else {}
    )
    await enforce(
        request,
        db,
        ctx,
        Action.READ,
        org_machine_resource(
            row.machine if row is not None else None, viewer=viewer, id=str(machine_id)
        ),
        await org_machine_attrs(
            viewer, row.machine, grants[row.machine.id], purpose=org_machine_policy.USE_PURPOSE
        )
        if row is not None
        else {"in_org": False, "purpose": org_machine_policy.USE_PURPOSE},
    )
    assert row is not None  # a machine not found is never allowed
    try:
        await admit_move(db, ctx=ctx, target=MoveTarget(row=row, usable=True))
    except ComputeRefusedError as exc:
        raise refused(exc) from exc
    return row.machine.id


def _refuse_a_different_replay(
    workspace: WorkspaceObject, payload: WorkspaceCreate, user: User
) -> None:
    """A retried ``client_id`` is the same request from the same person; one
    naming another title, or coming from anyone else, is a different request
    reusing the id, and is never handed somebody else's workspace as the one
    it just made."""
    if workspace.owner_user_id != user.id or workspace.title != payload.title:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "client_id_in_use",
                "message": "This client_id already made a workspace with another title",
            },
        )


@router.get("", response_model=WorkspaceList)
async def list_workspaces(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    cursor: str | None = None,
) -> WorkspaceList:
    """The workspaces the caller may read, newest first: their own and the
    ones shared with them. A workspace whose every chat is a spare nobody has
    claimed is not shown, the way the spare itself is not."""
    reader = await _reader(request, db, ctx, user)
    facts_of: dict[UUID, dict[str, object]] = {}
    chats_of: dict[UUID, ChatsSummary] = {}

    async def _readable(rows: list[WorkspaceObject]) -> list[WorkspaceObject]:
        readable = await sharing.readable_workspace_facts(db, rows, reader)
        chats = await summarize(db, [ws.id for ws, _ in readable])
        kept = []
        for workspace, facts in readable:
            held = chats[workspace.id]
            if held.all_spare:
                continue
            if held.empty and workspaces.is_adopted(workspace):
                # A workspace of one whose chat a previous build ended without
                # it: not shown, and retired by the reconcile pass.
                continue
            facts_of[workspace.id] = facts
            chats_of[workspace.id] = held
            kept.append(workspace)
        return kept

    page = await objects.object_service.list_objects(
        db,
        org_team_id=ctx.org_id,
        type=WORKSPACE_TYPE,
        limit=limit,
        cursor=cursor,
        cut=_readable,
    )
    nodes = await workspaces.nodes_for(db, ctx, page.items)
    progress = await workspaces.gather_progress(
        db, org_id=ctx.org_id, workspace_ids=[ws.id for ws in page.items]
    )
    unread = await chat_svc.workspace_unread_counts(db, ctx, [ws.id for ws in page.items])
    return WorkspaceList(
        items=[
            _read(
                ctx,
                ws,
                facts_of[ws.id],
                chats_of.get(ws.id, ChatsSummary()),
                nodes,
                progress,
                unread,
            )
            for ws in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/main", response_model=WorkspaceRead)
async def get_main_workspace(
    request: Request, user: CurrentUser, db: DbSession, ctx: CurrentPrincipal
) -> WorkspaceRead:
    """The caller's main workspace, made now if they have none: where a chat
    with no place named lands once a workspace may hold several chats.
    Making it is decided as a ``CREATE``; reading one that exists as a
    ``READ``."""
    reader = await _reader(request, db, ctx, user)
    workspace = await workspaces.find_main(db, owner=user, org_id=ctx.org_id)
    if workspace is None:
        await enforce(
            request,
            db,
            ctx,
            Action.CREATE,
            Resource(ResourceType.WORKSPACE, id=f"main:{user.id}", org_id=ctx.org_id),
            {**sharing.new_workspace_attrs(reader), "is_main": True},
        )
        workspace, made = await workspaces.ensure_main(db, owner=user, org_id=ctx.org_id)
        if made:
            await objects.object_service.announce(db, obj=workspace, actor=ctx.audit_dict())
        await db.commit()
        await db.refresh(workspace)
        attrs = await sharing.workspace_attrs(db, workspace, reader)
    else:
        attrs = await _decide(request, db, ctx, reader, workspace, Action.READ)
        await db.commit()
    return await _one(db, ctx, workspace, attrs)


@router.get("/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace(
    request: Request, workspace_id: UUID, user: CurrentUser, db: DbSession, ctx: CurrentPrincipal
) -> WorkspaceRead:
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    attrs = await _decide(request, db, ctx, reader, workspace, Action.READ)
    await db.commit()
    return await _one(db, ctx, workspace, attrs)


@router.patch("/{workspace_id}", response_model=WorkspaceRead)
async def rename_workspace(
    request: Request,
    workspace_id: UUID,
    payload: WorkspaceUpdate,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> WorkspaceRead:
    """Rename, naming the version read. A workspace of one is renamed with its
    chat: to the person looking at it they are one thing."""
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    attrs = await _decide(request, db, ctx, reader, workspace, Action.RENAME)
    try:
        updated = await workspaces.rename(
            db, workspace=workspace, title=payload.title, expected_version=payload.expected_version
        )
    except objects.object_service.VersionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "version_conflict",
                "message": "This workspace moved on; re-read it and try again",
                "current_version": exc.current.version,
            },
        ) from exc
    await objects.object_service.announce(db, obj=updated, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(updated)
    return await _one(db, ctx, updated, attrs)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    request: Request,
    workspace_id: UUID,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    background_tasks: BackgroundTasks,
) -> Response:
    """End a workspace and every chat in it: the owner's or an org admin's. A
    main workspace is not deleted.

    The workspace and its chats are gone from every read once this answers,
    and every lease under its folder is ended. Each chat then finishes ending
    the way a deleted chat does (its box told, its folder trashed), and the
    workspace's own folder goes to the trash, in the background:
    ``GET /{workspace_id}/deletion`` says how far that has got."""
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    facts = await _decide(request, db, ctx, reader, workspace, Action.DELETE)
    gone = await workspaces.end(db, workspace=workspace, actor=ctx.audit_dict())
    if gone is None:
        # Another request ended it first: this one ended nothing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if facts["owner_departed"] is True:
        await audit.record_offboarding(db, obj=workspace, by=user, acting=ctx)
    await db.commit()
    if workspace_end.is_ending(gone):
        background_tasks.add_task(nudge_workspace_deletions)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workspace_id}/deletion", response_model=WorkspaceDeletion)
async def get_workspace_deletion(
    request: Request, workspace_id: UUID, user: CurrentUser, db: DbSession, ctx: CurrentPrincipal
) -> WorkspaceDeletion:
    """Where a deleted workspace's deletion stands: ``deleting`` while its
    chats are still being ended and its folder trashed, with how many chats
    remain, then ``deleted``. Decided as the ``DELETE`` that started it, so
    whoever could delete the workspace can follow it; a workspace that was
    never deleted is not found here."""
    workspace = await workspaces.load_ended(db, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    reader = await _reader(request, db, ctx, user)
    await _decide(request, db, ctx, reader, workspace, Action.DELETE)
    remaining = await workspace_end.chats_remaining(db, workspace.id)
    await db.commit()
    return WorkspaceDeletion(
        id=workspace.id,
        state="deleting" if workspace_end.is_ending(workspace) else "deleted",
        chats_remaining=remaining,
    )


@router.get("/{workspace_id}/chats", response_model=ChatSessionList)
async def list_workspace_chats(
    request: Request,
    workspace_id: UUID,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    cursor: str | None = None,
) -> ChatSessionList:
    """The chats in a workspace the caller may read, oldest first, a page at a
    time. Each is decided by the chat's own read, as on the chat rail: reading
    a workspace shows the chats it holds that its sharing reaches. A page is
    cut by that read after it is taken, so it may hold fewer than ``limit``;
    ``next_cursor`` says whether more follow."""
    workspace = await _load(db, workspace_id)
    reader = await _reader(request, db, ctx, user)
    await _decide(request, db, ctx, reader, workspace, Action.READ)
    scope = objects.object_service.cursor_scope(
        org_team_id=workspace.org_team_id, type=f"chat@{workspace.id}"
    )
    after = objects.object_service.decode_cursor(cursor, scope=scope) if cursor else None
    chats, more = await workspaces.chats_page(db, workspace.id, limit=limit, after=after)
    readable = [
        (chat, facts)
        for chat, facts in await sharing.readable_chat_facts(db, chats, reader)
        if not chat_spares.is_spare(chat)
    ]
    items = await chat_svc.chat_list_items(db, ctx, readable)
    await db.commit()
    return ChatSessionList(
        items=items,
        next_cursor=objects.object_service.encode_cursor(chats[-1], scope=scope) if more else None,
    )
