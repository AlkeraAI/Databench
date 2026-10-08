"""Opening a chat or a workspace wakes it.

``POST /api/v1/chats/{id}/wake`` is asked by the chat page, a notebook tab in
the chat's workspace and a notebook's run. ``POST /api/v1/workspaces/{id}/wake``
is asked by a workspace page with no chat open, and wakes the workspace's most
recently active chat the caller may send in (``204`` when it holds none). Both
are the one wake of :mod:`backend.api.deps.chat_wake`, which every route that
asks for a wake shares; these routes decide who may ask and shape the answer.

Only someone who may ``SEND`` in the chat wakes it, by the chat policy: a wake
spends the org's compute. A reader who may only follow the chat is refused
with the send rule's own ``403``. A chat or workspace that is not there or not
readable, and a spare nobody has claimed, is the opaque ``404``: an unsent
chat has nothing to wake, and opening it never creates one. A start admission
refuses is the compute refusal's own ``402`` / ``429``.

A decision is filed for a wake that is tried and for a caller the policy
refuses, never for an open that changes nothing: a page asks on every return
to its window, and a chat that is up or was opened moments ago is answered
(``awake``, ``waking``, ``throttled``) with nothing on the audit lane.
"""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action, ResourceType
from alkera_core.authz.engine import authorize as decide_policy
from alkera_core.objects import chat_spares
from alkera_core.schemas.objects import ChatWakeRead
from fastapi import APIRouter, HTTPException, Request, Response, status

from backend.api.deps.chat_wake import reader_of, wake_chat_as, wake_first_sendable
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce
from backend.services import sharing, workspaces

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])
workspace_router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.post("/{chat_id}/wake", response_model=ChatWakeRead)
async def wake_chat(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> ChatWakeRead:
    """Wake the chat this person just opened, if it sleeps."""
    chat = await sharing.load_object(db, chat_id, type="chat")
    if chat is None or chat_spares.is_spare(chat):
        raise _NOT_FOUND
    reader = await reader_of(request, db, ctx, user)
    attrs = await sharing.chat_attrs(db, chat, reader)
    resource = sharing.object_resource(chat, type=ResourceType.CHAT)
    # Refused: decided again through the choke point, which files the denial
    # and raises it. READ first: its refusal is the opaque answer, before
    # SEND's could confirm the chat exists.
    if not decide_policy(ctx, Action.READ, resource, attrs).allowed:
        await enforce(request, db, ctx, Action.READ, resource, attrs)
    if not decide_policy(ctx, Action.SEND, resource, attrs).allowed:
        await enforce(request, db, ctx, Action.SEND, resource, attrs)
    return await wake_chat_as(request, db, ctx, chat, resource, attrs, user=user, reader=reader)


@workspace_router.post(
    "/{workspace_id}/wake",
    response_model=ChatWakeRead,
    responses={204: {"description": "The workspace holds no chat the caller may send in."}},
)
async def wake_workspace(
    request: Request, workspace_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: CurrentUser
) -> ChatWakeRead | Response:
    """Wake the workspace this person just opened: its most recently active
    chat they may send in."""
    workspace = await workspaces.load(db, workspace_id)
    if workspace is None:
        raise _NOT_FOUND
    reader = await reader_of(request, db, ctx, user)
    seen = await sharing.workspace_attrs(db, workspace, reader)
    place = sharing.object_resource(workspace, type=ResourceType.WORKSPACE)
    if not decide_policy(ctx, Action.READ, place, seen).allowed:
        await enforce(request, db, ctx, Action.READ, place, seen)
    recent = await workspaces.recent_chats(db, workspace.id)
    woke = await wake_first_sendable(request, db, ctx, recent, user=user, reader=reader)
    if woke is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    _chat, answer = woke
    return answer
