"""The cloud chat surface, driven through the real routes.

Every status branch the contract names, the rows each one leaves behind, and
the two things a reviewer should be able to check at a glance: a chat in
another org is a 404 that looks like any other 404, and a message posted
over HTTP leaves the same transcript entry and the same relay a message sent
over the socket would.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.events.outbox import MAX_PAYLOAD_BYTES, payload_size
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import (
    ChatMessage,
    EventOutbox,
    RealtimeDoc,
    TeamRole,
    User,
    WorkspaceObject,
)
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.chat import (
    PROMPT_CANCELLED_STOPPED,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    PromptCancelled,
    QuestionPrompt,
    QuestionRequest,
    SessionStatusChanged,
    TextPart,
)
from alkera_core.schemas.objects import (
    CHAT_RELAY_ADAPTER,
    ChatPromptRecord,
    PromoteRelay,
    PromptRelay,
    StopRelay,
)
from alkera_core.schemas.objects.api import (
    MAX_ANSWER_LENGTH,
    MAX_ANSWER_NOTE_LENGTH,
    MAX_MESSAGE_TEXT_LENGTH,
)
from alkera_core.schemas.objects.transcript import (
    RECORDED_ANSWER_ROLE,
    RESOLUTION_KINDS,
    answers_a_waiting_message,
    aside_note_id,
    prompt_cancelled_event_id,
    recorded_answer_event_id,
)
from backend.services.chats import chat_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


async def _other_org_member(client: AsyncClient) -> tuple[UUID, AsyncClient]:
    """A fresh org and a logged-in, verified member of its root."""
    from datetime import UTC, datetime

    async with AsyncSessionLocal() as session:
        org, admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other Org {secrets.token_hex(4)}",
            admin_email=f"other-{secrets.token_hex(4)}@example.com",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="other-pass-12345",
        )
        admin.email_verified_at = datetime.now(UTC)
        email, password = admin.email, "other-pass-12345"
        await session.commit()
        org_id = org.id
    await login(client, email, password)
    return org_id, client


async def _events(org_id: UUID, type_: EventType) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == type_.value)
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _messages(chat_id: UUID) -> list[ChatMessage]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.seq)
        )
        return list(rows.scalars().all())


async def _create_chat(client: AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json=body or {"title": "Ops"})
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Create, list, read
# ---------------------------------------------------------------------------


async def test_a_chat_is_created_with_no_machine_and_says_so(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """No machine is a state the UI renders, not a request that fails."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = await _create_chat(client, title="Why are prompts down?")
    assert body["machine_id"] is None
    assert body["machine_status"] == "none"
    assert body["last_seq"] == 0
    assert body["title"] == "Why are prompts down?"
    rows = await _events(org_admin.org_id, EventType.CHAT_UPDATED)
    assert [row.entity_id for row in rows][-1] == body["id"]


async def test_creating_twice_with_one_client_id_returns_the_same_chat(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A retried create over a flaky link must not leave two conversations."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"c-{uuid4().hex[:8]}"
    first = await _create_chat(client, title="One", client_id=client_id)
    second = await _create_chat(client, title="Two", client_id=client_id)
    assert first["id"] == second["id"]
    assert second["title"] == "One", "the retry does not rewrite what the first one said"
    announced = await _events(org_admin.org_id, EventType.CHAT_UPDATED)
    assert [row.entity_id for row in announced].count(first["id"]) == 1


async def test_the_list_pages_newest_first(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = [(await _create_chat(client, title=f"Chat {index}"))["id"] for index in range(3)]
    first = await client.get("/api/v1/chats", params={"limit": 2})
    assert first.status_code == 200
    page = first.json()
    assert [item["id"] for item in page["items"]] == [made[2], made[1]]
    assert page["next_cursor"]
    second = await client.get("/api/v1/chats", params={"limit": 2, "cursor": page["next_cursor"]})
    assert [item["id"] for item in second.json()["items"]] == [made[0]]
    assert second.json()["next_cursor"] is None


async def test_the_default_page_reaches_past_the_first_fifty_chats(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The page size used to be fifty, and the portal sent no cursor, so the
    fifty-first chat an org made was unreachable from the rail and the
    dashboard — it read exactly like a chat somebody had deleted.

    A page size is a transport unit, not a ceiling on what anyone may see. The
    default now carries every chat an org realistically holds in one round trip,
    and the oldest one is in it with no cursor to follow.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = [(await _create_chat(client, title=f"Chat {index}"))["id"] for index in range(55)]

    listed = await client.get("/api/v1/chats")

    assert listed.status_code == 200
    page = listed.json()
    assert len(page["items"]) == 55
    assert page["next_cursor"] is None
    assert made[0] in {item["id"] for item in page["items"]}, "the oldest chat is still reachable"


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("/api/v1/chats", id="chats"),
        pytest.param("/api/v1/objects", id="objects"),
        pytest.param("/api/v1/chat-templates", id="chat-templates"),
    ],
)
async def test_a_malformed_cursor_is_a_422_not_a_500(
    client: AsyncClient, org_admin: OrgWithAdmin, route: str
) -> None:
    """Every listing that pages, not just the two that remembered to translate
    the refusal themselves."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.get(route, params={"cursor": "not-a-cursor"})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("/api/v1/chats", id="chats"),
        pytest.param("/api/v1/objects", id="objects"),
        pytest.param("/api/v1/chat-templates", id="chat-templates"),
    ],
)
@pytest.mark.parametrize(
    "decoded",
    [
        pytest.param("héllo|2024-01-01T00:00:00+00:00|" + str(uuid4()), id="accent"),
        pytest.param("🙂🙂🙂|2024-01-01T00:00:00+00:00|" + str(uuid4()), id="emoji"),
        pytest.param("абвгдеёжзийк|2024-01-01T00:00:00+00:00|" + str(uuid4()), id="cyrillic"),
    ],
)
async def test_a_cursor_that_is_not_ascii_is_refused_like_any_other_garbage(
    client: AsyncClient, org_admin: OrgWithAdmin, route: str, decoded: str
) -> None:
    """Every listing that pages shares one codec, so one hostile string breaks
    all three at once.

    A cursor is caller-supplied text and arrives in every shape a caller can
    write. Comparing it as a `str` refused the non-ASCII ones with a TypeError
    — an opaque 500 on a plain GET, from the very class of bug this listing was
    being hardened against.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    cursor = base64.urlsafe_b64encode(decoded.encode()).decode().rstrip("=")
    response = await client.get(route, params={"cursor": cursor})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_cursor_from_another_org_is_refused_like_a_mangled_one(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A position in someone else's listing is not a position in yours.

    No row ever crossed — the query is re-scoped to the caller's org on every
    page — but a foreign cursor was accepted and silently resumed from a
    position that means nothing here, so a client could skip or repeat a page
    with nothing to tell it apart from a correct one.
    """
    await _other_org_member(client)
    for index in range(3):
        await _create_chat(client, title=f"Theirs {index}")
    foreign = (await client.get("/api/v1/chats", params={"limit": 1})).json()["next_cursor"]
    assert foreign

    await login(client, org_admin.admin_email, org_admin.admin_password)
    for index in range(3):
        await _create_chat(client, title=f"Mine {index}")
    replayed = await client.get("/api/v1/chats", params={"limit": 1, "cursor": foreign})
    assert replayed.status_code == 422, replayed.text
    # …while the caller's OWN cursor still pages, so the binding refuses the
    # foreign listing and nothing else.
    mine = (await client.get("/api/v1/chats", params={"limit": 1})).json()["next_cursor"]
    assert (
        await client.get("/api/v1/chats", params={"limit": 1, "cursor": mine})
    ).status_code == 200


async def test_a_cursor_from_another_listing_in_the_same_org_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The binding is per listing, not only per org: the same caller's cursor
    from the untyped object listing does not resume the chat listing."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    for index in range(3):
        await _create_chat(client, title=f"Chat {index}")
    untyped = (await client.get("/api/v1/objects", params={"limit": 1})).json()["next_cursor"]
    assert untyped
    crossed = await client.get("/api/v1/chats", params={"limit": 1, "cursor": untyped})
    assert crossed.status_code == 422, crossed.text


@pytest.mark.usefixtures("files_on")
async def test_a_chat_is_private_until_its_node_is_shared(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Another member of the org — no grant of any kind — is told the chat does
    not exist and does not see it in their listing, and the transcript route
    answers the same way. A Can-view share on the chat's node opens all three
    with no reconnect: the rung is read per request, never cached."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        hidden = await other.get(f"/api/v1/chats/{chat['id']}")
        listed = await other.get("/api/v1/chats", params={"limit": 10})
        transcript = await other.get(f"/api/v1/chats/{chat['id']}/messages")
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["message"] == "Not found"
        assert listed.status_code == 200 and listed.json()["items"] == []
        assert transcript.status_code == 404, transcript.text

        await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_READER)
        shown = await other.get(f"/api/v1/chats/{chat['id']}")
        listed = await other.get("/api/v1/chats", params={"limit": 10})
        transcript = await other.get(f"/api/v1/chats/{chat['id']}/messages")
    assert shown.status_code == 200, shown.text
    assert shown.json()["id"] == chat["id"]
    assert [item["id"] for item in listed.json()["items"]] == [chat["id"]]
    assert transcript.status_code == 200, transcript.text
    assert await _decisions(org_admin.org_id, entity_id=chat["id"]) == [
        {"action": "create", "effect": "allow"},
        {"action": "read", "effect": "deny"},
        {"action": "read", "effect": "deny"},
        {"action": "read", "effect": "allow"},
        {"action": "read", "effect": "allow"},
    ], "the two refusals are on record; the listing files no rows"


async def test_a_chat_of_another_org_is_an_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A caller who may not see a chat must not learn its id is live."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with app_client() as intruder:
        await _other_org_member(intruder)
        response = await intruder.get(f"/api/v1/chats/{chat['id']}")
        missing = await intruder.get(f"/api/v1/chats/{uuid4()}")
    assert response.status_code == 404
    assert missing.status_code == 404

    def _shape(body: dict[str, Any]) -> tuple[Any, Any]:
        error = body["error"]
        return error["code"], error["message"]

    # Everything but the per-request trace id: a foreign chat and an absent one
    # must be indistinguishable, or the 404 leaks that the id is live.
    assert _shape(response.json()) == _shape(missing.json()) == ("not_found", "Not found")


async def _object_of_type(
    session: AsyncSession, owner_id: UUID, *, type: str, spec: dict[str, Any], client_id: str
) -> str:
    """A workspace object seeded through the service rather than the objects
    route, which no longer saves a kind a chat template replaced. The rows
    still exist in deployments, and it is those rows the chat surface has to
    keep telling apart from a chat."""
    from alkera_core.models import User
    from backend.services.objects import object_service

    owner = await session.get(User, owner_id)
    assert owner is not None
    created, _ = await object_service.create_object(
        session,
        owner=owner,
        type=type,
        title="Saved",
        spec=spec,
        logical_id=client_id,
        org_id=owner.home_org_team_id,
    )
    await session.commit()
    return str(created.id)


async def test_an_id_of_the_wrong_type_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """``/chats/{id}`` must not answer about a saved query."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    object_id = await _object_of_type(
        real_session,
        org_admin.admin_id,
        type="query",
        spec={"sql_template": "SELECT 1", "source_chat_id": str(uuid4())},
        client_id=f"q-{uuid4().hex[:8]}",
    )
    response = await client.get(f"/api/v1/chats/{object_id}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def test_posting_a_message_records_it_and_relays_it_to_the_machine(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "how many prompts yesterday?", "client_id": "c1"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["seq"] == 1
    assert body["role"] == "user"
    assert body["kind"] == "prompt"
    assert body["payload"]["text"] == "how many prompts yesterday?"

    rows = await _messages(chat_id)
    assert [(row.seq, row.role, row.event_id) for row in rows] == [(1, "user", "usr:c1")]

    relays = [
        row
        for row in await _events(org_admin.org_id, EventType.DOC_OP)
        if row.entity_id == f"doc:chat:{chat_id}"
    ]
    assert relays, "the daemon learns about a message through the chat document"
    envelope = relays[-1].payload["envelope"]
    assert relays[-1].payload["relay"] is True
    assert envelope["payload"]["intent"] == "user_message"
    event = envelope["payload"]["events"][0]
    assert event["kind"] == "prompt"
    assert event["seq"] == 1
    assert event["message_id"] == body["id"]
    assert event["client_id"] == "c1"
    assert event["user_id"] == str(org_admin.admin_id)


@pytest.mark.parametrize(
    "length",
    [
        pytest.param(8_001, id="one-past-the-old-ceiling"),
        pytest.param(500_000, id="a-pasted-document"),
    ],
)
async def test_a_long_message_is_recorded_whole_rather_than_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, length: int
) -> None:
    """A message over the ceiling is refused and the typed text is GONE — there
    is no draft to go back to — so the ceiling has to sit far above anything
    anyone writes or pastes. It used to sit at eight thousand characters, which
    a pasted log, stack trace or schema clears without trying.

    Read back off the transcript rather than out of the create's own response:
    the row, the event-log row it is relayed as, and the ceilings on both are
    what a message this long actually has to pass.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    said = "".join("supercalifragilistic "[index % 21] for index in range(length))
    assert len(said) == length

    response = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": said, "client_id": "c-long"}
    )

    assert response.status_code == 201, response.text[:400]
    page = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 0, "limit": 10}
    )
    (entry,) = page.json()["items"]
    assert entry["payload"]["text"] == said, "the message must come back whole, not truncated"


def _entry_size(text: str, *, client_id: str, user_id: UUID) -> int:
    """What the transcript entry for ``text`` measures against the outbox cap."""
    return payload_size(
        ChatPromptRecord(text=text, client_id=client_id, user_id=str(user_id)).model_dump(
            mode="json"
        )
    )


@pytest.mark.parametrize(
    ("spare_bytes", "expected"),
    [
        pytest.param(0, 413, id="entry-fits-relay-does-not"),
        pytest.param(2048, 201, id="both-fit"),
    ],
)
async def test_a_message_the_relay_cannot_carry_is_refused_as_too_large(
    client: AsyncClient, org_admin: OrgWithAdmin, spare_bytes: int, expected: int
) -> None:
    """The refusal is the published 413, not an unhandled 500.

    A message becomes two rows of the same size class: the transcript entry, and
    the relay that carries the same text again inside a document envelope. The
    relay is the larger one, so a message in the margin between them cleared a
    guard that measured only the entry and then failed on the emit — which
    raises a bare ``ValueError`` nobody catches, so the caller got a 500 where
    the route publishes a 413.

    Both halves are driven: the largest text whose ENTRY still fits must be
    refused (its relay does not), and a text with room for the envelope must go
    through — so the boundary is the relay's and not a blanket tightening.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    client_id = "c-margin"
    # Each character escapes to six bytes in the measure the outbox uses, so the
    # fill lands on an exact byte count rather than near one.
    room = MAX_PAYLOAD_BYTES - _entry_size("", client_id=client_id, user_id=org_admin.admin_id)
    said = "é" * ((room - spare_bytes) // 6)
    size = _entry_size(said, client_id=client_id, user_id=org_admin.admin_id)
    assert size <= MAX_PAYLOAD_BYTES, "the entry alone is inside the cap in both cases"
    assert size > MAX_PAYLOAD_BYTES - 4096, "the refusal is about the envelope, not the message"
    assert len(said) <= MAX_MESSAGE_TEXT_LENGTH, "the schema's own ceiling is not what decides"

    response = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": said, "client_id": client_id}
    )

    assert response.status_code == expected, response.text[:400]
    rows = await _messages(UUID(chat["id"]))
    assert len(rows) == (1 if expected == 201 else 0), (
        "a refused message leaves no transcript entry behind"
    )


async def test_the_message_ceiling_is_the_deployments_to_set(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is a setting, not a compiled-in number: an operator who wants
    a smaller one gets it on the same request path, with no new image."""
    monkeypatch.setattr(settings, "chat_message_max_chars", 100)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)

    refused = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "x" * 101, "client_id": "c-capped"}
    )
    accepted = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "x" * 100, "client_id": "c-fits"}
    )

    assert refused.status_code == 422
    assert "100" in refused.text
    assert accepted.status_code == 201, accepted.text[:400]


async def test_a_relay_rides_seq_zero_even_once_the_document_has_moved(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A relay changes no state, so it carries no sequence.

    Stamping it with the document's CURRENT sequence makes it look like an op
    every subscriber has already applied: the browser's ordering guard drops
    anything at or below its cursor, so the relay works on an empty chat and
    stops working the moment the machine has appended anything.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    async with AsyncSessionLocal() as session:
        session.add(
            RealtimeDoc(
                doc_type="chat",
                doc_id=str(chat_id),
                org_id=org_admin.org_id,
                owner_user_id=org_admin.admin_id,
                epoch=1,
                seq=7,
                state={},
            )
        )
        await session.commit()

    posted = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "still there?", "client_id": "c-late"}
    )
    assert posted.status_code == 201, posted.text
    relays = [
        row
        for row in await _events(org_admin.org_id, EventType.DOC_OP)
        if row.entity_id == f"doc:chat:{chat_id}"
    ]
    envelope = relays[-1].payload["envelope"]
    assert envelope["epoch"] == 1, "the relay is still stamped with the document's epoch"
    assert envelope["seq"] == 0, "a relay moves no state, so it rides the unsequenced lane"


async def test_reposting_one_client_id_is_idempotent(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A browser that resends after a dropped response must not say it twice."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    body = {"text": "hello", "client_id": "same"}
    first = await client.post(f"/api/v1/chats/{chat['id']}/messages", json=body)
    second = await client.post(f"/api/v1/chats/{chat['id']}/messages", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(await _messages(UUID(chat["id"]))) == 1
    relays = [
        row
        for row in await _events(org_admin.org_id, EventType.DOC_OP)
        if row.entity_id == f"doc:chat:{chat['id']}"
    ]
    assert len(relays) == 1, "the retry is not relayed to the machine a second time"


async def test_reusing_a_client_id_for_different_words_is_a_409(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A retry resends the same words; different words under a recorded id are
    a client bug. Answering 201 with the original told the sender their new
    message landed when nothing was recorded or relayed."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    url = f"/api/v1/chats/{chat['id']}/messages"
    first = await client.post(url, json={"text": "hello", "client_id": "reused"})
    assert first.status_code == 201, first.text
    clash = await client.post(url, json={"text": "DIFFERENT", "client_id": "reused"})
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "client_id_in_use"
    stored = await _messages(UUID(chat["id"]))
    assert [row.payload["text"] for row in stored] == ["hello"]


async def test_a_member_the_chat_is_not_shared_with_may_not_send_into_it(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A chat is private until shared: a member nobody shared it with cannot
    see it, so their message is the same opaque not-found as their read — not
    the 403 a shared viewer gets — and nothing reaches the transcript."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        readable = await other.get(f"/api/v1/chats/{chat['id']}")
        response = await other.post(
            f"/api/v1/chats/{chat['id']}/messages",
            json={"text": "and by region?", "client_id": "m1"},
        )
    assert readable.status_code == 404, "they may not even see it"
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Not found"
    assert await _messages(UUID(chat["id"])) == []


# ---------------------------------------------------------------------------
# A chat's name
# ---------------------------------------------------------------------------


async def test_a_chat_is_named_after_its_first_prompt(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Every row in the list read "Untitled chat", because nothing ever named
    one: the browser opens a chat and then sends into it, so the create call
    carries no title. After two rehearsals the presenter cannot find the live
    chat on stage. The first thing said in a chat is what it is about."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, client_id=f"c-{uuid4().hex[:8]}")
    assert chat["title"] == "", "a chat opened before anything is said has no name yet"

    await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "which customers are trending down in share of voice?", "client_id": "m1"},
    )

    listed = await client.get("/api/v1/chats")
    named = [row for row in listed.json()["items"] if row["id"] == chat["id"]]
    assert named[0]["title"] == "Which customers are trending down in share of voice"


async def test_a_chat_nobody_has_spoken_in_keeps_no_name(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The other half of the contract: the name comes from a prompt, so a chat
    with no prompt stays unnamed and the surface says so in its own words."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, client_id=f"c-{uuid4().hex[:8]}")
    read = await client.get(f"/api/v1/chats/{chat['id']}")
    assert read.json()["title"] == ""


async def test_a_name_someone_chose_outranks_the_first_prompt(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Naming is not renaming: a chat created WITH a title keeps it,
    whatever is said in it afterwards."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="Monday audit")
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "which customers are trending down?", "client_id": "m1"},
    )
    read = await client.get(f"/api/v1/chats/{chat['id']}")
    assert read.json()["title"] == "Monday audit"


async def test_the_second_prompt_does_not_rename_the_chat(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A chat is named once, by its opening. A list whose rows re-title
    themselves as a conversation goes on is a list nobody can navigate."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, client_id=f"c-{uuid4().hex[:8]}")
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "how many prompts yesterday?", "client_id": "m1"},
    )
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "and by region?", "client_id": "m2"},
    )
    read = await client.get(f"/api/v1/chats/{chat['id']}")
    assert read.json()["title"] == "How many prompts yesterday"


async def test_naming_a_chat_is_not_an_edit_of_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The name is derived, like ``last_seq``: a client holding version N has
    not lost a race because the chat acquired the name of its own first
    sentence."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, client_id=f"c-{uuid4().hex[:8]}")
    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(WorkspaceObject.version).where(WorkspaceObject.id == UUID(chat["id"]))
            )
        ).scalar_one()
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "how many prompts yesterday?", "client_id": "m1"},
    )
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(WorkspaceObject).where(WorkspaceObject.id == UUID(chat["id"]))
            )
        ).scalar_one()
    assert row.title == "How many prompts yesterday"
    assert row.version == before


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        pytest.param("how many prompts yesterday?", "How many prompts yesterday", id="no-tail"),
        pytest.param("  which\n customers\tare down  ", "Which customers are down", id="collapsed"),
        pytest.param("Already Capitalised", "Already Capitalised", id="left-alone"),
        pytest.param("", "", id="nothing-said"),
        pytest.param("   \n\t ", "", id="whitespace-only"),
        pytest.param("?!...", "", id="punctuation-only"),
        pytest.param(
            "show me the target prompts with the most mentions this week, by customer",
            "Show me the target prompts with the most mentions this",
            id="cut-on-a-word",
        ),
        pytest.param(
            "a" * 80,
            "A" + "a" * 59,
            id="one-word-longer-than-the-cap-is-cut-where-it-is",
        ),
    ],
)
async def test_the_name_a_prompt_yields(prompt: str, expected: str) -> None:
    """The derivation itself, at its boundaries — the cap, the word boundary,
    the collapse, and every prompt that yields no name at all."""
    assert chat_service.title_from_prompt(prompt) == expected
    assert len(chat_service.title_from_prompt(prompt)) <= chat_service.TITLE_MAX_CHARS


async def test_messages_page_by_seq_and_report_the_next_cursor(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    for index in range(5):
        posted = await client.post(
            f"/api/v1/chats/{chat['id']}/messages",
            json={"text": f"m{index}", "client_id": f"c{index}"},
        )
        assert posted.status_code == 201
    first = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 0, "limit": 2}
    )
    assert first.status_code == 200
    page = first.json()
    assert [item["seq"] for item in page["items"]] == [1, 2]
    assert page["next_after_seq"] == 2
    assert page["resync_from"] is None
    second = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 2, "limit": 10}
    )
    assert [item["seq"] for item in second.json()["items"]] == [3, 4, 5]
    assert second.json()["next_after_seq"] == 5


async def test_an_empty_page_reports_the_cursor_it_was_given(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A forward read past the end hands the cursor straight back, so the live
    tail resumes from where it asked rather than from zero.

    The whole body is pinned, not just the cursor: an empty forward page is the
    one shape where every backward-paging field is at its own floor, and a page
    that started announcing older rows here would send a reader scrolling up
    into a transcript that has none.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 7, "limit": 10}
    )
    assert response.json() == {
        "items": [],
        "next_after_seq": 7,
        "resync_from": None,
        # The backward-paging fields at their floor: the cursor to page
        # further back is a backward-read answer, a forward read never claims
        # older rows, and only a backward page can open inside a turn.
        "prev_before": None,
        "has_older": False,
        "cut": False,
    }


async def test_a_cursor_below_the_retained_window_gets_an_in_band_resync_marker(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """N3.1: asking for messages we no longer hold is answered in band —
    "start again from here" — never with an error the client needs a branch
    for. Nothing prunes the table today, so the state is arranged directly."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    for index in range(4):
        await client.post(
            f"/api/v1/chats/{chat['id']}/messages",
            json={"text": f"m{index}", "client_id": f"c{index}"},
        )
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ChatMessage).where(ChatMessage.chat_id == UUID(chat["id"]), ChatMessage.seq <= 2)
        )
        for row in rows.scalars().all():
            await session.delete(row)
        await session.commit()
    response = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 0, "limit": 10}
    )
    body = response.json()
    assert body["resync_from"] == 3
    assert [item["seq"] for item in body["items"]] == [3, 4]
    still_current = await client.get(
        f"/api/v1/chats/{chat['id']}/messages", params={"after_seq": 3, "limit": 10}
    )
    assert still_current.json()["resync_from"] is None


