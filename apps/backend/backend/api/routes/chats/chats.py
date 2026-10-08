"""Cloud chat: sessions in Postgres, a paged transcript, and the relay onto the
chat document.

The browser talks to these routes; the machine that runs the agent talks to the
same chat over the realtime socket. Both meet on ``doc:chat:<chat_id>``: a
message posted here is recorded and then relayed onto that channel, so the
daemon sees a person speak whether they typed it into an open socket or posted
it over HTTP.

The chat row this surface creates is also the chat's DECLARATION. The socket
resolves ``doc:chat:<id>`` through it (:mod:`backend.services.realtime.chat_lookup`),
so a chat exists to be subscribed to exactly when it was created here — never
by being asked about — and the audience the socket admits is the audience the
policy below admits, read from one row.

Every route that decides access decides through :func:`backend.authz.enforce`
against ``chat.access`` — ``CREATE`` to start a chat, ``READ`` to open it or
page its transcript, ``SEND`` to speak in it, ``PROMOTE`` to pin a result out of
it, ``DELETE`` to take it out of the workspace — so a refusal is on record and a
chat in another org is an opaque 404 rather than a distinguishable 403. The
listing is the exception and
applies the SAME audience predicate the policy decides through, without
writing a decision row per row — a list of fifty chats must not put fifty rows
on the audit lane.

Nothing here returns a credential. A chat names a machine by id and status; a
receipt names a connection and a role. There is no field a secret fits in.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from alkera_core.auth import GatewayTokenClaims
from alkera_core.authz import Action, Resource, ResourceType
from alkera_core.authz.chat_scope import SCOPE_PRIVATE
from alkera_core.config import settings
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Read
from alkera_core.files.authz.readable import readable_ids
from alkera_core.files.copy import run_copy, start_copy
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.objects_bridge import (
    CHAT_TEMPLATE_TYPE,
    link_attachment,
    live_node_for,
    unlink_attachment,
    working_folder_node,
)
from alkera_core.files.ops import Operations
from alkera_core.files.quota import QuotaService
from alkera_core.files.repo import FilesRepo, id_batches
from alkera_core.machine_refusals import MACHINE_CREDENTIAL_REFUSED
from alkera_core.models import ChatMessage, User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_core.objects import chat_end, chat_spares
from alkera_core.observability.asgi import names_body_errors
from alkera_core.permission_presentation import MODE_RANK
from alkera_core.schemas.chat import STANDING_OPTIONS, FilePart
from alkera_core.schemas.me_chat import ChatModelOptions
from alkera_core.schemas.objects import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_MESSAGE_LIMIT,
    MAX_LIST_LIMIT,
    MAX_MESSAGE_LIMIT,
    AnswerRelay,
    ChartSpecError,
    ChatAttachmentCreate,
    ChatAttachmentList,
    ChatAttachmentRead,
    ChatCreate,
    ChatGatewayTokenRead,
    ChatInterruptAnswer,
    ChatMessageCreate,
    ChatMessageList,
    ChatMessageRead,
    ChatModelPin,
    ChatModelUpdate,
    ChatPermissionModeUpdate,
    ChatPromoteRequest,
    ChatPublisherStateUpdate,
    ChatSendAdmission,
    ChatSessionList,
    ChatSessionRead,
    ChatSpareState,
    ChatTemplateSpec,
    CloudPermissionMode,
    ModelRelay,
    ModeRelay,
    PromoteRelay,
    Receipt,
    ResultColumn,
    ResultSpec,
    StopRelay,
    WorkspaceObjectRead,
    unbound_chart_fields,
    validate_chart_spec,
)
from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from backend.api.deps.chat_access import decide_chat as _decide
from backend.api.deps.chat_access import load_chat as _load_chat
from backend.api.deps.chat_pins import resolve_model_pin, saved_default_pin, template_pin
from backend.api.deps.chat_placement import placed
from backend.api.deps.chat_publisher import chat_reader, enforce_publisher
from backend.api.deps.files import (
    FilesEnabled,
    as_platform,
    require_files_enabled,
)
from backend.api.deps.files_context import FilesCtx, files_context
from backend.api.deps.files_facts import facts_for
from backend.api.deps.files_nodes import _authorized_node
from backend.auth import membership_tokens
from backend.auth.dependencies import (
    CurrentPrincipal,
    CurrentUser,
    DbSession,
    PrincipalUser,
    optional_session_claims,
)
from backend.authz import OutboxDecisionSink, enforce
from backend.services import workspaces
from backend.services.audit import org_audit as org_audit_service
from backend.services.chats import (
    MAX_MESSAGE_SEQ,
    ClientIdReusedError,
    ModelSwitchRefusedError,
    chat_list_items,
    chat_service,
    decode_attachment_cursor,
    encode_attachment_cursor,
    may_send,
    mode_change,
    model_options,
    names_another_workspace,
    owner_names,
    plan_switch,
    sandbox_limits,
    turn_admission,
)
from backend.services.chats import (
    caps as _caps,
)
from backend.services.chats import (
    chat_nodes as _chat_nodes,
)
from backend.services.chats import (
    chat_read as _chat_read,
)
from backend.services.chats import (
    links as _links,
)
from backend.services.chats import spares as spare_service
from backend.services.compute.placement import (
    UNSERVEABLE,
    bound_machine_state,
    live_machines,
    rebind_if_stranded,
)
from backend.services.credentials import machine_credentials as machine_credential_service
from backend.services.files.context import FilesContext, build_files_context
from backend.services.objects import object_service
from backend.services.org import preferences as preferences_service
from backend.services.realtime import docsync
from backend.services.sharing import access

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])


def _message_read(message: ChatMessage) -> ChatMessageRead:
    return ChatMessageRead.model_validate(message)


async def _pin_legacy_chats(
    db: DbSession, ctx: CurrentPrincipal, user: CurrentUser, chats: Sequence[WorkspaceObject]
) -> list[WorkspaceObject]:
    """A chat from before every row carried a model is pinned on its first read.

    The box runs a chat only on the model its row names and has no default of
    its own, so a row created before the pin existed would sit with every turn
    refused until somebody moved it onto a model by hand. Instead the first
    read — a person opening the chat, or the box discovering it — resolves the
    pin exactly the way a create that named none does: the OWNER's saved
    default against the live catalog, else the hosted default, else the
    catalog's first model. The owner's, not the reader's: a chat shared with a
    member must not be moved onto that member's default the day they open it.

    Persisted on the row (what the box reads at open) and relayed (what a box
    already running the chat hears in time for the next turn). A catalog that
    cannot be reached or offers nothing leaves the row as it was: a read is
    not the place to refuse, the chat stays readable, and the picker remains
    the way out — the box refuses the turn with the reason rendered in the
    chat. Returns the rows written, for the caller to commit and refresh.
    """
    unpinned = [chat for chat in chats if chat_service.chat_spec_of(chat).model is None]
    if not unpinned:
        return []
    # Per owner AND org: a box's page spans orgs, and an owner's default in one
    # org is not a model another org's catalog serves.
    pins: dict[tuple[UUID, UUID], ChatModelPin | None] = {}
    written: list[WorkspaceObject] = []
    for chat in unpinned:
        key = (chat.owner_user_id, chat.org_team_id)
        if key not in pins:
            owner = user if chat.owner_user_id == user.id else await db.get(User, key[0])
            pins[key] = (
                await saved_default_pin(db, owner, org_id=chat.org_team_id, chat_id=chat.id)
                if owner is not None
                else None
            )
        pin = pins[key]
        if pin is None:
            continue
        chat = await chat_service.set_model(db, chat=chat, pin=pin)
        await chat_service.broadcast_relay(
            db,
            org_team_id=chat.org_team_id,
            chat_id=chat.id,
            events=[
                ModelRelay(pin=pin.model_dump(mode="json"), user_id=str(user.id)).model_dump(
                    mode="json"
                )
            ],
            actor=ctx.audit_dict(),
        )
        written.append(chat)
    return written


async def _starting_mode(
    db: DbSession, user: CurrentUser, org_id: UUID, picked: CloudPermissionMode | None = None
) -> CloudPermissionMode:
    """The stance a new chat opens in: the reader's pick, else the one every
    cloud door resolves.

    A pick wins outright — it is this chat's stance, chosen for this chat, and
    a preference is only ever the seed for one nobody chose. It needs no
    narrowing of its own because the body cannot spell a stance the route
    refuses: ``CloudPermissionMode`` is the same set the dedicated route takes.

    Absent a pick, the answer comes from
    :func:`preferences_service.starting_cloud_mode` rather than from this
    module, so a chat a Slack mention opens starts in the same stance as one
    opened here.
    """
    if picked is not None:
        return picked
    return await preferences_service.starting_cloud_mode(db, user_id=user.id, org_id=org_id)


def _require_chart_bound(chart: dict[str, object], keys: list[str]) -> None:
    """A chart names column KEYS; a field the promoted result does not declare
    is refused with the channel named.

    The same refusal the objects routes make on a create or an edit — a chart
    that reaches the store unbound draws nothing, and a promote that skipped
    this check would be the way around it. Nothing to bind against yet — a
    promote that declares no columns — binds trivially: the upload settles the
    columns, and refusing here would strand the ordinary save.
    """
    if not keys:
        return
    unbound = unbound_chart_fields(chart, keys)
    if unbound:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "chart_field_unbound",
                "message": f"{unbound[0]} names a field that is not a column of this result",
                "field": unbound[0],
            },
        )


@router.get("", response_model=ChatSessionList)
async def list_chats(
    request: Request,
    user: PrincipalUser,
    db: DbSession,
    ctx: CurrentPrincipal,
    limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    cursor: str | None = None,
) -> ChatSessionList:
    """The org's chats, newest first, cut to the ones the caller may read: their
    own, the ones shared with them, and — for a box — the ones bound to it.

    A box on its own machine credential has no org to list. It lists the chats
    bound to it, in every org it serves: that listing is how it discovers the
    turns it owes, and each row is admitted by the same decision the chat's
    own route makes, the tenancy floor included."""
    reader = await chat_reader(request, db, ctx, user)
    facts_of: dict[UUID, dict[str, object]] = {}

    async def _readable(rows: list[WorkspaceObject]) -> list[WorkspaceObject]:
        kept = []
        for chat, facts in await access.readable_chat_facts(db, rows, reader):
            # A spare (made ahead of its owner's first message) is no one's chat
            # until that send claims it, its box's included, or the box would
            # start an agent for it. Cut before paging so no cursor names one.
            if chat_spares.is_spare(chat):
                continue
            facts_of[chat.id] = facts
            kept.append(chat)
        return kept

    # A cursor this listing did not mint answers 422 from the codec itself, so
    # every listing that pages inherits the refusal instead of translating it.
    if user is None:
        page = await object_service.list_chats_on_machine(
            db, machine_id=ctx.acting_principal.id, limit=limit, cursor=cursor, cut=_readable
        )
    else:
        page = await object_service.list_objects(
            db,
            org_team_id=ctx.org_id,
            type="chat",
            limit=limit,
            cursor=cursor,
            cut=_readable,
        )
    visible = page.items
    # The list is how the box discovers the chats bound to it, so a row from
    # before every chat carried a model is pinned here as well as on a read —
    # by a person's read; a box on its credential discovers and pins nothing,
    # the same as its read of one chat. Strays heal first, before this request writes a row.
    pinned = await workspaces.adopt_strays(db, visible, announce_as=ctx.audit_dict())
    pinned += await _pin_legacy_chats(db, ctx, user, visible) if user is not None else []
    if pinned:
        await db.commit()
        for chat in pinned:
            await db.refresh(chat)
    return ChatSessionList(
        items=await chat_list_items(db, ctx, [(chat, facts_of[chat.id]) for chat in visible]),
        next_cursor=page.next_cursor,
    )


async def _workspace_taking_a_chat(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    reader: access.Reader,
    workspace_id: UUID | None,
) -> WorkspaceObject | None:
    """The workspace a new chat is started in, decided as ``WRITE`` on it (the
    chat drives an agent on the workspace owner's connections). Refused while a
    workspace holds one chat (``workspaces_multi_chat`` off: the box runs one
    sandbox per chat), and for a workspace of one adopted for an existing chat."""
    if workspace_id is None:
        return None
    workspace = await workspaces.load(db, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    resource = access.object_resource(workspace, type=ResourceType.WORKSPACE)
    attrs = await access.workspace_attrs(db, workspace, reader)
    await enforce(request, db, ctx, Action.WRITE, resource, attrs)
    if not settings.workspaces_multi_chat or workspaces.is_adopted(workspace):
        await db.commit()
        code = "workspace_holds_one_chat" if settings.workspaces_multi_chat else MULTI_OFF
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": code, "message": "This workspace holds one chat; start a new one"},
        )
    return workspace


#: The refusal for a chat named into a workspace while workspaces hold one chat.
MULTI_OFF = "workspaces_multi_chat_disabled"


@router.post("", response_model=ChatSessionRead, status_code=status.HTTP_201_CREATED)
async def create_chat(
    request: Request,
    payload: ChatCreate,
    user: CurrentUser,
    db: DbSession,
    ctx: CurrentPrincipal,
) -> ChatSessionRead:
    """Open a conversation and bind it to a machine.

    No machine is a state, not a failure: the chat is created with
    ``machine_status="none"`` and the UI says so. Creating with the same
    ``client_id`` twice returns the first chat rather than a second one.

    The decision is filed under the id the chat will have — the row a retried
    ``client_id`` already made, or the one minted here — so the audit trail
    for a chat starts at its creation and never at a placeholder.

    The chat is created in a workspace: the one ``workspace_id`` names (see
    :func:`_workspace_taking_a_chat`), else a workspace of its own while a
    workspace holds one chat, else its owner's main workspace.
    """
    reader = await chat_reader(request, db, ctx, user)
    workspace = await _workspace_taking_a_chat(request, db, ctx, reader, payload.workspace_id)
    if payload.client_id:
        # A retried client_id is a READ of the chat it already made, and is
        # decided as one: the lookup is scoped to chats (a saved query's own
        # id is never handed back dressed as a chat) and to LIVE ones (a deleted
        # chat is not handed back at all), and a member reusing the id of a chat
        # outside their audience gets the policy's opaque not-found, never the
        # chat. An id a deleted chat still holds cannot be re-used either, so the
        # create below answers 409 rather than resurrecting the tombstone.
        existing = await chat_service.find_chat_by_client_id(
            db, org_team_id=ctx.org_id, client_id=payload.client_id
        )
        # A replay naming another workspace falls through to the create's refusal.
        if existing is not None and not names_another_workspace(existing, workspace):
            facts = await access.chat_attrs(db, existing, reader)
            await enforce(
                request,
                db,
                ctx,
                Action.READ,
                access.object_resource(existing, type=ResourceType.CHAT),
                facts,
            )
            await db.commit()
            return _chat_read(
                existing,
                await live_machines(db, org_id=ctx.org_id),
                await _chat_nodes(db, ctx, [existing]),
                limits=await sandbox_limits(db, existing),
                **_caps(ctx, existing, facts),
                **await _links(db, existing.id),
            )
    if payload.claim_spare and not payload.source_node_id and workspace is None:
        claimed = await _claim_spare(request, db, ctx, user, reader, payload)
        if claimed is not None:
            return claimed
    object_id = uuid4()
    await enforce(
        request,
        db,
        ctx,
        Action.CREATE,
        Resource(ResourceType.CHAT, id=str(object_id), org_id=ctx.org_id),
        access.new_chat_attrs(reader),
    )
    template = (
        await _authorized_source(request, db, ctx, payload.source_node_id)
        if payload.source_node_id
        else None
    )
    title = payload.title
    brief: str | None = None
    if template is None:
        pin = await resolve_model_pin(
            db, user, org_id=ctx.org_id, model_id=payload.model, effort=payload.effort
        )
        mode = await _starting_mode(db, user, ctx.org_id, payload.permission_mode)
    else:
        template_spec = ChatTemplateSpec.model_validate(template.spec or {})
        brief = template_spec.brief
        if payload.model:
            pin = await resolve_model_pin(
                db, user, org_id=ctx.org_id, model_id=payload.model, effort=payload.effort
            )
        else:
            pin = await template_pin(db, user, template_spec, org_id=ctx.org_id)
        mode = _narrowed_mode(
            await _starting_mode(db, user, ctx.org_id, payload.permission_mode),
            template_spec.permission_mode,
        )
        # The template's title is only a DEFAULT: a caller who named the chat
        # named it, and a template does not get to rename somebody's chat.
        title = title or template.title
    # Placement runs once the stance is known: a writable chat is never bound to
    # a non-gVisor pool box (the sandbox guard reads the resolved permission mode).
    binding = await placed(db, ctx=ctx, user=user, mode=mode, workspace=workspace)
    try:
        chat, created = await chat_service.create_chat(
            db,
            owner=user,
            org_id=ctx.org_id,
            title=title,
            client_id=payload.client_id,
            machine_id=str(binding.machine_id) if binding else None,
            machine_status=binding.chat_status if binding else "none",
            model=pin,
            permission_mode=mode,
            object_id=object_id,
            source_node_id=payload.source_node_id,
            source_object_id=UUID(str(template.id)) if template is not None else None,
            template_brief=brief,
            workspace=workspace,
        )
    except workspaces.WorkspaceGoneError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    except workspaces.WorkspaceFolderMissingError as exc:
        # The main workspace's folder, or its ``.chats``, is in the trash: the
        # chat has nowhere to be filed until it is restored.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "workspace_folder_missing", "message": str(exc)},
        ) from exc
    except chat_service.ClientIdTakenError as exc:
        # The same refusal ``POST /objects`` gives a query that names a chat's
        # id: one code for one condition, whichever route meets it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "client_id_in_use", "message": str(exc)},
        ) from exc
    except object_service.LogicalIdRetiredError as exc:
        # The id belonged to a chat that was deleted. The tombstone still holds it
        # (the unique constraint is not partial), so the create cannot succeed and
        # the deleted chat must not be handed back as a live one — the same answer
        # the objects surface gives, under the same code.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "client_id_retired",
                "message": "This client_id belonged to a chat that was deleted; start a new one",
            },
        ) from exc
    if created and template is not None and payload.source_node_id is not None:
        # Whether the brief can be said at all is settled first: the copy below
        # commits as it goes, so a refusal after it would leave a started chat
        # holding the files with nothing said in it.
        _refuse_a_brief_that_cannot_be_said(chat, brief or "")
        # The files BEFORE the doorbell: the announce is what puts the chat on
        # the page and wakes a box for it, and a box that opens the chat while
        # the copy is still running finds a working directory missing the very
        # files the template exists to hand it.
        await _copy_template_files(
            request,
            db,
            ctx,
            user,
            chat=chat,
            template=template,
            template_node_id=payload.source_node_id,
        )
        # AFTER the files, for the reason the copy runs before the doorbell: the
        # brief is the instruction to get on with it, and an agent that reads it
        # before the working directory exists starts on files that are not there.
        await _post_template_brief(
            db,
            ctx,
            user,
            chat=chat,
            template=template,
            template_node_id=payload.source_node_id,
            brief=brief or "",
        )
    if created:
        await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(chat)
    return _chat_read(
        chat,
        await live_machines(db, org_id=chat.org_team_id),
        await _chat_nodes(db, ctx, [chat]),
        limits=await sandbox_limits(db, chat),
        **_caps(ctx, chat, await access.chat_attrs(db, chat, reader)),
        **await _links(db, chat.id),
    )


async def _copy_template_files(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    *,
    chat: WorkspaceObject,
    template: WorkspaceObject,
    template_node_id: UUID,
) -> None:
    """Put the template's files into the new chat's working directory.

    The CHILDREN of the template's ``scratch`` are copied, not ``scratch``
    itself: the chat already has its own, minted with the chat's folder, and
    nesting one inside the other would give the box a working directory whose
    top level is a folder called ``scratch``.

    Decided twice, because two different things are happening: COPY on the
    template's files (may this caller take these bytes away) and WRITE on the
    chat's working directory (may they put bytes here). The quota is asserted
    before the first row is written, so a drive that is full refuses the copy
    instead of half-doing it.

    A copy that fails leaves no half-built chat behind: the chat is tombstoned
    and the caller is told the copy failed, because a chat whose working
    directory holds some of a template's files is worse than no chat at all —
    the agent would run on a template it cannot see is incomplete.
    """
    if not settings.files_enabled:
        return
    files = await build_files_context(db, ctx)
    try:
        async with files.repo.transaction():
            template_node = await files.repo.node(NodeId(template_node_id))
            chat_node = await live_node_for(files.repo, chat.id)
            if template_node is None or chat_node is None:
                raise _TemplateCopyError("the template or the chat has no folder")
            template_scratch = await working_folder_node(files.repo, template_node)
            chat_scratch = await working_folder_node(files.repo, chat_node)
            if template_scratch is None:
                raise _TemplateCopyError("the template has no working directory")
            if chat_scratch is None:
                raise _TemplateCopyError("the chat has no working directory")
        await _authorized_node(request, files, UUID(str(template_scratch.id)), FilesAction.COPY)
        await _authorized_node(request, files, UUID(str(chat_scratch.id)), FilesAction.WRITE)
        async with files.repo.transaction():
            carried = [
                row
                for row in await files.repo.nodes_by_path_prefix(template_scratch.path_ids)
                if row.trashed_at is None and row.id != template_scratch.id
            ]
            children = [row for row in carried if row.parent_id == template_scratch.id]
            await QuotaService(
                files.repo, files.ctx, files.clock, files.store, ceilings=files.ceilings
            ).assert_room(
                DriveId(UUID(str(chat_scratch.drive_id))),
                bytes=sum(row.size for row in carried if row.kind == "file"),
                nodes=len(carried),
                parent_path=chat_scratch.path_ids,
            )
        for child in children:
            async with files.repo.transaction():
                op_id = await start_copy(
                    files.repo,
                    Operations(files.repo, files.ctx, files.clock, files.store),
                    node=child,
                    dest_parent=chat_scratch,
                )
            await run_copy(files.repo, files.ctx, op_id, clock=files.clock)
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        await _tombstone_started_chat(db, chat_id=chat.id)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "chat.template_copy_failed",
                "message": "The template's files could not be copied; the chat was not started",
            },
        ) from exc
    await org_audit_service.record(
        db,
        org_id=chat.org_team_id,
        actor=user,
        action="chat.started_from_template",
        target=str(chat.id),
        detail={
            "template_object_id": str(template.id),
            "template_node_id": str(template_node_id),
        },
        acting=ctx,
    )


#: What a chat's first message says about itself when nobody typed it: the
#: template's brief, posted by the server so the agent starts on it. Spelled
#: once here, and read by the reader that captions the bubble.
TEMPLATE_BRIEF_SOURCE = "template_brief"


async def _template_author(db: DbSession, template: WorkspaceObject) -> str:
    """Who wrote the brief, for the caption on the bubble it becomes.

    Their name, or their email when they never filled one in — the caption's
    job is to tell the reader these words are somebody else's, and an empty
    name tells them nothing. Read once, at creation, and copied onto the
    message like the brief itself: the author may later leave the org, and the
    record of who wrote the chat's first prompt should not change when they do.
    """
    owner = await db.get(User, template.owner_user_id)
    if owner is None:
        return ""
    return owner.display_name or owner.email


def _refuse_a_brief_that_cannot_be_said(chat: WorkspaceObject, brief: str) -> None:
    """Refuse an over-long brief BEFORE the template's files are copied.

    The copy runs on the Files repository's own transactions, which commit as
    they go — so by the time the brief is posted there is no longer a single
    rollback that can take the whole create back. Asked here instead, the
    refusal costs nothing: no files have moved, and the chat row dies with the
    request's own rollback.

    Unreachable from a template the API wrote (``ChatTemplateSpec`` caps the
    brief far below the transcript's ceiling). It is asked anyway because the
    spec is a JSON blob a migration or a hand-written row can also set, and the
    failure it would otherwise cause is the expensive kind: a started chat,
    holding a copy of the template's files, whose brief never arrived.
    """
    if not brief.strip():
        return
    try:
        chat_service.check_user_message_fits(
            text=brief, user_id=chat.owner_user_id, client_id=f"template-brief-{chat.id}"
        )
    except chat_service.ChatMessageTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "chat.template_brief_too_large", "message": str(exc)},
        ) from exc


async def _post_template_brief(
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    *,
    chat: WorkspaceObject,
    template: WorkspaceObject,
    template_node_id: UUID,
    brief: str,
) -> None:
    """Send the template's brief as the chat's first message.

    A template that carries no brief is a folder of files and nothing is sent —
    the chat opens silent, exactly as it did before. A brief that has words is
    the starter speaking: it is recorded as THEIR message, through the very
    function ``POST /chats/{id}/messages`` calls, so the box runs the turn it
    would run for anything they typed and the agent asks them what should
    differ this time. What it is not is a system preamble: a hidden instruction
    the reader cannot see is one they cannot edit, re-send, or argue with.

    The words are the template author's, and the reader is told so by the
    bubble's caption rather than by the words — which is why the provenance
    rides the record's metadata instead of being pasted into the text. The
    author is named there too: on a SHARED template the person who wrote the
    brief is not the person starting the chat, and a first prompt that appears
    to be the starter's own is the one thing the caption exists to prevent.

    Whether the brief fits was settled before the files were copied
    (:func:`_refuse_a_brief_that_cannot_be_said`), so there is no refusal left
    to make here: the Files copy commits on its own transactions, and a refusal
    raised at this point would leave a started chat, holding the template's
    files, whose brief was never said.
    """
    if not brief.strip():
        return
    client_id = f"template-brief-{chat.id}"
    provenance = {
        "source": TEMPLATE_BRIEF_SOURCE,
        "template_node_id": str(template_node_id),
        "template_object_id": str(template.id),
        "template_title": template.title,
        "template_author": await _template_author(db, template),
    }
    await chat_service.submit_user_message(
        db,
        chat=chat,
        user_id=user.id,
        text=brief,
        client_id=client_id,
        metadata=provenance,
        actor=ctx.audit_dict(),
        announce=False,
    )


class _TemplateCopyError(RuntimeError):
    """The template's files could not be put into the new chat's folder."""


async def _tombstone_started_chat(db: DbSession, *, chat_id: UUID) -> None:
    """Take back a chat whose template copy failed.

    Written through the ordinary delete so the chat's folder is trashed with it
    — a tombstoned row whose folder still stands in the drive is a chat nobody
    can open sitting in somebody's Files.
    """
    chat = await access.load_object(db, chat_id, type="chat")
    if chat is not None:
        await chat_service.delete_chat(db, chat=chat)


async def _claim_spare(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    reader: access.Reader,
    payload: ChatCreate,
) -> ChatSessionRead | None:
    """Hand the caller the chat warmed for them, or ``None`` to create one.

    The spare is locked ``SKIP LOCKED``: two sends from the same person at the
    same moment do not wait on each other — one claims, the other creates,
    both get a chat. A spare whose box is gone or refused it is reaped here
    and the create proceeds; the caller cannot tell which path answered.

    The claim is decided under its own action (``CLAIM``, the owner's alone)
    and recorded, and the composer's picks are honoured where they differ
    from what the spare was warmed with: the row is re-pinned and the box
    already running the session hears the relay before the first prompt.
    """
    # Only this org's spare: one warmed in another of the owner's orgs is that
    # org's chat, never read here, and stays standing for that org's page.
    spare = await chat_spares.lock_spare(db, owner_id=user.id, org_id=ctx.org_id)
    if spare is None:
        return None
    if not await spare_service.usable(db, spare):
        await chat_spares.reap(db, spare)
        return None
    await enforce(
        request,
        db,
        ctx,
        Action.CLAIM,
        access.object_resource(spare, type=ResourceType.CHAT),
        await access.chat_attrs(db, spare, reader),
    )
    spec = chat_service.chat_spec_of(spare)
    relays: list[dict[str, Any]] = []
    if payload.model or payload.effort:
        pin = await resolve_model_pin(
            db, user, org_id=ctx.org_id, model_id=payload.model, effort=payload.effort
        )
        if spec.model is None or (pin.id, pin.effort) != (spec.model.id, spec.model.effort):
            spare = await chat_service.set_model(db, chat=spare, pin=pin)
            relays.append(
                ModelRelay(pin=pin.model_dump(mode="json"), user_id=str(user.id)).model_dump(
                    mode="json"
                )
            )
    if payload.permission_mode is not None and payload.permission_mode != spec.permission_mode:
        spare = await chat_service.set_permission_mode(db, chat=spare, mode=payload.permission_mode)
        relays.append(
            ModeRelay(mode=payload.permission_mode, user_id=str(user.id)).model_dump(mode="json")
        )
    if payload.title:
        spare.title = payload.title
    await chat_spares.mark_claimed(db, spare)
    if relays:
        await chat_service.broadcast_relay(
            db,
            org_team_id=spare.org_team_id,
            chat_id=spare.id,
            events=relays,
            actor=ctx.audit_dict(),
        )
    await chat_service.announce_chat(db, chat=spare, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(spare)
    return _chat_read(
        spare,
        await live_machines(db, org_id=spare.org_team_id),
        await _chat_nodes(db, ctx, [spare]),
        limits=await sandbox_limits(db, spare),
        **_caps(ctx, spare, await access.chat_attrs(db, spare, reader)),
        **await _links(db, spare.id),
    )


@router.post("/spare", response_model=ChatSpareState)
async def warm_spare(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> ChatSpareState:
    """The chat page's heartbeat: keep one chat warmed for the caller.

    Called when the page opens and every minute while it is visible. Idempotent
    and never an error the page acts on: ``warm`` when a spare stands (its
    session opened on the box, or opening), ``none`` when nothing could be
    warmed — no live machine, the drive refusing a create, no model to pin —
    in which case the first message takes the ordinary create path.
    """
    if ctx.is_agent:
        # A box warms nothing for itself; only a person on the page does.
        return ChatSpareState(state="none")
    return await spare_service.warm(request, db, ctx, user)


@router.get("/{chat_id}", response_model=ChatSessionRead)
async def get_chat(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: PrincipalUser
) -> ChatSessionRead:
    chat = await _load_chat(db, chat_id)
    if chat_spares.is_spare(chat):
        # A spare is no one's until its owner's first send claims it, its box's
        # included: a box that read it would start an agent nobody asked for.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    facts = await _decide(request, db, ctx, user, chat, Action.READ)
    # Reading a chat is not running it: a message or an answer wakes it.
    pinned = bool(await workspaces.adopt_strays(db, [chat]))  # before this request writes it
    pinned = (user is not None and bool(await _pin_legacy_chats(db, ctx, user, [chat]))) or pinned
    if pinned:
        await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    await db.commit()
    if pinned:
        await db.refresh(chat)
    return _chat_read(
        chat,
        await live_machines(db, org_id=chat.org_team_id),
        await _chat_nodes(db, ctx, [chat]),
        limits=await sandbox_limits(db, chat),
        **_caps(ctx, chat, facts),
        **await _links(db, chat.id),
        owner_display_name=(await owner_names(db, [chat])).get(chat.owner_user_id, ""),
    )


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> Response:
    """Take a chat out of the workspace: its owner, an org admin who may read
    it, or an org admin offboarding a departed owner (on the org's audit
    chain). A reader who is none of those is refused (403); a chat they cannot
    read is the opaque 404. The row is tombstoned, never dropped, and the
    ``chat.updated`` doorbell tells the rail and the box to find it gone.
    """
    chat = await _load_chat(db, chat_id)
    facts = await _decide(request, db, ctx, user, chat, Action.DELETE)
    chat = await chat_service.delete_chat(db, chat=chat, actor=ctx.audit_dict())
    if facts["owner_departed"] is True:
        await org_audit_service.record_offboarding(db, obj=chat, by=user, acting=ctx)
    # The doorbell is marked: a socket already watching this chat has to be
    # decided again, not merely told to re-read a chat it can no longer open.
    await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict(), access_changed=True)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{chat_id}/messages", response_model=ChatMessageList)
async def list_chat_messages(
    request: Request,
    chat_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
    after_seq: int = Query(0, ge=0, le=MAX_MESSAGE_SEQ),
    limit: int = Query(DEFAULT_MESSAGE_LIMIT, ge=1, le=MAX_MESSAGE_LIMIT),
    before: int | None = Query(None, ge=1, le=MAX_MESSAGE_SEQ),
    tail: bool = Query(False),
) -> ChatMessageList:
    """A page of transcript: after ``after_seq`` (the live tail's forward read),
    or — with ``tail`` / ``before`` — the newest page and the pages below it,
    which is how a reader opens a long chat on its last turn and scrolls up.

    A backward page comes back ascending, sized ``limit`` and then lowered
    until its first row BOTH opens the turn it belongs to and opens every
    message the page carries. Two reaches do that, run alternately because
    neither settles it alone: a prompt the reader sent while the machine was
    still writing sits INSIDE that answer's rows, so anchoring on it shows the
    answer's end; and the answer's own first row sits one row under the prompt
    that turn began at, so stopping there opens on the box's echo of the
    person's message instead of on the message itself. The descent stops
    ``limit * (chat_page_turn_reach + chat_page_message_reach)`` rows under the
    page, so one page is at most ``limit`` plus that — 1400 rows at the default;
    past that — or for a message whose rows are further apart than the read
    goes — the page says ``cut`` and the page below carries the rest.
    ``prev_before`` is the next ``before`` and ``has_older`` says whether one
    exists. When the
    forward read asks for messages older than the oldest one still held, the
    page carries ``resync_from`` — an in-band "start again from here" rather
    than an error the client needs a branch for.
    """
    backward = tail or before is not None
    if (tail and before is not None) or (backward and after_seq > 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "messages_page_direction",
                "message": "Ask for one page at a time: after_seq, before, or tail",
            },
        )
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.READ)
    if backward:
        rows, prev_before, has_older, cut = await chat_service.list_messages_before(
            db, chat_id=chat.id, before=before, limit=limit
        )
        await db.commit()
        return ChatMessageList(
            items=[_message_read(row) for row in rows],
            next_after_seq=rows[-1].seq if rows else 0,
            prev_before=prev_before,
            has_older=has_older,
            cut=cut,
        )
    rows, next_after, resync_from = await chat_service.list_messages(
        db, chat_id=chat.id, after_seq=after_seq, limit=limit
    )
    await db.commit()
    return ChatMessageList(
        items=[_message_read(row) for row in rows],
        next_after_seq=next_after,
        resync_from=resync_from,
    )


# --------------------------------------------------------------------------
# attachments
# --------------------------------------------------------------------------


async def _authorized_source(
    request: Request, db: DbSession, ctx: CurrentPrincipal, node_id: UUID
) -> WorkspaceObject:
    """The chat template a ``.alkerachat.template`` folder stands for, once this
    caller has been decided READ on the folder itself.

    Two refusals, and they are deliberately different. A node that does not
    exist, one in another org, and one this caller may not read are the SAME
    opaque 404 — the decision is the Files decider's, made through exactly the
    predicate every other read of that node makes, so naming a node you cannot
    read tells you nothing about whether it is there. A node the caller CAN
    read but which is not a template is a 422 that says so: the caller is
    allowed to know, and "start a chat from this" is meaningless for a
    spreadsheet, for another chat, or for a saved query nobody re-runs any more.
    """
    if not settings.files_enabled:
        # No drive, so no folder to read: the same answer an absent node gives.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    files = await build_files_context(db, ctx)
    decided = await _authorized_node(request, files, node_id, FilesAction.READ)
    object_id = decided.node.target_object_id
    source = await access.load_object(db, UUID(str(object_id))) if object_id is not None else None
    if source is None or source.type != CHAT_TEMPLATE_TYPE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "chat.source_not_a_template",
                "message": (
                    "A chat can only be started from a chat template — that folder is not one"
                ),
            },
        )
    return source


def _narrowed_mode(
    reader: CloudPermissionMode, template: CloudPermissionMode
) -> CloudPermissionMode:
    """The stance a chat started from a template opens in.

    The reader's own stance, narrowed by the template's where the template's is
    stricter, and never widened by it. A template is a document somebody else
    wrote: if it could raise the stance, saving one would be a way to hand every
    reader a permission they never chose — ``bypass`` most of all, which is why
    it can only ever be a stance the reader already stands in, never one a
    template hands them.
    """
    if MODE_RANK[template] < MODE_RANK[reader]:
        return template
    return reader


def _attachment_item(node: FileNode, head: FileVersion | None) -> ChatAttachmentRead:
    """The wire shape of one linked node this caller may read."""
    return ChatAttachmentRead(
        node_id=node.id,
        name=node.name_display or node.name.decode("utf-8", "replace"),
        size=int(head.size_bytes) if head is not None else int(node.size),
        mime=(head.mime_sniffed if head is not None else None) or "application/octet-stream",
        state="available",
    )


def _attachment_read(decided: Read) -> ChatAttachmentRead:
    """The wire shape of one linked node the caller just authorized."""
    return _attachment_item(decided.node, decided.head)


def _attachment_unavailable(node_id: UUID) -> ChatAttachmentRead:
    """A linked node this caller cannot read right now: the id and nothing else.

    Not an omission, because omitting it would make "you may not open this"
    indistinguishable from "the server could not answer" — the very confusion a
    silent ``continue`` produced — and would quietly shrink a list whose length
    the chat's own spec already tells this member. Not a name either: the name
    belongs to the file's audience, not the chat's.
    """
    return ChatAttachmentRead(node_id=node_id, name="", size=0, mime="", state="unavailable")


def _file_part(decided: Read, *, message_id: str, index: int) -> FilePart:
    """One attachment as the part the transcript entry carries.

    ``node_id`` is what makes it fetchable: the box mints a content URL for the
    node and decides again, rather than trusting the digest recorded here.
    """
    item = _attachment_read(decided)
    head = decided.head
    return FilePart(
        part_id=f"{message_id}:att:{index}",
        message_id=message_id,
        node_id=str(item.node_id),
        sha256=head.content_hash if head is not None else "",
        filename=item.name,
        mime=item.mime,
        size=item.size,
        source="file",
    )


@router.post(
    "/{chat_id}/attachments",
    response_model=ChatAttachmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[FilesEnabled],
)
async def attach_file(
    request: Request,
    chat_id: UUID,
    payload: ChatAttachmentCreate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    files: FilesCtx,
) -> ChatAttachmentRead:
    """Link a Files node to a chat.

    TWO decisions, both on record: ``SEND`` on the chat (attaching a file is
    speaking in the conversation) and ``READ`` on the node through the Files
    decider (you cannot hand somebody a file you cannot read yourself). The
    link itself grants nothing — every later read of the node is decided again.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.SEND)
    decided = await _authorized_node(request, files, payload.node_id, FilesAction.READ)

    async def record(chat_id: UUID, node_id: NodeId) -> None:
        try:
            await chat_service.record_attachment(db, chat_id=chat_id, node_id=UUID(str(node_id)))
        except chat_service.TooManyAttachmentsError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "chat.too_many_attachments", "message": str(exc)},
            ) from exc

    async with files.repo.transaction():
        await link_attachment(
            files.repo, files.ctx, chat_id, NodeId(payload.node_id), record_reference=record
        )
    await db.commit()
    return _attachment_read(decided)


@router.get(
    "/{chat_id}/attachments", response_model=ChatAttachmentList, dependencies=[FilesEnabled]
)
async def list_chat_attachments(
    request: Request,
    chat_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    files: FilesCtx,
    limit: int = Query(
        chat_service.DEFAULT_ATTACHMENT_LIMIT, ge=1, le=chat_service.MAX_ATTACHMENT_LIMIT
    ),
    cursor: str | None = None,
) -> ChatAttachmentList:
    """A page of the chat's attachments, as THIS caller may read them.

    PAGED, because the links are unbounded over a chat's life: a tab that read
    every one of them decided every one of them too, and a conversation that
    accumulated files for a year answered slower every week. ``next_cursor``
    names the place AND the node the page ended on — both, because two links can
    hold one place — and a read that carries none is the last page.

    The stored list is a set of references, so it is never served verbatim:
    every id is decided again here. A node this caller may not read comes back
    as ``state="unavailable"`` with no name, and a node that is not in this org
    at all — the opaque not-yours answer the repo gives — is absent entirely,
    because the fact that such a node exists is not this chat's to disclose.

    The decision is BATCHED. Deciding one node at a time through ``enforce()``
    would write a committed ``authz.decision`` row per unreadable node, on a
    GET a portal tab re-issues on every invalidation — an ordinary read that
    any member could turn into unbounded audit growth, and rows that look like
    genuine access attempts while being a rendering pass. ``readable_ids``
    answers the same question the per-node decider answers, over rows already
    in memory, and records nothing: the one decision this request makes is the
    READ on the chat above.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.READ)
    node_ids, next_cursor = await chat_service.linked_attachments(
        db, chat.id, after=_after(cursor), limit=limit
    )
    items = await _readable_attachments(request, files, node_ids)
    await db.commit()
    return ChatAttachmentList(
        items=items,
        next_cursor=None if next_cursor is None else encode_attachment_cursor(next_cursor),
    )


def _after(cursor: str | None) -> chat_service.AttachmentCursor | None:
    """The place and node a page resumes after; a cursor this route did not
    mint is a 422 (see ``decode_attachment_cursor``)."""
    try:
        return None if cursor is None else decode_attachment_cursor(cursor)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "chat.bad_cursor", "message": "That is not a page cursor."},
        ) from None


async def _readable_attachments(
    request: Request, files: FilesContext, node_ids: Sequence[UUID]
) -> list[ChatAttachmentRead]:
    """Every linked id rendered as this caller may see it, in four statements.

    Nothing here swallows a failure. A ``FilesError`` raised by the loads or the
    decision propagates to ``backend.api.deps.files_errors``, which answers it: a transient refusal
    used to be caught alongside the permission case and made the attachment
    vanish from the list, so a chat member could not tell a file they may not
    open from one the server could not answer for.
    """
    if not node_ids:
        return []
    wanted = [NodeId(node_id) for node_id in node_ids]
    session = files.repo.session
    async with as_platform(session):
        facts = await facts_for(request, session, files.ctx, files.drive)
    async with files.repo.transaction():
        # Scoped by the repo, so a node of another org is simply not here.
        in_org = {node.id: node for node in await files.repo.nodes(wanted)}
        allowed = await readable_ids(files.repo, files.ctx, wanted, facts=facts)
        heads = await _heads_for(files.repo, [in_org[node_id] for node_id in allowed])
    items: list[ChatAttachmentRead] = []
    for node_id in node_ids:
        node = in_org.get(node_id)
        if node is None:
            continue
        if node_id not in allowed:
            items.append(_attachment_unavailable(node_id))
            continue
        head = None if node.head_version_id is None else heads.get(node.head_version_id)
        items.append(_attachment_item(node, head))
    return items


async def _heads_for(repo: FilesRepo, nodes: Sequence[FileNode]) -> dict[UUID, FileVersion]:
    """The head version of each node, in one statement per batch of them.

    The size and mime a client renders live on the head, not the node, and a
    statement per attachment is the N+1 the batched decision exists to avoid.

    The statement comes from the Files repo rather than a plain select, for the
    same reason the chat's own node does: the org predicate on a Files table is
    the repo's job, and a route that spells it itself is one edit away from
    handing a client another org's version row.

    One statement per batch, not one for the whole list: a chat's attachments
    are unbounded over its life while a statement carries at most 32767 bound
    parameters, so the tab of a chat that grew past that answered nothing at all
    rather than a page too many.
    """
    ids = [node.head_version_id for node in nodes if node.head_version_id is not None]
    heads: dict[UUID, FileVersion] = {}
    for batch in id_batches(ids):
        result: Any = await repo.execute_scoped(
            repo.select_versions().where(FileVersion.id.in_(batch))
        )
        heads.update((row.id, row) for row in result.scalars().all())
    return heads


@router.delete(
    "/{chat_id}/attachments/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[FilesEnabled],
)
async def detach_file(
    request: Request,
    chat_id: UUID,
    node_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> Response:
    """Take a linked node back off a chat.

    ``SEND`` on the chat, the same decision attaching makes: putting a file
    into the conversation and taking it out again are both speaking in it, and
    neither is decided against the file — a member who has since lost read on
    the node must still be able to withdraw the attachment they made.

    Idempotent, and it never reads the node. The reference outlives what it
    points at, so a node that was trashed or purged still detaches; a second
    DELETE of the same pair is another 204.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.SEND)

    async def forget(chat_id: UUID, node_id: NodeId) -> None:
        await chat_service.forget_attachment(db, chat_id=chat_id, node_id=UUID(str(node_id)))

    await unlink_attachment(chat_id, NodeId(node_id), forget_reference=forget)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _message_attachments(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    chat: WorkspaceObject,
    node_ids: Sequence[str],
    *,
    message_id: str,
) -> list[FilePart]:
    """The parts a message's ``attachments`` become, or a refusal.

    A message body is not a way to attach: every id it names MUST already be
    linked to THIS chat, so the SEND-plus-READ decision the link route made is
    never skipped by posting a message instead. The node is still authorized
    again here — the link is a reference, and a caller who has since lost read
    on the node cannot re-broadcast it — and a node that is no longer readable
    reaches the same opaque 404 the Files routes give.

    The Files context is built HERE rather than declared on the route, and only
    once the body actually names a file. Building one is not a read: it inserts
    a store row and commits the org's drive skeleton, so a route-level
    dependency would make every message in the product — including every
    message on a deployment with Files switched off — create Files state it
    never names. The kill switch is checked in the same place, so a body naming
    a file while Files is dark is the opaque 404 the Files routes give.
    """
    if not node_ids:
        return []
    await require_files_enabled()
    files = await files_context(request, db, ctx)
    named: list[UUID] = []
    for raw in node_ids:
        try:
            named.append(UUID(str(raw)))
        except ValueError:
            raise _not_linked(str(raw)) from None
    # The membership question is asked over the ids this body named, not over
    # every id the chat has ever held: a message naming one file must not read
    # a year of attachments to find out the chat holds it.
    linked = await chat_service.linked_among(db, chat.id, named)
    parts: list[FilePart] = []
    for index, node_id in enumerate(named):
        if node_id not in linked:
            raise _not_linked(str(node_id))
        decided = await _authorized_node(request, files, node_id, FilesAction.READ)
        parts.append(_file_part(decided, message_id=message_id, index=index))
    return parts


