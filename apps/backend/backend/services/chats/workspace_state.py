"""Reading and replacing one reader's workspace beside one chat.

The document is replaced whole, never merged: the client holds the tabs, the
server holds the last thing the client said, and a merge between the two would
invent a layout nobody asked for. So a save is one upsert on ``(chat, reader)``
and a read is one primary-key lookup.

A chat nobody has saved a layout for is not an error and not an empty answer —
it is the default document, so a first-time reader and a reader who closed
every tab are the same shape to the client.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from alkera_core.models.chat_workspace_state import ChatWorkspaceState as ChatWorkspaceStateRow
from alkera_core.schemas.objects import ChatWorkspaceState
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def load(
    db: AsyncSession, *, chat_id: UUID, user_id: UUID
) -> tuple[ChatWorkspaceState, datetime | None]:
    """This reader's layout for this chat, and when they last saved it.

    ``None`` for the timestamp says "never saved", which is what lets the
    client tell a restored layout from the default one it was handed.
    """
    row = await db.get(ChatWorkspaceStateRow, (chat_id, user_id))
    if row is None:
        return ChatWorkspaceState(), None
    return ChatWorkspaceState.model_validate(row.state), row.updated_at


async def save(
    db: AsyncSession,
    *,
    chat_id: UUID,
    user_id: UUID,
    org_team_id: UUID,
    state: ChatWorkspaceState,
) -> tuple[ChatWorkspaceState, datetime]:
    """Replace this reader's layout for this chat and answer what was stored.

    The upsert is the whole concurrency story: two tabs of the same browser
    saving at once both write the document they hold, and the last one to
    arrive is the one the reader sees next time — which is what a reader
    means by "where I left it".
    """
    document = state.model_dump(mode="json")
    stmt = (
        insert(ChatWorkspaceStateRow)
        .values(
            chat_id=chat_id,
            user_id=user_id,
            org_team_id=org_team_id,
            state=document,
        )
        .on_conflict_do_update(
            constraint="pk_chat_workspace_states",
            set_={"state": document, "updated_at": func.now()},
        )
        .returning(ChatWorkspaceStateRow.updated_at)
    )
    updated_at = (await db.execute(stmt)).scalar_one()
    return ChatWorkspaceState.model_validate(document), updated_at