async def test_posting_into_a_chat_of_another_org_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with app_client() as intruder:
        await _other_org_member(intruder)
        response = await intruder.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "x", "client_id": "x"}
        )
    assert response.status_code == 404
    assert await _messages(UUID(chat["id"])) == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"text": "", "client_id": "c"}, id="empty_text"),
        pytest.param({"text": "x"}, id="no_client_id"),
        pytest.param(
            {"text": "x" * (MAX_MESSAGE_TEXT_LENGTH + 1), "client_id": "c"},
            id="text_over_the_cap",
        ),
    ],
)
async def test_a_malformed_message_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, Any]
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(f"/api/v1/chats/{chat['id']}/messages", json=body)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------


async def test_promote_creates_a_pending_result_and_asks_the_machine_for_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-42",
            "title": "Daily prompts",
            "columns": [{"name": "day", "label": "Day"}, {"name": "n", "label": None}],
            "chart_spec": {
                "mark": "line",
                "encoding": {
                    "x": {"field": "day", "type": "temporal"},
                    "y": {"field": "n", "type": "quantitative"},
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["type"] == "result"
    assert body["status"] == "pending_upload"
    assert [(column["name"], column["label"]) for column in body["spec"]["columns"]] == [
        ("day", "Day"),
        ("n", ""),
    ]
    assert body["spec"]["chart_spec"]["mark"] == "line"
    assert body["spec"]["payload"] is None

    relays = [
        row
        for row in await _events(org_admin.org_id, EventType.DOC_OP)
        if row.entity_id == f"doc:chat:{chat['id']}"
    ]
    event = relays[-1].payload["envelope"]["payload"]["events"][0]
    # Compared against the model rather than a hand-transcribed dict: the relay
    # body is one shape both sides import, so a field added to it cannot pass
    # here while the machine that parses it never sees it.
    assert event == PromoteRelay(object_id=body["id"], event_id="ev-42").model_dump(mode="json")
    assert CHAT_RELAY_ADAPTER.validate_python(event).kind == "promote"
    announced = await _events(org_admin.org_id, EventType.WORKSPACE_OBJECT_CHANGED)
    assert announced[-1].entity_id == body["id"]
    assert announced[-1].payload == {"type": "result", "version": 1}


async def test_promoting_the_same_event_twice_pins_one_result(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    body = {"event_id": "ev-1", "title": "Once"}
    first = await client.post(f"/api/v1/chats/{chat['id']}/promote", json=body)
    second = await client.post(f"/api/v1/chats/{chat['id']}/promote", json=body)
    assert first.json()["id"] == second.json()["id"]


async def test_a_promote_with_a_chart_outside_the_allowlist_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The injection surface F6.1 names, refused at the route rather than
    stored and rendered later."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-2",
            "title": "Evil",
            "chart_spec": {
                "mark": "line",
                "encoding": {"x": {"field": "day", "type": "temporal"}},
                "data": {"url": "https://evil.example/rows.json"},
            },
        },
    )
    assert response.status_code == 422
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.org_team_id == org_admin.org_id,
                WorkspaceObject.type == "result",
                WorkspaceObject.title == "Evil",
            )
        )
        assert rows.scalars().all() == []


async def test_a_refused_chart_tells_the_browser_which_key_it_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The save dialog shows the server's own words when a spec is refused, so
    the refusal has to survive the error envelope as a readable sentence naming
    the offending key — not the generic "the request failed validation"."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-3",
            "title": "Evil",
            "chart_spec": {
                "mark": "line",
                "encoding": {"x": {"field": "day", "type": "temporal"}},
                "data": {"url": "https://evil.example/rows.json"},
            },
        },
    )
    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "data" in message
    assert "unsupported keys" in message
    # Never the value: an echoed URL is the injected string handed back.
    assert "evil.example" not in response.text


async def test_the_spec_the_browser_derives_is_stored_exactly_as_sent(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The one shape the save dialog can produce — a line mark and the two
    typed channels — round-trips through the allowlist unchanged. The reader in
    the portal draws what is stored, so a validator that silently rewrote this
    would be a chart the reader cannot draw."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    derived = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "mark": "line",
        "encoding": {
            "x": {"field": "day", "type": "temporal"},
            "y": {"field": "orders", "type": "quantitative"},
        },
    }
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-4",
            "title": "Orders by engine",
            "columns": [{"name": "day", "label": None}, {"name": "orders", "label": None}],
            "chart_spec": derived,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["chart_spec"] == derived


async def test_a_promoted_chart_may_split_into_series_by_a_color_channel(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A `color` channel splits a result into one series per value. The reader
    draws every profile mark now, so the writer admits the split and stores it
    exactly as sent."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chart = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "mark": "line",
        "encoding": {
            "x": {"field": "day", "type": "temporal"},
            "y": {"field": "orders", "type": "quantitative"},
            "color": {"field": "engine", "type": "nominal"},
        },
    }
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-colour",
            "title": "Orders by engine",
            "columns": [
                {"name": "day", "label": None},
                {"name": "orders", "label": None},
                {"name": "engine", "label": None},
            ],
            "chart_spec": chart,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["chart_spec"] == chart


async def test_a_promoted_chart_must_name_columns_the_result_declares(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A chart binds to column KEYS. A promote whose encoding names something the
    declared columns do not have would be saved and then draw nothing — an empty
    figure on a result the reader chose to keep — so it is refused here, at the
    write, with the CHANNEL named and the field's value left out of the answer.

    The same check runs on the objects routes; a promote that skipped it would be
    the way around it.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-unbound",
            "title": "Unbound",
            "columns": [{"name": "day", "label": "Day"}, {"name": "orders", "label": "Orders"}],
            "chart_spec": {
                "mark": "line",
                "encoding": {
                    "x": {"field": "Day", "type": "temporal"},
                    "y": {"field": "orders", "type": "quantitative"},
                },
            },
        },
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "chart_field_unbound"
    assert error["details"]["field"] == "encoding.x.field"
    assert "Day" not in error["message"]
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.org_team_id == org_admin.org_id,
                WorkspaceObject.title == "Unbound",
            )
        )
        assert rows.scalars().all() == []