def _not_linked(node_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "chat.attachment_not_linked",
            "message": "attach the file to this chat before sending it",
            "nodeId": node_id,
        },
    )


async def _refuse_an_unreachable_machine(
    db: DbSession, *, chat: WorkspaceObject, org_team_id: UUID
) -> None:
    """Refuse a message the org's boxes cannot answer.

    Placement has already run: the chat is on the best machine the org has. If
    that machine's daemon has gone quiet past the liveness window, nothing will
    read the relay — the prompt would be recorded, the composer would clear, and
    the reader would wait for an answer that no process is going to write. Say
    so instead, with the action that fixes it.

    A chat with NO machine at all is a different state and still accepts the
    message: the transcript is the durable thing, and a box that starts later
    picks the chat up. Only a machine that exists and is not answering is a
    refusal, because it is the one case where the reader has been told there is
    a live workspace.
    """
    spec = chat_service.chat_spec_of(chat)
    if (
        await bound_machine_state(db, org_team_id=org_team_id, machine_id=spec.machine_id)
        != "unreachable"
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "machine_unreachable",
            "message": (
                "This chat's workspace machine stopped answering. "
                "Start it again to resume the chat, then send the message."
            ),
            "machineId": spec.machine_id,
        },
    )


async def _nobody_serves(db: DbSession, *, chat: WorkspaceObject, org_team_id: UUID) -> bool:
    """Whether no box will act on a relay to this chat right now.

    True when the chat is bound to nothing or to a machine that cannot answer
    (gone quiet, asleep, off the plane, draining), when the box put the chat
    to sleep and holds no session for it, or when the box said it cannot
    publish the chat. In each the relay reaches no process that is running
    the turn, so whatever the relay would have had the box write to the chat's
    document, nothing will.
    """
    spec = chat_service.chat_spec_of(chat)
    if spec.publisher_refusal or spec.mirror_state == "asleep":
        return True
    state = await bound_machine_state(db, org_team_id=org_team_id, machine_id=spec.machine_id)
    return state in UNSERVEABLE


