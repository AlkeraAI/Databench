"""The per-reader chat workspace row, against the real schema.

What is worth pinning is not that the columns exist but the three things the
table promises and the route relies on: the key is ``(chat, reader)`` so one
chat holds one layout per person and no more, and both parents cascade so a
deleted chat or a deleted account leaves no orphan behind.
"""

from __future__ import annotations

import secrets
import uuid

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ChatWorkspaceState, Team, User, WorkspaceObject
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _org(session: AsyncSession) -> Team:
    team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
    session.add(team)
    await session.flush()
    return team


async def _user(session: AsyncSession, team: Team) -> User:
    user = User(
        home_org_team_id=team.id,
        email=f"reader-{secrets.token_hex(6)}@alkera.dev",
        first_name="Read",
        last_name="Er",
    )
    session.add(user)
    await session.flush()
    return user


async def _chat(session: AsyncSession, team: Team, owner: User) -> WorkspaceObject:
    chat = WorkspaceObject(
        org_team_id=team.id,
        logical_id=f"chat-{secrets.token_hex(4)}",
        type="chat",
        title="Ops",
        owner_user_id=owner.id,
        visibility_scope="private",
    )
    session.add(chat)
    await session.flush()
    return chat


def _document(tab_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "tabs": [
            {
                "schema_version": "1.0.0",
                "id": tab_id,
                "kind": "file",
                "node_id": str(uuid.uuid4()),
                "name": "q3-revenue.html",
                "path": "q3-revenue.html",
                "params": {},
            }
        ],
        "active_tab_id": tab_id,
    }


async def _count(chat_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatWorkspaceState)
                    .where(ChatWorkspaceState.chat_id == chat_id)
                )
            ).scalar_one()
        )


async def test_a_layout_round_trips_with_its_document_intact() -> None:
    """The document is stored as JSON and read back unchanged — the row is a
    place to keep a layout, not a place that reshapes one."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        owner = await _user(session, team)
        chat = await _chat(session, team, owner)
        session.add(
            ChatWorkspaceState(
                chat_id=chat.id,
                user_id=owner.id,
                org_team_id=team.id,
                state=_document("t1"),
            )
        )
        await session.commit()
        chat_id, user_id = chat.id, owner.id

    async with AsyncSessionLocal() as session:
        row = await session.get(ChatWorkspaceState, (chat_id, user_id))
        assert row is not None
        assert row.state["active_tab_id"] == "t1"
        assert row.state["tabs"][0]["name"] == "q3-revenue.html"
        assert row.org_team_id is not None
        assert row.created_at is not None
        assert row.updated_at is not None


async def test_a_reader_with_no_saved_layout_defaults_to_an_empty_document() -> None:
    """The column's default exists so a row written without a document is an
    empty layout rather than a null the reader has to guess at."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        owner = await _user(session, team)
        chat = await _chat(session, team, owner)
        row = ChatWorkspaceState(chat_id=chat.id, user_id=owner.id, org_team_id=team.id)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.state == {}


async def test_two_readers_of_one_chat_get_two_rows() -> None:
    """A chat is one conversation and as many workspaces as it has readers."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        owner = await _user(session, team)
        guest = await _user(session, team)
        chat = await _chat(session, team, owner)
        session.add_all(
            [
                ChatWorkspaceState(
                    chat_id=chat.id, user_id=owner.id, org_team_id=team.id, state=_document("mine")
                ),
                ChatWorkspaceState(
                    chat_id=chat.id, user_id=guest.id, org_team_id=team.id, state=_document("yours")
                ),
            ]
        )
        await session.commit()
        chat_id, owner_id, guest_id = chat.id, owner.id, guest.id

    async with AsyncSessionLocal() as session:
        mine = await session.get(ChatWorkspaceState, (chat_id, owner_id))
        yours = await session.get(ChatWorkspaceState, (chat_id, guest_id))
        assert mine is not None and yours is not None
        assert mine.state["active_tab_id"] == "mine"
        assert yours.state["active_tab_id"] == "yours"
    assert await _count(chat_id) == 2


async def test_one_reader_cannot_hold_two_layouts_for_one_chat() -> None:
    """The key is what makes a save a replacement: without it a client that
    retried would accumulate layouts and the read would have to pick one."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        owner = await _user(session, team)
        chat = await _chat(session, team, owner)
        session.add(
            ChatWorkspaceState(
                chat_id=chat.id, user_id=owner.id, org_team_id=team.id, state=_document("t1")
            )
        )
        await session.commit()
        chat_id, user_id, team_id = chat.id, owner.id, team.id

    async with AsyncSessionLocal() as session:
        session.add(
            ChatWorkspaceState(
                chat_id=chat_id, user_id=user_id, org_team_id=team_id, state=_document("t2")
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.parametrize("parent", ["chat", "user"])
async def test_a_deleted_parent_takes_its_layouts_with_it(parent: str) -> None:
    """Neither a deleted chat's layouts nor a deleted account's are worth
    keeping, and an orphan here would be a row nothing can ever address."""
    async with AsyncSessionLocal() as session:
        team = await _org(session)
        owner = await _user(session, team)
        chat = await _chat(session, team, owner)
        session.add(
            ChatWorkspaceState(
                chat_id=chat.id, user_id=owner.id, org_team_id=team.id, state=_document("t1")
            )
        )
        await session.commit()
        chat_id, user_id = chat.id, owner.id
    assert await _count(chat_id) == 1

    async with AsyncSessionLocal() as session:
        if parent == "chat":
            await session.execute(delete(WorkspaceObject).where(WorkspaceObject.id == chat_id))
        else:
            await session.execute(delete(User).where(User.id == user_id))
        await session.commit()

    assert await _count(chat_id) == 0
