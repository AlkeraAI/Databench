"""Where a chat's owner and audience come from.

A chat document used to come into existence by being spoken to: the first
``hello`` for an unknown id created the row and made its caller the owner. That
made the id its own capability — anyone in the org who could guess or replay an
id took ownership of a document nobody had created, and a document with no
creation event has no ACL to check. So the socket no longer resolves a chat's
scope from the act of asking for it. It asks here, and an id this function does
not know is ``not_found``.

The answer is the chat's workspace object: the ``workspace_objects`` row of
type ``chat`` that ``POST /api/v1/chats`` writes. That row is the one
declaration a chat has — its owner (the peer that publishes it), the team it
is narrowed to, and the visibility scope both surfaces decide from — and this
is the ONE place it is read for the socket. The socket, the registry and the
REST policy all consume the scope through :class:`ChatDocScope`; none of them
needs to know where it was read from.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from alkera_core.models import WorkspaceObject
from alkera_core.schemas.objects import ChatSpec
from pydantic import ValidationError
from sqlalchemy import Uuid, and_, case, cast, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased


@dataclass(frozen=True, slots=True)
class ChatDocScope:
    """A declared chat: who owns it, the team it is narrowed to (``None`` when
    no team does), the visibility scope its audience is decided from — ``org``,
    ``team:<uuid>`` or ``private``, the grammar of
    :mod:`alkera_core.authz.chat_scope` — and the machine it is bound to, which
    publishes it beside the owner (:func:`alkera_core.authz.chat_writable`).
    The team and the scope are two spellings of one row; a reader that finds
    them disagreeing refuses."""

    owner_user_id: UUID | None
    team_id: UUID | None
    visibility_scope: str
    machine_id: str | None = None
    #: ``(workspace id, its owner)`` when the chat is in a workspace that holds
    #: several chats, which then decides every verb on it; ``None`` for a
    #: workspace of one. Read in the same statement as the chat.
    workspace: tuple[UUID, UUID] | None = None


def machine_of(chat: WorkspaceObject) -> str | None:
    """The machine a chat row is bound to, or ``None`` — for a row with no
    binding, and for a spec this reader cannot parse (an unreadable binding
    names no publisher rather than a guessed one)."""
    try:
        return ChatSpec.model_validate(chat.spec or {}).machine_id
    except ValidationError:
        return None


async def lookup_chat_doc(db: AsyncSession, *, org_id: UUID, chat_id: str) -> ChatDocScope | None:
    """The scope of the chat ``chat_id`` names inside ``org_id``, or ``None``
    when this org has declared no such chat.

    A chat's id is its workspace object's row id, so a string that is not one
    names nothing, and an object of another type under that id is not a chat
    either. The org is half the key on purpose: another tenant's chat is never
    this caller's to find, whatever id they present.
    """
    try:
        row_id = UUID(chat_id)
    except ValueError:
        return None
    workspace = aliased(WorkspaceObject, name="workspace")
    named = WorkspaceObject.spec["workspace_id"].astext
    row = (
        await db.execute(
            select(WorkspaceObject, workspace.id, workspace.owner_user_id)
            .outerjoin(
                workspace,
                and_(
                    workspace.id
                    == case((named.regexp_match(_UUID_SHAPE), cast(named, Uuid)), else_=None),
                    workspace.org_team_id == org_id,
                    workspace.type == "workspace",
                    workspace.spec["layout"].astext == "native",
                ),
            )
            .where(
                WorkspaceObject.id == row_id,
                WorkspaceObject.org_team_id == org_id,
                WorkspaceObject.type == "chat",
                WorkspaceObject.deleted_at == 0,
            )
        )
    ).first()
    if row is None:
        return None
    chat, workspace_id, workspace_owner = row
    return ChatDocScope(
        owner_user_id=chat.owner_user_id,
        team_id=chat.team_id,
        visibility_scope=chat.visibility_scope,
        machine_id=machine_of(chat),
        workspace=(workspace_id, workspace_owner) if workspace_id is not None else None,
    )


#: A spec value that can be cast to a uuid; anything else names no workspace.
_UUID_SHAPE = "^[0-9a-fA-F-]{36}$"


__all__ = ["ChatDocScope", "lookup_chat_doc", "machine_of"]
