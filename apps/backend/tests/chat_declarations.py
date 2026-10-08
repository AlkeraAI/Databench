"""Declaring a chat, for the socket tests, without the REST surface.

A chat must be declared before it can be subscribed to: the socket resolves its
owner and audience through ``chat_lookup.lookup_chat_doc`` and refuses an id
nothing declared. Production's declaration is the chat's workspace object,
written by ``POST /api/v1/chats``; a socket test that wants a chat that surface
cannot give it — a client-chosen id, an unowned chat, a document created lazily
by the first ``hello`` — declares it here instead.

The declaration is deliberately NOT a ``realtime_docs`` row: that row is the
document, created lazily by the first ``hello``, and separating the two is
exactly the shape the objects table gives us. So a test can still exercise
creation, the per-org document ceiling, and a grant minted before the row
existed — while an undeclared id stays ``not_found``.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import scope_for_team
from backend.services.realtime import channels, chat_lookup
from backend.services.realtime.chat_lookup import ChatDocScope
from sqlalchemy.ext.asyncio import AsyncSession

#: Chat scopes a test declared, keyed ``(org_id, chat_id)``.
DECLARED: dict[tuple[UUID, str], ChatDocScope] = {}


def declare_chat(
    org_id: UUID,
    *,
    owner_user_id: UUID | None,
    team_id: UUID | None = None,
    visibility_scope: str | None = None,
    machine_id: str | None = None,
) -> str:
    """Declare a chat in ``org_id`` and return its channel name. The scope
    defaults to the one ``team_id`` spells; pass ``"private"`` for a chat only
    its owner (and an org admin) may read. ``machine_id`` binds the chat to a
    machine, whose agent then publishes it beside the owner."""
    doc_id = f"sess-{uuid4().hex[:10]}"
    DECLARED[(org_id, doc_id)] = ChatDocScope(
        owner_user_id=owner_user_id,
        team_id=team_id,
        visibility_scope=visibility_scope or scope_for_team(team_id),
        machine_id=machine_id,
    )
    return f"doc:chat:{doc_id}"


def redeclare_chat(
    channel: str,
    org_id: UUID,
    *,
    owner_user_id: UUID | None,
    team_id: UUID | None = None,
    visibility_scope: str | None = None,
    machine_id: str | None = None,
) -> None:
    """Move a declared chat's owner, audience or machine — what editing the
    chat object does. The document row, if one exists, is not touched."""
    DECLARED[(org_id, channel.split(":", 2)[2])] = ChatDocScope(
        owner_user_id=owner_user_id,
        team_id=team_id,
        visibility_scope=visibility_scope or scope_for_team(team_id),
        machine_id=machine_id,
    )


@pytest.fixture(autouse=True)
def declared_chats(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answer the chat-scope lookup from this test's declarations, and from the
    database for everything else. Inert for a test that declares nothing."""
    real = chat_lookup.lookup_chat_doc

    async def _lookup(db: AsyncSession, *, org_id: UUID, chat_id: str) -> ChatDocScope | None:
        declared = DECLARED.get((org_id, chat_id))
        if declared is not None:
            return declared
        return await real(db, org_id=org_id, chat_id=chat_id)

    monkeypatch.setattr(channels, "lookup_chat_doc", _lookup)
    DECLARED.clear()
    yield
    DECLARED.clear()


__all__ = ["DECLARED", "declare_chat", "declared_chats", "redeclare_chat"]