async def test_a_promoted_chart_bound_to_a_declared_column_is_kept(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The other side of the refusal: a chart naming keys, against columns whose
    LABELS differ from them, is stored as sent — binding is by key, so renaming a
    column for the reader can never detach its chart."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-bound",
            "title": "Bound",
            "columns": [{"name": "day", "label": "Day"}, {"name": "orders", "label": "Orders"}],
            "chart_spec": {
                "mark": "line",
                "encoding": {
                    "x": {"field": "day", "type": "temporal"},
                    "y": {"field": "orders", "type": "quantitative"},
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["chart_spec"]["encoding"]["x"]["field"] == "day"


async def test_a_promote_that_declares_no_columns_still_keeps_its_chart(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Nothing to bind against binds trivially. The columns are optional at
    promote — the machine settles them when it delivers the payload — so a
    binding check that refused an undeclared result would refuse the ordinary
    save rather than the broken one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/promote",
        json={
            "event_id": "ev-nocolumns",
            "title": "Undeclared",
            "chart_spec": {
                "mark": "line",
                "encoding": {
                    "x": {"field": "day", "type": "temporal"},
                    "y": {"field": "orders", "type": "quantitative"},
                },
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["columns"] == []
    assert response.json()["spec"]["chart_spec"]["encoding"]["y"]["field"] == "orders"


async def test_the_relay_helper_and_the_route_agree_on_the_channel(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The daemon subscribes to one channel name; a mismatch here is a chat
    that silently never reaches the machine."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(f"/api/v1/chats/{chat['id']}/messages", json={"text": "hi", "client_id": "c"})
    relays = await _events(org_admin.org_id, EventType.DOC_OP)
    assert chat_service.channel_key(UUID(chat["id"])) in {row.entity_id for row in relays}


#: A credential is recognised by its SHAPE, not by a word that could equally
#: name an ordinary field. A provider key, a bearer header and a JWT each wear
#: a literal prefix nothing else on this surface does; ``secret`` /
#: ``password`` / ``api_key`` / ``private_key`` stay bare substrings because a
#: body carrying one of those words is announcing the thing itself. The bare
#: word ``token`` is deliberately NOT here: the chats list renders a model
#: facet whose keys include ``max_output_tokens``, and a scan that called that
#: a leak would only ever be re-blessed away or force the payload to be renamed.
CREDENTIAL_SHAPES: tuple[tuple[str, str], ...] = (
    ("slack token", r"xox[abprs]-"),
    ("alkera token", r"\balk_[A-Za-z0-9]"),
    ("provider api key", r"\bsk-[A-Za-z0-9]"),
    ("bearer header", r"\bBearer\s+\S"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}"),
    ("secret", r"(?i)secret"),
    ("password", r"(?i)password"),
    ("api key field", r"(?i)api_key"),
    ("private key field", r"(?i)private_key"),
)


def _credentials_in(body: str) -> list[str]:
    """Every credential shape the body carries, named so a failure says which."""
    return [name for name, pattern in CREDENTIAL_SHAPES if re.search(pattern, body)]


async def test_no_response_on_this_surface_carries_a_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """No chat response carries a credential, asserted rather than inspected."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(f"/api/v1/chats/{chat['id']}/messages", json={"text": "hi", "client_id": "c"})
    bodies = [
        (await client.get("/api/v1/chats")).text,
        (await client.get(f"/api/v1/chats/{chat['id']}")).text,
        (await client.get(f"/api/v1/chats/{chat['id']}/messages")).text,
    ]
    for body in bodies:
        assert _credentials_in(body) == [], body


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        pytest.param("xoxb-1234-5678-abcdef", "slack token", id="slack"),
        pytest.param("alk_live_9f3c2a", "alkera token", id="alkera"),
        pytest.param("sk-proj-ABCdef123", "provider api key", id="provider"),
        pytest.param("Bearer abc.def.ghi", "bearer header", id="bearer"),
        pytest.param("eyJhbGciOiJIUzI1NiJ9.e30.x", "jwt", id="jwt"),
        pytest.param('"client_secret": "hunter2"', "secret", id="secret"),
        pytest.param('"password": "hunter2"', "password", id="password"),
    ],
)
async def test_the_credential_scan_catches_a_planted_credential(
    planted: str, expected: str
) -> None:
    """The scan above only proves something if it fires on the real thing.

    Each case is a credential dropped into a body shaped like the one the route
    returns — model facet and all — so a scan that stopped matching, because a
    false positive was over-corrected say, fails here instead of passing
    silently forever.
    """
    body = f'{{"id":"abc","title":"t","model":{{"max_output_tokens":64000}},"x":"{planted}"}}'
    assert expected in _credentials_in(body)


async def test_the_credential_scan_does_not_fire_on_an_ordinary_model_facet() -> None:
    """``max_output_tokens`` is a field name, not a credential.

    The regression this pins: a scan for the bare word ``token`` called the
    chats list a leak the day the model facet joined the payload.
    """
    facet = '{"model":{"id":"m","max_output_tokens":64000,"tokenizer":"o200k"}}'
    assert _credentials_in(facet) == []


async def test_an_anonymous_caller_is_refused(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/chats")).status_code == 401
    assert (await client.post("/api/v1/chats", json={})).status_code == 401


async def test_the_chat_row_records_its_owner_and_audience(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat["id"]))
    assert row is not None
    assert row.owner_user_id == org_admin.admin_id
    assert row.org_team_id == org_admin.org_id
    assert row.team_id is None
    assert row.visibility_scope == "private", "a chat is its owner's until its node is shared"
    assert row.namespace == "workspace"


async def test_a_deleted_user_does_not_orphan_a_transcript(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The FK cascade, proven rather than assumed: an object goes with its
    owner and its transcript goes with it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        chat = await _create_chat(other)
        await other.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "x", "client_id": "c"}
        )
    assert len(await _messages(UUID(chat["id"]))) == 1
    async with AsyncSessionLocal() as session:
        victim = await session.get(User, member.id)
        assert victim is not None
        await session.delete(victim)
        await session.commit()
    assert await _messages(UUID(chat["id"])) == []


# ---------------------------------------------------------------------------
# Who owns a promoted result
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_an_org_admin_promoting_from_a_members_chat_makes_the_member_the_owner(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The result belongs to the chat it came out of: REST lets an org admin who
    can READ the chat promote out of it, files the object under the CHAT's
    owner, and records the promoter on the receipt — so the daemon, which binds
    every relay to the chat's owner, and REST name the same person.

    The member shares the chat first, because promoting lifts content out of a
    conversation: an admin nobody shared it with is told it does not exist (the
    case below this one), and this test is about WHO OWNS what comes out, not
    about who may reach in."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="The member's chat")
        assert chat["owner_user_id"] == str(member.id)

    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    admin_user = await real_session.get(User, org_admin.admin_id)
    assert chat_row is not None and admin_user is not None
    await share_chat(real_session, chat=chat_row, owner=member, user=admin_user, role="reader")

    await login(client, org_admin.admin_email, org_admin.admin_password)
    promoted = await client.post(
        f"/api/v1/chats/{chat['id']}/promote", json={"event_id": "ev-admin", "title": "Rows"}
    )
    assert promoted.status_code == 201, promoted.text
    body = promoted.json()
    assert body["owner_user_id"] == str(member.id), "owned by the chat's owner, not the promoter"
    assert body["spec"]["source_chat_id"] == chat["id"]
    assert body["spec"]["source_event_id"] == "ev-admin"
    promoted_by = body["spec"]["receipt"]["principal_chain"]["promoted_by"]
    assert promoted_by["acting"] == {
        **promoted_by["acting"],
        "kind": "user",
        "id": str(org_admin.admin_id),
    }

    async with app_client() as theirs:
        await login(theirs, member.email, password)
        mine = await theirs.get(f"/api/v1/objects/{body['id']}")
        assert mine.status_code == 200
        assert mine.json()["owner_user_id"] == str(member.id)
        # And the owner may edit what the admin promoted into their chat.
        renamed = await theirs.put(
            f"/api/v1/objects/{body['id']}", json={"title": "Mine", "expected_version": 1}
        )
        assert renamed.status_code == 200, renamed.text


async def test_a_result_promoted_out_of_a_private_chat_is_private_too(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A chat is private until its node is shared, and the result lifted out of
    it takes the same audience. A member the owner never shared anything with
    is told the object does not exist — on the object, on its rows, and in the
    listing — while the owner still reaches it (the rows answer 409 for them:
    admitted, payload pending) and the org admin reads every scope as before."""
    owner, owner_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    other, other_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert owner_password is not None and other_password is not None

    async with app_client() as theirs:
        await login(theirs, owner.email, owner_password)
        chat = await _create_chat(theirs, title="Customer revenue")
        promoted = await theirs.post(
            f"/api/v1/chats/{chat['id']}/promote",
            json={"event_id": "ev-revenue", "title": "Revenue by customer"},
        )
        assert promoted.status_code == 201, promoted.text
        result_id = promoted.json()["id"]
        assert (await theirs.get(f"/api/v1/objects/{result_id}")).status_code == 200
        pending = await theirs.get(f"/api/v1/objects/{result_id}/rows")
        assert pending.status_code == 409, pending.text

    async with app_client() as stranger:
        await login(stranger, other.email, other_password)
        assert (await stranger.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
        hidden = await stranger.get(f"/api/v1/objects/{result_id}")
        assert hidden.status_code == 404, hidden.text
        rows = await stranger.get(f"/api/v1/objects/{result_id}/rows")
        assert rows.status_code == 404, rows.text
        listed = await stranger.get("/api/v1/objects", params={"type": "result", "limit": 200})
        assert listed.status_code == 200, listed.text
        assert result_id not in {item["id"] for item in listed.json()["items"]}

    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"/api/v1/objects/{result_id}")).status_code == 200


@pytest.mark.usefixtures("files_on")
async def test_an_org_admin_cannot_promote_out_of_a_chat_they_may_not_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The companion to the two tests above, and the reason the first one has to
    share the chat. A promoted result is decided by the OBJECT policy, which an
    org admin reads at every scope — so an admin able to promote out of a chat
    they cannot open would be handed its content through the door beside the one
    that refused them. The refusal is the chat's own opaque not-found."""
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="Nobody else's")

    await login(client, org_admin.admin_email, org_admin.admin_password)
    refused = await client.post(
        f"/api/v1/chats/{chat['id']}/promote", json={"event_id": "ev-1", "title": "Rows"}
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "not_found"
    # Nor delete it: a chat the admin cannot read is the same not-found on
    # every door, DELETE included.
    assert (await client.delete(f"/api/v1/chats/{chat['id']}")).status_code == 404


# ---------------------------------------------------------------------------
# A retried ``client_id`` is a read of the chat it made — and only of a chat
# ---------------------------------------------------------------------------


async def _decisions(org_id: UUID, *, entity_id: str) -> list[dict[str, Any]]:
    """Every ``authz.decision`` filed under ``entity_id``, oldest first, as
    ``{action, effect}`` pairs."""
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity_id == entity_id,
            )
            .order_by(EventOutbox.id)
        )
        return [
            {"action": row.payload["action"], "effect": row.payload["effect"]}
            for row in rows.scalars().all()
        ]


async def test_a_client_id_held_by_a_saved_query_is_a_conflict_not_a_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The idempotency lookup is scoped to chats: a ``client_id`` that names a
    saved query must not hand that query back rendered as a chat."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"shared-{uuid4().hex[:8]}"
    query_id = await _object_of_type(
        real_session,
        org_admin.admin_id,
        type="query",
        spec={
            "sql_template": "SELECT 1",
            "source_chat_id": str(uuid4()),
            "engine": "postgres",
        },
        client_id=client_id,
    )
    response = await client.post("/api/v1/chats", json={"title": "Chat", "client_id": client_id})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "client_id_in_use", (
        "the code a query create gets for a chat's id — one condition, one code"
    )
    assert query_id not in response.text
    # The saved query is untouched and no chat was minted under its id.
    listed = await client.get("/api/v1/chats")
    assert query_id not in [chat["id"] for chat in listed.json()["items"]]
    assert await _decisions(org_admin.org_id, entity_id=query_id) == [], (
        "the refused chat create files nothing under the id it collided with"
    )


async def test_a_retried_client_id_is_decided_as_a_read_of_the_chat_it_made(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"c-{uuid4().hex[:8]}"
    first = await _create_chat(client, title="One", client_id=client_id)
    second = await _create_chat(client, title="Two", client_id=client_id)
    assert first["id"] == second["id"]
    assert await _decisions(org_admin.org_id, entity_id=first["id"]) == [
        {"action": "create", "effect": "allow"},
        {"action": "read", "effect": "allow"},
    ]


async def test_a_deleted_chats_client_id_is_neither_returned_nor_re_used(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A tombstone still holds its logical id — the unique constraint is not
    partial — so the retried create can neither hand the deleted chat back as a
    live one nor mint a second chat under the same id. The two lookups have to
    agree: the chat lookup answers ``None`` for a tombstone exactly where the
    object store refuses to re-use it, and the caller is told so."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"c-{uuid4().hex[:8]}"
    chat = await _create_chat(client, title="Yesterday's chat", client_id=client_id)
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat["id"]))
        assert row is not None
        row.deleted_at = 1_700_000_000.0
        await session.commit()
        found = await chat_service.find_chat_by_client_id(
            session, org_team_id=org_admin.org_id, client_id=client_id
        )
    assert found is None, "a tombstone is not a live chat"

    response = await client.post("/api/v1/chats", json={"title": "Again", "client_id": client_id})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "client_id_retired"
    assert chat["id"] not in response.text, "the deleted chat was handed back"
    # …and nothing was created under the retired id.
    listed = await client.get("/api/v1/chats")
    assert chat["id"] not in [item["id"] for item in listed.json()["items"]]
    assert await _decisions(org_admin.org_id, entity_id=chat["id"]) == [
        {"action": "create", "effect": "allow"}
    ], "the retry is not decided as a read of the deleted chat"


async def test_a_retried_client_id_of_a_chat_the_caller_may_not_read_is_not_found(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Another member reusing the ``client_id`` of a chat outside their audience
    gets the opaque not-found the READ policy gives, never the chat."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"c-{uuid4().hex[:8]}"
    theirs = await _create_chat(client, title="Admin's private notes", client_id=client_id)
    row = await real_session.get(WorkspaceObject, UUID(theirs["id"]))
    assert row is not None
    row.visibility_scope = "private"
    await real_session.commit()

    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as mine:
        await login(mine, member.email, password)
        response = await mine.post("/api/v1/chats", json={"title": "Mine", "client_id": client_id})
    assert response.status_code == 404, response.text
    assert theirs["id"] not in response.text
    decisions = await _decisions(org_admin.org_id, entity_id=theirs["id"])
    assert decisions == [
        {"action": "create", "effect": "allow"},
        {"action": "read", "effect": "deny"},
    ], "the retry is decided as a read of the chat that exists, never as a create"


# ---------------------------------------------------------------------------
# Answering an ask
# ---------------------------------------------------------------------------


async def _relays(org_id: UUID, chat_id: str) -> list[dict[str, Any]]:
    """The relay events put on one chat's channel, oldest first."""
    return [
        row.payload["envelope"]["payload"]["events"][0]
        for row in await _events(org_id, EventType.DOC_OP)
        if row.entity_id == f"doc:chat:{chat_id}"
    ]


_ASKED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _permission_ask(chat_id: str, request_id: str = "perm-1") -> dict[str, Any]:
    """A write ask as the harness raises it, offering allow and reject."""
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_ASKED_AT,
        session_id=chat_id,
        request_id=request_id,
        permission_kind="edit",
        canonical_kind="edit",
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
        ],
    ).model_dump(mode="json")


def _question_ask(chat_id: str, request_id: str = "q-1") -> dict[str, Any]:
    return QuestionRequest(
        event_id=f"ev-{request_id}",
        time=_ASKED_AT,
        session_id=chat_id,
        request_id=request_id,
        questions=[
            QuestionPrompt(question="Which warehouse?", custom=True),
            QuestionPrompt(question="Since when?", custom=True),
        ],
    ).model_dump(mode="json")


async def _machine_published(chat_id: str, *events: dict[str, Any]) -> None:
    """Rows the box's mirror published: the harness event in the envelope the
    document's persist step stores, exactly as a live box writes them."""
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        await chat_service.persist_published_events(
            session,
            chat=chat,
            events=[
                {
                    "event_id": event["event_id"],
                    "role": "assistant",
                    "kind": event["event_type"],
                    "payload": event,
                }
                for event in events
            ],
        )
        await session.commit()


def _resolutions(rows: list[ChatMessage], request_id: str) -> list[ChatMessage]:
    return [
        row
        for row in rows
        if row.kind in RESOLUTION_KINDS and row.payload["payload"]["request_id"] == request_id
    ]


@pytest.mark.parametrize(
    ("ask", "body", "expected", "recorded"),
    [
        pytest.param(
            "permission",
            {"interrupt_id": "perm-1", "option_id": "allow_once"},
            {"kind": "prompt", "interrupt_id": "perm-1", "option_id": "allow_once"},
            ("permission.resolved", {"option_id": "allow_once", "decided_by": "user"}),
            id="a-permission-option",
        ),
        pytest.param(
            "question",
            {"interrupt_id": "q-1", "answers": [["Postgres"], ["Yesterday"]]},
            {"kind": "prompt", "interrupt_id": "q-1", "answers": [["Postgres"], ["Yesterday"]]},
            ("question.answered", {"answers": [["Postgres"], ["Yesterday"]], "decided_by": "user"}),
            id="a-questions-answers",
        ),
        pytest.param(
            "question",
            {"interrupt_id": "q-1", "answers": [["Accept"]], "note": "  Skip the migration.  "},
            {
                "kind": "prompt",
                "interrupt_id": "q-1",
                "answers": [["Accept"]],
                "note": "Skip the migration.",
            },
            ("question.answered", {"answers": [["Accept"]], "note": "Skip the migration."}),
            id="answers-with-a-note",
        ),
        pytest.param(
            "question",
            {"interrupt_id": "q-1", "answers": [["Accept"]], "note": "   "},
            {"kind": "prompt", "interrupt_id": "q-1", "answers": [["Accept"]], "note": None},
            ("question.answered", {"answers": [["Accept"]], "note": None}),
            id="a-blank-note-is-no-note",
        ),
        pytest.param(
            "question",
            {"interrupt_id": "q-1", "reject": True, "reason": "not now"},
            {"kind": "prompt", "interrupt_id": "q-1", "reject": True, "reason": "not now"},
            ("question.rejected", {"reason": "not now"}),
            id="a-rejection",
        ),
    ],
)
async def test_an_answer_reaches_the_machine_in_the_shape_it_validates_and_is_recorded(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    ask: str,
    body: dict[str, Any],
    expected: dict[str, Any],
    recorded: tuple[str, dict[str, Any]],
) -> None:
    """The browser can answer an ask the transcript holds: what arrives on the
    channel is exactly the relay the daemon's answer path reads (the ask named
    by id, carrying one answer), and the transcript now carries the ask's
    resolution — the same event the machine would publish, decided by this
    user, under the reader's role — so a box that was not listening reads the
    decision instead of asking again."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    request_id = body["interrupt_id"]
    await _machine_published(
        chat["id"],
        _permission_ask(chat["id"], request_id)
        if ask == "permission"
        else _question_ask(chat["id"], request_id),
    )
    response = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)
    assert response.status_code == 202, response.text

    relay = (await _relays(org_admin.org_id, chat["id"]))[-1]
    assert {key: relay[key] for key in expected} == expected
    assert relay["user_id"] == str(org_admin.admin_id)

    kind, fields = recorded
    rows = _resolutions(await _messages(UUID(chat["id"])), request_id)
    assert [row.kind for row in rows] == [kind]
    row = rows[0]
    assert row.role == RECORDED_ANSWER_ROLE, "a reader's decision, told apart from the machine's"
    assert row.event_id == recorded_answer_event_id(request_id)
    assert row.payload["role"] == RECORDED_ANSWER_ROLE and row.payload["kind"] == kind
    event = row.payload["payload"]
    assert {key: event[key] for key in fields} == fields
    assert event["request_id"] == request_id and event["session_id"] == chat["id"]
    assert row.seq == 2, "the next entry after the ask — the chat's counter moved"


@pytest.mark.parametrize(
    ("ask", "body", "kind"),
    [
        pytest.param(
            "permission",
            {"interrupt_id": "perm-1", "option_id": "allow_once"},
            "permission.resolved",
            id="a-permission-option",
        ),
        pytest.param(
            "question",
            {"interrupt_id": "q-1", "answers": [["Postgres"]]},
            "question.answered",
            id="a-questions-answers",
        ),
    ],
)
async def test_a_recorded_answer_names_the_member_who_made_it(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    ask: str,
    body: dict[str, Any],
    kind: str,
) -> None:
    """A chat several people read gets several people who could have answered,
    so the resolution carries who decided and not only what was chosen. The
    roster lives on the server and nowhere the box can see it, so the id and
    the display name go onto the row at the moment the route writes it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
        admin.first_name, admin.last_name = "Dana", "Okafor"
        await session.commit()

    chat = await _create_chat(client)
    request_id = body["interrupt_id"]
    await _machine_published(
        chat["id"],
        _permission_ask(chat["id"], request_id)
        if ask == "permission"
        else _question_ask(chat["id"], request_id),
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)).status_code == 202

    row = _resolutions(await _messages(UUID(chat["id"])), request_id)[0]
    assert row.kind == kind
    event = row.payload["payload"]
    assert event["decided_by"] == "user"
    assert event["decided_by_user_id"] == str(org_admin.admin_id)
    assert event["decided_by_name"] == "Dana Okafor"
    if kind == "permission.resolved":
        # Where it was answered, so a Slack card settles to "Allowed on web by".
        assert event["decided_via"] == "web"


async def _chat_doc(chat_id: str, org_id: UUID) -> RealtimeDoc:
    """The chat's realtime document, as the first socket hello creates it.

    A reader is only ever told anything because they are subscribed to this
    document, so a test about what a watching reader sees has to have one.
    """
    from alkera_core.events import actor_for_user
    from backend.services.realtime.channels import Channel, ChannelGrant
    from backend.services.realtime.docsync import DocRegistry
    from backend.services.realtime.filters import load_entitlements

    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        user = await session.get(User, chat.owner_user_id)
        assert user is not None
        channel = Channel("chat", chat_id)
        grant = ChannelGrant(
            channel=channel,
            org_id=org_id,
            can_write=True,
            owner_user_id=user.id,
            team_id=None,
        )
        doc = await DocRegistry().ensure(
            session,
            grant=grant,
            user=user,
            actor=actor_for_user(user, org_id=user.home_org_team_id),
            ent=await load_entitlements(session, user, org_id=user.home_org_team_id),
        )
        await session.commit()
        return doc


async def _box_appended(chat_id: str, org_id: UUID, *events: dict[str, Any]) -> None:
    """Events the BOX appends through the real document op, not a shortcut.

    A machine reaches the transcript by exactly one route — an `append` on the
    chat document — and what that route does to what it carries is the thing
    under test, so a test about the box's words has to take it.
    """
    from alkera_core.events import actor_for_user
    from alkera_core.schemas.realtime import DocEnvelope, OpPayload
    from backend.services.realtime.channels import Channel, ChannelGrant
    from backend.services.realtime.docsync import DocRegistry
    from backend.services.realtime.filters import load_entitlements

    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        user = await session.get(User, chat.owner_user_id)
        assert user is not None
        doc = await session.get(RealtimeDoc, (org_id, "chat", chat_id))
        assert doc is not None, "the document is created before the box speaks"
        channel = Channel("chat", chat_id)
        grant = ChannelGrant(
            channel=channel, org_id=org_id, can_write=True, owner_user_id=user.id, team_id=None
        )
        envelope = DocEnvelope(
            doc_id=chat_id,
            doc_type="chat",
            epoch=doc.epoch,
            peer_id="p:box",
            seq=0,
            kind="op",
            payload=OpPayload(
                op_id=f"box-{uuid4().hex[:8]}",
                intent="append",
                events=[
                    {
                        "event_id": event["event_id"],
                        "role": "assistant",
                        "kind": event["event_type"],
                        "payload": event,
                    }
                    for event in events
                ],
            ).model_dump(mode="json"),
        )
        await DocRegistry().apply_op(
            session,
            grant=grant,
            user=user,
            envelope=envelope,
            actor=actor_for_user(user, org_id=user.home_org_team_id),
            ent=await load_entitlements(session, user, org_id=user.home_org_team_id),
        )
        await session.commit()


def _appended_events(rows: list[EventOutbox], chat_id: str) -> list[dict[str, Any]]:
    """Every event carried by an `append` frame on this chat's channel."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.entity_id != f"doc:chat:{chat_id}":
            continue
        payload = row.payload["envelope"]["payload"]
        if payload.get("intent") != "append":
            continue
        out.extend(payload.get("events") or [])
    return out


async def test_a_watching_reader_is_told_who_decided_while_they_are_watching(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The decision has to reach the chat's channel, not only the table.

    A reader watching the chat learns everything from the document's frames.
    The answer's relay is addressed to the machine and carries no resolution a
    transcript reader can fold, and the machine's own settlement of the ask
    names nobody, because no box has a roster — so a decision written only to
    the row appeared to everyone watching as a nameless settlement, and the
    name turned up only if they reloaded.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
        admin.first_name, admin.last_name = "Dana", "Okafor"
        await session.commit()

    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await _machine_published(chat["id"], _permission_ask(chat["id"], "perm-1"))
    answered = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "perm-1", "option_id": "allow_once"},
    )
    assert answered.status_code == 202, answered.text

    live = _appended_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    resolutions = [e for e in live if e.get("kind") == "permission.resolved"]
    assert len(resolutions) == 1, "the decision goes out on the chat's channel, once"
    event = resolutions[0]["payload"]
    assert event["request_id"] == "perm-1" and event["option_id"] == "allow_once"
    assert event["decided_by"] == "user"
    assert event["decided_by_name"] == "Dana Okafor"
    assert event["decided_by_user_id"] == str(org_admin.admin_id)


async def test_the_frame_a_reader_folds_is_the_row_the_transcript_keeps(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """One event, not two spellings of one. A reader folds the live frame and
    then pages the transcript over REST; if those were separate events the
    same decision would render twice, and the sequence the frame carries would
    name a row that does not exist."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await _machine_published(chat["id"], _permission_ask(chat["id"], "perm-1"))
    assert (
        await client.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": "reject_once"},
        )
    ).status_code == 202

    row = _resolutions(await _messages(UUID(chat["id"])), "perm-1")[0]
    live = _appended_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    frame = next(e for e in live if e.get("kind") == "permission.resolved")
    assert frame["event_id"] == row.event_id == recorded_answer_event_id("perm-1")
    assert frame["seq"] == row.seq, "the frame names the row the reader can ask for again"
    assert frame["payload"] == row.payload["payload"]


@pytest.mark.parametrize(
    "box_first",
    [
        pytest.param(False, id="the-box-settles-after"),
        pytest.param(True, id="the-box-settles-first"),
    ],
)
async def test_the_boxs_own_settlement_never_takes_the_name_off_the_decision(
    client: AsyncClient, org_admin: OrgWithAdmin, box_first: bool
) -> None:
    """A person's decision and the machine's settlement of the same ask both
    stand on the record, and only the server's ever names anybody.

    The machine settles the ask itself — its harness was blocked on it — and
    its event is de-named on the way in, because no box has a roster and none
    is a party that gets to name a colleague. Answering after it is refused
    outright, so in that order the record simply names nobody; answering first
    leaves exactly one named decision, the server's, and the machine's later
    settlement cannot take the name off it.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
        admin.first_name, admin.last_name = "Dana", "Okafor"
        await session.commit()

    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await _machine_published(chat["id"], _permission_ask(chat["id"], "perm-1"))
    box_settlement = {
        "event_id": "ev-box-resolved",
        "event_type": "permission.resolved",
        "schema_version": "1.1.0",
        "time": _ASKED_AT.isoformat(),
        "session_id": chat["id"],
        "request_id": "perm-1",
        "option_id": "allow_once",
        "decided_by": "user",
        "decided_by_name": "Someone Else",
    }
    if box_first:
        await _box_appended(chat["id"], org_admin.org_id, box_settlement)
    answered = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "perm-1", "option_id": "allow_once"},
    )
    if box_first:
        # An ask the machine already settled is not one a person can answer:
        # the transcript holds the decision and a second one would be a second
        # answer to the same question.
        assert answered.status_code == 409, answered.text
    else:
        assert answered.status_code == 202, answered.text
        await _box_appended(chat["id"], org_admin.org_id, box_settlement)

    rows = _resolutions(await _messages(UUID(chat["id"])), "perm-1")
    named = [
        row.payload["payload"]["decided_by_name"]
        for row in rows
        if row.payload["payload"].get("decided_by_name")
    ]
    assert named == ([] if box_first else ["Dana Okafor"]), (
        "only a decision the server recorded names anybody"
    )
    assert "Someone Else" not in repr([row.payload for row in rows]), (
        "the box's claim about a person never reaches the record, in either order"
    )

    # The window is what a reader opening the chat — or coming back after a
    # reload — folds, so the decision has to read the same way there as it did
    # live. The box's append passes through the same door and must not have
    # taken the name off the server's event on its way in.
    async with AsyncSessionLocal() as session:
        doc = await session.get(RealtimeDoc, (org_admin.org_id, "chat", chat["id"]))
    assert doc is not None
    window = [
        event["payload"]
        for event in doc.state["events"]
        if event.get("kind") == "permission.resolved"
    ]
    assert [e.get("decided_by_name") for e in window if e.get("decided_by_name")] == (
        [] if box_first else ["Dana Okafor"]
    ), "the snapshot names whoever the record names, and nobody else"


async def test_a_stop_met_twice_is_one_note(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """The note's id is derived from where the transcript stood, not minted
    fresh, so the same stop is recognisably the same note however often it is
    met: published live and read back off the record, pressed twice on a
    turn that already stopped, echoed by a mirror repeating what it heard. A
    minted id makes each of those a second sentence on the tape that nothing
    can tell apart from a real second stop.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    for _ in range(3):
        assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    notes = {
        row.event_id.rsplit("-", 1)[0]
        for row in await _messages(UUID(chat["id"]))
        if row.event_id.startswith("stop-")
    }
    assert len(notes) == 1, "three presses on one turn are one stop and one line"
    rows = [row for row in await _messages(UUID(chat["id"])) if row.event_id.startswith("stop-")]
    assert [row.kind for row in rows] == ["message.created", "part.created", "message.completed"]

    # And the window a reader folds holds it once, not once per press.
    async with AsyncSessionLocal() as session:
        doc = await session.get(RealtimeDoc, (org_admin.org_id, "chat", chat["id"]))
    assert doc is not None
    live = [e for e in doc.state["events"] if str(e.get("event_id", "")).startswith("stop-")]
    assert len(live) == 3, "the note's three events, once each"


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize(
    "second_reader",
    [pytest.param(False, id="one reader presses twice"), pytest.param(True, id="two readers")],
)
async def test_a_second_press_while_the_box_is_talking_is_still_one_stop(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, second_reader: bool
) -> None:
    """A stop is named by the turn it ends, not by how far the transcript had
    got when it was pressed.

    A box mid-turn appends a part every few tens of milliseconds and has not
    yet seen the relay, so between two presses a moment apart — one reader's
    double click, or two readers on the same turn — the transcript always
    moves. A note named after the position would be a second "Stopped by"
    sentence for one stop, and a third press after another part a third, with
    nothing on the tape to tell them from real ones.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "go", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    # The box, which has not heard the relay yet, writes the part it was in the
    # middle of.
    await _machine_published(
        chat["id"],
        PartCreated(
            event_id="prt-midturn",
            time=datetime.now(UTC),
            session_id=chat["id"],
            part=TextPart(part_id="prt-midturn-1", message_id="msg-mid", text="still going"),
        ).model_dump(mode="json"),
    )

    if second_reader:
        member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
        assert password is not None
        chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
        owner = await real_session.get(User, org_admin.admin_id)
        assert chat_row is not None and owner is not None
        await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
        async with app_client() as other:
            await login(other, member.email, password)
            assert (await other.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    else:
        assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = [row for row in await _messages(UUID(chat["id"])) if row.event_id.startswith("stop-")]
    assert [row.kind for row in rows] == ["message.created", "part.created", "message.completed"], (
        "one stop, one note, however much the box wrote between the presses"
    )

    # And live: the second press publishes nothing, so a reader watching folds
    # the sentence once.
    async with AsyncSessionLocal() as session:
        doc = await session.get(RealtimeDoc, (org_admin.org_id, "chat", chat["id"]))
    assert doc is not None
    live = [e for e in doc.state["events"] if str(e.get("event_id", "")).startswith("stop-")]
    assert len(live) == 3, "the note's three events reach the window once"


async def test_a_stop_on_the_next_turn_is_its_own_note(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The other half of the same rule: a press that ends a LATER turn is a
    different stop and owes the reader its own sentence. Without this the
    derived id would be a way of losing notices rather than of not inventing
    them."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "first", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "second", "client_id": "s2"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    notes = {
        row.event_id.rsplit("-", 1)[0]
        for row in await _messages(UUID(chat["id"]))
        if row.event_id.startswith("stop-")
    }
    assert len(notes) == 2, "two turns stopped is two sentences"


async def test_a_turn_an_answered_ask_restarted_gets_its_own_stop_note(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A person's message is not the only thing that starts a turn: answering
    an ask the agent was blocked on restarts one, on a chat where nobody has
    typed since.

    So the turn is named after the person's last ROW, not their last message —
    which is the complement of the rule both ends already share for "what
    answers a waiting message": a row of the reader's asks something, and
    everything else reports on it. Named after the last message instead, the
    second Stop re-derives the first turn's id, is dropped as a duplicate, and
    the reader watches a turn end with nothing saying who ended it.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "run it", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    # The agent asks, the reader allows: the turn runs again with no new
    # message above it.
    await _machine_published(chat["id"], _permission_ask(chat["id"], "perm-restart"))
    answered = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "perm-restart", "option_id": "allow_once"},
    )
    assert answered.status_code == 202, answered.text

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    notes = {
        row.event_id.rsplit("-", 1)[0]
        for row in await _messages(UUID(chat["id"]))
        if row.event_id.startswith("stop-")
    }
    assert len(notes) == 2, "the turn the answer restarted is stopped under its own name"


async def test_the_decider_id_outlives_the_name_the_transcript_froze(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The two fields answer two different questions, which is why both are
    kept. "Who approved this write" is asked months later, of somebody who may
    since have been renamed or removed, and two colleagues can share a display
    name — only the id answers that. The name is what a reader sees, so it is
    frozen at the moment of the decision: a rename moves the roster and leaves
    the record of what was true at the time exactly where it was."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
        admin.first_name, admin.last_name = "Dana", "Okafor"
        await session.commit()

    chat = await _create_chat(client)
    await _machine_published(chat["id"], _permission_ask(chat["id"], "perm-1"))
    answered = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "perm-1", "option_id": "allow_once"},
    )
    assert answered.status_code == 202, answered.text

    async with AsyncSessionLocal() as session:
        renamed = await session.get(User, org_admin.admin_id)
        assert renamed is not None
        renamed.first_name, renamed.last_name = "Dana", "Marchetti"
        await session.commit()

    event = _resolutions(await _messages(UUID(chat["id"])), "perm-1")[0].payload["payload"]
    assert event["decided_by_name"] == "Dana Okafor", "the tape keeps what was true at the time"
    assert event["decided_by_user_id"] == str(org_admin.admin_id), "the id still finds the person"


async def test_a_declined_question_records_no_decider(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Declining to answer is not a decision anyone made: the row says the ask
    went unanswered, and there is no person to put next to it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(chat["id"], _question_ask(chat["id"], "q-1"))
    answered = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "q-1", "reject": True, "reason": "not now"},
    )
    assert answered.status_code == 202, answered.text
    event = _resolutions(await _messages(UUID(chat["id"])), "q-1")[0].payload["payload"]
    assert event["event_type"] == "question.rejected"
    assert "decided_by_user_id" not in event and "decided_by_name" not in event


async def test_an_answer_to_a_sleeping_chat_is_recorded_as_the_asks_resolution(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The chat is bound to no machine — asleep — so the relay reaches nobody.
    The answer is not lost: the transcript carries the resolution, the page a
    browser (and the box, on its next open) reads serves it, and it is a
    resolution row, never a prompt — the reader chose, they did not speak."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    assert chat["machine_id"] is None, "no box holds this chat"
    await _machine_published(chat["id"], _permission_ask(chat["id"]))
    answered = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "perm-1", "option_id": "reject_once"},
    )
    assert answered.status_code == 202, answered.text
    rows = await _messages(UUID(chat["id"]))
    assert [row.kind for row in rows] == ["permission.request", "permission.resolved"]
    assert not [row for row in rows if row.kind == chat_service.USER_MESSAGE_KIND]
    page = await client.get(f"/api/v1/chats/{chat['id']}/messages")
    served = page.json()["items"]
    assert [item["kind"] for item in served] == ["permission.request", "permission.resolved"]
    assert served[-1]["role"] == RECORDED_ANSWER_ROLE
    assert served[-1]["payload"]["payload"]["option_id"] == "reject_once"
    assert served[-1]["payload"]["payload"]["decided_by"] == "user"


async def test_answering_an_ask_the_transcript_does_not_hold_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """An id nobody asked is not an ask: nothing is recorded, nothing relayed,
    and the refusal is named so a client can tell it from a chat it cannot see."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(chat["id"], _permission_ask(chat["id"], "perm-real"))
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/answer",
        json={"interrupt_id": "perm-invented", "option_id": "allow_once"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "ask_not_found"
    assert await _relays(org_admin.org_id, chat["id"]) == []
    assert len(await _messages(UUID(chat["id"]))) == 1, "only the ask"


@pytest.mark.parametrize(
    "first_resolution",
    [
        pytest.param("a-reader-through-this-route", id="answered-here"),
        pytest.param("the-machine", id="settled-by-the-machine"),
    ],
)
async def test_an_ask_already_resolved_takes_no_second_answer(
    client: AsyncClient, org_admin: OrgWithAdmin, first_resolution: str
) -> None:
    """A second Allow on a card — from a slow tab, or after the machine settled
    the ask itself — records nothing and relays nothing: one ask, one decision."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(chat["id"], _permission_ask(chat["id"]))
    body = {"interrupt_id": "perm-1", "option_id": "allow_once"}
    if first_resolution == "the-machine":
        await _machine_published(
            chat["id"],
            PermissionResolved(
                event_id="ev-perm-1-resolved",
                time=_ASKED_AT,
                session_id=chat["id"],
                request_id="perm-1",
                option_id="allow_once",
                decided_by="user",
            ).model_dump(mode="json"),
        )
        relays_before = 0
    else:
        first = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)
        assert first.status_code == 202, first.text
        relays_before = 1
    again = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "ask_already_answered"
    assert len(await _relays(org_admin.org_id, chat["id"])) == relays_before
    assert len(_resolutions(await _messages(UUID(chat["id"])), "perm-1")) == 1


@pytest.mark.parametrize(
    ("ask", "body"),
    [
        pytest.param(
            "permission",
            {"interrupt_id": "x", "option_id": "allow_always"},
            id="an-option-the-ask-never-offered",
        ),
        pytest.param(
            "permission", {"interrupt_id": "x", "answers": [["yes"]]}, id="answers-to-a-permission"
        ),
        pytest.param(
            "permission", {"interrupt_id": "x", "reject": True}, id="a-decline-of-a-permission"
        ),
        pytest.param(
            "question",
            {"interrupt_id": "x", "option_id": "allow_once"},
            id="an-option-to-a-question",
        ),
    ],
)
async def test_an_answer_the_ask_cannot_take_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, ask: str, body: dict[str, Any]
) -> None:
    """What is recorded is a resolution the box can act on, so an answer the
    ask cannot take — an option it never offered, a question's answers to a
    permission — is refused in band rather than written as a decision."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(
        chat["id"],
        _permission_ask(chat["id"], "x") if ask == "permission" else _question_ask(chat["id"], "x"),
    )
    response = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "answer_does_not_fit_ask"
    assert await _relays(org_admin.org_id, chat["id"]) == []
    assert _resolutions(await _messages(UUID(chat["id"])), "x") == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"option_id": "allow_once"}, id="no-ask-named"),
        pytest.param({"interrupt_id": "", "option_id": "allow_once"}, id="empty-ask-id"),
        pytest.param({"interrupt_id": "p"}, id="no-answer-at-all"),
        pytest.param(
            {"interrupt_id": "p", "option_id": "allow_once", "reject": True}, id="two-answers"
        ),
        pytest.param(
            {"interrupt_id": "p", "option_id": "allow_once", "answers": [["y"]]},
            id="an-option-and-answers",
        ),
        pytest.param({"interrupt_id": "p", "answers": []}, id="no-prompts-answered"),
        pytest.param({"interrupt_id": "p", "option_id": "x" * 65}, id="option-over-the-cap"),
        pytest.param({"interrupt_id": "p" * 256, "option_id": "x"}, id="ask-id-over-the-cap"),
        pytest.param({"interrupt_id": "p", "answers": [["y"]] * 33}, id="too-many-prompts"),
        pytest.param(
            {"interrupt_id": "p", "answers": [["y" * (MAX_ANSWER_LENGTH + 1)]]},
            id="answer-over-the-cap",
        ),
        pytest.param(
            {"interrupt_id": "p", "option_id": "allow_once", "note": "go"},
            id="a-note-on-a-permission-option",
        ),
        pytest.param(
            {"interrupt_id": "p", "reject": True, "note": "go"}, id="a-note-on-a-rejection"
        ),
    ],
)
async def test_an_ambiguous_or_oversized_answer_never_reaches_the_machine(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, Any]
) -> None:
    """The machine resolves one answer per ask; a relay carrying none, two, or
    an unbounded one would be resolved there by field order. Refused in band."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    response = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)
    assert response.status_code == 422, response.text
    assert await _relays(org_admin.org_id, chat["id"]) == []


async def test_a_note_past_its_bound_is_refused_with_a_sentence(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A note is guidance typed into a card; one past the bound is refused in
    band, with a sentence that names the bound, and nothing is recorded."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(chat["id"], _question_ask(chat["id"], "q-1"))
    at_bound = {
        "interrupt_id": "q-1",
        "answers": [["Accept"]],
        "note": "n" * MAX_ANSWER_NOTE_LENGTH,
    }
    past = {**at_bound, "note": "n" * (MAX_ANSWER_NOTE_LENGTH + 1)}
    response = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=past)
    assert response.status_code == 422, response.text
    assert f"a note is at most {MAX_ANSWER_NOTE_LENGTH} characters" in response.text
    assert await _relays(org_admin.org_id, chat["id"]) == []
    assert _resolutions(await _messages(UUID(chat["id"])), "q-1") == []
    ok = await client.post(f"/api/v1/chats/{chat['id']}/answer", json=at_bound)
    assert ok.status_code == 202, ok.text


async def test_answering_a_chat_of_another_org_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Answering is speaking, so it passes the same gate a message passes — and
    a chat the caller may not see stays an opaque 404."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with app_client() as intruder:
        await _other_org_member(intruder)
        response = await intruder.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": "allow_once"},
        )
    assert response.status_code == 404
    assert await _relays(org_admin.org_id, chat["id"]) == []


@pytest.mark.usefixtures("files_on")
async def test_only_a_shared_editor_may_answer_an_ask(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The agent blocks on ONE ask, and releasing it steers the conversation —
    the same gate that lets somebody send. A watcher is refused; the colleague
    the chat was shared with at ``writer`` releases it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(chat["id"], _permission_ask(chat["id"]))
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        watcher = await other.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": "reject_once"},
        )
        assert watcher.status_code == 404, "unshared, the member cannot even see the chat"
        assert _resolutions(await _messages(UUID(chat["id"])), "perm-1") == []
        await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_READER)
        viewer = await other.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": "reject_once"},
        )
        assert viewer.status_code == 403, "a Can-view share reads the ask but may not answer it"
        assert _resolutions(await _messages(UUID(chat["id"])), "perm-1") == [], (
            "and a refused answer records nothing"
        )
        await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
        response = await other.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": "reject_once"},
        )
    assert response.status_code == 202
    assert (await _relays(org_admin.org_id, chat["id"]))[-1]["user_id"] == str(member.id)


def _standing_ask(chat_id: str, request_id: str) -> dict[str, Any]:
    """A write ask that offers the standing answers too."""
    return PermissionRequest(
        event_id=f"ev-{request_id}",
        time=_ASKED_AT,
        session_id=chat_id,
        request_id=request_id,
        permission_kind="edit",
        canonical_kind="edit",
        options=[
            PermissionOption(option_id="allow_once", name="Allow"),
            PermissionOption(option_id="allow_always", name="Always allow"),
            PermissionOption(option_id="reject_once", name="Reject"),
            PermissionOption(option_id="reject_always", name="Always reject"),
        ],
    ).model_dump(mode="json")


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize("standing", ["allow_always", "reject_always"])
async def test_only_the_chat_owner_may_answer_always(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, standing: str
) -> None:
    """An "Always" answer is recorded on the box as a rule of the chat's owner
    and applies in their other chats there. A colleague who may answer the ask
    answers it once; their "Always" is refused, on record, and records nothing.
    The owner's own "Always" goes through."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _machine_published(chat["id"], _standing_ask(chat["id"], "perm-1"))
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
    async with app_client() as other:
        await login(other, member.email, password)
        refused = await other.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": standing},
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == chat_policy.STANDING_ANSWER_DENIED_CODE
        assert _resolutions(await _messages(UUID(chat["id"])), "perm-1") == []
        assert await _relays(org_admin.org_id, chat["id"]) == []
        assert (await _decisions(org_admin.org_id, entity_id=chat["id"]))[-1] == {
            "action": "answer_standing",
            "effect": "deny",
        }
        once = await other.post(
            f"/api/v1/chats/{chat['id']}/answer",
            json={"interrupt_id": "perm-1", "option_id": "allow_once"},
        )
        assert once.status_code == 202, once.text

    mine = await _create_chat(client)
    await _machine_published(mine["id"], _standing_ask(mine["id"], "perm-2"))
    answered = await client.post(
        f"/api/v1/chats/{mine['id']}/answer",
        json={"interrupt_id": "perm-2", "option_id": standing},
    )
    assert answered.status_code == 202, answered.text
    assert (await _relays(org_admin.org_id, mine["id"]))[-1]["option_id"] == standing


