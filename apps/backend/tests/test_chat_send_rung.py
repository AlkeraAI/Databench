"""``POST /api/v1/chats/{id}/messages`` — the ladder rung it takes, on record.

Sending drives the chat's agent, and what the agent answers lands in everybody's
copy of the conversation, so it takes the share ladder's WRITE rung on the
chat's node: the owner, or a colleague shared at ``writer`` or above. A viewer
or a commenter may watch and is refused, and every one of those answers leaves
an ``authz.decision`` row — the allow in the request's own transaction, the deny
in a committed session of its own, so it survives the refused request's
rollback.

The socket half of this contract lives in ``test_docsync.py``
(``test_the_send_policys_answer_is_the_same_through_the_socket``): both doors
run the same policy over the same facts and file the same rows, and the two
tests assert the same reasons on purpose, so a change that narrows one door
alone fails the other.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl as files_acl
from alkera_core.files.acl import OP_ACL_REWRITE
from alkera_core.files.authz.grants import ACL_REWRITING, Principal
from alkera_core.files.authz.ladder import ROLE_COMMENTER, ROLE_READER, ROLE_WRITER
from alkera_core.files.ids import NodeId
from alkera_core.models import ChatMessage, EventOutbox, User, WorkspaceObject
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.chat_shares import files_on, share_chat, share_chat_with  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


async def _create_chat(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _decisions(org_id: UUID, chat_id: UUID) -> list[tuple[str, str, str]]:
    """Every SEND decision filed for this chat, oldest first."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.org_id == org_id,
                    EventOutbox.type == "authz.decision",
                    EventOutbox.entity_id == str(chat_id),
                )
                .order_by(EventOutbox.id)
            )
        ).scalars()
    return [
        (row.payload["action"], row.payload["effect"], row.payload["reason"])
        for row in rows
        if row.payload["action"] == "send"
    ]


async def _messages(chat_id: UUID) -> list[ChatMessage]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.seq)
        )
        return list(rows.scalars().all())


async def _send(client: AsyncClient, chat_id: str, client_id: str) -> Any:
    return await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "and by region?", "client_id": client_id},
    )


@pytest.mark.parametrize(
    ("role", "status"),
    [
        pytest.param(None, 404, id="a-member-the-chat-is-not-shared-with"),
        pytest.param(ROLE_READER, 403, id="shared-can-view"),
        pytest.param(ROLE_COMMENTER, 403, id="shared-can-comment"),
        pytest.param(ROLE_WRITER, 201, id="shared-can-edit"),
    ],
)
async def test_the_rung_decides_who_may_send_and_the_decision_is_on_record(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    role: str | None,
    status: int,
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, chat_id)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    if role is not None:
        await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=role)

    async with app_client() as other:
        await login(other, member.email, password)
        # A chat is private until shared: with no rung the member cannot even
        # see it, and the send is the same opaque not-found as the read.
        readable = await other.get(f"/api/v1/chats/{chat['id']}")
        assert readable.status_code == (404 if role is None else 200), readable.text
        response = await _send(other, chat["id"], "m1")

    assert response.status_code == status, response.text
    if status == 404:
        assert response.json()["error"]["message"] == "Not found"
        assert await _messages(chat_id) == [], "nothing reached the transcript"
        assert await _decisions(org_admin.org_id, chat_id) == [
            ("send", "deny", "not_in_audience")
        ], "the deny survived the refused request's rollback"
        return
    if status == 403:
        error = response.json()["error"]
        assert (error["code"], error["message"]) == (
            chat_policy.SEND_DENIED_CODE,
            chat_policy.SEND_DENIED_MESSAGE,
        )
        assert await _messages(chat_id) == [], "nothing reached the transcript"
        assert await _decisions(org_admin.org_id, chat_id) == [
            ("send", "deny", "send_rung_required")
        ], "the deny survived the refused request's rollback"
        return
    rows = await _messages(chat_id)
    assert [row.payload["user_id"] for row in rows] == [str(member.id)]
    assert await _decisions(org_admin.org_id, chat_id) == [("send", "allow", "writer_may_send")]


async def test_the_owner_needs_no_grant_on_their_own_chat(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The chat's owner is its publisher and outranks any grant, so the
    narrowing never locks somebody out of the conversation they started."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    assert (await _send(client, chat["id"], "o1")).status_code == 201
    assert await _decisions(org_admin.org_id, UUID(chat["id"])) == [
        ("send", "allow", "writer_may_send")
    ]


async def test_a_rung_granted_mid_session_opens_the_door_with_no_reconnect(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The rung is read per request, never cached on the session: the same
    logged-in client that was refused as a commenter sends the moment the chat
    is shared with them at ``writer``."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, chat_id)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_COMMENTER)

    async with app_client() as other:
        await login(other, member.email, password)
        refused = await _send(other, chat["id"], "m1")
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == chat_policy.SEND_DENIED_CODE
        await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
        allowed = await _send(other, chat["id"], "m2")

    assert allowed.status_code == 201
    assert [row.payload["client_id"] for row in await _messages(chat_id)] == ["m2"]
    assert await _decisions(org_admin.org_id, chat_id) == [
        ("send", "deny", "send_rung_required"),
        ("send", "allow", "writer_may_send"),
    ]


async def _invalidate_the_chats_subtree(db: Any, *, chat_id: UUID, owner: User) -> None:
    """Mark the chat's node ``acl_rewriting`` and queue its repair -- the exact
    call a move makes on the subtree it has re-parented."""
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(db, ctx) as repo:
        node = (
            await repo.session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == chat_id, FileNode.trashed_at.is_(None)
                )
            )
        ).scalar_one()
        await files_acl.invalidate_subtree(repo, ctx, node)
    await db.commit()