@router.post(
    "/{chat_id}/messages", response_model=ChatMessageRead, status_code=status.HTTP_201_CREATED
)
async def post_chat_message(
    request: Request,
    chat_id: UUID,
    payload: ChatMessageCreate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> ChatMessageRead:
    """Say something in a chat: recorded, then relayed to the machine.

    Any reader may send — a message is a relay to the publisher, not a write to
    the transcript by the reader — so ``SEND`` is the audience gate plus a
    verified email. The recording and the relay are the same function the
    socket path calls, so a message typed into an open socket and one posted
    here become the same kind of transcript entry.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.SEND)
    # A message is the moment the chat has to be somewhere: if the box it was
    # bound to is gone, placement runs again and the chat moves to a live one
    # before the message is relayed, so a fresh box picks it up.
    chat, _ = await rebind_if_stranded(db, chat=chat, ctx=ctx, org_team_id=chat.org_team_id)
    await _refuse_an_unreachable_machine(db, chat=chat, org_team_id=chat.org_team_id)
    # The transcript id the entry will carry, minted before the parts so each
    # part is addressed by the message it belongs to. It is derived from the
    # client id, so it is the same id ``append_user_message`` lands on -- and
    # the same one a retry of this send lands on.
    message_id = chat_service.event_id_for_client_message(payload.client_id)
    parts = await _message_attachments(
        request, db, ctx, chat, payload.attachments, message_id=message_id
    )
    try:
        message, _created = await chat_service.submit_user_message(
            db,
            chat=chat,
            user_id=user.id,
            text=payload.text,
            client_id=payload.client_id,
            attachments=parts,
            actor=ctx.audit_dict(),
        )
    except chat_service.ChatMessageTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except ClientIdReusedError as exc:
        raise HTTPException(409, {"code": "client_id_in_use", "message": str(exc)}) from exc
    await db.commit()
    return _message_read(message)


@router.post("/{chat_id}/answer", status_code=status.HTTP_202_ACCEPTED)
async def answer_chat_interrupt(
    request: Request,
    chat_id: UUID,
    payload: ChatInterruptAnswer,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> Response:
    """Answer an ask the agent is blocked on: recorded, then relayed.

    A permission or question ask is raised by the harness on the machine and
    waits there — for an hour, overnight, past the box's sleep. Answering it is
    speaking in the chat, so the gate is ``SEND``, the same one a message
    passes. Unlike a message the answer is not a prompt row: it is recorded as
    the ask's resolution (``permission.resolved`` / ``question.answered`` /
    ``question.rejected``, decided by this user), the very event the machine
    publishes when its harness settles an ask, so the transcript is the one
    record of what was asked and what was decided.

    The row is the durable answer. A box holding the ask hears the relay and
    settles it at once; a chat asleep, or whose box is gone, has nobody to
    relay to, and the row is what the next box reads when it opens the chat —
    it acts on the decision instead of asking the person a second time. The
    ask must be one the transcript holds (an id nobody asked is a 404
    ``ask_not_found``), still open (a second answer is a 409
    ``ask_already_answered``), and the answer one the ask can take (an option
    it never offered, a question's answers to a permission: 422
    ``answer_does_not_fit_ask``). An "Always" option becomes the chat owner's
    rule in their other chats, so it is decided again as ``ANSWER_STANDING``.
    Whether an allow may approve a write stays the machine's (the fence's).
    The caller is stamped onto the resolution: the option alone does not say
    whose call it was, and the roster lives here, not on the machine.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.SEND)
    if payload.option_id in STANDING_OPTIONS:
        await _decide(request, db, ctx, user, chat, Action.ANSWER_STANDING)
    try:
        recorded = await chat_service.record_interrupt_answer(
            db,
            chat=chat,
            request_id=payload.interrupt_id,
            option_id=payload.option_id,
            answers=payload.answers,
            reject=payload.reject,
            reason=payload.reason,
            note=payload.note,
            decided_by=user,
            via="web",
        )
    except chat_service.AskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ask_not_found", "message": str(exc)},
        ) from exc
    except chat_service.AskAlreadyAnsweredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ask_already_answered", "message": str(exc)},
        ) from exc
    except chat_service.AnswerDoesNotFitError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "answer_does_not_fit_ask", "message": str(exc)},
        ) from exc
    # An answer is the moment a sleeping chat has to be somewhere again: if the
    # box it was bound to is gone, placement runs and a live box picks the chat
    # — and the recorded answer — up. A chat with no live box at all is not
    # refused: the row is the answer, and it waits for the next box.
    chat, _ = await rebind_if_stranded(db, chat=chat, ctx=ctx, org_team_id=chat.org_team_id)
    # Answering asks the agent to go on: a slept chat is woken to read the answer.
    await chat_service.request_wake(db, chat=chat)
    # The resolution goes out live under the server's own peer id, as the same
    # event the row holds. It is what a viewer's card settles on: the relay
    # below is addressed to the machine and carries no decision a transcript
    # reader can fold, and the machine's own resolution — which arrives later
    # and names nobody, because no box has a roster — would otherwise be the
    # only thing anyone watching ever saw.
    await docsync.publish_server_events(
        db,
        chat=chat,
        user=user,
        entries=[recorded.payload],
        actor=ctx.audit_dict(),
    )
    answer = AnswerRelay(interrupt_id=payload.interrupt_id, user_id=str(user.id))
    if payload.option_id is not None:
        answer.option_id = payload.option_id
    elif payload.answers is not None:
        answer.answers = payload.answers
        answer.note = payload.note
    else:
        answer.reject = True
        answer.reason = payload.reason
    await chat_service.broadcast_relay(
        db,
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        events=[answer.model_dump(mode="json")],
        actor=ctx.audit_dict(),
    )
    await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    await db.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/{chat_id}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_chat_turn(
    request: Request,
    chat_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> Response:
    """Stop the turn this chat's session is running.

    Stopping is speaking in the chat — it decides what the agent does next —
    so the gate is ``SEND``, the same one a message, an answer and a mode
    switch pass. A reader who may say something here may end what they started.

    The relay is how a box running the chat right now cancels its turn, and a
    box that is not running it ignores the relay exactly as it ignores a mode
    switch for a chat it does not hold — which is why the 202 says the stop was
    relayed and never that a turn was caught.

    What is durable is what the transcript keeps, and it is recorded BEFORE the
    relay so nothing depends on a box being there to hear it. Two facts: the
    line naming who ended the turn, because only the server knows which member
    pressed Stop; and, for every message no machine has begun answering, that
    it was never run. That second one is what makes a Stop pressed in the
    window between sending a message and the box starting it mean anything at
    all — the message is a row, and a row a reader wrote and nothing answered
    is what the next box to open the chat would otherwise pick up and run.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.SEND)
    note = await chat_service.record_turn_stopped(
        db,
        chat=chat,
        # The name they gave, or nothing. A roster can fall back on the half of
        # an address in front of the @ because it is drawn beside a face the
        # reader already knows; a sentence in the transcript is read by
        # everyone in the chat and keeps forever, and putting somebody's
        # address there is not worth knowing who pressed Stop. Unnamed reads as
        # "a member", which the note already says.
        who=user.display_name,
    )
    # The note goes out live, as the same events the rows hold. Only the server
    # knows which member pressed Stop, so only the server can write this line —
    # and written to the table alone it reached nobody who was watching: their
    # transcript opened an empty notice where the sentence belongs and filled
    # it in only on a reload.
    await docsync.publish_server_events(
        db,
        chat=chat,
        user=user,
        entries=[row.payload for row in note],
        actor=ctx.audit_dict(),
    )
    # The word "working" on the chat's document is the box's to take back
    # when it ends the turn the relay below asks it to end. A chat no box holds
    # has nobody to take it back, and the document would say the turn is
    # running — every listing owing a turn, the composer offering Stop alone —
    # until some box took the chat again. So the server ends it here, the way
    # the box would, and only when the relay is known to reach nobody.
    if await _nobody_serves(db, chat=chat, org_team_id=chat.org_team_id):
        await docsync.end_turn_nobody_runs(db, chat=chat, user=user, actor=ctx.audit_dict())
    await chat_service.broadcast_relay(
        db,
        org_team_id=chat.org_team_id,
        chat_id=chat.id,
        events=[StopRelay(user_id=str(user.id)).model_dump(mode="json")],
        actor=ctx.audit_dict(),
    )
    await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    await db.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.put("/{chat_id}/permission-mode", response_model=ChatSessionRead)
@names_body_errors(
    "invalid_permission_mode",
    'Name the stance in "mode": the allowed modes are read_only, default, auto, plan and bypass.',
)
async def set_chat_permission_mode(
    request: Request,
    chat_id: UUID,
    payload: ChatPermissionModeUpdate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> ChatSessionRead:
    """Put this chat's session into a permission stance.

    Choosing the stance is speaking in the chat — it changes what the agent will
    do on the next turn — so the gate is ``SEND``, the same one a message and an
    answer pass. A reader who may not speak here may not decide how the agent
    behaves either.

    The row is the durable answer: the box reads it when it opens the session,
    so a chat resumed on a fresh machine comes back in the stance the reader
    left it in. The relay is how a box that is running the chat RIGHT NOW hears
    about it without waiting for a resume; a box that is not running it never
    sees the relay and loses nothing.

    Only a stance a harness runs in is spellable (``ChatPermissionModeUpdate``)
    — a body naming anything else is a 422. The set is the editor's own five,
    because a stance only ever decides WHO IS ASKED: the machine keeps its path
    fence whatever mode a chat is in, and the connected data is read-only in
    every one of them.
    """
    chat = await _load_chat(db, chat_id)
    facts = await _decide(request, db, ctx, user, chat, Action.SEND)
    chat = await mode_change.change_permission_mode(
        db, chat=chat, mode=payload.mode, user=user, via="web", actor=ctx.audit_dict()
    )
    await db.commit()
    await db.refresh(chat)
    return _chat_read(
        chat,
        await live_machines(db, org_id=chat.org_team_id),
        await _chat_nodes(db, ctx, [chat]),
        limits=await sandbox_limits(db, chat),
        **_caps(ctx, chat, facts),
        **await _links(db, chat.id),
    )


@router.put("/{chat_id}/model", response_model=ChatSessionRead)
async def set_chat_model(
    request: Request,
    chat_id: UUID,
    payload: ChatModelUpdate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> ChatSessionRead:
    """Move this chat onto a model, or change its effort. Gated on ``SEND``
    (whoever may send may switch). Refusals write nothing: 422 for a pick the
    payer's catalog cannot confirm, 409 for one the reasoning history rules out
    or when ``expected_model_id`` names a model the chat has since left."""
    chat = await _load_chat(db, chat_id)
    facts = await _decide(request, db, ctx, user, chat, Action.SEND)
    try:
        pin, ledger = await plan_switch(
            db, chat, user, model_id=payload.model, effort=payload.effort
        )
        chat = await mode_change.change_model(
            db,
            chat=chat,
            pin=pin,
            user=user,
            via="web",
            actor=ctx.audit_dict(),
            ledger=ledger,
            expected_model_id=payload.expected_model_id,
        )
    except ModelSwitchRefusedError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    await db.commit()
    await db.refresh(chat)
    return _chat_read(
        chat,
        await live_machines(db, org_id=chat.org_team_id),
        await _chat_nodes(db, ctx, [chat]),
        limits=await sandbox_limits(db, chat),
        **_caps(ctx, chat, facts),
        **await _links(db, chat.id),
    )


@router.get("/{chat_id}/model-options", response_model=ChatModelOptions)
async def chat_model_options(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> ChatModelOptions:
    """Every model the chat's payer may use, each with whether this chat may move
    to it and why not. READ, so a reader who may not send sees why it is locked."""
    chat = await _load_chat(db, chat_id)
    facts = await _decide(request, db, ctx, user, chat, Action.READ)
    return await model_options(db, chat, user, can_switch=may_send(ctx, chat, facts))


@router.get("/{chat_id}/send-admission", response_model=ChatSendAdmission)
async def send_admission(
    request: Request,
    chat_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
    user_id: UUID = Query(..., description="The message's author."),
) -> ChatSendAdmission:
    """Whether ``user_id`` may still send here, asked by the box about to run
    their message (a share can be revoked between the send and the pickup).
    Only the chat's publisher may ask; the answer is the send rule's own, and
    a refusal is filed like a refused send."""
    chat = await _load_chat(db, chat_id)
    await enforce_publisher(request, db, ctx, chat, await chat_reader(request, db, ctx, user))
    answer, refused = await turn_admission(db, chat=chat, author_id=user_id)
    if refused is not None:
        await OutboxDecisionSink().record_deny(db, refused.event("GET", request.url.path))
    return answer


async def _machine_gateway_token(
    db: DbSession, ctx: CurrentPrincipal, chat: WorkspaceObject
) -> tuple[str, GatewayTokenClaims]:
    """The chat's gateway credential minted under the box's own machine
    credential, for the chat's payer (``billed_user_of``), who must still read
    the chat. The credential the request resolved is read again by id: a
    revoke that landed between the two reads ends here, with the same opaque
    refusal the credential gets at the door."""
    credential = (
        await machine_credential_service.get(db, UUID(ctx.credential_id))
        if ctx.credential_id
        else None
    )
    if credential is None or credential.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": MACHINE_CREDENTIAL_REFUSED, "message": "Machine credential refused"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    reads = await access.payer_may_read(db, chat)
    return await membership_tokens.mint_chat_gateway_token(db, chat, credential, reads)


@router.post("/{chat_id}/gateway-token", response_model=ChatGatewayTokenRead)
async def mint_chat_gateway_token(
    request: Request,
    chat_id: UUID,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> ChatGatewayTokenRead:
    """The chat's gateway credential, for the machine that publishes it.

    The agent that runs a chat must hold a credential the gateway accepts and
    nothing else accepts: the box's own bearer would let a prompt-injected
    agent act as the box against this API. So the box asks here, per chat,
    and hands the agent what comes back — a token scoped to the gateway and
    bound to this chat. Only the publisher may ask: the same decision as its
    state report (``WRITE`` on ``chat.access``).

    Who pays follows the parent. A box on a person's session mints the token
    UNDER that session, billed to that person and revoked with the session and
    their membership; the caller must be a session with a ``jti``. A box on
    its own machine credential mints under the credential, billed to the
    chat's payer while the credential and the payer's membership stand. A
    payer who can no longer read the chat is refused (``payer_lost_access``).
    """
    chat = await _load_chat(db, chat_id)
    reader = await chat_reader(request, db, ctx, user)
    await enforce_publisher(request, db, ctx, chat, reader)
    try:
        if user is None:
            token, claims = await _machine_gateway_token(db, ctx, chat)
        else:
            session = optional_session_claims(request)
            if session is None or session.jti is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "gateway_token_requires_session",
                        "message": "A chat's gateway token is minted under a revocable session",
                    },
                )
            token, claims = await membership_tokens.mint_chat_gateway_token(
                db, chat, session=session
            )
    except ValueError as exc:
        code = getattr(exc, "code", "gateway_token_refused")  # a payer who lost the chat
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, "message": str(exc)}
        ) from exc
    await db.commit()
    return ChatGatewayTokenRead(
        token=token, expires_at=datetime.fromtimestamp(claims.expires_at, tz=UTC)
    )


@router.put("/{chat_id}/publisher-state", response_model=ChatSessionRead)
async def set_publisher_state(
    request: Request,
    chat_id: UUID,
    payload: ChatPublisherStateUpdate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: PrincipalUser,
) -> ChatSessionRead:
    """The machine says whether it can publish this chat.

    A box that the gateway will not let write a chat's transcript must not
    leave the reader with a working composer and no answer: it reports the
    refusal here, the chat reads as ``refused`` with the reason, and the banner
    says so. When it publishes again it reports ``publishing`` and the flag
    clears. Only a machine may say it — an agent whose asserted id is the
    chat's bound machine or the org's current workspace machine (``WRITE`` on
    ``chat.access``), or a platform box on its own credential for a chat bound
    to it; a person, or any other agent, is refused.
    """
    chat = await _load_chat(db, chat_id)
    reader = await chat_reader(request, db, ctx, user)
    await enforce_publisher(request, db, ctx, chat, reader)
    chat = await chat_service.set_publisher_state(
        db,
        chat=chat,
        state=payload.state,
        reason=payload.reason,
        kind=payload.refusal_kind,
        actor=ctx.audit_dict(),
    )
    await workspaces.record_sandbox_report(db, chat=chat, ctx=ctx, report=payload)
    ended = False
    if payload.state == "asleep":
        # A box closing a chat is an ending like any other, through the one
        # transition: whatever lease its hand-back did not end is ended here,
        # and the chat reads asleep in the words every ending uses.
        ended = (
            await chat_end.end_chat(db, chat.id, payload.ending, actor=ctx.audit_dict())
        ).changed
    if not ended:
        await chat_service.announce_chat(db, chat=chat, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(chat)
    return _chat_read(
        chat,
        await live_machines(db, org_id=chat.org_team_id),
        await _chat_nodes(db, ctx, [chat]),
        limits=await sandbox_limits(db, chat),
        **_caps(ctx, chat, await access.chat_attrs(db, chat, reader)),
        **await _links(db, chat.id),
    )


@router.post(
    "/{chat_id}/promote", response_model=WorkspaceObjectRead, status_code=status.HTTP_201_CREATED
)
async def promote_result(
    request: Request,
    chat_id: UUID,
    payload: ChatPromoteRequest,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> WorkspaceObjectRead:
    """Pin a result the chat produced.

    Promoting creates a durable object, so it is the chat owner's or an org
    admin's. Whoever presses it, the result belongs to the chat it came out
    of — its owner is the CHAT's owner — because the daemon binds every relay
    to the chat's owner and the two surfaces must name the same person; the
    promoter is recorded on the receipt's principal chain instead. The result
    is as private as the conversation it was lifted out of: its rows reach
    another member only through a share on the result's own node, never by
    the org-wide audience a team-less object would otherwise default to. The
    cloud creates the object NOW, in ``pending_upload``,
    and asks the machine for the payload behind it. Until the upload lands
    the object exists, is listed, and has no rows — which is honest, and is
    what lets the UI show "saving…" instead of inventing a result.
    """
    chat = await _load_chat(db, chat_id)
    await _decide(request, db, ctx, user, chat, Action.PROMOTE)
    chart: dict[str, object] | None = None
    if payload.chart_spec is not None:
        try:
            chart = validate_chart_spec(payload.chart_spec).persisted()
        except ChartSpecError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        _require_chart_bound(chart, [column.name for column in (payload.columns or [])])
    spec = ResultSpec(
        source_chat_id=str(chat.id),
        source_event_id=payload.event_id,
        columns=[
            ResultColumn(name=column.name, label=column.label or "")
            for column in (payload.columns or [])
        ],
        # The promoter is on the receipt from the start; the machine's own
        # chain joins it when the payload lands.
        receipt=Receipt(principal_chain={"promoted_by": ctx.audit_dict()}),
        chart_spec=chart,
    )
    obj, created = await object_service.create_object(
        db,
        owner=user,
        org_id=chat.org_team_id,
        owner_user_id=chat.owner_user_id,
        type="result",
        title=payload.title,
        spec=spec.model_dump(mode="json"),
        logical_id=f"promote:{chat.id}:{payload.event_id}",
        status="pending_upload",
        visibility_scope=SCOPE_PRIVATE,
    )
    if created:
        await chat_service.broadcast_relay(
            db,
            org_team_id=chat.org_team_id,
            chat_id=chat.id,
            events=[
                PromoteRelay(object_id=str(obj.id), event_id=payload.event_id).model_dump(
                    mode="json"
                )
            ],
            actor=ctx.audit_dict(),
        )
        await object_service.announce(db, obj=obj, actor=ctx.audit_dict())
    await db.commit()
    await db.refresh(obj)
    return WorkspaceObjectRead.model_validate(obj)


__all__ = ["router"]