@pytest.mark.usefixtures("files_on")
async def test_the_chat_read_says_who_may_answer_always(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The card offers "Always" off ``can_answer_always``, the same decision the
    answer route makes: the owner yes, a colleague who may send no."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    mine = await client.get(f"/api/v1/chats/{chat['id']}")
    assert (mine.json()["can_send"], mine.json()["can_answer_always"]) == (True, True)
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
    async with app_client() as other:
        await login(other, member.email, password)
        theirs = await other.get(f"/api/v1/chats/{chat['id']}")
    assert (theirs.json()["can_send"], theirs.json()["can_answer_always"]) == (True, False)


# ---------------------------------------------------------------------------
# The machine says whether it can publish the chat
# ---------------------------------------------------------------------------


async def _registered_machine(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, *, pod: str
) -> tuple[str, dict[str, str]]:
    """The box registers with the operator's device token; returns the machine
    id and the headers the box then speaks with — the token plus the agent
    assertion naming that machine."""
    from alkera_core.authz import agent_headers
    from tests._compute_helpers import make_grant, make_machine_type
    from tests.conftest import mint_cli_token

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    registered = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": pod,
            "name": "demo-box",
            "machine_type_code": machine_type.provider_type_id,
        },
        headers={"Authorization": f"Bearer {token}", **agent_headers("booting")},
    )
    assert registered.status_code == 201, registered.text
    machine_id = str(registered.json()["id"])
    return machine_id, {"Authorization": f"Bearer {token}", **agent_headers(machine_id)}


async def _decision_rows(org_id: UUID, chat_id: str) -> list[tuple[str, str, str]]:
    from alkera_core.authz import AUTHZ_EVENT_TYPE

    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ_EVENT_TYPE,
                EventOutbox.entity_id == chat_id,
            )
            .order_by(EventOutbox.id)
        )
    return [(r.payload["action"], r.payload["effect"], r.payload["reason"]) for r in rows.scalars()]