async def test_a_pending_subtree_repair_does_not_take_a_shared_rung_away(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Moving a node -- or granting on a folder above it -- marks the whole
    subtree ``acl_rewriting`` and queues a repair to run later. The mark says
    the node's cached ACL is stale, not that nobody holds a rung: the grant
    still stands on the chain, so the colleague shared at ``writer`` still
    sends. Reading the mark as "no rung" made every deliberate share under a
    moved folder view-only for as long as the queued repair sat there -- which
    is until a background worker drains it, not until the next request.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, chat_id)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
    await _invalidate_the_chats_subtree(real_session, chat_id=chat_id, owner=owner)

    async with AsyncSessionLocal() as db:
        state = (
            await db.execute(select(FileNode.state).where(FileNode.target_object_id == chat_id))
        ).scalar_one()
        pending = list(
            (
                await db.execute(
                    select(FileOp.state).where(
                        FileOp.org_team_id == org_admin.org_id, FileOp.kind == OP_ACL_REWRITE
                    )
                )
            ).scalars()
        )
    assert state == ACL_REWRITING, "the chat's node really is mid-repair"
    assert pending and set(pending) == {"queued"}, "and every repair is still waiting to run"

    async with app_client() as other:
        await login(other, member.email, password)
        response = await _send(other, chat["id"], "m1")

    assert response.status_code == 201, response.text
    assert [row.payload["client_id"] for row in await _messages(chat_id)] == ["m1"]
    assert await _decisions(org_admin.org_id, chat_id) == [("send", "allow", "writer_may_send")]


async def test_a_revoked_grant_is_gone_the_instant_it_is_revoked_mid_repair(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The other direction of the same read, so the fallback cannot be a
    rubber stamp: with the node marked, the rung comes from the chain's live
    grants, and a revoked one is not among them.

    A person holds one share on one node, so withdrawing their own grant
    withdraws all of their access. The "Can view" the member keeps therefore
    comes from a team they are on: what the revoke takes away is exactly the
    write, and they are refused the send rather than losing the chat."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, chat_id)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    reviewers = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name=f"Reviewers {chat_id.hex[:6]}"
    )
    await membership_service.add_member(real_session, team_id=reviewers.id, user_id=member.id)
    await real_session.commit()
    await share_chat_with(
        real_session,
        chat=chat_row,
        owner=owner,
        principal=Principal(kind="team", id=reviewers.id),
        role=ROLE_READER,
    )
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(real_session, ctx) as repo:
        node = (
            await repo.session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == chat_id, FileNode.trashed_at.is_(None)
                )
            )
        ).scalar_one()
        share = next(
            s
            for s in await repo.shares_of(NodeId(node.id))
            if s.principal_id == member.id and s.role == ROLE_WRITER
        )
        await files_acl.revoke(repo, ctx, node, share.id)
        await files_acl.invalidate_subtree(repo, ctx, node)
    await real_session.commit()

    async with app_client() as other:
        await login(other, member.email, password)
        response = await _send(other, chat["id"], "m1")

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == chat_policy.SEND_DENIED_CODE
    assert await _messages(chat_id) == []


@pytest.mark.parametrize(
    ("role", "can_send"),
    [
        pytest.param(ROLE_READER, False, id="shared-can-view"),
        pytest.param(ROLE_COMMENTER, False, id="shared-can-comment"),
        pytest.param(ROLE_WRITER, True, id="shared-can-edit"),
    ],
)
async def test_a_chat_read_says_whether_this_reader_may_speak_in_it(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    role: str,
    can_send: bool,
) -> None:
    """``can_send`` on a read is the send route's own answer, ahead of time.

    A composer rendered on the read alone offers a box that answers 403 to the
    rung below ``writer``, and the reader finds that out by typing. The flag is
    asserted against what the send ACTUALLY does on the same rung, so it cannot
    drift into a second opinion; and the read files no SEND decision, because
    nothing was attempted.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, chat_id)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=role)

    async with app_client() as other:
        await login(other, member.email, password)
        single = await other.get(f"/api/v1/chats/{chat['id']}")
        listed = await other.get("/api/v1/chats")
        assert single.status_code == 200, single.text
        assert listed.status_code == 200, listed.text
        # Reading said nothing about a send, so nothing is on the audit lane.
        assert await _decisions(org_admin.org_id, chat_id) == []
        sent = await _send(other, chat["id"], "c1")

    assert single.json()["can_send"] is can_send
    rows = {row["id"]: row for row in listed.json()["items"]}
    assert rows[chat["id"]]["can_send"] is can_send
    assert (sent.status_code == 201) is can_send, sent.text


async def test_the_owner_reads_their_own_chat_as_one_they_may_speak_in(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The owner needs no grant to send, so the flag must not be read off the
    share ladder alone — a chat nobody has shared is still the owner's to drive."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)

    assert chat["can_send"] is True, "the create's own read already knows"
    single = await client.get(f"/api/v1/chats/{chat['id']}")
    listed = await client.get("/api/v1/chats")

    assert single.json()["can_send"] is True
    assert [row["can_send"] for row in listed.json()["items"] if row["id"] == chat["id"]] == [True]


async def test_an_unverified_member_who_holds_the_rung_still_reads_can_send_false(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Every gate the policy applies to a send applies to the flag.

    Email verification is decided after the rung, so a member shared at "Can
    edit" on an unverified address may read the chat and may not drive it. A
    flag derived from the rung alone would tell them otherwise and the composer
    would open onto a 403.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=False)
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)

    async with app_client() as other:
        await login(other, member.email, password)
        single = await other.get(f"/api/v1/chats/{chat['id']}")
        sent = await _send(other, chat["id"], "u1")

    assert single.status_code == 200, single.text
    assert single.json()["can_send"] is False
    assert sent.status_code == 403, sent.text
