"""One input, every door: may an org admin open a chat nobody shared with them?

``settings.chat_org_admin_reads_private`` is a tenancy boundary controlled by a
boolean, and the only thing that makes it safe to flip is that every door onto a
chat asks it — the chat route, the object route, the objects LIST, the promote
that lifts content out of a conversation, and the socket channel that streams
it. A door that answers from its own rule is how a private chat came to be
listed for an admin who was 404'd when they opened it, and how a colleague
holding "Can view" came to be missing from a list that showed them the chat.

So the tests below run the SAME five doors at both values of the setting. Off —
the shipped default — an unshared org admin gets nothing anywhere. On, they read
everywhere and still cannot rename or send.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import EventOutbox, User, WorkspaceObject
from backend.services.realtime import channels as channel_service
from backend.services.realtime.channels import Channel
from backend.services.realtime.filters import load_entitlements
from httpx import AsyncClient
from sqlalchemy import select
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@pytest.fixture
def admin_reads_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment that lets an org admin open a private chat."""
    monkeypatch.setattr(settings, "chat_org_admin_reads_private", True)


async def _member_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> tuple[dict[str, Any], User]:
    """A chat owned by an ordinary member, never shared with anybody — and the
    admin left signed in, because every probe below is the admin's."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await login(client, member.email, password)
    created = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    return body, member


async def _listed(client: AsyncClient) -> set[str]:
    response = await client.get("/api/v1/objects", params={"type": "chat"})
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


async def _subscribes(real_session: Any, user: User, chat_id: str) -> bool:
    """Whether this user's socket is admitted to the chat's channel — the real
    channel authorization the WS gateway runs at subscribe."""
    try:
        await channel_service.authorize(
            real_session,
            user,
            Channel("chat", chat_id),
            ent=await load_entitlements(real_session, user, org_id=user.home_org_team_id),
            agent_id=None,
        )
    except channel_service.ChannelError as refused:
        assert refused.code == "not_found", refused.code
        return False
    return True


async def _admin(real_session: Any, org_admin: OrgWithAdmin) -> User:
    user = await real_session.get(User, org_admin.admin_id)
    assert user is not None
    return user


# -- the shipped default: the admin gets nothing, through any door ----------


async def test_an_unshared_org_admin_is_refused_by_every_door(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    chat, _member = await _member_chat(client, org_admin, real_session)
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
    assert (await client.get(f"/api/v1/objects/{chat['id']}")).status_code == 404
    assert chat["id"] not in await _listed(client)
    assert await _subscribes(real_session, await _admin(real_session, org_admin), chat["id"]) is (
        False
    )


async def test_the_objects_list_never_carries_a_private_chats_contents(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The leak this list had was not the id — it was the row: the title, the
    owner and the whole spec, which is the payload the by-id door was closed
    over. Asserting absence from the id set alone would pass on a body that
    still carried the title."""
    chat, _member = await _member_chat(client, org_admin, real_session)
    response = await client.get("/api/v1/objects", params={"type": "chat"})
    assert response.status_code == 200
    assert "Quarterly review" not in response.text
    assert chat["id"] not in response.text