async def test_the_bound_machine_marks_a_members_chat_refused_and_clears_it_again(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A box the gateway would not let publish a chat says so on the chat: it
    reads ``refused`` with the gateway's reason (the banner's copy), the
    ``chat.updated`` doorbell rings, and when the box publishes again the flag
    clears and the binding's own status is back. Both calls are the machine's
    alone — the decision rows say who and why."""
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-vis-1")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="The member's chat")
    assert chat["machine_id"] == machine_id and chat["machine_status"] == "starting"
    assert chat["machine_refusal_reason"] is None
    chat_id = chat["id"]
    doorbells_before = len(await _events(org_admin.org_id, EventType.CHAT_UPDATED))

    refused = await client.put(
        f"/api/v1/chats/{chat_id}/publisher-state",
        json={"state": "refused", "reason": "forbidden: only the publisher writes"},
        headers=box,
    )
    assert refused.status_code == 200, refused.text
    assert refused.json()["machine_status"] == "refused"
    assert refused.json()["machine_refusal_reason"] == "forbidden: only the publisher writes"

    async with app_client() as theirs:
        await login(theirs, member.email, password)
        seen = await theirs.get(f"/api/v1/chats/{chat_id}")
        assert seen.json()["machine_status"] == "refused", "the member's banner reads it"
        assert seen.json()["machine_refusal_reason"] == "forbidden: only the publisher writes"
        listed = await theirs.get("/api/v1/chats")
        row = next(item for item in listed.json()["items"] if item["id"] == chat_id)
        assert row["machine_status"] == "refused"
    assert len(await _events(org_admin.org_id, EventType.CHAT_UPDATED)) == doorbells_before + 1

    cleared = await client.put(
        f"/api/v1/chats/{chat_id}/publisher-state", json={"state": "publishing"}, headers=box
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["machine_status"] == "starting", "the binding's own status is back"
    assert cleared.json()["machine_refusal_reason"] is None
    async with AsyncSessionLocal() as session:
        row_obj = await session.get(WorkspaceObject, UUID(chat_id))
        assert row_obj is not None and "publisher_refusal" not in row_obj.spec
        assert row_obj.version == 1, "a derived flag, not an edit"

    rows = await _decision_rows(org_admin.org_id, chat_id)
    assert [row for row in rows if row[0] == "write"] == [
        ("write", "allow", "bound_machine_reports"),
        ("write", "allow", "bound_machine_reports"),
    ]


@pytest.mark.parametrize(
    ("sent", "kind", "final"),
    [
        pytest.param("moving", "moving", True, id="a-verdict-is-final"),
        pytest.param("workspace_elsewhere", "workspace_elsewhere", False, id="a-wait-is-not"),
        pytest.param("from_a_newer_box", None, False, id="an-unknown-kind-is-none-and-not-final"),
        pytest.param(None, None, False, id="no-kind-from-an-older-box"),
    ],
)
async def test_a_refusal_carries_its_kind_and_whether_it_is_final_to_the_reader(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    sent: str | None,
    kind: str | None,
    final: bool,
) -> None:
    """The reader is shown the refusal's kind, never the box's sentence, and
    only a final kind ends a running turn. A kind this server does not know
    does not cost the report."""
    _machine_id, box = await _registered_machine(
        client, org_admin, real_session, pod=f"pod-kind-{sent}"
    )
    # The operator's browser is its own client: a cookie on the box's client
    # would win over the box's token.
    async with app_client() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat = await _create_chat(browser, title="A refused chat")
        assert chat["machine_refusal_kind"] is None and chat["machine_refusal_final"] is False
        body: dict[str, Any] = {"state": "refused", "reason": "the box's own words"}
        if sent is not None:
            body["refusal_kind"] = sent
        refused = await client.put(
            f"/api/v1/chats/{chat['id']}/publisher-state", json=body, headers=box
        )
        assert refused.status_code == 200, refused.text
        seen = (await browser.get(f"/api/v1/chats/{chat['id']}")).json()
        assert seen["machine_status"] == "refused"
        assert seen["machine_refusal_kind"] == kind
        assert seen["machine_refusal_final"] is final

        cleared = await client.put(
            f"/api/v1/chats/{chat['id']}/publisher-state",
            json={"state": "publishing"},
            headers=box,
        )
        assert cleared.status_code == 200, cleared.text
        again = (await browser.get(f"/api/v1/chats/{chat['id']}")).json()
        assert again["machine_refusal_kind"] is None
        assert again["machine_refusal_final"] is False


async def test_a_refusal_with_no_words_still_names_the_state(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-vis-2")
    # The operator's browser is its own client: a cookie on the box's client
    # would win over the Bearer and turn the box's report into a person's.
    async with app_client() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat = await _create_chat(browser, title="mine")
    assert chat["machine_id"] == machine_id
    refused = await client.put(
        f"/api/v1/chats/{chat['id']}/publisher-state", json={"state": "refused"}, headers=box
    )
    assert refused.status_code == 200, refused.text
    assert refused.json()["machine_status"] == "refused"
    assert refused.json()["machine_refusal_reason"] == "refused"


async def test_the_bound_machine_reads_a_members_transcript_through_the_binding_alone(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The box's catch-up read. A chat is private until its node is shared, and
    the operator whose token the box holds owns no rung on a colleague's chat:
    the binding is the grant. The box speaking as the machine the chat is
    bound to reads the transcript (``bound_machine_reads`` on record); the
    same token speaking as a person, as a machine the chat is not bound to,
    the box on a chat bound to no machine, and a member of another org are all
    told the chat does not exist."""
    from alkera_core.authz import agent_headers

    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        unbound = await _create_chat(theirs, title="before any box")
    assert unbound["machine_id"] is None

    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-read-1")
    # The box coming up takes every chat of the org that has no machine, this
    # one included; the read below is about a chat that is bound to nothing AT
    # the read, so the binding is taken off it again by hand.
    await _unbind_chat(unbound["id"])
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        bound = await _create_chat(theirs, title="the member's chat")
        posted = await theirs.post(
            f"/api/v1/chats/{bound['id']}/messages", json={"text": "how many?", "client_id": "c1"}
        )
        assert posted.status_code == 201, posted.text
    assert bound["machine_id"] == machine_id
    transcript = f"/api/v1/chats/{bound['id']}/messages"

    read = await client.get(transcript, headers=box)
    assert read.status_code == 200, read.text
    assert [item["role"] for item in read.json()["items"]] == ["user"]
    assert read.json()["items"][0]["payload"]["text"] == "how many?"

    as_a_person = await client.get(transcript, headers={"Authorization": box["Authorization"]})
    assert as_a_person.status_code == 404, "the operator is not the owner and holds no rung"
    as_another_machine = await client.get(
        transcript, headers={"Authorization": box["Authorization"], **agent_headers(str(uuid4()))}
    )
    assert as_another_machine.status_code == 404, "a machine the chat is not bound to"
    on_an_unbound_chat = await client.get(f"/api/v1/chats/{unbound['id']}/messages", headers=box)
    assert on_an_unbound_chat.status_code == 404, "a chat bound to no machine admits no machine"

    reads = [row for row in await _decision_rows(org_admin.org_id, bound["id"]) if row[0] == "read"]
    assert reads == [
        ("read", "allow", "bound_machine_reads"),
        ("read", "deny", "not_in_audience"),
        ("read", "deny", "not_in_audience"),
    ]

    async with app_client() as elsewhere:
        _other_org, stranger = await _other_org_member(elsewhere)
        foreign = await stranger.get(transcript, headers=agent_headers(machine_id))
    assert foreign.status_code == 404, "another org's member, even naming the machine"


async def _unbind_chat(chat_id: str) -> None:
    """A chat bound to no machine, as one reads while its org has none."""
    from alkera_core.models import WorkspaceObject

    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        chat.spec = {**chat.spec, "machine_id": None, "machine_status": "none"}
        await session.commit()


async def _release_machine(machine_id: str) -> None:
    """The box's row leaves the plane — what the meter does when its
    heartbeat lapses, or an operator releasing it."""
    from alkera_core.models.compute import ComputeAllocation

    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.state = "released"
        await session.commit()


async def test_a_colleague_asserting_the_bound_machines_id_is_not_the_machine(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The box's id is on every chat it serves (``machine_id`` on the read),
    and the agent assertion is a header anyone can put on their own session.
    So a machine assertion is admitted only when the request proves it IS
    that machine: a live workspace machine of the caller's org whose
    registered operator is the delegating user. A colleague of the chat's
    owner — no rung, the bound machine's id on their own session — is a
    person with a forged header, not the box: the transcript, the chat and
    the listing answer not-found and the denial is on record. The operator
    asserting a machine that has been released is refused the same way,
    while the box itself reads."""
    from alkera_core.authz import agent_headers

    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-forge-1")
    owner, owner_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    colleague, colleague_pw = await make_member(
        real_session, org_id=org_admin.org_id, verified=True
    )
    assert owner_pw is not None and colleague_pw is not None
    async with app_client() as theirs:
        await login(theirs, owner.email, owner_pw)
        chat = await _create_chat(theirs, title="the owner's chat")
        posted = await theirs.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "private", "client_id": "c1"}
        )
        assert posted.status_code == 201, posted.text
    assert chat["machine_id"] == machine_id, "the box's id is on the chat every reader sees"
    transcript = f"/api/v1/chats/{chat['id']}/messages"

    async with app_client() as forged:
        await login(forged, colleague.email, colleague_pw)
        as_the_box = await forged.get(transcript, headers=agent_headers(machine_id))
        assert as_the_box.status_code == 404, (
            "a colleague naming the bound machine on their own session is not the machine"
        )
        assert as_the_box.json()["error"]["message"] == "Not found"
        seen = await forged.get(f"/api/v1/chats/{chat['id']}", headers=agent_headers(machine_id))
        assert seen.status_code == 404, seen.text
        listed = await forged.get("/api/v1/chats", headers=agent_headers(machine_id))
        assert listed.status_code == 200, listed.text
        assert chat["id"] not in [item["id"] for item in listed.json()["items"]]

    genuine = await client.get(transcript, headers=box)
    assert genuine.status_code == 200, genuine.text
    assert [item["payload"]["text"] for item in genuine.json()["items"]] == ["private"]

    await _release_machine(machine_id)
    after_release = await client.get(transcript, headers=box)
    assert after_release.status_code == 404, "a released machine is no machine at all"

    reads = [row for row in await _decision_rows(org_admin.org_id, chat["id"]) if row[0] == "read"]
    assert reads == [
        ("read", "deny", "not_in_audience"),
        ("read", "deny", "not_in_audience"),
        ("read", "allow", "bound_machine_reads"),
        ("read", "deny", "not_in_audience"),
    ]


async def test_only_a_machine_may_say_whether_it_publishes_a_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The chat's owner as a person, an org admin, and the operator's token
    speaking as any other agent are all refused — the state is the machine's
    report, not a setting — and the chat is left exactly as it was."""
    from alkera_core.authz import agent_headers
    from tests.conftest import mint_cli_token

    machine_id, _box = await _registered_machine(client, org_admin, real_session, pod="pod-vis-3")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="The member's chat")
        as_owner = await theirs.put(
            f"/api/v1/chats/{chat['id']}/publisher-state", json={"state": "refused"}
        )
        assert as_owner.status_code == 403, as_owner.text
        assert as_owner.json()["error"]["message"] == "Not allowed"

    await login(client, org_admin.admin_email, org_admin.admin_password)
    as_admin = await client.put(
        f"/api/v1/chats/{chat['id']}/publisher-state", json={"state": "refused"}
    )
    assert as_admin.status_code == 403, as_admin.text

    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    bare = app_client()
    as_other_agent = await bare.put(
        f"/api/v1/chats/{chat['id']}/publisher-state",
        json={"state": "refused"},
        headers={"Authorization": f"Bearer {token}", **agent_headers("machine:demo-box")},
    )
    assert as_other_agent.status_code == 403, as_other_agent.text
    as_chat_agent = await bare.put(
        f"/api/v1/chats/{chat['id']}/publisher-state",
        json={"state": "refused"},
        headers={"Authorization": f"Bearer {token}", **agent_headers(chat["id"])},
    )
    assert as_chat_agent.status_code == 403, as_chat_agent.text
    await bare.aclose()

    async with app_client() as theirs:
        await login(theirs, member.email, password)
        untouched = await theirs.get(f"/api/v1/chats/{chat['id']}")
        assert untouched.json()["machine_status"] == "starting"
        assert untouched.json()["machine_refusal_reason"] is None
    rows = await _decision_rows(org_admin.org_id, chat["id"])
    assert [row for row in rows if row[0] == "write"] == [
        ("write", "deny", "publisher_machine_required")
    ] * 4
    assert machine_id, "the bound machine exists; nobody else spoke for it"


async def test_the_orgs_current_machine_may_speak_for_a_chat_bound_to_a_past_one(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A re-provisioned box registers under a new id while older chats stay
    bound to the old one: the machine that now serves the org may still say
    it cannot publish them, which is exactly what it will find out."""
    from alkera_core.authz import agent_headers
    from tests.conftest import mint_cli_token

    old_machine, _old_box = await _registered_machine(
        client, org_admin, real_session, pod="pod-vis-4a"
    )
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="before the re-provision")
    assert chat["machine_id"] == old_machine

    # The new box: the org's current machine (most recently heard from).
    new_machine, new_box = await _registered_machine(
        client, org_admin, real_session, pod="pod-vis-4b"
    )
    beat = await client.post(f"/api/v1/machines/{new_machine}/heartbeat", headers=new_box)
    assert beat.status_code == 204, beat.text
    current = await client.get("/api/v1/machines/current")
    assert current.json()["machine_id"] == new_machine

    refused = await client.put(
        f"/api/v1/chats/{chat['id']}/publisher-state",
        json={"state": "refused", "reason": "not_publisher"},
        headers=new_box,
    )
    assert refused.status_code == 200, refused.text
    assert refused.json()["machine_status"] == "refused"

    # A machine that is neither: refused.
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    stranger = await client.put(
        f"/api/v1/chats/{chat['id']}/publisher-state",
        json={"state": "publishing"},
        headers={"Authorization": f"Bearer {token}", **agent_headers(str(uuid4()))},
    )
    assert stranger.status_code == 403, stranger.text


async def test_a_chat_of_another_org_cannot_be_marked_by_this_orgs_machine(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-vis-5")
    other = app_client()
    _other_org, other = await _other_org_member(other)
    foreign = await _create_chat(other, title="theirs")
    await other.aclose()
    refused = await client.put(
        f"/api/v1/chats/{foreign['id']}/publisher-state", json={"state": "refused"}, headers=box
    )
    assert refused.status_code == 404, refused.text


# ---------------------------------------------------------------------------
# Deleting a chat
# ---------------------------------------------------------------------------


async def _member_client(
    client: AsyncClient, real_session: AsyncSession, org_id: UUID
) -> tuple[User, AsyncClient]:
    """A fresh, verified, logged-in member of ``org_id`` on its own client."""
    member, password = await make_member(
        real_session, org_id=org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    mine = app_client()
    await login(mine, member.email, password)
    return member, mine


def _error_shape(body: dict[str, Any]) -> tuple[Any, Any]:
    error = body["error"]
    return error["code"], error["message"]


async def test_the_owner_deletes_a_chat_and_it_is_gone_from_every_read(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A delete is a tombstone: the read surfaces all answer as if the chat
    never existed, the transcript stays, and the decision is on record."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="Going away")
    posted = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "hello", "client_id": "m-1"}
    )
    assert posted.status_code == 201, posted.text
    announced_before = len(await _events(org_admin.org_id, EventType.CHAT_UPDATED))

    deleted = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert deleted.status_code == 204, deleted.text
    assert deleted.content == b""

    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
    listed = await client.get("/api/v1/chats")
    assert listed.status_code == 200
    assert chat["id"] not in [item["id"] for item in listed.json()["items"]]
    again = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "still there?", "client_id": "m-2"}
    )
    assert again.status_code == 404, again.text
    assert (await client.get(f"/api/v1/chats/{chat['id']}/messages")).status_code == 404

    row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    assert row is not None, "a delete is a tombstone, never a dropped row"
    assert row.deleted_at > 0
    assert [message.seq for message in await _messages(UUID(chat["id"]))] == [1], (
        "the transcript stays with the tombstone"
    )
    announced = await _events(org_admin.org_id, EventType.CHAT_UPDATED)
    assert len(announced) == announced_before + 1
    assert announced[-1].entity_id == chat["id"], "the delete rings the same doorbell a rename does"
    assert await _decisions(org_admin.org_id, entity_id=chat["id"]) == [
        {"action": "create", "effect": "allow"},
        {"action": "send", "effect": "allow"},
        {"action": "delete", "effect": "allow"},
    ]


async def test_an_org_admin_deletes_a_members_chat(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the deployment lets admins read private chats, an admin deletes
    one; where it does not, the chat is the same not-found on DELETE as on
    every other door."""
    monkeypatch.setattr(settings, "chat_org_admin_reads_private", True)
    _member, mine = await _member_client(client, real_session, org_admin.org_id)
    chat = await _create_chat(mine, title="A member's chat")
    await mine.aclose()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    deleted = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
    assert await _decisions(org_admin.org_id, entity_id=chat["id"]) == [
        {"action": "create", "effect": "allow"},
        {"action": "delete", "effect": "allow"},
    ]


@pytest.mark.usefixtures("files_on")
async def test_a_member_may_not_delete_another_members_chat(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A shared reader who is neither the owner nor an org admin is refused,
    and the refusal is a plain 403 — the chat was shared with them, so there
    is nothing to hide — with the deny on record and the chat untouched."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="The admin's chat")
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, mine = await _member_client(client, real_session, org_admin.org_id)
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_READER)
    assert (await mine.get(f"/api/v1/chats/{chat['id']}")).status_code == 200
    refused = await mine.delete(f"/api/v1/chats/{chat['id']}")
    await mine.aclose()
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["message"] == chat_policy.DELETE_DENIED_MESSAGE
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 200
    assert await _decisions(org_admin.org_id, entity_id=chat["id"]) == [
        {"action": "create", "effect": "allow"},
        {"action": "read", "effect": "allow"},
        {"action": "delete", "effect": "deny"},
        {"action": "read", "effect": "allow"},
    ], "the member's read, their refused delete, then the owner's read of the chat still there"


async def test_deleting_a_chat_of_another_org_is_an_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with app_client() as intruder:
        other_org, _ = await _other_org_member(intruder)
        response = await intruder.delete(f"/api/v1/chats/{chat['id']}")
        missing = await intruder.delete(f"/api/v1/chats/{uuid4()}")
    assert response.status_code == 404, response.text
    assert missing.status_code == 404
    assert (
        _error_shape(response.json()) == _error_shape(missing.json()) == ("not_found", "Not found")
    )
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).status_code == 200
    assert {"action": "delete", "effect": "deny"} in await _decisions(
        org_admin.org_id, entity_id=chat["id"]
    ), "the deny is filed under the chat's org, beside its create and read rows"
    assert await _decisions(other_org, entity_id=chat["id"]) == []


async def test_deleting_a_deleted_chat_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    assert (await client.delete(f"/api/v1/chats/{chat['id']}")).status_code == 204
    second = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert second.status_code == 404, second.text
    assert await _decisions(org_admin.org_id, entity_id=chat["id"]) == [
        {"action": "create", "effect": "allow"},
        {"action": "delete", "effect": "allow"},
    ], "a tombstone is not found before any policy is asked"


# ---------------------------------------------------------------------------
# Starting a chat FROM a replication context
# ---------------------------------------------------------------------------


_REPORT_SPEC: dict[str, Any] = {
    "title": "Weekly mentions",
    "questions": [
        {
            "name": "region",
            "type": "enum",
            "label": "Region",
            "enum_values": ["emea", "amer"],
            "prompt": "Which region should this report cover?",
        }
    ],
    "connections": [{"plugin": "postgres", "handle": "warehouse"}],
    "steps": [
        {
            "kind": "query",
            "text": "SELECT week_start, n FROM mentions WHERE region = {region}",
            "connection": "warehouse",
        },
        {"kind": "render", "text": "A line chart, then the table.", "connection": ""},
    ],
    "narrative": "What moved this period.",
    "rendering": {"format": "pdf", "template": "Summary, then one section per region."},
}


def _refusal(response: Any) -> Any:
    """A refusal stripped of the one field that differs per request, so two of
    them can be compared for being indistinguishable."""
    body = dict(response.json())
    error = dict(body.get("error") or body)
    error.pop("trace_id", None)
    return error


async def _report_node(session: AsyncSession, owner_id: UUID) -> tuple[str, str]:
    """A saved report, and the Files node its folder IS."""
    object_id = await _object_of_type(
        session,
        owner_id,
        type="report",
        spec=_REPORT_SPEC,
        client_id=f"o-{uuid4().hex[:8]}",
    )
    return object_id, await _folder_node(session, object_id)


