"""The cloud chat's seams, end to end, against a real uvicorn.

Every hop a browser and a daemon actually make, over real TCP, with nothing
patched: log in, create a chat over REST, open the chat's document over the
socket, post a message over REST, watch the relay arrive on the socket, find
the row in ``chat_messages`` with the sequence the server assigned, and page it
back over REST. Then the machine half: a chat created with no machine says so,
and once the daemon has registered (and then heartbeated) a machine, the next
chat is bound to it and says ``starting`` (then ``ready``).

This is the integration check for three lanes meeting on one row: the chat's
workspace object is its declaration (the socket subscribes through it), the
authz policies decide REST, and the placement resolver binds the machine.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.authz import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, read_after
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import ChatMessage, User, WorkspaceObject
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member, mint_cli_token, served_client
from tests.test_ws_gateway import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


async def _transcript(chat_id: str) -> list[ChatMessage]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.chat_id == UUID(chat_id))
            .order_by(ChatMessage.seq)
        )
        return list(rows.scalars().all())


async def test_a_chat_created_over_rest_is_relayed_over_the_socket_and_paged_back(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)

        # No machine is registered: the chat is created and says so.
        current = await browser.get("/api/v1/machines/current")
        assert current.status_code == 200, current.text
        assert current.json()["status"] == "none"
        created = await browser.post("/api/v1/chats", json={"title": "Why are prompts down?"})
        assert created.status_code == 201, created.text
        chat = created.json()
        assert chat["machine_id"] is None
        assert chat["machine_status"] == "none"
        assert chat["last_seq"] == 0
        chat_id: str = chat["id"]
        channel = f"doc:chat:{chat_id}"

        async with connect(uvicorn_server, browser) as viewer:
            # The creator owns the document: the workspace object is the
            # declaration the socket resolved.
            assert await viewer.subscribe(channel) is True
            snapshot = await viewer.hello(channel)
            assert snapshot["epoch"] == 1
            assert snapshot["payload"]["state"]["events"] == []

            posted = await browser.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={"text": "hello from the browser", "client_id": "c-1"},
            )
            assert posted.status_code == 201, posted.text
            message = posted.json()
            assert (message["seq"], message["role"], message["kind"]) == (1, "user", "prompt")

            relayed = await viewer.next_doc("op")
            assert relayed["payload"]["intent"] == "user_message"
            (event,) = relayed["payload"]["events"]
            assert event["kind"] == "prompt"
            assert event["seq"] == 1
            assert event["text"] == "hello from the browser"
            assert event["client_id"] == "c-1"
            assert event["message_id"] == message["id"]
            assert event["user_id"] == str(org_admin.admin_id)

        rows = await _transcript(chat_id)
        assert [(row.seq, row.role, row.event_id) for row in rows] == [(1, "user", "usr:c-1")]
        assert rows[0].payload["text"] == "hello from the browser"

        page = await browser.get(f"/api/v1/chats/{chat_id}/messages")
        assert page.status_code == 200, page.text
        body = page.json()
        assert [item["seq"] for item in body["items"]] == [1]
        assert body["items"][0]["id"] == message["id"]
        assert body["next_after_seq"] == 1
        assert body["resync_from"] is None
        # Both backward-paging fields are at their floor on a forward read: the
        # cursor to page further back is a backward-read answer, and a forward
        # read never claims older rows. A reader arriving on this chat is not
        # offered a scroll-up into a transcript with nothing above it.
        assert body["prev_before"] is None
        assert body["has_older"] is False
        after = await browser.get(f"/api/v1/chats/{chat_id}/messages", params={"after_seq": 1})
        assert after.json() == {
            "items": [],
            "next_after_seq": 1,
            "resync_from": None,
            "prev_before": None,
            "has_older": False,
            "cut": False,
        }

        listed = await browser.get(f"/api/v1/chats/{chat_id}")
        assert listed.status_code == 200
        assert listed.json()["last_seq"] == 1


async def test_a_chat_is_bound_to_the_machine_the_daemon_registered(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The placement seam over the wire: before a machine exists a chat says
    ``none``; a registered machine that has never heartbeated is ``starting``;
    one heartbeat later it is ``ready`` — and the chat row records the binding
    the resolver answered with."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    daemon_headers = {"Authorization": f"Bearer {token}", **agent_headers("sess-e2e-seams")}

    async with served_client(uvicorn_server) as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        before = await browser.post("/api/v1/chats", json={"title": "before the box"})
        assert before.status_code == 201, before.text
        assert before.json()["machine_status"] == "none"

        async with AsyncClient(base_url=f"http://{uvicorn_server}") as daemon:
            registered = await daemon.post(
                "/api/v1/machines/register",
                json={
                    "provider": "runpod",
                    "provider_pod_id": "pod-e2e-seams",
                    "name": "demo-box",
                    "machine_type_code": machine_type.provider_type_id,
                },
                headers=daemon_headers,
            )
            assert registered.status_code == 201, registered.text
            machine_id: str = registered.json()["id"]

            starting = await browser.post("/api/v1/chats", json={"title": "while it boots"})
            assert starting.status_code == 201, starting.text
            assert starting.json()["machine_id"] == machine_id
            assert starting.json()["machine_status"] == "starting"
            assert (await browser.get("/api/v1/machines/current")).json()["status"] == "starting"

            beat = await daemon.post(
                f"/api/v1/machines/{machine_id}/heartbeat", headers=daemon_headers
            )
            assert beat.status_code == 204, beat.text

        ready = await browser.post("/api/v1/chats", json={"title": "once it answers"})
        assert ready.status_code == 201, ready.text
        assert ready.json()["machine_id"] == machine_id
        assert ready.json()["machine_status"] == "ready"
        current = await browser.get("/api/v1/machines/current")
        assert current.json()["machine_id"] == machine_id
        assert current.json()["status"] == "ready"


async def _daemon_socket(addr: str, *, token: str, agent_id: str | None) -> tuple[AsyncClient, str]:
    """A ticket minted the way the mirror mints one: the device JWT plus the
    agent assertion (``None`` for a bare Bearer), and the client it came from."""
    headers = {"Authorization": f"Bearer {token}"}
    if agent_id is not None:
        headers.update(agent_headers(agent_id))
    daemon = AsyncClient(base_url=f"http://{addr}", headers=headers)
    minted = await daemon.post("/api/v1/ws/tickets")
    assert minted.status_code == 200, minted.text
    return daemon, str(minted.json()["ticket"])


async def _chat_row(db: AsyncSession, chat_id: str) -> WorkspaceObject:
    """The chat's row, refreshed from the served app's own transaction — the
    REST creation happened on the uvicorn side of the socket, so this session
    has never seen it."""
    row = await db.get(WorkspaceObject, UUID(chat_id))
    assert row is not None
    return row


async def _user_row(db: AsyncSession, user_id: UUID) -> User:
    row = await db.get(User, user_id)
    assert row is not None
    return row


async def _doc_op_actors(org_id: UUID, channel: str) -> list[dict[str, object]]:
    async with AsyncSessionLocal() as db:
        rows = await read_after(db, after_id=0, org_id=org_id, limit=1000)
    return [
        dict(row.actor or {})
        for row in rows
        if row.type == EventType.DOC_OP.value and row.entity_id == channel
    ]


async def test_the_registered_machine_publishes_a_chat_a_member_created(
    files_on: None,  # noqa: F811
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
) -> None:
    """The cross-user seam, over real TCP: a non-admin member creates a chat,
    which binds to the machine the operator's daemon registered. The daemon's
    socket — the OPERATOR's device JWT, minted as the agent whose id is that
    machine's — writes the member's chat: its appends and its meta land, its
    chunks reach the member's viewer socket, the transcript is durable, and
    every ``doc.op`` it wrote is audited as ``[operator, machine]``. The same
    device JWT opened as another agent, or as no agent, is a reader: refused
    ``forbidden`` on every write, with the chat left untouched.

    The operator reads the member's chat because the member SHARED it at the
    reader rung — a chat is private until it is shared, so being the org's
    admin buys no sight of it. That share is what makes the last leg mean
    "a reader may not write" rather than "a stranger cannot find it"."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    operator_token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    member, member_password = await make_member(
        real_session, org_id=org_admin.org_id, verified=True
    )
    assert member_password is not None

    async with AsyncClient(base_url=f"http://{uvicorn_server}") as registrar:
        registered = await registrar.post(
            "/api/v1/machines/register",
            json={
                "provider": "runpod",
                "provider_pod_id": "pod-cross-user",
                "name": "demo-box",
                "machine_type_code": machine_type.provider_type_id,
            },
            headers={"Authorization": f"Bearer {operator_token}", **agent_headers("booting")},
        )
        assert registered.status_code == 201, registered.text
        machine_id: str = registered.json()["id"]

    async with served_client(uvicorn_server) as browser:
        await login(browser, member.email, member_password)
        created = await browser.post("/api/v1/chats", json={"title": "Why are prompts down?"})
        assert created.status_code == 201, created.text
        chat = created.json()
        assert chat["owner_user_id"] == str(member.id)
        assert chat["machine_id"] == machine_id
        chat_id: str = chat["id"]
        channel = f"doc:chat:{chat_id}"

        # The member shares their chat with the operator at the reader rung.
        await share_chat(
            real_session,
            chat=await _chat_row(real_session, chat_id),
            owner=member,
            user=await _user_row(real_session, org_admin.admin_id),
            role=ROLE_READER,
        )

        publisher_client, publisher_ticket = await _daemon_socket(
            uvicorn_server, token=operator_token, agent_id=machine_id
        )
        async with (
            connect(uvicorn_server, browser) as viewer,
            connect(uvicorn_server, publisher_client, ticket=publisher_ticket) as publisher,
        ):
            assert await viewer.subscribe(channel) is True, "the member owns their chat"
            await viewer.hello(channel)
            assert await publisher.subscribe(channel) is True, "the bound machine publishes it"
            snapshot = await publisher.hello(channel)
            epoch = int(snapshot["epoch"])

            ack = await publisher.op(
                channel,
                epoch=epoch,
                intent="append",
                op_id="append-e1",
                events=[
                    {
                        "event_id": "e1",
                        "event_type": "message.created",
                        "role": "assistant",
                        "payload": {"message_id": "m1"},
                    }
                ],
            )
            assert ack["kind"] == "ack", ack
            assert ack["payload"]["changed"] is True and ack["payload"]["seq"] == 1
            landed = await viewer.next_doc("op")
            assert landed["payload"]["intent"] == "append"
            assert landed["payload"]["events"][0]["event_id"] == "e1"

            meta = await publisher.op(
                channel,
                epoch=epoch,
                intent="set_meta",
                meta={"turn_state": {"state": "working"}},
            )
            assert meta["kind"] == "ack", meta
            assert (await viewer.next_doc("op"))["payload"]["intent"] == "set_meta"

            await publisher.send(
                publisher.envelope(
                    channel,
                    "op",
                    epoch=epoch,
                    payload={
                        "op_id": "chunk-1",
                        "intent": "chunk",
                        "events": [
                            {
                                "event_id": "k1",
                                "event_type": "agent.message_chunk",
                                "text": "Thinking",
                            }
                        ],
                    },
                )
            )
            chunk = await viewer.next_doc("op")
            assert chunk["seq"] == 0 and chunk["payload"]["intent"] == "chunk"
            assert chunk["payload"]["events"][0]["text"] == "Thinking"
            await publisher.expect_nothing(0.5)
        await publisher_client.aclose()

        rows = await _transcript(chat_id)
        assert [(row.seq, row.role, row.event_id) for row in rows] == [(1, "assistant", "e1")]
        actors = await _doc_op_actors(org_admin.org_id, channel)
        assert actors, "every durable write left a doc.op row"
        for actor in actors:
            acting = actor["acting"]
            assert isinstance(acting, dict)
            assert (acting["kind"], acting["id"]) == ("agent", machine_id)
            delegating = actor["delegating_user"]
            assert isinstance(delegating, dict)
            assert (delegating["kind"], delegating["id"]) == ("user", str(org_admin.admin_id))
            chain = actor["chain"]
            assert isinstance(chain, list) and [link["kind"] for link in chain] == [
                "user",
                "agent",
            ]

        # The same credential as anyone but the bound machine is a reader.
        for agent_id, label in ((None, "no assertion"), ("machine:demo-box", "another agent")):
            other_client, other_ticket = await _daemon_socket(
                uvicorn_server, token=operator_token, agent_id=agent_id
            )
            async with connect(uvicorn_server, other_client, ticket=other_ticket) as reader:
                assert await reader.subscribe(channel) is False, label
                await reader.hello(channel)
                denied = await reader.op(
                    channel,
                    epoch=epoch,
                    intent="append",
                    events=[{"event_id": "e2", "event_type": "message.created"}],
                )
                assert denied["kind"] == "error" and denied["payload"]["code"] == "forbidden"
                denied_meta = await reader.op(
                    channel, epoch=epoch, intent="set_meta", meta={"turn_state": {}}
                )
                assert denied_meta["payload"]["code"] == "forbidden", label
                await reader.send(
                    reader.envelope(
                        channel,
                        "op",
                        epoch=epoch,
                        payload={
                            "op_id": "chunk-x",
                            "intent": "chunk",
                            "events": [
                                {"event_id": "kx", "event_type": "agent.message_chunk", "text": "x"}
                            ],
                        },
                    )
                )
                refused_chunk = await reader.next_doc("error")
                assert refused_chunk["payload"]["code"] == "forbidden", label
            await other_client.aclose()

        assert [(row.seq, row.event_id) for row in await _transcript(chat_id)] == [(1, "e1")], (
            "a refused socket leaves the transcript as it was"
        )
        page = await browser.get(f"/api/v1/chats/{chat_id}/messages")
        assert [item["event_id"] for item in page.json()["items"]] == ["e1"]


