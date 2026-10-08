"""Loading a chat and deciding an action on it, for every route that does."""

from __future__ import annotations

from uuid import UUID

from alkera_core.authz import Action, ResourceType
from alkera_core.models import User, WorkspaceObject
from fastapi import HTTPException, Request, status

from backend.api.deps.chat_publisher import chat_reader
from backend.auth.dependencies import CurrentPrincipal, DbSession
from backend.authz import enforce
from backend.services import sharing


async def load_chat(db: DbSession, chat_id: UUID) -> WorkspaceObject:
    chat = await sharing.load_object(db, chat_id, type="chat")
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return chat


async def decide_chat(
    request: Request,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: User | None,
    chat: WorkspaceObject,
    action: Action,
) -> dict[str, object]:
    """Decide ``action`` on ``chat`` and hand back the facts it decided on, so
    a route that has already paid for them can ask the policy a second
    question (``may_send``) without resolving the caller's rung twice."""
    reader = await chat_reader(request, db, ctx, user)
    attrs = await sharing.chat_attrs(db, chat, reader)
    await enforce(
        request,
        db,
        ctx,
        action,
        sharing.object_resource(chat, type=ResourceType.CHAT),
        attrs,
    )
    return attrs