async def _folder_node(session: AsyncSession, object_id: str) -> str:
    """The Files folder one workspace object IS."""
    rows = list(
        (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == UUID(object_id),
                    FileNode.trashed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    folders = [row for row in rows if row.kind == "folder"]
    assert rows, f"no live node stands for {object_id}"
    return str((folders or rows)[0].id)


async def _template_node(
    session: AsyncSession,
    owner_id: UUID,
    *,
    title: str = "Weekly numbers",
    brief: str = "Pull the weekly numbers.",
    permission_mode: str = "read_only",
    model: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """A chat template, and the Files node its folder IS."""
    spec: dict[str, Any] = {"brief": brief, "permission_mode": permission_mode}
    if model is not None:
        spec["model"] = model
    from alkera_core.models import User as _User
    from backend.services.objects import object_service

    owner = await session.get(_User, owner_id)
    assert owner is not None
    created, _ = await object_service.create_object(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="chat_template",
        title=title,
        spec=spec,
        logical_id=f"t-{uuid4().hex[:8]}",
    )
    await session.commit()
    return str(created.id), await _folder_node(session, str(created.id))


async def _set_starting_mode(session: AsyncSession, user_id: UUID, org_id: UUID, mode: str) -> None:
    """The stance this reader's next cloud chat starts in, saved as a preference
    — the same row the composer's own control writes."""
    from backend.services.org import preferences as preferences_service

    await preferences_service.merge(
        session, user_id=user_id, org_id=org_id, incoming={"default_permission_mode": mode}
    )
    await session.commit()


async def _scratch_of(session: AsyncSession, node_id: str) -> FileNode:
    """The ``scratch`` folder under one object's folder."""
    node = (
        await session.execute(
            select(FileNode).where(
                FileNode.parent_id == UUID(node_id),
                FileNode.name == b"scratch",
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalar_one()
    return node


async def _put_bytes(
    client: AsyncClient,
    session: AsyncSession,
    org: OrgWithAdmin,
    parent: FileNode,
    name: str,
    payload: bytes,
) -> str:
    """Land one real file under ``parent`` through the upload path, and answer
    its content hash — the thing a copy has to reproduce."""
    from blake3 import blake3

    prefix = "/api/v1/files"
    opened = await client.post(
        f"{prefix}/uploads",
        json={"declaredSize": len(payload), "name": name, "parentId": str(parent.id)},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert opened.status_code == 201, opened.text
    upload_id = opened.json()["uploadId"]
    checksum = blake3(payload).digest().hex()
    part = await client.put(
        f"{prefix}/uploads/{upload_id}/parts/1",
        content=payload,
        headers={"Idempotency-Key": uuid4().hex, "X-Part-Checksum": checksum},
    )
    assert part.status_code == 200, part.text
    done = await client.post(
        f"{prefix}/uploads/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}]},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert done.status_code == 202, done.text
    # The route promises an operation; the worker is not part of a route test,
    # so the commit that turns it into a head version is run here in-process.
    import uuid as _uuid

    from alkera_core.authz.principal import ActingContext
    from alkera_core.config import settings as _settings
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.ids import DomainId, OperationId, OrgScope, SessionId
    from alkera_core.files.repo import FilesRepo
    from alkera_core.files.uploads import UploadCompletion
    from backend.services.files.store import store_factory

    repo = FilesRepo(session, OrgScope(org_team_id=org.org_id))
    ctx = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)
    async with repo.transaction():
        drive = await repo.drive_for_org()
    assert drive is not None
    store = await store_factory(_settings, clock=SystemClock()).for_domain(
        DomainId(drive.dedup_domain_id)
    )
    from alkera_core.files.errors import PreconditionFailed

    try:
        await UploadCompletion(repo, ctx, SystemClock(), store).promote(
            SessionId(_uuid.UUID(upload_id)), OperationId(_uuid.UUID(done.json()["id"]))
        )
    except PreconditionFailed:
        # A small upload can be promoted inline before the answer is read; the
        # bytes are landed either way, which is all this helper promises.
        pass
    await session.commit()
    return checksum


def _files_under(root: Path) -> list[Path]:
    """Every file below ``root`` — what a store that was written to has and one
    that was not does not."""
    return [path for path in root.rglob("*") if path.is_file()]


@pytest.fixture
def a_store_an_earlier_test_left_installed(tmp_path: Path) -> Iterator[Path]:
    """The process holding a store before this test arrives — the ordinary state
    of an xdist worker, since the factory is process-wide and built once.

    Requested BEFORE ``files_on`` so the fixture under test has something to
    displace; without that, a fixture that only edits settings looks correct.
    """
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.store.scoped import FilesystemScoped
    from backend.services.files.store import set_store_factory

    earlier = tmp_path / "a-store-from-earlier"
    earlier.mkdir()
    set_store_factory(FilesystemScoped(earlier, clock=SystemClock()))
    try:
        yield earlier
    finally:
        set_store_factory(None)


@pytest.mark.usefixtures("a_store_an_earlier_test_left_installed", "files_on")
async def test_a_chat_tests_bytes_land_in_the_store_that_test_configured(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    tmp_path: Path,
    a_store_an_earlier_test_left_installed: Path,
) -> None:
    """Enabling Files for a chat test has to point the backend at this test's
    own store, not merely say so in settings.

    The factory a request borrows from is process-wide and built on first use,
    so a worker that opened one earlier keeps serving it however the settings
    read now. A chat test that writes a byte then writes it into whatever the
    environment named — a developer's dev object store, or, on a machine
    running none, nothing at all, which is a 503 out of a route that has
    nothing to do with the chat under test.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _object_id, node_id = await _template_node(real_session, org_admin.admin_id)
    scratch = await _scratch_of(real_session, node_id)

    await _put_bytes(client, real_session, org_admin, scratch, "query.sql", b"SELECT 1;\n")

    assert _files_under(tmp_path / "files-store"), (
        "the bytes did not land in the store this test configured"
    )
    assert not _files_under(a_store_an_earlier_test_left_installed), (
        "the bytes went to the store an earlier test left installed"
    )


@pytest.mark.usefixtures("files_on")
async def test_a_chat_started_from_a_template_carries_its_files_and_provenance(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The point of a template: the new chat opens with the template's files
    already in its working directory, and knows what it came from.

    The bytes are the test, not the row count — a copy that made empty nodes
    would leave the agent looking at filenames with nothing in them. The brief
    rides the chat's own spec because the box runs as the machine and the
    template may be a folder only its author may read.
    """
    from alkera_core.models.files.versions import FileVersion

    await login(client, org_admin.admin_email, org_admin.admin_password)
    object_id, node_id = await _template_node(real_session, org_admin.admin_id, title="Weekly")
    scratch = await _scratch_of(real_session, node_id)
    digest = await _put_bytes(client, real_session, org_admin, scratch, "query.sql", b"SELECT 1;\n")

    chat = await _create_chat(client, source_node_id=node_id)

    assert chat["source_node_id"] == node_id
    assert chat["source_object_id"] == object_id
    assert chat["title"] == "Weekly", "the template's title is the default"
    stored = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    spec = chat_service.chat_spec_of(stored)
    assert (spec.source_node_id, spec.source_object_id) == (node_id, object_id)
    assert stored.spec["metadata"]["template_brief"] == "Pull the weekly numbers."
    chat_scratch = await _scratch_of(real_session, chat["files_node_id"])
    copied = list(
        (
            await real_session.execute(
                select(FileNode).where(
                    FileNode.parent_id == chat_scratch.id, FileNode.trashed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert [bytes(row.name) for row in copied] == [b"query.sql"]
    head = (
        (
            await real_session.execute(
                select(FileVersion)
                .where(FileVersion.node_id == copied[0].id)
                .order_by(FileVersion.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert head is not None and head.content_hash == digest


@pytest.mark.usefixtures("files_on")
async def test_the_chat_is_announced_only_once_its_template_files_are_there(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The doorbell wakes a box for the chat. A box that opens the chat while
    the copy is still running finds a working directory missing the very files
    the template exists to hand it, so the announce is written after the copy
    rows — which is what the outbox order proves."""
    from alkera_core.models import EventOutbox

    await login(client, org_admin.admin_email, org_admin.admin_password)
    _object_id, node_id = await _template_node(real_session, org_admin.admin_id)
    scratch = await _scratch_of(real_session, node_id)
    await _put_bytes(client, real_session, org_admin, scratch, "notes.md", b"# how\n")

    chat = await _create_chat(client, source_node_id=node_id)

    rows = list(
        (
            await real_session.execute(
                select(EventOutbox)
                .where(EventOutbox.org_id == org_admin.org_id)
                .order_by(EventOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    announced = [
        index
        for index, row in enumerate(rows)
        if row.type == "chat.updated" and row.entity_id == chat["id"]
    ]
    assert announced, "the chat was never announced"
    files_before = [
        index
        for index, row in enumerate(rows)
        if row.type.startswith("file_node.") and index < announced[0]
    ]
    assert files_before, "no Files row was written before the chat was announced"


@pytest.mark.usefixtures("files_on")
async def test_a_template_with_a_brief_opens_the_chat_by_saying_it(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A brief is an instruction, so the chat starts with it said.

    It is the starter's own message — the same row, role and kind anything they
    type becomes — because a hidden preamble is one they cannot see, edit or
    re-send. What marks it out is the record's provenance, which is how the
    reader captions the bubble without the words being altered to say so.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    object_id, node_id = await _template_node(
        real_session, org_admin.admin_id, title="Weekly", brief="Pull the weekly numbers."
    )
    scratch = await _scratch_of(real_session, node_id)
    await _put_bytes(client, real_session, org_admin, scratch, "query.sql", b"SELECT 1;\n")

    chat = await _create_chat(client, source_node_id=node_id)

    rows = list(
        (
            await real_session.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == UUID(chat["id"]))
                .order_by(ChatMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the chat opened silent"
    first = rows[0]
    assert (first.role, first.kind) == ("user", chat_service.USER_MESSAGE_KIND)
    assert first.payload["text"] == "Pull the weekly numbers."
    owner = await real_session.get(User, org_admin.admin_id)
    assert first.payload["metadata"] == {
        "source": "template_brief",
        "template_node_id": node_id,
        "template_object_id": object_id,
        "template_title": "Weekly",
        "template_author": owner.display_name or owner.email,
    }


@pytest.mark.usefixtures("files_on")
async def test_the_brief_names_the_author_who_wrote_it(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A shared template's brief is somebody else's words opening your chat.

    The starter is recorded as the speaker — it is their chat and their turn to
    spend — so nothing in the row itself says the sentence was written by
    another person. The author rides the provenance for exactly that reason,
    and is the template OWNER's name, never the starter's.
    """
    from alkera_core.models import User as _User

    await login(client, org_admin.admin_email, org_admin.admin_password)
    author = await real_session.get(_User, org_admin.admin_id)
    author.first_name, author.last_name = "Grace", "Hopper"
    await real_session.commit()
    _object_id, node_id = await _template_node(
        real_session, org_admin.admin_id, brief="Pull the weekly numbers."
    )

    chat = await _create_chat(client, source_node_id=node_id)

    first = (
        (
            await real_session.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == UUID(chat["id"]))
                .order_by(ChatMessage.seq)
            )
        )
        .scalars()
        .first()
    )
    assert first is not None
    assert first.payload["metadata"]["template_author"] == "Grace Hopper"


@pytest.mark.usefixtures("files_on")
async def test_an_author_with_no_name_is_still_named_by_their_email(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A caption whose author is blank tells the reader nothing about whose
    words these are, which is the one thing it exists to say. An author who
    never filled in a name is named by the address that IS their name here."""
    from alkera_core.models import User as _User

    await login(client, org_admin.admin_email, org_admin.admin_password)
    author = await real_session.get(_User, org_admin.admin_id)
    author.first_name, author.last_name = "", ""
    await real_session.commit()
    _object_id, node_id = await _template_node(
        real_session, org_admin.admin_id, brief="Pull the weekly numbers."
    )

    chat = await _create_chat(client, source_node_id=node_id)

    first = (
        (
            await real_session.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == UUID(chat["id"]))
                .order_by(ChatMessage.seq)
            )
        )
        .scalars()
        .first()
    )
    assert first is not None
    assert first.payload["metadata"]["template_author"] == org_admin.admin_email


@pytest.mark.usefixtures("files_on")
async def test_the_brief_is_said_only_once_the_template_files_are_there(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The brief tells the agent to get on with the work, and the work is the
    files. A brief relayed before the copy is an agent sent to read a working
    directory that is still being filled — so the relay is written after the
    Files rows and before the doorbell, which is what the outbox order proves.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _object_id, node_id = await _template_node(
        real_session, org_admin.admin_id, brief="Refresh the report."
    )
    scratch = await _scratch_of(real_session, node_id)
    await _put_bytes(client, real_session, org_admin, scratch, "notes.md", b"# how\n")

    chat = await _create_chat(client, source_node_id=node_id)

    rows = list(
        (
            await real_session.execute(
                select(EventOutbox)
                .where(EventOutbox.org_id == org_admin.org_id)
                .order_by(EventOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    # The relay rides the chat's DOCUMENT channel, not the chat's own entity —
    # it is what the box is subscribed to, which is the point of sending it.
    channel = chat_service.channel_key(UUID(chat["id"]))
    relayed = [
        index
        for index, row in enumerate(rows)
        if row.entity_id == channel and "Refresh the report." in json.dumps(row.payload)
    ]
    files_rows = [index for index, row in enumerate(rows) if row.type.startswith("file_node.")]
    announced = [
        index
        for index, row in enumerate(rows)
        if row.type == "chat.updated" and row.entity_id == chat["id"]
    ]
    assert relayed, "the brief was never relayed to the box"
    assert files_rows and files_rows[0] < relayed[0], "the brief went out before the files"
    assert announced and relayed[0] < announced[0], "the doorbell rang before the brief"


@pytest.mark.usefixtures("files_on")
async def test_a_template_with_no_brief_opens_the_chat_silent(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A template that is only files has nothing to say, and says nothing: no
    row, no empty bubble, no turn burnt on whitespace. Whitespace is the same
    as nothing — a brief somebody cleared to spaces is not an instruction."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _object_id, node_id = await _template_node(real_session, org_admin.admin_id, brief="   \n ")

    chat = await _create_chat(client, source_node_id=node_id)

    rows = list(
        (
            await real_session.execute(
                select(ChatMessage).where(ChatMessage.chat_id == UUID(chat["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.usefixtures("files_on")
async def test_a_chat_names_no_source_when_it_was_not_started_from_one(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The overwhelmingly common create is unchanged: no source named, none
    recorded, and the field reads null rather than absent."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="Ops")
    assert chat["source_node_id"] is None


@pytest.mark.usefixtures("files_on")
async def test_a_template_the_caller_cannot_read_is_an_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A reader outside the template's org is told the node does not exist — and
    is told exactly what an id that never existed is told, so naming a node you
    cannot read leaks nothing about whether it is there. The decision is the
    Files decider's, through the same predicate every other read of that node
    makes: the refusal is not a type check that happened to fail, which is why
    the SAME id answers 201 for someone who may read it.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _object_id, node_id = await _template_node(real_session, org_admin.admin_id)

    async with app_client() as other:
        await _other_org_member(other)
        refused = await other.post(
            "/api/v1/chats", json={"title": "Sneak", "source_node_id": node_id}
        )
        absent = await other.post(
            "/api/v1/chats", json={"title": "Sneak", "source_node_id": str(uuid4())}
        )
    allowed = await client.post(
        "/api/v1/chats", json={"title": "Re-run", "source_node_id": node_id}
    )

    assert refused.status_code == 404, refused.text
    assert absent.status_code == 404, absent.text
    assert _refusal(refused) == _refusal(absent)
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["source_node_id"] == node_id


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize(
    "kind",
    [pytest.param("chat", id="another-chat"), pytest.param("report", id="a-saved-report")],
)
async def test_a_node_that_is_not_a_template_is_refused_by_name(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, kind: str
) -> None:
    """A chat's own folder and a saved report are both perfectly readable to
    their owner and neither is something to start a chat FROM: a chat has no
    brief to hand on, and a report is a rendering nobody re-runs any more. The
    refusal is a 422 that says which kind of thing is wanted — not the 404 an
    unreadable node gets, because the caller is allowed to know."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    if kind == "chat":
        node_id = (await _create_chat(client, title="Ops"))["files_node_id"]
    else:
        _object_id, node_id = await _report_node(real_session, org_admin.admin_id)

    response = await client.post(
        "/api/v1/chats",
        json={"title": "From that", "source_node_id": node_id},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "chat.source_not_a_template"


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize(
    ("reader_mode", "template_mode", "expected"),
    [
        pytest.param("default", "read_only", "read_only", id="a-stricter-template-narrows"),
        pytest.param("read_only", "default", "read_only", id="a-looser-template-does-not-widen"),
        pytest.param("read_only", "bypass", "read_only", id="bypass-is-never-inherited"),
        pytest.param("plan", "bypass", "plan", id="bypass-does-not-widen-plan-either"),
        pytest.param("bypass", "bypass", "bypass", id="the-readers-own-bypass-stands"),
    ],
)
async def test_a_template_can_narrow_the_stance_but_never_widen_it(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    reader_mode: str,
    template_mode: str,
    expected: str,
) -> None:
    """A template is a document somebody else wrote. If it could raise the
    stance, saving one would be a way to hand every reader a permission they
    never chose — ``bypass`` most of all. So the chat opens in the READER's own
    stance, narrowed where the template's is stricter and never widened."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await _set_starting_mode(real_session, org_admin.admin_id, org_admin.org_id, reader_mode)
    _object_id, node_id = await _template_node(
        real_session, org_admin.admin_id, permission_mode=template_mode
    )

    chat = await _create_chat(client, source_node_id=node_id)

    assert chat["permission_mode"] == expected


@pytest.mark.usefixtures("files_on")
async def test_a_create_that_names_a_template_never_claims_a_warmed_spare(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A spare is warmed blank: its working directory is empty and its pins are
    whatever the warming decided. Handing one back for a create that names a
    template would answer with a chat that has none of the template's files in
    it — so the template path always makes a new chat, and the spare is left
    exactly as it was for the next ordinary send."""
    from alkera_core.models import User as _User
    from alkera_core.objects import chat_spares

    await login(client, org_admin.admin_email, org_admin.admin_password)
    owner = await real_session.get(_User, org_admin.admin_id)
    assert owner is not None
    spare, _ = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="",
        client_id=None,
        machine_id=None,
        machine_status="none",
        spare=True,
    )
    await real_session.commit()
    spare_id = spare.id
    _object_id, node_id = await _template_node(real_session, org_admin.admin_id)

    chat = await _create_chat(client, source_node_id=node_id, claim_spare=True)

    assert chat["id"] != str(spare_id), "the spare was handed back for a template create"
    real_session.expire_all()
    still = await real_session.get(WorkspaceObject, spare_id)
    assert still is not None, "the spare was reaped by a create that should not have touched it"
    assert chat_spares.is_spare(still), "the spare was claimed by a create that named a template"
    # Left standing, a warm spare is a fixture for every later test in this
    # database: the next one that warms or sweeps one would find two.
    await chat_spares.reap(real_session, still)
    await real_session.commit()


async def test_a_chat_whose_folder_is_trashed_says_so_rather_than_going_quiet(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """``files_node_id`` alone cannot tell "this chat has no drive" apart from
    "somebody threw the working directory away" — both read null. The flag is
    what separates them, so a Files panel can say which one happened."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="Ops")
    node_id = chat["files_node_id"]
    assert node_id is not None
    assert chat["files_node_trashed"] is False
    scratch = await _scratch_of(real_session, node_id)

    item = await client.get(f"/api/v1/files/drives/{scratch.drive_id}/items/{node_id}")
    assert item.status_code == 200, item.text
    trashed = await client.delete(
        f"/api/v1/files/drives/{scratch.drive_id}/items/{node_id}",
        headers={"Idempotency-Key": uuid4().hex, "If-Match": item.json()["etag"]},
    )
    assert trashed.status_code in (200, 202, 204), trashed.text

    read = await client.get(f"/api/v1/chats/{chat['id']}")
    assert read.status_code == 200, read.text
    assert read.json()["files_node_id"] is None
    assert read.json()["files_node_trashed"] is True


async def test_deleting_a_chat_trashes_its_folder_once_and_leaves_it_in_the_trash(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A chat IS its folder, so deleting it puts that folder in the trash — the
    one place the retention window can still bring it back from.

    Exactly once, and with the stamp the first delete wrote: a second delete of
    an already-tombstoned chat is a 404 that must not re-trash the node, or the
    row would read as freshly thrown away every time somebody retried it.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="Going into the trash")
    node_id = chat["files_node_id"]
    assert node_id is not None

    deleted = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert deleted.status_code == 204, deleted.text

    real_session.expire_all()
    node = await real_session.get(FileNode, UUID(node_id))
    assert node is not None, "the node is trashed, never dropped"
    assert node.trashed_at is not None, "the chat's folder went to the trash with it"
    first_trashed_at = node.trashed_at

    again = await client.delete(f"/api/v1/chats/{chat['id']}")
    assert again.status_code == 404, again.text

    real_session.expire_all()
    node = await real_session.get(FileNode, UUID(node_id))
    assert node is not None
    assert node.trashed_at == first_trashed_at, "the second delete re-trashes nothing"
    live = (
        await real_session.execute(
            select(FileNode).where(FileNode.id == UUID(node_id), FileNode.trashed_at.is_(None))
        )
    ).scalar_one_or_none()
    assert live is None, "no live copy of a deleted chat's folder is left behind"


async def test_the_operators_other_sessions_are_not_the_box(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The box holds one credential: the device token it registered with. The
    operator who registered it is a person on every other session — the
    browser cookie they log in with, a device token minted afterwards — and a
    person asserting the bound machine's id on either is told a colleague's
    unshared chat does not exist, with the denial on record. Only the
    registering token, asserting the machine, reads the transcript."""
    from alkera_core.authz import agent_headers
    from tests.conftest import mint_cli_token

    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-sess-1")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="the member's private chat")
        posted = await theirs.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "private", "client_id": "c1"}
        )
        assert posted.status_code == 201, posted.text
    assert chat["machine_id"] == machine_id
    transcript = f"/api/v1/chats/{chat['id']}/messages"

    async with app_client() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        on_a_cookie = await browser.get(transcript, headers=agent_headers(machine_id))
    assert on_a_cookie.status_code == 404, "the operator's browser session is not the box"

    later = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    on_a_later_token = await client.get(
        transcript, headers={"Authorization": f"Bearer {later}", **agent_headers(machine_id)}
    )
    assert on_a_later_token.status_code == 404, "a device token minted afterwards is not the box"

    as_the_box = await client.get(transcript, headers=box)
    assert as_the_box.status_code == 200, as_the_box.text
    assert as_the_box.json()["items"][0]["payload"]["text"] == "private"

    reads = [row for row in await _decision_rows(org_admin.org_id, chat["id"]) if row[0] == "read"]
    assert reads == [
        ("read", "deny", "not_in_audience"),
        ("read", "deny", "not_in_audience"),
        ("read", "allow", "bound_machine_reads"),
    ]


async def test_the_box_registering_again_with_a_new_device_token_stays_the_box(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A daemon restarted after a fresh ``alkera login`` registers the same pod
    with a new token and answers 200: the row follows its operator's current
    credential, so the new token is the box and the old one, still valid as a
    session, is now a person naming a machine — refused the transcript."""
    from alkera_core.authz import agent_headers
    from tests._compute_helpers import make_grant, make_machine_type
    from tests.conftest import mint_cli_token

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    body = {
        "provider": "runpod",
        "provider_pod_id": "pod-relogin-1",
        "name": "demo-box",
        "machine_type_code": machine_type.provider_type_id,
    }

    async def device_token() -> str:
        return await mint_cli_token(
            user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
        )

    first = await device_token()
    registered = await client.post(
        "/api/v1/machines/register",
        json=body,
        headers={"Authorization": f"Bearer {first}", **agent_headers("booting")},
    )
    assert registered.status_code == 201, registered.text
    machine_id = str(registered.json()["id"])
    old_box = {"Authorization": f"Bearer {first}", **agent_headers(machine_id)}

    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="served across a re-login")
    assert chat["machine_id"] == machine_id
    transcript = f"/api/v1/chats/{chat['id']}/messages"
    assert (await client.get(transcript, headers=old_box)).status_code == 200

    fresh = await device_token()
    again = await client.post(
        "/api/v1/machines/register",
        json=body,
        headers={"Authorization": f"Bearer {fresh}", **agent_headers("booting")},
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == machine_id

    new_box = {"Authorization": f"Bearer {fresh}", **agent_headers(machine_id)}
    assert (await client.get(transcript, headers=new_box)).status_code == 200
    assert (await client.get(transcript, headers=old_box)).status_code == 404, (
        "the token the box no longer holds is a person naming a machine"
    )


async def test_a_slept_chat_reads_asleep_and_a_reader_opening_it_leaves_it_asleep(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The machine's heartbeat says the BOX is alive; only the box can say
    whether it holds THIS chat's session. A box that put the chat to sleep
    reports so, and the chat reads ``asleep`` on a machine that is ``ready``,
    on the detail and in the list, and its session reads ``asleep`` too.
    Reading a chat is not running it: a person opening it stamps no wake and
    rings no doorbell, so no agent starts for a chat somebody only looked at.
    The box reporting the chat open again reads ``ready`` and ``awake``. A box
    that stopped answering keeps its own word over a sleeping row: a chat
    asleep on a dead box is a dead box, and its session is not awake."""
    from datetime import timedelta

    from alkera_core.models import ComputeAllocation

    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-sleep-1")
    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    assert beat.status_code in (200, 204), beat.text
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    state = "/api/v1/chats/{chat_id}/publisher-state"
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="Sleepy")
        chat_id = chat["id"]
        assert chat["machine_status"] == "ready" and chat["wake_requested_at"] is None
        detail = f"/api/v1/chats/{chat_id}"

        awake = await client.put(
            state.format(chat_id=chat_id), json={"state": "publishing"}, headers=box
        )
        assert awake.status_code == 200, awake.text
        assert awake.json()["machine_status"] == "ready"
        assert awake.json()["session_state"] == "awake"

        slept = await client.put(
            state.format(chat_id=chat_id), json={"state": "asleep"}, headers=box
        )
        assert slept.status_code == 200, slept.text
        assert slept.json()["machine_status"] == "asleep"
        assert slept.json()["machine_refusal_reason"] is None
        assert slept.json()["wake_requested_at"] is None
        assert slept.json()["session_state"] == "asleep"

        # The box's own discovery reads the row: that is not a person opening it.
        own = await client.get(detail, headers=box)
        assert own.status_code == 200, own.text
        assert own.json()["machine_status"] == "asleep" and own.json()["wake_requested_at"] is None
        doorbells = len(await _events(org_admin.org_id, EventType.CHAT_UPDATED))

        for _ in range(2):
            opened = await theirs.get(detail)
            assert opened.status_code == 200, opened.text
            assert opened.json()["machine_status"] == "asleep"
            assert opened.json()["session_state"] == "asleep"
            assert opened.json()["wake_requested_at"] is None, "reading a chat never wakes it"
        assert len(await _events(org_admin.org_id, EventType.CHAT_UPDATED)) == doorbells
        listed = await theirs.get("/api/v1/chats")
        row = next(item for item in listed.json()["items"] if item["id"] == chat_id)
        assert row["machine_status"] == "asleep"

        woke = await client.put(
            state.format(chat_id=chat_id), json={"state": "publishing"}, headers=box
        )
        assert woke.json()["machine_status"] == "ready"
        assert woke.json()["session_state"] == "awake"
        async with AsyncSessionLocal() as session:
            row_obj = await session.get(WorkspaceObject, UUID(chat_id))
            assert row_obj is not None and row_obj.version == 1, "a derived flag, not an edit"

        # The box sleeps the chat, then stops answering: the machine's word wins.
        await client.put(state.format(chat_id=chat_id), json={"state": "asleep"}, headers=box)
        async with AsyncSessionLocal() as session:
            alloc = await session.get(ComputeAllocation, UUID(machine_id))
            assert alloc is not None
            alloc.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
            await session.commit()
        dead = await theirs.get(detail)
        assert dead.json()["machine_status"] == "unreachable"
        assert dead.json()["session_state"] == "asleep"


async def test_a_chat_its_box_has_no_slot_for_reads_queued_until_it_is_served(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Seen under load: chats with a message waiting read ``ready`` for four
    minutes while their box served others. The box reports ``waiting`` when it
    has the message but no free slot, and the chat reads ``queued``; a second
    ``waiting`` keeps the first stamp (the wait began then), and the box's next
    report on the chat clears it. A ``waiting`` is not an edit and refuses
    nothing: the refusal reason stays empty."""
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-q-1")
    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    assert beat.status_code in (200, 204), beat.text
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    state = "/api/v1/chats/{chat_id}/publisher-state"

    async def _stamp(chat_id: str) -> Any:
        async with AsyncSessionLocal() as session:
            row = await session.get(WorkspaceObject, UUID(chat_id))
            assert row is not None
            return row.spec.get("slot_wait_at"), row.version

    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = (await _create_chat(theirs, title="Queued"))["id"]
        await client.put(state.format(chat_id=chat_id), json={"state": "asleep"}, headers=box)
        sent = await theirs.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": "hello", "client_id": "c-q1"}
        )
        assert sent.status_code == 201, sent.text
        assert (await theirs.get(f"/api/v1/chats/{chat_id}")).json()["session_state"] == "waking"

        waiting = await client.put(
            state.format(chat_id=chat_id), json={"state": "waiting"}, headers=box
        )
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["session_state"] == "queued"
        assert waiting.json()["machine_refusal_reason"] is None
        first, version = await _stamp(chat_id)
        assert first is not None
        listed = await theirs.get("/api/v1/chats")
        row = next(item for item in listed.json()["items"] if item["id"] == chat_id)
        assert row["session_state"] == "queued"

        again = await client.put(
            state.format(chat_id=chat_id), json={"state": "waiting"}, headers=box
        )
        assert again.json()["session_state"] == "queued"
        assert await _stamp(chat_id) == (first, version), "the wait began at the first report"

        served = await client.put(
            state.format(chat_id=chat_id), json={"state": "publishing"}, headers=box
        )
        assert served.json()["session_state"] == "working"
        assert (await _stamp(chat_id))[0] is None


async def test_answering_an_ask_in_a_slept_chat_wakes_it(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Opening a slept chat no longer wakes it, so the answer to the ask it was
    parked on must: answering asks the agent to go on. The answer stamps the
    wake (the box takes the chat and reads the recorded resolution) and the
    chat reads ``waking`` until the box reports it open."""
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-ask-1")
    assert (
        await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    ).status_code in (200, 204)
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    state = "/api/v1/chats/{chat_id}/publisher-state"
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = (await _create_chat(theirs, title="Parked"))["id"]
        await client.put(state.format(chat_id=chat_id), json={"state": "publishing"}, headers=box)
        await _machine_published(chat_id, _permission_ask(chat_id))
        await client.put(state.format(chat_id=chat_id), json={"state": "asleep"}, headers=box)
        assert (await theirs.get(f"/api/v1/chats/{chat_id}")).json()["wake_requested_at"] is None

        answered = await theirs.post(
            f"/api/v1/chats/{chat_id}/answer",
            json={"interrupt_id": "perm-1", "option_id": "allow_once"},
        )
        assert answered.status_code == 202, answered.text
        read = (await theirs.get(f"/api/v1/chats/{chat_id}")).json()
        assert read["wake_requested_at"] is not None, "the answer wakes the slept chat"
        assert read["session_state"] == "waking"

        woke = await client.put(
            state.format(chat_id=chat_id), json={"state": "publishing"}, headers=box
        )
        assert woke.json()["wake_requested_at"] is None
        assert woke.json()["session_state"] == "awake"


async def test_a_box_putting_a_chat_to_sleep_answers_any_wake_still_standing(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The box took the chat for a wake, but its ``awake`` report was lost: the
    wake stood, and the box re-took the chat after every sleep with nobody
    reading. A box reporting the chat asleep has held it, so the wake is
    answered with that report too."""
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-wake-2")
    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    assert beat.status_code in (200, 204), beat.text
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    state = "/api/v1/chats/{chat_id}/publisher-state"
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = (await _create_chat(theirs, title="Stale wake"))["id"]
        await client.put(state.format(chat_id=chat_id), json={"state": "publishing"}, headers=box)
        await _machine_published(chat_id, _permission_ask(chat_id))
        await client.put(state.format(chat_id=chat_id), json={"state": "asleep"}, headers=box)
        await theirs.post(
            f"/api/v1/chats/{chat_id}/answer",
            json={"interrupt_id": "perm-1", "option_id": "allow_once"},
        )
        assert (await theirs.get(f"/api/v1/chats/{chat_id}")).json()["wake_requested_at"]

        # The box took it, its "publishing" report never arrived, and it slept.
        slept = await client.put(
            state.format(chat_id=chat_id), json={"state": "asleep"}, headers=box
        )
        assert slept.status_code == 200, slept.text
        assert slept.json()["wake_requested_at"] is None
        assert slept.json()["session_state"] == "asleep"


# ---------------------------------------------------------------------------
# Stopping the running turn
# ---------------------------------------------------------------------------


async def _stop_relays(org_id: UUID, chat_id: UUID) -> list[dict[str, Any]]:
    """Every relay body on this chat's channel whose kind is a stop."""
    out: list[dict[str, Any]] = []
    for row in await _events(org_id, EventType.DOC_OP):
        if row.entity_id != f"doc:chat:{chat_id}":
            continue
        for event in row.payload["envelope"]["payload"]["events"]:
            if event.get("kind") == "stop":
                out.append(event)
    return out


async def test_stopping_a_turn_relays_the_stop_and_records_who_pressed_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Stop is a relay the box cancels its turn on, and a line in the
    transcript: a reader who comes back to a turn that ended mid-answer has no
    other way to learn that a person ended it, or which one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )

    response = await client.post(f"/api/v1/chats/{chat['id']}/stop")
    assert response.status_code == 202, response.text

    relays = await _stop_relays(org_admin.org_id, chat_id)
    assert len(relays) == 1, "the box hears the stop on the chat's channel"
    assert relays[0]["user_id"] == str(org_admin.admin_id)

    rows = await _messages(chat_id)
    note = [row for row in rows if row.role == "system" and row.kind != "prompt.cancelled"]
    assert [row.kind for row in note] == ["message.created", "part.created", "message.completed"]
    text = note[1].payload["payload"]["part"]["text"]
    assert text.startswith("Stopped by ")
    assert "Admin" in text


async def test_a_stop_by_someone_with_no_name_never_puts_their_address_on_the_tape(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A profile is completed after signup, so a chat can be stopped by
    somebody who has not given a name yet. A roster may fall back on the half
    of an address in front of the @, beside a face the reader already knows;
    this sentence is read by everyone in the chat and keeps forever, so it says
    "a member" rather than publishing where to write to them."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as session:
        admin = await session.get(User, org_admin.admin_id)
        assert admin is not None
        admin.first_name, admin.last_name = "", ""
        await session.commit()

    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    note = [row for row in await _messages(UUID(chat["id"])) if row.event_id.startswith("stop-")]
    text = note[1].payload["payload"]["part"]["text"]
    assert text == "Stopped by a member of this workspace."
    local = org_admin.admin_email.split("@", 1)[0]
    assert local not in text and org_admin.admin_email not in text


async def test_the_stop_note_reaches_a_reader_who_is_watching_and_one_who_reloads(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The sentence a stop leaves behind is the server's to write and has to
    reach the channel, not only the table.

    The note is three events — the system message, its text, its close — and
    only the server can write them, because only the server knows which member
    pressed Stop. Written to the table alone they reached nobody who was
    watching: their transcript opened the notice block with nothing in it, and
    the sentence appeared only once they reloaded. The live frames and the rows
    have to be the SAME events, under the same ids and sequences, or a reader
    who folds both renders the note twice and can name a row that does not
    exist.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = [row for row in await _messages(UUID(chat["id"])) if row.event_id.startswith("stop-")]
    assert [row.kind for row in rows] == ["message.created", "part.created", "message.completed"]

    live = _appended_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    note = [event for event in live if str(event.get("event_id", "")).startswith("stop-")]
    assert [event["kind"] for event in note] == [row.kind for row in rows], (
        "a watching reader is told the whole note, not part of it"
    )
    assert [event["event_id"] for event in note] == [row.event_id for row in rows]
    assert [event["seq"] for event in note] == [row.seq for row in rows]
    assert [event["payload"] for event in note] == [row.payload["payload"] for row in rows]
    assert "Stopped by " in note[1]["payload"]["part"]["text"], "the sentence itself goes out live"

    # What a reload folds is the window, and it has to be the same note again —
    # one entry per event, so the reader who watched and the reader who came
    # back arrive at the same transcript.
    async with AsyncSessionLocal() as session:
        doc = await session.get(RealtimeDoc, (org_admin.org_id, "chat", chat["id"]))
    assert doc is not None
    window = [
        event for event in doc.state["events"] if str(event.get("event_id", "")).startswith("stop-")
    ]
    assert window == note, "the snapshot carries exactly what the live frames carried"


async def test_the_relay_a_stop_mints_is_not_a_prompt_the_box_would_run(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The union routes on the wire ``kind``: a stop that parsed as a prompt
    would be handed to the harness as a turn instead of ending one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    relay = (await _stop_relays(org_admin.org_id, UUID(chat["id"])))[0]
    body = CHAT_RELAY_ADAPTER.validate_python(relay)
    assert isinstance(body, StopRelay)
    assert not isinstance(body, PromptRelay)


async def test_stopping_a_chat_nobody_shared_is_the_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A chat is private until shared, so a member who cannot see it gets the
    same opaque answer their read gets — and nothing is recorded or relayed."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        response = await other.post(f"/api/v1/chats/{chat['id']}/stop")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Not found"
    assert await _stop_relays(org_admin.org_id, chat_id) == []
    assert [row for row in await _messages(chat_id) if row.role == "system"] == []


async def test_a_stop_files_its_decision_either_way(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Both the allow and the refusal are on record — the refusal in its own
    committed session, so the rolled-back request still leaves the row."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    assert {"action": "send", "effect": "allow"} in await _decisions(
        org_admin.org_id, entity_id=chat["id"]
    )

    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        assert (await other.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 404
    assert {"action": "send", "effect": "deny"} in await _decisions(
        org_admin.org_id, entity_id=chat["id"]
    )


async def test_two_stops_are_two_lines_in_the_transcript(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A stop that ends a different turn is a different line.

    What tells the two presses apart is the turn between them: the note's id
    is derived from where the transcript stood, so a press after the chat has
    moved on sits past everything that turn wrote and takes an id of its own.
    Two presses with nothing in between are one stop — see
    ``test_a_stop_met_twice_is_one_note``, which is what lets a reader meet
    the note live, again off the record, and again from a mirror's echo
    without the sentence appearing three times.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "again", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = [row for row in await _messages(UUID(chat["id"])) if row.event_id.startswith("stop-")]
    assert len({row.event_id for row in rows}) == 6, "two notes, three rows each, no id shared"
    notes = [row for row in rows if row.kind == "part.created"]
    assert len(notes) == 2
    parts = [note.payload["payload"]["part"] for note in notes]
    assert len({part["message_id"] for part in parts}) == 2
    assert len({part["part_id"] for part in parts}) == 2, (
        "a part id shared across stops lets the second overwrite the first in every fold"
    )

    # And both notices are in the window a reader folds, each with its sentence.
    async with AsyncSessionLocal() as session:
        doc = await session.get(RealtimeDoc, (org_admin.org_id, "chat", chat["id"]))
    assert doc is not None
    texts = [
        event["payload"]["part"]["text"]
        for event in doc.state["events"]
        if event.get("kind") == "part.created"
        and str(event.get("event_id", "")).startswith("stop-")
    ]
    assert len(texts) == 2 and all(text.startswith("Stopped by ") for text in texts)


def _cancelled(rows: list[ChatMessage]) -> list[dict[str, Any]]:
    """Every message this chat's transcript says was never run, oldest first."""
    return [row.payload["payload"] for row in rows if row.kind == "prompt.cancelled"]


async def test_a_stop_before_the_box_answers_says_the_message_was_never_run(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The window a slow box opens: the message is recorded and relayed, and
    the reader presses Stop before anything comes back.

    Nothing about that window is durable except the message itself, so a Stop
    that only ended a turn would leave a row a reader wrote and nothing
    answered — which is precisely what the next box to open this chat runs. It
    is therefore recorded as not sent, named by the id the reader's own copy
    carries, and the line naming who stopped it lands as it always did.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = await _messages(UUID(chat["id"]))
    assert [
        (event["message_id"], event["client_id"], event["reason"]) for event in _cancelled(rows)
    ] == [("usr:s1", "s1", "stopped")]
    note = [row for row in rows if row.kind == "part.created"]
    assert note[0].payload["payload"]["part"]["text"].startswith("Stopped by ")


async def test_a_stop_unsays_every_message_still_waiting(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Whatever is waiting is waiting in order, and all of it is stopped: a
    reader who sent three messages and stopped is told about three, not the
    last one."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    for client_id in ("a", "b", "c"):
        await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "go", "client_id": client_id}
        )

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    assert [event["message_id"] for event in _cancelled(await _messages(UUID(chat["id"])))] == [
        "usr:a",
        "usr:b",
        "usr:c",
    ]


async def test_a_stop_publishes_everything_it_recorded_to_a_reader_who_is_watching(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A stop records two different facts and a reader who is watching has to
    be told BOTH: the messages it means will never run, and the line naming
    who ended the turn. Each is one event — the row and the frame are the same
    thing, under the same id and the same transcript sequence — so a reader
    who folded the frames and one who reads the page back arrive at the same
    transcript, and neither sees anything twice.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await client.post(f"/api/v1/chats/{chat['id']}/messages", json={"text": "go", "client_id": "a"})
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = {
        row.event_id: row
        for row in await _messages(UUID(chat["id"]))
        if row.kind == "prompt.cancelled" or row.event_id.startswith("stop-")
    }
    assert len(rows) == 4, "one cancelled message, and the note's three rows"

    live = _appended_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    published = {event["event_id"]: event for event in live if event["event_id"] in rows}
    assert set(published) == set(rows), "everything the stop recorded went out live"
    for event_id, event in published.items():
        row = rows[event_id]
        assert event["kind"] == row.kind
        assert event["seq"] == row.seq, "the frame names the row a reader can ask for again"
        assert event["payload"] == row.payload["payload"]
    assert any(e["kind"] == "prompt.cancelled" for e in published.values())
    assert any(e["kind"] == "part.created" for e in published.values())


async def test_a_stop_does_not_unsay_a_message_the_box_answered(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case, and the reason the watermark exists: a message the
    box HAS begun answering was sent. Stopping it ends a turn that ran — the
    line says who ended it — and claiming it was never sent would contradict
    the answer sitting above it in the same transcript."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    await _machine_published(
        chat["id"],
        SessionStatusChanged(
            event_id="ev-running",
            time=_ASKED_AT,
            session_id=chat["id"],
            status="running",
        ).model_dump(mode="json"),
    )

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = await _messages(UUID(chat["id"]))
    assert _cancelled(rows) == []
    said = next(row for row in rows if row.kind == "part.created")
    assert said.payload["payload"]["part"]["text"].startswith("Stopped by ")


async def test_a_second_stop_does_not_unsay_the_same_message_twice(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A message is not sent once per press. The second stop finds the first
    one's rows above it and has nothing left to say about the message, so the
    reader is told once."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    assert len(_cancelled(await _messages(UUID(chat["id"])))) == 1


async def test_a_message_sent_after_a_stop_is_not_born_cancelled(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The stop is about what was waiting when it was pressed. A reader who
    stops and then asks again has asked again — the new message is above the
    stop's own rows, so nothing says it was never sent."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "start over", "client_id": "s2"}
    )

    assert [event["message_id"] for event in _cancelled(await _messages(UUID(chat["id"])))] == [
        "usr:s1"
    ]


#: The note a box writes about ITSELF after a restart, exactly as the mirror
#: writes one: three rows whose event ids are built from an aside id. The box
#: steps over these when it decides what it still owes a turn, so the server
#: has to as well — the same rows drive
#: ``apps/cli/tests/cloud/test_cloud_catch_up_rows.py``.
_ASIDE = aside_note_id("restart1")


def _aside_note(chat_id: str) -> list[dict[str, Any]]:
    now = _ASKED_AT
    return [
        MessageCreated(
            event_id=f"{_ASIDE}-created",
            time=now,
            session_id=chat_id,
            message_id=_ASIDE,
            role="system",
        ).model_dump(mode="json"),
        PartCreated(
            event_id=f"{_ASIDE}-text",
            time=now,
            session_id=chat_id,
            part=TextPart(
                part_id=f"{_ASIDE}-part",
                message_id=_ASIDE,
                text="The workspace restarted while it was answering.",
                synthetic=True,
            ),
        ).model_dump(mode="json"),
        MessageCompleted(
            event_id=f"{_ASIDE}-done",
            time=now,
            session_id=chat_id,
            message_id=_ASIDE,
            finish_reason="error",
        ).model_dump(mode="json"),
    ]


async def test_a_note_the_box_wrote_about_itself_does_not_answer_the_message_below_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The hole a restart opened: a box that came back said so on the
    transcript, and that note sits above the message nobody has answered.

    It is a machine row, so a watermark counting machine rows read it as the
    answer and left the message uncancelled — while the BOX, which steps over
    its own asides, went on treating that message as one it still owed a turn.
    Stop then bound neither of them, and the restarted box re-ran a message the
    reader had ended. Both sides ask api-core the same question now.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    await _machine_published(chat["id"], *_aside_note(chat["id"]))

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    assert [event["message_id"] for event in _cancelled(await _messages(UUID(chat["id"])))] == [
        "usr:s1"
    ]


async def test_the_server_and_the_box_read_the_same_rows_the_same_way(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The SQL the watermark selects on is a transcription of api-core's rule,
    so it is worth proving it transcribes it: over a real transcript holding
    every kind of row this chat can carry, the rows Postgres calls an answer
    are exactly the ones the Python predicate does."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    chat_id = UUID(chat["id"])
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )
    await _machine_published(
        chat["id"],
        *_aside_note(chat["id"]),
        SessionStatusChanged(
            event_id="ev-running", time=_ASKED_AT, session_id=chat["id"], status="running"
        ).model_dump(mode="json"),
        _permission_ask(chat["id"]),
    )
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = await _messages(chat_id)
    assert len(rows) > 6, "the transcript carries every kind of row this test is about"
    by_python = {
        row.seq for row in rows if answers_a_waiting_message(role=row.role, event_id=row.event_id)
    }
    selected = await real_session.execute(
        select(ChatMessage.seq).where(
            ChatMessage.chat_id == chat_id, chat_service.answers_a_waiting_message_clause()
        )
    )
    assert set(selected.scalars().all()) == by_python
    assert by_python != {row.seq for row in rows}, "and it is not simply every row"


async def test_the_server_and_the_box_name_one_cancellation_the_same_row(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """Both ends report a message they dropped, and a reader must be told once.

    The box publishes its own report when a Stop empties its lane; the server
    writes one for the same message when nothing had begun answering it. They
    are the same fact, so they are the same row — whichever lands second is the
    one that finds the id already recorded, in either order.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    server_first = await _create_chat(client)
    box_first = await _create_chat(client)
    for chat in (server_first, box_first):
        await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "count", "client_id": "d1"}
        )

    boxes_own = PromptCancelled(
        event_id=prompt_cancelled_event_id("usr:d1"),
        time=_ASKED_AT,
        session_id=box_first["id"],
        message_id="usr:d1",
        client_id="d1",
        reason=PROMPT_CANCELLED_STOPPED,
    ).model_dump(mode="json")

    assert (await client.post(f"/api/v1/chats/{server_first['id']}/stop")).status_code == 202
    await _machine_published(server_first["id"], {**boxes_own, "session_id": server_first["id"]})

    await _machine_published(box_first["id"], boxes_own)
    assert (await client.post(f"/api/v1/chats/{box_first['id']}/stop")).status_code == 202

    for chat in (server_first, box_first):
        rows = await _messages(UUID(chat["id"]))
        assert len(_cancelled(rows)) == 1, (
            "one message dropped is one line, whichever end noticed first"
        )
        assert (
            len([row for row in rows if row.event_id == prompt_cancelled_event_id("usr:d1")]) == 1
        )
    # The two orders reach that by different routes, and both are the point: a
    # report the server wrote first is the id the box's own publish finds
    # already recorded, and a report the BOX wrote first is a machine row that
    # answers the message under it, so the stop finds nothing left to say.


async def test_one_stop_unsays_every_readers_waiting_message(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A chat two people are in: the stop is about the chat, not the presser.

    Both messages are queued behind the same turn and neither will run, so a
    reader whose message a colleague's Stop dropped is told the same thing the
    presser is — otherwise theirs is the one that sits unanswered forever.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    owner = await real_session.get(User, org_admin.admin_id)
    chat_row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
    )
    assert password is not None
    await share_chat(real_session, chat=chat_row, owner=owner, user=member, role=ROLE_WRITER)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "mine", "client_id": "owner-1"}
    )
    async with app_client() as other:
        await login(other, member.email, password)
        assert (
            await other.post(
                f"/api/v1/chats/{chat['id']}/messages",
                json={"text": "and mine", "client_id": "member-1"},
            )
        ).status_code == 201

        assert (await other.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    assert [event["message_id"] for event in _cancelled(await _messages(UUID(chat["id"])))] == [
        "usr:owner-1",
        "usr:member-1",
    ]


async def test_a_stop_reports_on_a_bounded_number_of_waiting_messages(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Every row a stop writes is written with this chat's row locked, so the
    reply cannot be as long as the backlog: past the cap the newest messages
    are the ones reported, and the line naming who stopped it still lands.

    The backlog is built through the recording function rather than the route,
    because the route's own throttle refuses a person who sends this fast —
    which is the reason the cap is a backstop and not the product's limit.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    over = chat_service.MAX_STOPPED_PROMPTS + 3
    async with AsyncSessionLocal() as session:
        chat_row = await session.get(WorkspaceObject, UUID(chat["id"]))
        assert chat_row is not None
        for index in range(over):
            await chat_service.append_user_message(
                session,
                chat=chat_row,
                user_id=org_admin.admin_id,
                text="go",
                client_id=f"b{index:04d}",
            )
        await session.commit()

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    rows = await _messages(UUID(chat["id"]))
    named = [event["message_id"] for event in _cancelled(rows)]
    assert len(named) == chat_service.MAX_STOPPED_PROMPTS
    assert named[-1] == f"usr:b{over - 1:04d}", "the newest is reported"
    assert named[0] == f"usr:b{over - chat_service.MAX_STOPPED_PROMPTS:04d}", "the oldest is not"
    assert any(
        row.kind == "part.created" and row.payload["payload"]["part"]["text"].startswith("Stopped")
        for row in rows
    )


async def test_the_stops_record_is_decided_by_what_the_transcript_held_when_it_landed(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Which of the two records a message gets, when a Stop and a turn race.

    One rule, read once: a message a machine had already answered for is a turn
    that ran, and the stop's line names who ended it; a message nothing had
    answered for is one that was never run, and that is what the transcript
    says. The reading is taken at the moment the stop is RECORDED, because that
    is the only moment both facts are on one ordered log — and a machine row
    that lands afterwards does not reach back and unsay it.

    The box is what makes that true: a turn the reader ended publishes nothing
    more (``apps/cli/tests/cloud/test_cloud_stop_drains_queue.py``), so the row
    below cannot happen for a message this stop reported. It is written here
    anyway, because the rule must hold even if one did.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "count them", "client_id": "s1"}
    )

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    cancelled_at = [
        row.seq for row in await _messages(UUID(chat["id"])) if row.kind == "prompt.cancelled"
    ]

    late = SessionStatusChanged(
        event_id="ev-late-running", time=_ASKED_AT, session_id=chat["id"], status="running"
    ).model_dump(mode="json")
    await _machine_published(chat["id"], late)

    rows = await _messages(UUID(chat["id"]))
    still = [row.seq for row in rows if row.kind == "prompt.cancelled"]
    assert still == cancelled_at, "the record stands; the late row does not unsay it"
    landed = next(row for row in rows if row.event_id == "ev-late-running")
    assert landed.seq > still[0], "and it is ordered after it, where a reader folds it last"
    assert answers_a_waiting_message(role=landed.role, event_id=landed.event_id), (
        "the same row, had it been there first, is exactly what would have "
        "made this message a turn that ran instead"
    )

    # And a second Stop on that transcript reports nothing: the message is
    # answered now, by the very row the first stop wrote.
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    assert [
        row.seq for row in await _messages(UUID(chat["id"])) if row.kind == "prompt.cancelled"
    ] == still


# ---------------------------------------------------------------------------
# The live frame and the row it names are one event
# ---------------------------------------------------------------------------


def _relayed_events(rows: list[EventOutbox], chat_id: str) -> list[dict[str, Any]]:
    """Every event carried by a ``user_message`` relay on this chat's channel."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.entity_id != f"doc:chat:{chat_id}":
            continue
        payload = row.payload["envelope"]["payload"]
        if payload.get("intent") != "user_message":
            continue
        out.extend(payload.get("events") or [])
    return out


@pytest.mark.usefixtures("files_on")
async def test_the_briefs_relay_carries_the_time_its_row_was_written(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """A template's brief is the first thing in the chat, and it is dated once.

    The relay is how everyone watching hears the bubble; the row is what a
    reload folds. The fold ORDERS by the time the relay carries, so a relay
    with none put the opening bubble at the epoch live and at its real time
    after a reload — the same message, in two places, from one send.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    _object_id, node_id = await _template_node(
        real_session, org_admin.admin_id, title="Weekly", brief="Pull the weekly numbers."
    )
    scratch = await _scratch_of(real_session, node_id)
    await _put_bytes(client, real_session, org_admin, scratch, "query.sql", b"SELECT 1;\n")

    chat = await _create_chat(client, source_node_id=node_id)

    row = (await _messages(UUID(chat["id"])))[0]
    frames = _relayed_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    briefs = [e for e in frames if e.get("client_id") == f"template-brief-{chat['id']}"]
    assert len(briefs) == 1, "the brief is said once"
    frame = briefs[0]
    assert frame["message_id"] == str(row.id)
    assert frame["seq"] == row.seq
    assert frame["text"] == row.payload["text"]
    assert frame["metadata"] == row.payload["metadata"]
    assert frame["at"] is not None, "a relay with no time is folded at the epoch"
    assert datetime.fromisoformat(frame["at"]) == row.created_at


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        pytest.param(
            {"interrupt_id": "q-1", "answers": [["Snowflake"], ["last week"]]},
            "question.answered",
            id="answered",
        ),
        pytest.param(
            {"interrupt_id": "q-1", "reject": True, "reason": "ask me later"},
            "question.rejected",
            id="rejected",
        ),
    ],
)
async def test_a_questions_resolution_reaches_the_channel_as_the_row_it_wrote(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, Any], kind: str
) -> None:
    """The permission half of ``/answer`` was pinned; the question half was not.

    A question is answered with labels, and the relay that carries them is
    addressed to the machine — a transcript reader's relay fold reads an
    option id and skips a frame without one outright. So if the resolution did
    not go out as its own event, the answer a person typed would reach every
    other reader only on a reload.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    await _machine_published(chat["id"], _question_ask(chat["id"], "q-1"))
    assert (
        await client.post(f"/api/v1/chats/{chat['id']}/answer", json=body)
    ).status_code == 202, kind

    row = _resolutions(await _messages(UUID(chat["id"])), "q-1")[0]
    assert row.kind == kind
    live = _appended_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    frames = [e for e in live if e.get("kind") == kind]
    assert len(frames) == 1, "one resolution, on the channel, once"
    assert frames[0]["event_id"] == row.event_id
    assert frames[0]["seq"] == row.seq
    assert frames[0]["payload"] == row.payload["payload"]


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            {"event_type": "part.started", "part_id": "p1", "part_type": "text"},
            id="part.started",
        ),
        pytest.param(
            {"event_type": "part.completed", "part_id": "p1", "part_type": "text", "text": "hi"},
            id="part.completed",
        ),
        pytest.param(
            {"event_type": "tool.call", "tool_call_id": "t1", "tool_name": "bash"},
            id="tool.call",
        ),
        pytest.param(
            {"event_type": "tool.call_update", "tool_call_id": "t1", "status": "completed"},
            id="tool.call_update",
        ),
        pytest.param(
            {"event_type": "session.status_changed", "status": "idle"},
            id="session.status_changed",
        ),
    ],
)
async def test_every_machine_kind_is_rebroadcast_as_the_row_it_became(
    client: AsyncClient, org_admin: OrgWithAdmin, event: dict[str, Any]
) -> None:
    """The append door treats every kind alike, and this is what pins it.

    ``message.created`` and ``message.completed`` were covered; the parts, the
    tool calls and the session's own status were not, and those are most of a
    turn. One door writes them all, so a kind whose frame and row drifted apart
    would be a card that renders one way live and another after a reload.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await _chat_doc(chat["id"], org_admin.org_id)
    published = {
        "event_id": f"ev-{event['event_type']}",
        "schema_version": "1.0.0",
        "time": _ASKED_AT.isoformat(),
        "session_id": chat["id"],
        **event,
    }
    await _box_appended(chat["id"], org_admin.org_id, published)

    rows = [r for r in await _messages(UUID(chat["id"])) if r.kind == event["event_type"]]
    assert len(rows) == 1, "the machine's event became exactly one row"
    live = _appended_events(await _events(org_admin.org_id, EventType.DOC_OP), chat["id"])
    frames = [e for e in live if e.get("kind") == event["event_type"]]
    assert len(frames) == 1
    assert frames[0]["event_id"] == rows[0].event_id
    assert frames[0]["seq"] == rows[0].seq, "the frame names the row the reader can page back to"
    assert frames[0]["payload"] == rows[0].payload["payload"]


# ---------------------------------------------------------------------------
# what the listing says a box should take first
# ---------------------------------------------------------------------------


async def _machine_row(chat_id: str, seq_event: str, *, role: str = "assistant") -> None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        last = (
            await session.execute(
                select(ChatMessage.seq)
                .where(ChatMessage.chat_id == chat.id)
                .order_by(ChatMessage.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        session.add(
            ChatMessage(
                chat_id=chat.id,
                org_team_id=chat.org_team_id,
                seq=(last or 0) + 1,
                role=role,
                kind="message.completed",
                event_id=seq_event,
                payload={},
            )
        )
        await session.commit()


async def _working_doc(chat_id: str) -> None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        session.add(
            RealtimeDoc(
                org_id=chat.org_team_id,
                doc_type="chat",
                doc_id=chat_id,
                turn_state="working",
            )
        )
        await session.commit()


async def test_the_listing_says_which_chats_owe_a_turn(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The box takes these first; each case is a way a careless rule would
    either make a waiting person wait behind hundreds of idle chats, or put an
    answered chat ahead of them."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    unanswered = await _create_chat(client, title="unanswered")
    answered = await _create_chat(client, title="answered")
    aside_only = await _create_chat(client, title="aside-only")
    interrupted = await _create_chat(client, title="interrupted")
    await _create_chat(client, title="silent")
    for chat in (unanswered, answered, aside_only):
        posted = await client.post(
            f"/api/v1/chats/{chat['id']}/messages",
            json={"text": "how many rows?", "client_id": f"c-{chat['title']}"},
        )
        assert posted.status_code == 201, posted.text
    await _machine_row(answered["id"], "oc-done")
    # A note the box wrote about itself answers nothing.
    await _machine_row(aside_only["id"], aside_note_id("restarting"))
    await _working_doc(interrupted["id"])

    listed = await client.get("/api/v1/chats", params={"limit": 50})
    assert listed.status_code == 200, listed.text
    by_title = {item["title"]: item for item in listed.json()["items"]}
    assert {title: by_title[title]["pending_turn"] for title in by_title} == {
        "unanswered": True,
        "answered": False,
        "aside-only": True,
        "interrupted": True,
        "silent": False,
    }
    assert by_title["unanswered"]["last_activity_at"] is not None
    assert by_title["silent"]["last_activity_at"] is None
    for title, owed in (("unanswered", True), ("answered", False), ("interrupted", True)):
        single = await client.get(f"/api/v1/chats/{by_title[title]['id']}")
        assert single.status_code == 200, single.text
        assert single.json()["pending_turn"] is owed, (
            f"the single read of {title!r} must say what the listing says"
        )


async def _doc_turn_state(chat_id: str, state: str) -> None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        doc = await session.get(RealtimeDoc, (chat.org_team_id, "chat", chat_id))
        if doc is None:
            session.add(
                RealtimeDoc(
                    org_id=chat.org_team_id, doc_type="chat", doc_id=chat_id, turn_state=state
                )
            )
        else:
            doc.turn_state = state
        await session.commit()


async def test_a_new_chats_first_message_owes_a_turn_until_the_box_answers_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The walkthrough's chat: created, its first message sent, no box has
    touched it. Every read a box or a browser makes — the listing, and the
    single read a box re-reads a chat with on a frame — says a turn is owed.
    It stays owed while the box's document says the turn is working (a box
    that takes the chat cold must restart it), and stops once the answer is
    on the record and the document is idle."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="brand new")

    async def owed() -> tuple[bool, bool]:
        single = await client.get(f"/api/v1/chats/{chat['id']}")
        assert single.status_code == 200, single.text
        listed = await client.get("/api/v1/chats", params={"limit": 50})
        row = next(item for item in listed.json()["items"] if item["id"] == chat["id"])
        return single.json()["pending_turn"], row["pending_turn"]

    assert await owed() == (False, False), "a chat nobody spoke in owes nothing"
    posted = await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "select count(*) from orders", "client_id": "first"},
    )
    assert posted.status_code == 201, posted.text
    assert await owed() == (True, True), "the very first message owes a turn"

    await _doc_turn_state(chat["id"], "working")
    await _machine_row(chat["id"], "oc-started")
    assert await owed() == (True, True), "a turn the document says is working is still owed"

    await _machine_row(chat["id"], "oc-answer")
    await _doc_turn_state(chat["id"], "idle")
    assert await owed() == (False, False), "an answered message owes nothing"

    again = await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        json={"text": "and by month?", "client_id": "second"},
    )
    assert again.status_code == 201, again.text
    assert await owed() == (True, True), "a follow-up above the answer owes a turn again"


# ---------------------------------------------------------------------------
# The chat's gateway token: the publisher's alone, never a session
# ---------------------------------------------------------------------------


async def test_the_bound_machine_mints_the_chats_gateway_token_and_nobody_else_does(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The box asks for a member's chat's gateway credential and gets a token
    bound to that chat, minted under the box's own session (its jti) and
    billed to that session's person, that the API refuses as a session. A person — the chat's owner
    with a cookie, the operator with the machine's id typed onto their own
    session — is refused; the decision rows say who and why."""
    from alkera_core.auth import (
        GATEWAY_TOKEN_TTL_SECONDS,
        decode_gateway_token,
        decode_session_token,
    )
    from alkera_core.authz import agent_headers

    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-gw-1")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat = await _create_chat(theirs, title="The member's chat")
        assert chat["machine_id"] == machine_id
        owner_asks = await theirs.post(f"/api/v1/chats/{chat['id']}/gateway-token")
    assert owner_asks.status_code == 403, "the owner is a person, not the publisher"

    minted = await client.post(f"/api/v1/chats/{chat['id']}/gateway-token", headers=box)
    assert minted.status_code == 200, minted.text
    body = minted.json()
    box_session = decode_session_token(box["Authorization"].removeprefix("Bearer "))
    claims = decode_gateway_token(body["token"])
    assert claims.chat_id == UUID(chat["id"])
    # A person is at this box, so the token is their session's own: it bills
    # the operator in the chat's org, never the member whose chat it serves.
    assert claims.user_id == org_admin.admin_id and claims.org_id == org_admin.org_id
    assert claims.parent_user_id is None
    assert claims.parent_jti == box_session.jti
    assert claims.parent_issued_at == box_session.issued_at
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires.timestamp() == claims.expires_at
    assert 0 < claims.expires_at - claims.issued_at <= GATEWAY_TOKEN_TTL_SECONDS
    assert claims.expires_at <= box_session.expires_at

    # Not a session anywhere on this API: the token itself, as Bearer, with or
    # without the box's agent assertion.
    for headers in (
        {"Authorization": f"Bearer {body['token']}"},
        {"Authorization": f"Bearer {body['token']}", **agent_headers(machine_id)},
    ):
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
        assert (await client.get(f"/api/v1/chats/{chat['id']}", headers=headers)).status_code == 401
        again = await client.post(f"/api/v1/chats/{chat['id']}/gateway-token", headers=headers)
        assert again.status_code == 401, "it cannot mint its own successor"

    # The operator's browser typing the machine's id onto its own session is a
    # person: the assertion is unverified, so the publisher gate refuses it.
    async with app_client() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        typed = await browser.post(
            f"/api/v1/chats/{chat['id']}/gateway-token", headers=agent_headers(machine_id)
        )
    assert typed.status_code == 403

    rows = await _decision_rows(org_admin.org_id, chat["id"])
    writes = [row for row in rows if row[0] == "write"]
    assert [row for row in writes if row[1] == "allow"] == [
        ("write", "allow", "bound_machine_reports")
    ], "one mint, one allow — the box's"
    assert [row[2] for row in writes if row[1] == "deny"] == [
        "publisher_machine_required",
        "publisher_machine_required",
    ], "the owner's ask and the operator's typed assertion, refused on record"


async def test_another_orgs_chat_is_not_found_to_the_box(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="pod-gw-2")
    async with app_client() as other:
        _org_id, other = await _other_org_member(other)
        chat = await _create_chat(other, title="Theirs")
    minted = await client.post(f"/api/v1/chats/{chat['id']}/gateway-token", headers=box)
    assert minted.status_code == 404