async def test_the_socket_relay_and_the_rest_route_agree_on_who_may_send(
    files_on: None,  # noqa: F811
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
) -> None:
    """Two doors, one SEND gate. An unverified member is refused over REST with
    ``email_verification_required``; the same member relaying a ``prompt`` over
    the socket is refused with the same code and message in the error frame,
    nothing reaches the other peers, and nothing is written. A verified member's
    relay through the same socket path is relayed and recorded.

    Both members hold the SAME writer rung on the chat, granted by its owner —
    so the only thing that differs between them is whether their address is
    verified, and the refusal cannot be the policy quietly hiding the chat."""
    cold, cold_password = await make_member(real_session, org_id=org_admin.org_id, verified=False)
    warm, warm_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert cold_password is not None and warm_password is not None
    async with (
        served_client(uvicorn_server) as owner,
        served_client(uvicorn_server) as cold_browser,
        served_client(uvicorn_server) as warm_browser,
    ):
        await login(owner, org_admin.admin_email, org_admin.admin_password)
        await login(cold_browser, cold.email, cold_password)
        await login(warm_browser, warm.email, warm_password)
        created = await owner.post("/api/v1/chats", json={"title": "Two doors"})
        assert created.status_code == 201, created.text
        chat_id: str = created.json()["id"]
        channel = f"doc:chat:{chat_id}"

        chat_row = await _chat_row(real_session, chat_id)
        admin = await _user_row(real_session, org_admin.admin_id)
        for member in (cold, warm):
            await share_chat(
                real_session, chat=chat_row, owner=admin, user=member, role=ROLE_WRITER
            )

        # The REST door's answer, verbatim: the socket must give the same one.
        refused = await cold_browser.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": "hello", "client_id": "cold-1"}
        )
        assert refused.status_code == 403, refused.text
        rest_error = refused.json()["error"]
        assert rest_error["code"] == "email_verification_required"

        async with (
            connect(uvicorn_server, owner) as viewer,
            connect(uvicorn_server, cold_browser) as cold_socket,
            connect(uvicorn_server, warm_browser) as warm_socket,
        ):
            assert await viewer.subscribe(channel) is True
            await viewer.hello(channel)
            # Both members hold the same writer rung, and ``can_write`` reports
            # that rung — the verification gate is a separate one, applied when
            # the op arrives, so the two sockets are indistinguishable here and
            # everything below is the gate alone.
            assert await cold_socket.subscribe(channel) is True
            await cold_socket.hello(channel)
            assert await warm_socket.subscribe(channel) is True
            await warm_socket.hello(channel)

            error = await cold_socket.op(
                channel,
                epoch=1,
                intent="user_message",
                events=[
                    {"event_id": "u-cold", "kind": "prompt", "text": "hello", "client_id": "cold-1"}
                ],
            )
            assert error["kind"] == "error", error
            assert error["payload"]["code"] == rest_error["code"]
            assert error["payload"]["message"] == rest_error["message"]

            ack = await warm_socket.op(
                channel,
                epoch=1,
                intent="user_message",
                events=[
                    {
                        "event_id": "u-warm",
                        "kind": "prompt",
                        "text": "hi all",
                        "client_id": "warm-1",
                    }
                ],
            )
            assert ack["kind"] == "ack", ack

            # The first relay the owner's socket sees is the verified member's:
            # the refused one never left the server.
            relayed = await viewer.next_doc("op")
            (event,) = relayed["payload"]["events"]
            assert event["client_id"] == "warm-1"
            assert event["user_id"] == str(warm.id)
            assert event["seq"] == 1
            assert not [
                frame
                for frame in viewer.skipped
                if frame.get("t") == "doc" and frame["envelope"]["kind"] == "op"
            ]

    rows = await _transcript(chat_id)
    assert [(row.seq, row.event_id) for row in rows] == [(1, "usr:warm-1")]
