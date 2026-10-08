"""A chat attachment is a Files node linked to a chat — nothing more.

The two decisions the link route makes (``SEND`` on the chat, ``READ`` on the
node through the Files decider) are both on record, and neither is a grant: the
listing decides every node again per request, so a chat shared more widely than
a file does not widen the file. A message body cannot attach — it may only name
what is already linked — which is what keeps the SEND-plus-READ pair from being
skippable by posting a message instead.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from _files_kit import NOT_FOUND, refusal
from _oracle import counting
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key
from alkera_core.db.session import AsyncSessionLocal, engine
from alkera_core.files import repo as files_repo
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.files.errors import Conflict
from alkera_core.models import (
    ChatAttachment,
    EventOutbox,
    FileDrive,
    FileHistory,
    FileVersion,
    User,
    WorkspaceObject,
)
from alkera_core.schemas.objects import CHAT_RELAY_ADAPTER
from backend.api.routes.chats import chats as chat_routes
from backend.services.chats import chat_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import Uuid, event, func, literal, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import share_chat
from tests.conftest import app_client, login
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

#: The app and the fixtures share one process-wide async engine; its sync
#: facade is what the statement listener binds to.
SYNC_ENGINE = engine.sync_engine

#: Any statement naming a Files table. A plain message post must issue none:
#: building the Files context inserts a store row and commits a drive skeleton.
FILES_TABLE: Final[re.Pattern[str]] = re.compile(r"\bfile_\w+|\bdedup_domains\b")


async def _chat(client: AsyncClient) -> str:
    response = await client.post("/api/v1/chats", json={"clientId": secrets.token_hex(8)})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _decisions(resource_type: str, action: str) -> list[dict[str, Any]]:
    """Every recorded decision on one resource type for one action."""
    async with AsyncSessionLocal() as session:
        rows = (
            (await session.execute(select(EventOutbox).where(EventOutbox.type == "authz.decision")))
            .scalars()
            .all()
        )
    payloads = [dict(row.payload or {}) for row in rows]
    return [
        payload
        for payload in payloads
        if payload.get("action") == action
        and (payload.get("resource") or {}).get("type") == resource_type
    ]


async def _other_org(client: AsyncClient) -> tuple[AsyncClient, uuid.UUID, uuid.UUID]:
    """A logged-in, verified admin of a DIFFERENT org, and that org's ids."""
    async with AsyncSessionLocal() as session:
        org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other {secrets.token_hex(4)}",
            admin_email=f"other-{secrets.token_hex(4)}@example.com",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="other-pass-12345",
        )
        admin.email_verified_at = datetime.now(UTC)
        email, org_id, admin_id = admin.email, org.id, admin.id
        await session.commit()
    other = app_client()
    await login(other, email, "other-pass-12345")
    return other, org_id, admin_id


async def _linked(chat_id: str) -> list[str]:
    """The node ids this chat holds, in attach order — read off the rows the
    link IS, not off the chat's spec: the spec array is a back-filled relic."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ChatAttachment.node_id)
            .where(ChatAttachment.chat_id == uuid.UUID(chat_id))
            .order_by(ChatAttachment.position, ChatAttachment.node_id)
        )
        return [str(node_id) for node_id in rows.scalars().all()]


async def test_attaching_a_readable_node_links_it_and_records_both_decisions(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    node = await fx.node(b"report.json", size=2048)
    await real_session.commit()
    chat_id = await _chat(files_client)

    response = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
    )

    assert response.status_code == 201, response.text
    assert response.json()["nodeId"] == str(node.id)
    assert response.json()["name"] == "report.json"
    assert await _linked(chat_id) == [str(node.id)]
    # Both halves of the contract are on the audit lane, not just the chat's.
    assert await _decisions("chat", "send"), "the chat's SEND decision is not on record"
    reads = await _decisions("file_node", "read")
    assert str(node.id) in {(row.get("resource") or {}).get("id") for row in reads}


async def test_attaching_the_same_node_twice_links_it_once(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    node = await fx.node(b"once.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    body = {"nodeId": str(node.id)}

    first = await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json=body)
    second = await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json=body)

    assert (first.status_code, second.status_code) == (201, 201)
    assert await _linked(chat_id) == [str(node.id)]


async def test_a_node_of_another_org_is_an_opaque_not_found(
    files_client: AsyncClient, client: AsyncClient
) -> None:
    """The chat is the caller's; the node is not. The refusal must not tell the
    two apart from a node that never existed."""
    chat_id = await _chat(files_client)
    _, org_id, admin_id = await _other_org(client)
    async with AsyncSessionLocal() as session:
        node = await FilesFixtures(session, org_id, admin_id).node(b"theirs.json")
        await session.commit()
        foreign_id = str(node.id)

    real = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": foreign_id}
    )
    absent = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(uuid.uuid4())}
    )

    assert real.status_code == 404
    assert absent.status_code == 404
    assert refusal(real) == refusal(absent)
    assert await _linked(chat_id) == []


async def test_the_listing_omits_a_node_the_caller_cannot_read(
    files_client: AsyncClient, client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The stored list is a set of references, decided again per request: a
    node that has since gone unreadable is omitted, never served."""
    readable = await fx.node(b"kept.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(readable.id)}
        )
    ).status_code == 201

    _, org_id, admin_id = await _other_org(client)
    async with AsyncSessionLocal() as session:
        foreign = await FilesFixtures(session, org_id, admin_id).node(b"theirs.json")
        # Linked straight into the table: the route would never make this link
        # (it decides READ on the node first), and a reference to a node that
        # has since moved out of reach is exactly what the read must survive.
        session.add(
            ChatAttachment(
                chat_id=uuid.UUID(chat_id), node_id=uuid.UUID(str(foreign.id)), position=2
            )
        )
        await session.commit()

    listed = await files_client.get(f"/api/v1/chats/{chat_id}/attachments")

    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()["items"]]
    assert names == ["kept.json"]
    # The reference itself is untouched — omission is a read-time decision.
    assert len(await _linked(chat_id)) == 2


