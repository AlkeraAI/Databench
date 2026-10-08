"""Per-person read state of a chat, driven through the real routes.

Each person keeps their own read mark. What a listing reports for one reader
(``unread``, ``needs_you``, a workspace's ``unread_count``) follows from the
transcript and that reader's mark alone, so one person reading never clears a
chat for another, and a person's own messages never make a chat unread for
them. The mark routes move only the caller's own mark, on chats they may read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import ChatMessage, ChatReadMark, EventOutbox, User, WorkspaceObject
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

CHATS = "/api/v1/chats"
WORKSPACES = "/api/v1/workspaces"


async def _chat(client: AsyncClient) -> dict[str, Any]:
    made = await client.post(CHATS, json={"title": "Ops"})
    assert made.status_code == 201, made.text
    body: dict[str, Any] = made.json()
    return body


async def _box_event(chat_id: str, kind: str, payload: dict[str, Any] | None = None) -> int:
    """Record what the box would publish into the transcript: the next row,
    and the chat's ``last_seq`` moved to it, as an append does."""
    async with AsyncSessionLocal() as db:
        chat = await db.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        top = await db.scalar(
            select(func.coalesce(func.max(ChatMessage.seq), 0)).where(
                ChatMessage.chat_id == chat.id
            )
        )
        seq = int(top or 0) + 1
        db.add(
            ChatMessage(
                chat_id=chat.id,
                org_team_id=chat.org_team_id,
                seq=seq,
                role="assistant",
                kind=kind,
                event_id=str(uuid4()),
                payload={"event_id": str(uuid4()), "kind": kind, "payload": payload or {}},
            )
        )
        chat.spec = {**chat.spec, "last_seq": seq}
        flag_modified(chat, "spec")
        await db.commit()
        return seq


async def _send(client: AsyncClient, chat_id: str, text: str) -> int:
    sent = await client.post(
        f"{CHATS}/{chat_id}/messages", json={"text": text, "client_id": uuid4().hex}
    )
    assert sent.status_code == 201, sent.text
    return int(sent.json()["seq"])


async def _row(client: AsyncClient, chat_id: str) -> dict[str, Any]:
    listed = await client.get(CHATS, params={"limit": 50})
    assert listed.status_code == 200, listed.text
    rows = [item for item in listed.json()["items"] if item["id"] == chat_id]
    assert len(rows) == 1, listed.text
    return rows[0]


async def _state(client: AsyncClient, chat_id: str) -> tuple[bool, bool]:
    row = await _row(client, chat_id)
    return row["unread"], row["needs_you"]


async def _read(client: AsyncClient, chat_id: str, seq: int) -> dict[str, Any]:
    marked = await client.post(f"{CHATS}/{chat_id}/read", json={"seq": seq})
    assert marked.status_code == 200, marked.text
    body: dict[str, Any] = marked.json()
    return body


async def _member(real_session: AsyncSession, org_admin: OrgWithAdmin) -> tuple[User, str]:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    return member, password


async def _shared(
    real_session: AsyncSession, org_admin: OrgWithAdmin, chat_id: str, member: User, role: str
) -> None:
    owner = await real_session.get(User, org_admin.admin_id)
    chat = await real_session.get(WorkspaceObject, UUID(chat_id))
    assert owner is not None and chat is not None
    await share_chat(real_session, chat=chat, owner=owner, user=member, role=role)


# ---------------------------------------------------------------------------
# Unread
# ---------------------------------------------------------------------------