async def test_a_shared_reader_is_in_the_list_the_admin_is_not_in(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The inversion, from the other end. Before the list asked the chat's own
    question, the person holding no rung saw the chat and the person holding
    one did not — the same inversion the rename door had."""
    chat, member = await _member_chat(client, org_admin, real_session)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    colleague, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=member, user=colleague, role=ROLE_READER)

    await login(client, colleague.email, password)
    assert chat["id"] in await _listed(client)

    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert chat["id"] not in await _listed(client)


async def test_the_owners_own_chat_is_still_in_their_list(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The control: cutting the list by the chat's own predicate must not cut
    the owner out of it."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await login(client, member.email, password)
    created = await client.post("/api/v1/chats", json={"title": "Mine"})
    assert created.status_code == 201
    assert created.json()["id"] in await _listed(client)


async def test_a_promoted_result_is_not_a_way_around_the_closed_doors(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Promoting mints a durable object decided by the OBJECT policy, which an
    org admin reads. So promoting out of a chat they may not read would hand
    them its contents through the door beside the one that refused them."""
    chat, _member = await _member_chat(client, org_admin, real_session)
    promoted = await client.post(
        f"/api/v1/chats/{chat['id']}/promote", json={"event_id": "ev-1", "title": "Rows"}
    )
    assert promoted.status_code == 404, promoted.text
    assert await _decisions(UUID(chat["id"]), "promote") == [("deny", "not_in_audience")]


async def test_the_owner_may_still_promote_out_of_their_own_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The control for the gate above: it must close the admin's path without
    closing the one the feature exists for."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await login(client, member.email, password)
    created = await client.post("/api/v1/chats", json={"title": "Mine"})
    assert created.status_code == 201
    promoted = await client.post(
        f"/api/v1/chats/{created.json()['id']}/promote",
        json={"event_id": "ev-1", "title": "Rows"},
    )
    assert promoted.status_code == 201, promoted.text


async def test_nor_may_an_org_admin_delete_a_chat_they_cannot_open(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A chat the admin cannot read is the same not-found on DELETE as on every
    other door: deleting never tells them it exists, and never ends it unseen."""
    chat, _member = await _member_chat(client, org_admin, real_session)
    refused = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["message"] == "Not found"


@pytest.mark.usefixtures("admin_reads_private")
async def test_the_flag_lets_an_org_admin_delete_what_it_lets_them_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    chat, _member = await _member_chat(client, org_admin, real_session)
    assert (await client.delete(f"/api/v1/chats/{chat['id']}")).status_code == 204


# -- the deployment that says yes: every door follows, and only for reading --


@pytest.mark.usefixtures("admin_reads_private")
async def test_the_flag_opens_every_door_including_the_socket(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The socket is the door this setting was most likely to leave behind: its
    channel rules are a separate module with their own copy of the question.
    With the flag on and the socket not following, the admin would open a chat
    over REST that never streams a frame."""
    chat, _member = await _member_chat(client, org_admin, real_session)
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 200
    assert (await client.get(f"/api/v1/objects/{chat['id']}")).status_code == 200
    assert chat["id"] in await _listed(client)
    assert await _subscribes(real_session, await _admin(real_session, org_admin), chat["id"]) is (
        True
    )


@pytest.mark.usefixtures("admin_reads_private")
async def test_the_flag_admits_a_reader_and_nothing_more(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Reading is the whole branch. The admin the setting admits still holds no
    rung, so they may not retitle the conversation and may not drive its agent.
    """
    chat, _member = await _member_chat(client, org_admin, real_session)
    version = (await real_session.get(WorkspaceObject, UUID(chat["id"]))).version
    renamed = await client.put(
        f"/api/v1/objects/{chat['id']}",
        json={"title": "Renamed", "expected_version": version},
    )
    assert renamed.status_code == 403, renamed.text
    assert renamed.json()["error"]["code"] == "chat_rename_requires_edit"

    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "hello", "client_id": "c-1"}
    )
    assert sent.status_code == 403, sent.text
    assert sent.json()["error"]["code"] == "chat_send_requires_edit"


@pytest.mark.usefixtures("admin_reads_private")
async def test_the_flag_names_itself_on_the_decision_row(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """An auditor has to be able to tell a read the deployment granted from one
    a colleague granted, so the reason is the setting's own name."""
    chat, _member = await _member_chat(client, org_admin, real_session)
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 200
    assert await _decisions(UUID(chat["id"]), "read") == [("allow", "org_admin_reads_private")]


@pytest.mark.usefixtures("admin_reads_private")
async def test_the_flag_admits_the_admin_and_still_nobody_else(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The setting is about admins. An ordinary member the chat was never
    shared with is refused at both values of it."""
    chat, _member = await _member_chat(client, org_admin, real_session)
    stranger, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await login(client, stranger.email, password)
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
    assert chat["id"] not in await _listed(client)
    assert await _subscribes(real_session, stranger, chat["id"]) is False


async def _decisions(chat_id: UUID, action: str) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.type == "authz.decision",
                    EventOutbox.entity_id == str(chat_id),
                )
                .order_by(EventOutbox.id)
            )
        ).scalars()
    return [
        (row.payload["effect"], row.payload["reason"])
        for row in rows
        if row.payload["action"] == action
    ]