async def test_a_message_naming_an_unlinked_node_is_refused(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A body is not a way to attach: the SEND-plus-READ pair the link route
    makes cannot be skipped by posting a message instead."""
    node = await fx.node(b"unlinked.json")
    await real_session.commit()
    chat_id = await _chat(files_client)

    response = await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "look", "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "chat.attachment_not_linked"
    assert error["details"]["nodeId"] == str(node.id)
    # A refused body leaves no transcript entry.
    async with AsyncSessionLocal() as session:
        rows, _, _ = await chat_service.list_messages(
            session, chat_id=uuid.UUID(chat_id), after_seq=0, limit=10
        )
    assert rows == []


async def test_a_message_naming_a_linked_node_carries_a_file_part(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    node = await fx.node(b"data.json", size=4096)
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
    ).status_code == 201

    response = await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "read it", "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
    )

    assert response.status_code == 201, response.text
    async with AsyncSessionLocal() as session:
        rows, _, _ = await chat_service.list_messages(
            session, chat_id=uuid.UUID(chat_id), after_seq=0, limit=10
        )
    parts = rows[0].payload["attachments"]
    assert [part["node_id"] for part in parts] == [str(node.id)]
    assert parts[0]["source"] == "file"
    assert parts[0]["filename"] == "data.json"
    assert parts[0]["schema_version"] == "1.1.0"


async def test_the_relay_to_the_machine_carries_the_same_parts(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The box materializes files off the relay, so a part recorded but not
    relayed would be an attachment the agent can never see."""
    node = await fx.node(b"relayed.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})

    await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "go", "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
    )

    async with AsyncSessionLocal() as session:
        docs = (
            (
                await session.execute(
                    select(EventOutbox)
                    .where(
                        EventOutbox.type == "doc.op",
                        EventOutbox.entity_id == chat_service.channel_key(uuid.UUID(chat_id)),
                    )
                    .order_by(EventOutbox.id)
                )
            )
            .scalars()
            .all()
        )
    relays = [
        CHAT_RELAY_ADAPTER.validate_python(event)
        for doc in docs
        for event in ((doc.payload or {}).get("envelope") or {})
        .get("payload", {})
        .get("events", [])
        if isinstance(event, dict) and event.get("kind") == "prompt"
    ]
    assert relays, "the prompt relay never reached the machine's channel"
    assert [part.node_id for part in relays[-1].attachments] == [str(node.id)]


async def test_the_chat_detail_exposes_its_attachments(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    node = await fx.node(b"listed.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})

    detail = await files_client.get(f"/api/v1/chats/{chat_id}")

    assert detail.status_code == 200
    assert detail.json()["attachments"] == [str(node.id)]


async def test_a_chat_of_another_org_cannot_be_attached_to(
    files_client: AsyncClient, client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    node = await fx.node(b"mine.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    other, _, _ = await _other_org(client)

    response = await other.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
    )

    assert response.status_code == 404
    assert await _linked(chat_id) == []


# --------------------------------------------------------------------------
# The kill switch reaches the chat surface too
# --------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["POST", "GET"], ids=["attach", "list"])
async def test_the_attachment_routes_are_dark_when_files_is_disabled(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """The chat attachment surface is a Files consumer, so the flag has to
    reach it: with Files off it answers exactly what every route under the
    Files router answers, byte for byte.

    The node is real and already linked, so with the flag ON both calls
    succeed -- the 404 below can only be the kill switch, never a node the
    caller could not have reached anyway.
    """
    node = await fx.node(b"gated.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    body = {"nodeId": str(node.id)}
    url = f"/api/v1/chats/{chat_id}/attachments"
    lit = await files_client.request(verb, url, json=body if verb == "POST" else None)
    assert lit.status_code in (200, 201), lit.text

    monkeypatch.setattr(settings, "files_enabled", False)
    response = await files_client.request(verb, url, json=body if verb == "POST" else None)

    assert response.status_code == 404, response.text
    assert refusal(response) == NOT_FOUND


async def test_a_message_with_no_attachments_touches_no_files_table(
    files_client: AsyncClient,
) -> None:
    """A chat message is not a Files request. Building the Files context is an
    INSERT into ``file_stores`` plus a committed drive skeleton, so declaring
    it unconditionally would make every message in the product pay for -- and
    create -- Files state it never names."""
    chat_id = await _chat(files_client)
    # Warm whatever caches the first post pays for, so the count below is the
    # steady-state cost of a plain message.
    await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "warm", "client_id": secrets.token_hex(8)},
    )

    with counting(SYNC_ENGINE) as statements:
        response = await files_client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "hello", "client_id": secrets.token_hex(8)},
        )

    assert response.status_code == 201, response.text
    touched = [line for line in statements if FILES_TABLE.search(line)]
    assert touched == [], f"a plain message post issued {len(touched)} Files statements"


async def test_a_message_with_no_attachments_creates_no_drive_while_files_is_dark(
    files_client: AsyncClient, files_org: FilesOrgFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is a kill switch, not a preference: with Files off a message
    still posts, and it leaves this org no drive behind.

    Everything happens with the flag already off, so the org has never had a
    Files context built for it: any drive found below was built by the message
    post itself."""
    monkeypatch.setattr(settings, "files_enabled", False)
    chat_id = await _chat(files_client)

    response = await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "still works", "client_id": secrets.token_hex(8)},
    )

    assert response.status_code == 201, response.text
    async with AsyncSessionLocal() as session:
        drives = (
            await session.execute(
                select(func.count())
                .select_from(FileDrive)
                .where(FileDrive.org_team_id == files_org.org.org_id)
            )
        ).scalar_one()
    assert drives == 0, "a plain message post built a Files drive with the flag off"


async def test_a_message_naming_an_attachment_is_refused_while_files_is_dark(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attachment named while Files is dark reaches the same opaque 404 the
    Files routes give -- never a part built out of a context the flag forbids."""
    node = await fx.node(b"dark.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
    ).status_code == 201
    monkeypatch.setattr(settings, "files_enabled", False)

    response = await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "go", "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
    )

    assert response.status_code == 404, response.text
    assert refusal(response) == NOT_FOUND


# --------------------------------------------------------------------------
# Part identity
# --------------------------------------------------------------------------


async def test_the_recorded_part_carries_the_message_identity_not_the_chat(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A part is addressed by ``message_id``/``part_id``: stamping the chat id
    would make every message in a chat claim the same part."""
    node = await fx.node(b"identity.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})

    posted = await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "one", "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
    )

    assert posted.status_code == 201, posted.text
    entry = posted.json()
    part = entry["payload"]["attachments"][0]
    assert part["message_id"] != chat_id, "the part is stamped with the chat, not the message"
    assert part["message_id"] == entry["event_id"]
    assert part["part_id"] == f"{entry['event_id']}:att:0"


async def test_two_messages_in_one_chat_never_share_a_part_id(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    node = await fx.node(b"shared.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})

    ids: list[str] = []
    for text in ("first", "second"):
        posted = await files_client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": text, "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
        )
        assert posted.status_code == 201, posted.text
        ids.append(posted.json()["payload"]["attachments"][0]["part_id"])

    assert len(set(ids)) == 2, f"both messages claim the same part: {ids}"


# --------------------------------------------------------------------------
# Linking is a read
# --------------------------------------------------------------------------


async def test_linking_a_node_writes_no_file_history_row(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The link is decided as ``READ``. ``file_history`` is the node's write
    vocabulary -- an ``attrs`` row there would say the node's metadata changed,
    which reading it never does, and would put an entry a WRITE earns on a
    caller who only has read."""
    node = await fx.node(b"nohistory.json")
    await real_session.commit()
    chat_id = await _chat(files_client)

    response = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
    )

    assert response.status_code == 201, response.text
    async with AsyncSessionLocal() as session:
        rows = (
            (await session.execute(select(FileHistory).where(FileHistory.node_id == node.id)))
            .scalars()
            .all()
        )
    assert [row.kind for row in rows] == [], "linking wrote into the node's write history"
    # The reference itself is still recorded, on the chat, where it belongs.
    assert await _linked(chat_id) == [str(node.id)]


async def test_removing_an_attachment_takes_it_off_the_chat_and_is_idempotent(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A file taken off the composer stops being attached to the chat.

    Removing a chip used to drop it from the browser's own state only, so a
    node that had already linked stayed on the chat for every other reader.
    """
    node = await fx.node(b"withdrawn.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
    ).status_code == 201
    assert await _linked(chat_id) == [str(node.id)]

    removed = await files_client.delete(f"/api/v1/chats/{chat_id}/attachments/{node.id}")

    assert removed.status_code == 204, removed.text
    listed = await files_client.get(f"/api/v1/chats/{chat_id}/attachments")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert await _linked(chat_id) == []
    # A detach is the absence of a link, so doing it twice is not an error.
    again = await files_client.delete(f"/api/v1/chats/{chat_id}/attachments/{node.id}")
    assert again.status_code == 204, again.text
    assert await _linked(chat_id) == []


async def test_a_message_cannot_name_a_node_that_was_detached(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """Detaching really un-links: the body that named it is refused again."""
    node = await fx.node(b"gone.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    await files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})
    assert (
        await files_client.delete(f"/api/v1/chats/{chat_id}/attachments/{node.id}")
    ).status_code == 204

    refused = await files_client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={"text": "here", "client_id": secrets.token_hex(8), "attachments": [str(node.id)]},
    )

    assert refused.status_code == 422, refused.text
    assert "chat.attachment_not_linked" in refused.text


async def test_the_listing_writes_no_decision_row_per_attachment(
    files_client: AsyncClient, client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A pollable GET decides once, not once per linked node.

    Deciding each node through ``enforce()`` committed an ``authz.decision``
    row per unreadable attachment on EVERY request, so any member could grow
    the platform audit lane without bound by leaving a tab open.
    """
    readable = await fx.node(b"mine.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(readable.id)}
        )
    ).status_code == 201

    _, org_id, admin_id = await _other_org(client)
    async with AsyncSessionLocal() as session:
        foreign = FilesFixtures(session, org_id, admin_id)
        first = await foreign.node(b"a.json")
        second = await foreign.node(b"b.json")
        chat = await session.get(WorkspaceObject, uuid.UUID(chat_id))
        assert chat is not None
        chat.spec = {
            **chat.spec,
            "attachments": [str(readable.id), str(first.id), str(second.id)],
        }
        await session.commit()

    before = len(await _decisions("file_node", "read"))
    assert (await files_client.get(f"/api/v1/chats/{chat_id}/attachments")).status_code == 200
    assert (await files_client.get(f"/api/v1/chats/{chat_id}/attachments")).status_code == 200

    assert len(await _decisions("file_node", "read")) == before


async def test_a_transient_files_failure_does_not_hide_an_attachment(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal the route did not mean is answered, never rendered as absence.

    ``except FilesError: continue`` swallowed the whole Files vocabulary, so a
    ``Conflict`` or a store failure made the attachment vanish from the list —
    indistinguishable from a node the caller may not read.
    """
    node = await fx.node(b"present.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
    ).status_code == 201

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise Conflict("files.busy", "the drive is busy")

    monkeypatch.setattr(chat_routes, "facts_for", boom)

    listed = await files_client.get(f"/api/v1/chats/{chat_id}/attachments")

    assert listed.status_code != 200, listed.text
    assert listed.status_code >= 400


async def test_a_member_who_cannot_read_a_node_sees_it_as_unavailable(
    files_client: AsyncClient,
    client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """An attachment a chat member may not open is named as unavailable.

    Omitting it made "you may not open this" look identical to "the server
    could not answer" and quietly shortened a list whose length the chat's own
    spec already tells every member. The name is still withheld: it belongs to
    the file's audience, not the chat's.
    """
    node = await fx.node(b"secret.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    assert (
        await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
    ).status_code == 201
    # A chat is private until shared, so the member reaches it the only way
    # anybody but its owner does: a grant on the chat's node at the rung that
    # lets them read it. Flipping ``visibility_scope`` used to be enough and no
    # longer is — the scope names the audience a chat may be shared into, not a
    # reader.
    chat = await real_session.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    owner = await real_session.get(User, files_org.org.admin_id)
    assert owner is not None
    await share_chat(real_session, chat=chat, owner=owner, user=files_org.member, role=ROLE_READER)

    # A client of its own: ``files_client`` IS ``client``, so logging the
    # member in on it would take the admin's session with it.
    member = app_client()
    await login(member, files_org.member.email, files_org.member_password)
    listed = await member.get(f"/api/v1/chats/{chat_id}/attachments")

    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [item["nodeId"] for item in items] == [str(node.id)]
    assert items[0]["state"] == "unavailable"
    assert items[0]["name"] == ""
    # The admin still sees it in full — "unavailable" is per caller, per request.
    mine = await files_client.get(f"/api/v1/chats/{chat_id}/attachments")
    assert [item["name"] for item in mine.json()["items"]] == ["secret.json"]


def _bound(parameters: Any) -> list[Any]:
    """One statement's bound values, flat, whatever the driver was handed."""
    if isinstance(parameters, dict):
        return list(parameters.values())
    if isinstance(parameters, (list, tuple)):
        flat: list[Any] = []
        for value in parameters:
            flat.extend(_bound(value) if isinstance(value, (list, tuple, dict)) else [value])
        return flat
    return [parameters]


@contextmanager
def _binds(engine: Any) -> Iterator[list[tuple[str, list[Any]]]]:
    """Every statement executed in the block, with the values it bound."""
    seen: list[tuple[str, list[Any]]] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        seen.append((statement, _bound(parameters)))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_no_read_of_a_chats_heads_binds_more_ids_than_one_statement_holds(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The head version of every attachment is read a batch at a time.

    A chat's attachments are unbounded over its life and the head ids are bound
    one apiece, so a single ``IN`` over the whole list stops being a statement
    PostgreSQL will accept at 32767 of them: past that the driver refuses the
    query and the attachments tab answers 500 for good, for a list a member
    built one ordinary attach at a time. The batch size is pinned small here so
    the ceiling is reached with a handful of real attachments rather than forty
    thousand -- what is asserted is that no statement binds more ids than one
    batch, which is the property that keeps the real ceiling out of reach, and
    that a list longer than a batch still comes back whole with the size and
    mime that live on the head.
    """
    monkeypatch.setattr(files_repo, "ID_BATCH", 2)
    chat_id = await _chat(files_client)
    sizes = {}
    for index in range(5):
        node = await fx.node(f"attached-{index}.txt".encode())
        version = await fx.version(node, size_bytes=100 + index)
        sizes[str(node.id)] = int(version.size_bytes)
        linked = await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
        assert linked.status_code == 201, linked.text
    await real_session.commit()
    head_ids = {
        str(version_id)
        for version_id in (
            await real_session.execute(
                select(FileVersion.id).where(FileVersion.node_id.in_([uuid.UUID(n) for n in sizes]))
            )
        ).scalars()
    }
    assert len(head_ids) == 5

    with _binds(SYNC_ENGINE) as statements:
        response = await files_client.get(f"/api/v1/chats/{chat_id}/attachments")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {item["nodeId"]: item["size"] for item in items} == sizes
    assert {item["mime"] for item in items} == {"text/plain"}
    widest = max(
        (len([value for value in values if str(value) in head_ids]) for _sql, values in statements),
        default=0,
    )
    assert widest <= files_repo.ID_BATCH, (
        f"a statement bound {widest} head ids at a batch size of {files_repo.ID_BATCH}"
    )


#: How many links a chat holds in the growth tests. Far past anything the old
#: JSON array could carry in one statement, and past what a page returns, so a
#: cost that grew with the list shows up as a timeout rather than a pass.
GROWN = 100_000


async def _seed_links(chat_id: str, count: int) -> None:
    """``count`` links on this chat, in one statement.

    The ids name no live node, which is the point: the read must stay bounded
    over a list of any length, and what each id resolves to is decided per
    request afterwards. Positions carry on from the highest the chat already
    holds, the way an attach assigns one, so seeding after a real attach
    appends behind it instead of landing on top of its place in the order.
    """
    chat = uuid.UUID(chat_id)
    async with AsyncSessionLocal() as session:
        highest = int(
            (
                await session.execute(
                    select(func.coalesce(func.max(ChatAttachment.position), 0)).where(
                        ChatAttachment.chat_id == chat
                    )
                )
            ).scalar_one()
        )
        rows = select(
            literal(chat, Uuid),
            func.gen_random_uuid(),
            func.generate_series(highest + 1, highest + count).label("position"),
        )
        await session.execute(
            ChatAttachment.__table__.insert().from_select(["chat_id", "node_id", "position"], rows)
        )
        await session.commit()


async def test_attaching_to_a_chat_of_a_hundred_thousand_links_costs_what_the_first_one_did(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """An attach is one row, whatever the chat already holds.

    The links used to be a JSON array inside the chat's spec: attaching read
    every id back, appended and wrote all of them again under the chat row's
    lock, so the hundred-thousand-and-first attach carried a hundred thousand
    ids through the driver. What is asserted is the property that keeps that
    from coming back — no statement the attach issues binds more than a handful
    of values, at a list size where a whole-array rewrite could not hide.
    """
    node = await fx.node(b"the-next-one.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    await _seed_links(chat_id, GROWN)

    with _binds(SYNC_ENGINE) as statements:
        response = await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )

    assert response.status_code == 201, response.text
    widest = max((len(values) for _sql, values in statements), default=0)
    assert widest < 100, f"an attach bound {widest} values onto a chat holding {GROWN} links"
    # The new link is last, and the ones that were there are untouched.
    linked = await _linked(chat_id)
    assert linked[-1] == str(node.id)
    assert len(linked) == GROWN + 1


async def _cursor_at(chat_id: str, position: int) -> str:
    """The cursor a page that ended on this chat's link at ``position`` mints.

    Read off the row rather than spelled from the position alone: the cursor is
    the pair the order is taken on, so a test that jumps into the middle of a
    long list asks for the same place a reader would have arrived at.
    """
    async with AsyncSessionLocal() as session:
        node_id = (
            await session.execute(
                select(ChatAttachment.node_id).where(
                    ChatAttachment.chat_id == uuid.UUID(chat_id),
                    ChatAttachment.position == position,
                )
            )
        ).scalar_one()
    return f"{position}:{node_id}"


async def test_a_chat_of_a_hundred_thousand_links_reads_one_bounded_page_at_a_time(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The attachments tab reads a page, never the chat's whole history.

    Every linked id is decided again on the read, so a tab that read all of
    them decided all of them: a conversation that accumulated files for a year
    answered slower every week, and past 32767 ids the driver refused the
    statement outright. A page is a page at any size, and ``next_cursor`` is
    how the tab asks for the next one.

    Twelve live nodes are attached AFTER a hundred thousand stale references,
    so a read that quietly started from the top would never reach them and a
    read that took the whole list would be the very statement this refuses.
    """
    chat_id = await _chat(files_client)
    await _seed_links(chat_id, GROWN)
    names = [f"live-{index}.json" for index in range(12)]
    for name in names:
        node = await fx.node(name.encode())
        await real_session.commit()
        linked = await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
        assert linked.status_code == 201, linked.text

    with _binds(SYNC_ENGINE) as statements:
        first = await files_client.get(f"/api/v1/chats/{chat_id}/attachments?limit=10")

    assert first.status_code == 200, first.text
    # A page of ten LINKS. Nothing in this one resolves to a node anyone can
    # read, so it renders empty with more behind it — the same convention the
    # chat listing keeps, and the proof that the read did not walk the list
    # looking for something to show.
    assert first.json()["items"] == []
    assert first.json()["next_cursor"] == await _cursor_at(chat_id, 10)
    widest = max((len(values) for _sql, values in statements), default=0)
    assert widest < 100, f"a page of ten bound {widest} values on a chat holding {GROWN} links"

    skip_the_stale = await _cursor_at(chat_id, GROWN)
    page = await files_client.get(
        f"/api/v1/chats/{chat_id}/attachments?limit=10&cursor={skip_the_stale}"
    )
    rest = await files_client.get(
        f"/api/v1/chats/{chat_id}/attachments?limit=10&cursor={page.json()['next_cursor']}"
    )

    assert [item["name"] for item in page.json()["items"]] == names[:10]
    assert [item["name"] for item in rest.json()["items"]] == names[10:]
    assert rest.json()["next_cursor"] is None, "the last page says it is the last"


#: A node id the shapes below can carry. Any well-formed one does; it names
#: nothing, because what is on trial is the reading of the cursor.
A_NODE: Final[str] = "00000000-0000-0000-0000-000000000001"

#: "10" in Arabic-Indic digits, built rather than spelled so the source
#: carries no character that reads as another. ``int()`` accepts it; no page
#: ever minted it.
TEN_IN_ANOTHER_SCRIPT: Final[str] = chr(0x661) + chr(0x660)

#: One past what a ``BIGINT`` column can hold.
PAST_THE_COLUMN: Final[str] = str(2**63)


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("not-a-position", id="neither-half"),
        pytest.param("10", id="the-place-alone"),
        pytest.param("10:", id="no-node"),
        pytest.param("10:not-a-node", id="the-node-is-not-one"),
        pytest.param(f":{A_NODE}", id="no-place"),
        pytest.param(f"10:{A_NODE}:extra", id="a-third-half"),
        pytest.param(f"{PAST_THE_COLUMN}:{A_NODE}", id="one-past-the-column"),
        pytest.param(f"9{PAST_THE_COLUMN}0:{A_NODE}", id="far-past-the-column"),
        pytest.param(f"-{PAST_THE_COLUMN}:{A_NODE}", id="past-the-column-below-zero"),
        pytest.param(f"-1:{A_NODE}", id="before-the-first-place"),
        pytest.param(f"0:{A_NODE}", id="a-place-no-row-holds"),
        pytest.param(f"+10:{A_NODE}", id="signed"),
        pytest.param(f" 10 :{A_NODE}", id="padded"),
        pytest.param(f"1_0:{A_NODE}", id="underscored"),
        pytest.param(f"{TEN_IN_ANOTHER_SCRIPT}:{A_NODE}", id="digits-of-another-script"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_cursor_this_route_did_not_mint_is_refused(
    files_client: AsyncClient, cursor: str
) -> None:
    """A client paging on a mangled cursor must not silently re-read page one
    and loop forever — and must never be answered with a crash.

    Every shape here is one no page ever minted, and every one is the SAME
    refusal. That matters twice over. A place with no node is what this route
    minted before the order became a pair, and accepting it would quietly
    resume past a shared place instead of inside it. And a place larger than
    the column reached the driver as a bind it could not make, so a query
    string any member of the chat could type answered 500 — the one answer the
    route's own contract does not allow.
    """
    chat_id = await _chat(files_client)

    # Through ``params`` so the client encodes the shape rather than the test
    # spelling it: a bare ``+`` in a query string is a space by the time the
    # route reads it, and the sign is one of the things on trial here.
    response = await files_client.get(
        f"/api/v1/chats/{chat_id}/attachments", params={"cursor": cursor}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "chat.bad_cursor"


async def test_a_cursor_the_route_minted_resumes_where_the_page_ended(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The shapes above are refused because they were never minted — so the one
    that WAS minted has to still work, or the refusal is just a broken reader."""
    chat_id = await _chat(files_client)
    names = [f"minted-{index}.json" for index in range(3)]
    for name in names:
        node = await fx.node(name.encode())
        await real_session.commit()
        linked = await files_client.post(
            f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
        )
        assert linked.status_code == 201, linked.text

    first = await files_client.get(f"/api/v1/chats/{chat_id}/attachments?limit=1")
    assert first.status_code == 200, first.text
    minted = first.json()["next_cursor"]
    rest = await files_client.get(f"/api/v1/chats/{chat_id}/attachments?cursor={minted}")

    assert rest.status_code == 200, rest.text
    assert [item["name"] for item in first.json()["items"]] == names[:1]
    assert [item["name"] for item in rest.json()["items"]] == names[1:]


async def test_the_chat_listing_carries_the_count_and_never_the_ids(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A rail says how many files a conversation holds, not which.

    Fifty chats that each held a thousand links would be a fifty-thousand-id
    response nothing on the page renders — and, before the links were rows,
    fifty spec arrays read whole out of Postgres to build it. The single read
    still carries the ids, because a reader who opened the chat is about to
    show them.
    """
    node = await fx.node(b"listed.json")
    await real_session.commit()
    chat_id = await _chat(files_client)
    attached = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)}
    )
    assert attached.status_code == 201, attached.text
    await _seed_links(chat_id, 40)

    listed = await files_client.get("/api/v1/chats")
    single = await files_client.get(f"/api/v1/chats/{chat_id}")

    row = next(item for item in listed.json()["items"] if item["id"] == chat_id)
    assert row["attachment_count"] == 41
    assert row["attachments"] == [], "a listing must not carry a chat's ids"
    body = single.json()
    assert body["attachment_count"] == 41
    assert len(body["attachments"]) == 41
    assert body["attachments"][0] == str(node.id), "attach order survives"


#: How long a rendezvous waits for the rest of its party before giving up.
#: Not synchronization — the barrier is that — but the escape hatch that turns
#: a request which never arrived (an auth fixture that broke, a route that
#: raised before the writer) into a failure rather than a suite that hangs.
RENDEZVOUS_SECONDS = 60.0


def _all_at_once(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """Hold every attach at the writer's door until ``count`` are standing there.

    ``asyncio.gather`` starts the requests together but does not keep them
    together: on a busy machine the eight arrive at the writer one after
    another, each reading the place the one before it had already committed —
    so the overlap the bug needs is a race the test would be hoping for rather
    than one it arranges. The barrier puts all of them at the door at the same
    moment. Nothing inside the writer is replaced: what happens past the door
    is its own doing, and every assertion is on the rows it left.
    """
    barrier = asyncio.Barrier(count)
    writer = chat_service.record_attachment

    async def together(db: AsyncSession, *, chat_id: uuid.UUID, node_id: uuid.UUID) -> bool:
        await asyncio.wait_for(barrier.wait(), RENDEZVOUS_SECONDS)
        return await writer(db, chat_id=chat_id, node_id=node_id)

    monkeypatch.setattr(chat_service, "record_attachment", together)


async def _positions(chat_id: str) -> list[int]:
    """The places this chat's links hold, lowest first."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ChatAttachment.position)
            .where(ChatAttachment.chat_id == uuid.UUID(chat_id))
            .order_by(ChatAttachment.position)
        )
        return [int(position) for position in rows.scalars().all()]


async def _walk(client: AsyncClient, chat_id: str, *, limit: int) -> list[str]:
    """Every node id a paging reader reaches, page by page, as the tab does.

    Pages until the route says there is no next one, so a link the cursor
    steps over is a link this never sees.
    """
    seen: list[str] = []
    query = f"/api/v1/chats/{chat_id}/attachments?limit={limit}"
    for _page in range(200):
        response = await client.get(query)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["nodeId"] for item in body["items"])
        if body["next_cursor"] is None:
            return seen
        query = f"/api/v1/chats/{chat_id}/attachments?limit={limit}&cursor={body['next_cursor']}"
    raise AssertionError("the walk never reached the last page")


async def test_eight_attaches_at_once_take_eight_places_and_every_one_pages(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight attaches in flight together each get a place of their own.

    A place was ``max(position) + 1`` read inside the insert with nothing
    serializing it, so eight readers of one number wrote one number eight
    times — and the tab, which resumes a page after the last position it saw,
    then stepped over every link that shared a place with the one that ended
    the page. The files were attached and the chat never showed them again.
    """
    _all_at_once(monkeypatch, 8)
    chat_id = await _chat(files_client)
    nodes = [await fx.node(f"racer-{index}.json".encode()) for index in range(8)]
    await real_session.commit()

    answers = await asyncio.gather(
        *(
            files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})
            for node in nodes
        )
    )

    assert [answer.status_code for answer in answers] == [201] * 8, [a.text for a in answers]
    places = await _positions(chat_id)
    assert len(set(places)) == 8, f"eight attaches settled on {places}"
    assert await _walk(files_client, chat_id, limit=1) == await _linked(chat_id)


async def test_links_that_already_share_a_place_all_reach_a_paging_reader(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """A tie written before attaches were serialized still pages whole.

    Those rows are already in the database of any deployment that ran the
    racing writer, so the read has to be right about them rather than merely
    stop making new ones: the cursor is the pair the order is taken on, so a
    page that ends inside a tie resumes inside it.
    """
    chat_id = await _chat(files_client)
    nodes = [await fx.node(f"tied-{index}.json".encode()) for index in range(4)]
    await real_session.commit()
    async with AsyncSessionLocal() as session:
        for node in nodes:
            session.add(ChatAttachment(chat_id=uuid.UUID(chat_id), node_id=node.id, position=7))
        await session.commit()

    assert await _walk(files_client, chat_id, limit=1) == await _linked(chat_id)


async def test_the_ceiling_admits_one_of_eight_attaches_at_once(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that sets a ceiling gets the ceiling, not a crowd.

    The check counted and then inserted, so eight attaches that all counted
    the same one free place all took it. One is admitted; the other seven are
    refused with the ceiling's own code.
    """
    monkeypatch.setattr(settings, "chat_max_attachments", 4)
    _all_at_once(monkeypatch, 8)
    chat_id = await _chat(files_client)
    await _seed_links(chat_id, 3)
    nodes = [await fx.node(f"last-{index}.json".encode()) for index in range(8)]
    await real_session.commit()

    answers = await asyncio.gather(
        *(
            files_client.post(f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(node.id)})
            for node in nodes
        )
    )

    assert sorted(answer.status_code for answer in answers) == [201] + [422] * 7, [
        answer.text for answer in answers
    ]
    refused = [answer for answer in answers if answer.status_code == 422]
    assert {answer.json()["error"]["code"] for answer in refused} == {"chat.too_many_attachments"}
    assert len(await _linked(chat_id)) == 4


async def test_a_chat_at_its_ceiling_still_re_attaches_what_it_already_holds(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attach is a link, so attaching a linked node again is not an error —
    including at the ceiling, which is where it used to become one.

    The count was taken before the insert discovered there was nothing to
    insert, so the fullest chat refused the one attach that would have added
    nothing: a browser that re-sent after a dropped response, or a person who
    dragged the same file twice, got "too many attachments" for a file already
    in the rail. A NEW node at the ceiling is still refused, so the ceiling is
    still a ceiling.
    """
    monkeypatch.setattr(settings, "chat_max_attachments", 1)
    chat_id = await _chat(files_client)
    held, another = [await fx.node(f"ceiling-{i}.json".encode()) for i in range(2)]
    await real_session.commit()
    first = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(held.id)}
    )
    assert first.status_code == 201, first.text

    again = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(held.id)}
    )
    new_one = await files_client.post(
        f"/api/v1/chats/{chat_id}/attachments", json={"nodeId": str(another.id)}
    )

    assert again.status_code == 201, again.text
    assert again.json()["nodeId"] == str(held.id)
    assert new_one.status_code == 422, new_one.text
    assert new_one.json()["error"]["code"] == "chat.too_many_attachments"
    assert await _linked(chat_id) == [str(held.id)]


#: How long to keep asking whether a request that should be waiting for a lock
#: has either taken one or finished without one. Not a synchronization device —
#: both answers are terminal and one of them always arrives — but the bound
#: that turns a host too loaded to run the request at all into a failure with
#: a sentence on it.
PARKED_DEADLINE_SECONDS = 60.0


async def _waiting_for_this_chat(chat_id: str) -> int:
    """How many backends are parked on THIS chat's attach lock.

    Asked of Postgres rather than of the clock: a request that is merely slow
    is not waiting, and a request that is waiting says so here. Keyed to the one
    chat, not to the database, because the suite shards — a sibling test parked
    on a row lock of its own would otherwise read as this one's proof. The key
    is derived the way the writer derives it (``hashtext`` of the chat id, in
    the writer's own lock class), so a change of serialization mechanism has to
    come back and change this probe too; that is the price of a probe that can
    tell one chat's queue from every other wait on the box.
    """
    async with AsyncSessionLocal() as session:
        return int(
            (
                await session.execute(
                    sql_text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted "
                        "AND classid = :cls "
                        "AND objid = (hashtext(:chat)::bigint & 4294967295)::oid "
                        "AND database = "
                        "(SELECT oid FROM pg_database WHERE datname = current_database())"
                    ),
                    {"cls": advisory_key("chat-attach", uuid.UUID(chat_id)).klass, "chat": chat_id},
                )
            ).scalar_one()
        )