async def test_a_new_chat_and_your_own_messages_never_read_as_unread(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    assert await _state(client, chat["id"]) == (False, False)
    await _send(client, chat["id"], "how many prompts yesterday?")
    await _send(client, chat["id"], "and the day before?")
    assert await _state(client, chat["id"]) == (False, False)


async def test_events_inside_a_turn_are_not_activity_until_the_turn_ends(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _send(client, chat["id"], "go")
    await _box_event(chat["id"], "message.created")
    await _box_event(chat["id"], "part.created")
    assert await _state(client, chat["id"]) == (False, False)
    finished = await _box_event(chat["id"], "turn.finished", {"stop_reason": "end_turn"})
    assert await _state(client, chat["id"]) == (True, False)

    state = await _read(client, chat["id"], finished)
    assert state == {"chat_id": chat["id"], "unread": False, "needs_you": False}
    assert await _state(client, chat["id"]) == (False, False)


async def test_one_person_reading_never_clears_the_chat_for_another(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The owner and a collaborator share one chat. The collaborator's message
    is unread for the owner and not for its author; the agent's finished turn
    is unread for both; each one's read moves only their own mark."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    member, password = await _member(real_session, org_admin)
    await _shared(real_session, org_admin, chat["id"], member, ROLE_WRITER)
    async with app_client() as other:
        await login(other, member.email, password)
        assert await _state(other, chat["id"]) == (False, False), "nothing has happened yet"
        sent = await _send(other, chat["id"], "can you check the totals?")
        assert await _state(other, chat["id"]) == (False, False), "your own message"
        assert await _state(client, chat["id"]) == (True, False), "a collaborator's message"

        await _read(client, chat["id"], sent)
        assert await _state(client, chat["id"]) == (False, False)

        finished = await _box_event(chat["id"], "turn.finished")
        assert await _state(client, chat["id"]) == (True, False)
        assert await _state(other, chat["id"]) == (True, False)

        await _read(other, chat["id"], finished)
        assert await _state(other, chat["id"]) == (False, False)
        assert await _state(client, chat["id"]) == (True, False), "the owner has not read it"


async def test_the_mark_only_moves_forward_and_never_past_the_transcript(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _send(client, chat["id"], "go")
    first = await _box_event(chat["id"], "turn.finished")
    await _read(client, chat["id"], first)

    # A late request from an older page leaves the mark where it is.
    await _read(client, chat["id"], 0)
    assert await _state(client, chat["id"]) == (False, False)

    # A page cannot mark read what has not been written yet: a mark far
    # ahead is capped at the transcript, so the next turn still reads unread.
    await _read(client, chat["id"], 10_000)
    await _box_event(chat["id"], "turn.finished")
    assert await _state(client, chat["id"]) == (True, False)

    async with AsyncSessionLocal() as db:
        mark = await db.get(ChatReadMark, (UUID(chat["id"]), org_admin.admin_id))
        assert mark is not None
        assert mark.last_read_seq == first


async def test_mark_as_unread_holds_until_the_next_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    seq = await _send(client, chat["id"], "go")
    marked = await client.post(f"{CHATS}/{chat['id']}/unread")
    assert marked.status_code == 200, marked.text
    assert marked.json()["unread"] is True
    assert await _state(client, chat["id"]) == (True, False)
    await _read(client, chat["id"], seq)
    assert await _state(client, chat["id"]) == (False, False)


# ---------------------------------------------------------------------------
# Needs you
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ask_kind", "resolution_kind"),
    [
        pytest.param("permission.request", "permission.resolved", id="permission"),
        pytest.param("question.request", "question.answered", id="question"),
        pytest.param("question.request", "question.rejected", id="question-rejected"),
    ],
)
async def test_an_open_ask_needs_whoever_may_answer_it_until_it_is_settled(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    ask_kind: str,
    resolution_kind: str,
) -> None:
    """The owner may answer; a Can-view reader may not, so the ask is not
    theirs. Reading the chat does not clear it, settling the ask does."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    viewer, password = await _member(real_session, org_admin)
    await _shared(real_session, org_admin, chat["id"], viewer, ROLE_READER)
    await _send(client, chat["id"], "go")
    asked = await _box_event(chat["id"], ask_kind, {"request_id": "r1"})
    await _box_event(chat["id"], ask_kind, {"request_id": "r2"})
    await _box_event(chat["id"], resolution_kind, {"request_id": "r2"})
    assert (await _state(client, chat["id"]))[1] is True

    await _read(client, chat["id"], asked + 2)
    assert await _state(client, chat["id"]) == (False, True), "reading does not answer"
    async with app_client() as other:
        await login(other, viewer.email, password)
        assert (await _state(other, chat["id"]))[1] is False, "a viewer may not answer"

    await _box_event(chat["id"], resolution_kind, {"request_id": "r1"})
    assert (await _state(client, chat["id"]))[1] is False


async def test_an_ask_left_behind_by_an_ended_turn_needs_nobody(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    await _box_event(chat["id"], "permission.request", {"request_id": "r1"})
    assert (await _state(client, chat["id"]))[1] is True
    await _box_event(chat["id"], "turn.finished", {"stop_reason": "cancelled"})
    assert (await _state(client, chat["id"]))[1] is False


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


async def _workspace_unread(client: AsyncClient, workspace_id: str) -> int:
    got = await client.get(f"{WORKSPACES}/{workspace_id}")
    assert got.status_code == 200, got.text
    listed = await client.get(WORKSPACES, params={"limit": 50})
    assert listed.status_code == 200, listed.text
    in_list = [w for w in listed.json()["items"] if w["id"] == workspace_id]
    assert len(in_list) == 1
    assert in_list[0]["unread_count"] == got.json()["unread_count"]
    count: int = got.json()["unread_count"]
    return count


async def test_a_workspace_counts_its_unread_chats_and_marks_them_all_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    workspace_id = chat["workspace_id"]
    assert workspace_id is not None
    assert await _workspace_unread(client, workspace_id) == 0
    await _box_event(chat["id"], "turn.finished")
    assert await _workspace_unread(client, workspace_id) == 1

    member, _password = await _member(real_session, org_admin)
    await _shared(real_session, org_admin, chat["id"], member, ROLE_WRITER)

    marked = await client.post(f"{WORKSPACES}/{workspace_id}/read")
    assert marked.status_code == 204, marked.text
    assert await _workspace_unread(client, workspace_id) == 0
    assert await _state(client, chat["id"]) == (False, False)
    async with AsyncSessionLocal() as db:
        theirs = await db.get(ChatReadMark, (UUID(chat["id"]), member.id))
    assert theirs is None, "marking all read touches the caller's marks only"


# ---------------------------------------------------------------------------
# Who may move a mark
# ---------------------------------------------------------------------------


async def _decisions(org_id: UUID, entity_id: str) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity_id == entity_id,
            )
            .order_by(EventOutbox.id)
        )
        return [(r.payload["action"], r.payload["effect"]) for r in rows.scalars().all()]


async def test_a_chat_you_may_not_read_is_an_opaque_not_found_and_leaves_no_mark(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    member, password = await _member(real_session, org_admin)
    async with app_client() as other:
        await login(other, member.email, password)
        read = await other.post(f"{CHATS}/{chat['id']}/read", json={"seq": 1})
        unread = await other.post(f"{CHATS}/{chat['id']}/unread")
        everything = await other.post(f"{WORKSPACES}/{chat['workspace_id']}/read")
        missing = await other.post(f"{CHATS}/{uuid4()}/read", json={"seq": 1})
    assert read.status_code == 404, read.text
    assert unread.status_code == 404, unread.text
    assert everything.status_code == 404, everything.text
    assert missing.status_code == 404, missing.text
    async with AsyncSessionLocal() as db:
        assert await db.get(ChatReadMark, (UUID(chat["id"]), member.id)) is None
    assert await _decisions(org_admin.org_id, chat["id"]) == [
        ("create", "allow"),
        ("read", "deny"),
        ("read", "deny"),
    ], "both refusals are filed; the routine read files no allow"


async def test_a_negative_sequence_is_refused(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client)
    refused = await client.post(f"{CHATS}/{chat['id']}/read", json={"seq": -1})
    assert refused.status_code == 422, refused.text
