"""The workspace a reader has open beside a chat: read it, replace it.

Two routes, one decision. Both answer only to whoever may READ the chat, and
both decide it exactly the way ``GET /chats/{id}`` does — the same facts through
the same policy — so a chat that is invisible in the rail cannot have its
layout read or written through a side door. An unreadable chat is the opaque
404 the rest of the chat surface gives, on the read AND on the write: a 403 on
the write would confirm the chat exists to someone who may not know that.

Only a person's own session may ask. A CI token, a proxy token and a personal
access token are automation acting for an org or on a user's behalf, and a
saved tab layout is neither: it is one human's view of one chat, so a
credential that is not that human's session is refused rather than quietly
writing a layout nobody will ever see.

The stored document is bounded and ids-only, and both limits are enforced here
rather than trusted from the client: an oversized document is refused with the
code the client trims and retries on, and a document with a tab whose
``node_id`` is anything but a node id is refused outright. The row is replaced
whole — the client holds the tabs, the server holds the last thing it was told.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from alkera_core.authz import Action, PrincipalKind, ResourceType
from alkera_core.models import User, WorkspaceObject
from alkera_core.schemas.objects import (
    MAX_WORKSPACE_STATE_BYTES,
    ChatWorkspaceState,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError

from backend.auth.dependencies import (
    CurrentPrincipal,
    DbSession,
    current_user,
    require_verified_or_grace,
)
from backend.authz import enforce, role_resolver
from backend.services.chats import workspace_state as workspace_state_service
from backend.services.sharing import access

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])


class ChatWorkspaceRead(BaseModel):
    """The layout, and when this reader last saved it (``null`` = never).

    ``state`` is a dumped workspace document. It is declared as a free-form
    object rather than the model itself because the persisted shape carries the
    ``metadata`` bag every versioned model has, and two schemas offering a bag
    under the same generic name collide in the generated Python client — the
    generator drops BOTH rather than naming one of them. The document's own
    model stays the single source of truth: every write is parsed through it
    and every answer is built from it.
    """

    state: dict[str, Any] = Field(default_factory=dict, title="ChatWorkspaceReadState")
    updated_at: datetime | None = None


class ChatWorkspaceWrite(BaseModel):
    """A full replacement. The document is taken raw and parsed by the route so
    a refusal can name what was wrong with it rather than leaking the shape of
    the model through a framework error."""

    state: dict[str, Any] = Field(default_factory=dict, title="ChatWorkspaceWriteState")


async def _session_user(request: Request, db: DbSession, ctx: CurrentPrincipal) -> User:
    """The person behind this request, refusing every credential that is not
    their own session.

    Resolved through the principal first so a token gets the 403 that says
    "this surface is not for automation" instead of the 401 that says "your
    credential did not parse" — the credential parsed fine; it is the wrong
    kind for a personal layout. An agent acting inside a user's session is
    refused for the same reason: the box does not decide which tabs the person
    watching it has open.

    The verification gate every other product router carries is applied here
    rather than on the include, because a router-level gate resolves the user
    first and would answer a token the 401 that says "your credential did not
    parse" before this surface could say what it actually means.
    """
    if ctx.acting_principal.kind is not PrincipalKind.USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "workspace_needs_a_user_session",
                "message": "A chat's workspace belongs to a person, not to a token",
            },
        )
    return await require_verified_or_grace(await current_user(request, db))


SessionUser = Annotated[User, Depends(_session_user)]


async def _readable_chat(
    request: Request, db: DbSession, ctx: CurrentPrincipal, user: User, chat_id: UUID
) -> WorkspaceObject:
    """The chat, refused unless this caller may read it, with the decision left
    on record either way. A chat that does not exist and a chat this caller may
    not see are one answer."""
    chat = await access.load_object(db, chat_id, type="chat")
    if chat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    reader = await access.resolve_reader(
        db, ctx=ctx, roles=role_resolver(request, db, ctx), user=user
    )
    await enforce(
        request,
        db,
        ctx,
        Action.READ,
        access.object_resource(chat, type=ResourceType.CHAT),
        await access.chat_attrs(db, chat, reader),
    )
    return chat


def _parsed(raw: dict[str, Any]) -> ChatWorkspaceState:
    """``raw`` as a workspace document, refused on either bound it can break.

    Size is measured on the document as it would be STORED, not as it arrived,
    so a client cannot squeeze past the ceiling with whitespace or by omitting
    the fields the server fills in. The limits are the reason this row is safe
    to read on every chat open.
    """
    try:
        state = ChatWorkspaceState.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "state"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_workspace_state",
                "message": f"{field}: {first.get('msg', 'is not valid')}",
                "field": field,
            },
        ) from exc
    stored = json.dumps(state.model_dump(mode="json"), separators=(",", ":"))
    if len(stored.encode()) > MAX_WORKSPACE_STATE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "workspace_state_too_large",
                "message": "Close some tabs — this chat's workspace is too big to save",
                "limit_bytes": MAX_WORKSPACE_STATE_BYTES,
            },
        )
    return state


@router.get("/{chat_id}/workspace", response_model=ChatWorkspaceRead)
async def get_chat_workspace(
    request: Request, chat_id: UUID, db: DbSession, ctx: CurrentPrincipal, user: SessionUser
) -> ChatWorkspaceRead:
    """This reader's tabs beside this chat; the default document when they
    have never saved one."""
    await _readable_chat(request, db, ctx, user, chat_id)
    state, updated_at = await workspace_state_service.load(db, chat_id=chat_id, user_id=user.id)
    await db.commit()
    return ChatWorkspaceRead(state=state.model_dump(mode="json"), updated_at=updated_at)


@router.put("/{chat_id}/workspace", response_model=ChatWorkspaceRead)
async def put_chat_workspace(
    request: Request,
    chat_id: UUID,
    body: ChatWorkspaceWrite,
    db: DbSession,
    ctx: CurrentPrincipal,
    user: SessionUser,
) -> ChatWorkspaceRead:
    """Replace this reader's tabs beside this chat.

    The decision comes before the parse so a caller who may not read the chat
    learns nothing about what a valid document looks like.
    """
    chat = await _readable_chat(request, db, ctx, user, chat_id)
    state = _parsed(body.state)
    stored, updated_at = await workspace_state_service.save(
        db,
        chat_id=chat_id,
        user_id=user.id,
        org_team_id=chat.org_team_id,
        state=state,
    )
    await db.commit()
    return ChatWorkspaceRead(state=stored.model_dump(mode="json"), updated_at=updated_at)
