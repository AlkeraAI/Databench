"""A person's own read state of a chat: read, unread, and read everything.

``POST /api/v1/chats/{id}/read`` moves the caller's read mark forward to the
sequence their page has shown. The chat page asks it as events arrive, so an
allowed call files no decision row (the same rule the wake on open follows: a
page's routine asks do not flood the audit lane); a refused one is filed and
answers the opaque ``404``. ``POST /api/v1/chats/{id}/unread`` and
``POST /api/v1/workspaces/{id}/read`` are a person's explicit choices and are
decided and filed as ``READ``.

Every route acts on the caller's own marks only: the person comes from the
session, never from the body, and only a person has read state (an agent
assertion or a box is refused). A chat the caller may not read, and a spare
nobody has claimed, is the opaque ``404``.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action, ResourceType
from alkera_core.authz.engine import authorize as decide_policy
from alkera_core.models import WorkspaceObject
from alkera_core.objects import chat_spares
from alkera_core.schemas.objects import ChatReadMarkUpdate, ChatReadStateRead
from fastapi import APIRouter, HTTPException, Request, Response, status

from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services import chats as chat_domain
from backend.services import sharing, workspaces

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])
workspace_router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

#: How many of a workspace's chats are read per statement while marking them
#: all read.
WORKSPACE_PAGE = 200

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
_PERSON_REQUIRED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail={"code": "person_required", "message": "Only a person has read state."},
)


def _person(ctx: CurrentPrincipal) -> UUID:
    person = chat_domain.reader_id(ctx)
    if person is None:
        raise _PERSON_REQUIRED
    return person


async def _readable_chat(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
    chat_id: UUID,
    *,
    file_allow: bool,
) -> tuple[WorkspaceObject, bool]:
    """The chat, decided as ``READ`` for this caller, and whether they may
    answer its asks (``SEND``). ``file_allow`` files the allow as well as a
    refusal; without it only a refusal reaches the audit lane."""
    chat = await sharing.load_object(db, chat_id, type="chat")
    if chat is None or chat_spares.is_spare(chat):
        raise _NOT_FOUND
    reader = await sharing.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )
    attrs = await sharing.chat_attrs(db, chat, reader)
    resource = sharing.object_resource(chat, type=ResourceType.CHAT)
    if file_allow or not decide_policy(ctx, Action.READ, resource, attrs).allowed:
        await enforce(request, db, ctx, Action.READ, resource, attrs)
    return chat, chat_domain.may_send(ctx, chat, attrs)


async def _state(
    db: DbSession, ctx: CurrentPrincipal, chat: WorkspaceObject, may_answer: bool
) -> ChatReadStateRead:
    states = await chat_domain.read_states(db, ctx, [(chat.id, may_answer)])
    state = states.get(chat.id, chat_domain.ReadState())
    return ChatReadStateRead(chat_id=chat.id, unread=state.unread, needs_you=state.needs_you)


@router.post("/{chat_id}/read", response_model=ChatReadStateRead)
async def mark_chat_read(
    request: Request,
    chat_id: UUID,
    payload: ChatReadMarkUpdate,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: CurrentUser,
) -> ChatReadStateRead:
    """Mark the chat read up to ``seq`` for the caller, and clear their "Mark
    as unread". Idempotent; a lower ``seq`` leaves the mark where it is."""
    person = _person(ctx)
    chat, may_answer = await _readable_chat(request, db, ctx, user, chat_id, file_allow=False)
    await chat_domain.mark_read(db, user_id=person, chat=chat, seq=payload.seq)
    await db.commit()
    return await _state(db, ctx, chat, may_answer)


@router.post("/{chat_id}/unread", response_model=ChatReadStateRead)
async def mark_chat_unread(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> ChatReadStateRead:
    """Mark the chat unread for the caller until they next read it."""
    person = _person(ctx)
    chat, may_answer = await _readable_chat(request, db, ctx, user, chat_id, file_allow=True)
    await chat_domain.mark_unread(db, user_id=person, chat=chat)
    await db.commit()
    return await _state(db, ctx, chat, may_answer)


@workspace_router.post("/{workspace_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_workspace_read(
    request: Request, workspace_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> Response:
    """Mark every chat in the workspace the caller may read as read, for the
    caller alone."""
    person = _person(ctx)
    workspace = await workspaces.load(db, workspace_id)
    if workspace is None:
        raise _NOT_FOUND
    reader = await sharing.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )
    attrs = await sharing.workspace_attrs(db, workspace, reader)
    await enforce(
        request,
        db,
        ctx,
        Action.READ,
        sharing.object_resource(workspace, type=ResourceType.WORKSPACE),
        attrs,
    )
    after = None
    while True:
        page, more = await workspaces.chats_page(
            db, workspace.id, limit=WORKSPACE_PAGE, after=after
        )
        readable = [
            chat
            for chat, _facts in await sharing.readable_chat_facts(db, page, reader)
            if not chat_spares.is_spare(chat)
        ]
        await chat_domain.mark_all_read(db, user_id=person, chats=readable)
        if not more or not page:
            break
        after = (page[-1].created_at, page[-1].id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