async def _parked(task: asyncio.Task[Any], chat_id: str) -> bool:
    """Whether ``task`` is waiting for ``chat_id``'s attach lock. ``False`` once
    it has answered without ever waiting for one."""
    deadline = time.monotonic() + PARKED_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if task.done():
            return False
        if await _waiting_for_this_chat(chat_id):
            return True
        await asyncio.sleep(0.05)
    raise AssertionError("the request neither answered nor waited for the chat's lock")


async def test_an_attach_waits_for_its_own_chat_and_for_no_other(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """Attaches queue per chat, so one busy conversation is not every one.

    An attach that is still open holds the next attach to THAT chat — which is
    what makes the places distinct — and holds nothing at all over a different
    chat: a second conversation is served while the first waits.
    """
    held_chat = await _chat(files_client)
    other_chat = await _chat(files_client)
    holder, racer, elsewhere = [await fx.node(f"queue-{i}.json".encode()) for i in range(3)]
    await real_session.commit()

    async with AsyncSessionLocal() as blocker:
        await chat_service.record_attachment(
            blocker, chat_id=uuid.UUID(held_chat), node_id=holder.id
        )
        same_chat = asyncio.create_task(
            files_client.post(
                f"/api/v1/chats/{held_chat}/attachments", json={"nodeId": str(racer.id)}
            )
        )
        # Under a deadline, because the failure this line is here to catch is a
        # lock held over every chat rather than this one — and that failure does
        # not answer at all. Without the bound the test would hang on it instead
        # of naming it.
        free = await asyncio.wait_for(
            files_client.post(
                f"/api/v1/chats/{other_chat}/attachments", json={"nodeId": str(elsewhere.id)}
            ),
            PARKED_DEADLINE_SECONDS,
        )
        assert free.status_code == 201, "an attach to another chat waited behind this one"
        assert await _parked(same_chat, held_chat), (
            "an attach to a busy chat took a place without waiting for it"
        )
        await blocker.rollback()

    assert (await same_chat).status_code == 201
