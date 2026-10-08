"""A turn the server ends says why on the transcript.

When a machine stopped for credit, left the plane or stopped reporting under a
running turn, the server ended the turn by stamping the document idle and
nothing else: a reader saw an answer stop mid-sentence with nothing after it,
and a reader who came back a day later had no way to learn why. The ending now
leaves one system line naming the reason, in the rows, in the live window
every reader opens on, and on the frame viewers fold, all at once. The line is
an aside, so a message still waiting above it stays owed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import ChatMessage
from alkera_core.objects import chat_end
from alkera_core.schemas.objects.transcript import answers_a_waiting_message
from alkera_core.schemas.realtime import SERVER_PEER_ID
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login
from tests.test_chat_stop_unserved import (
    _chat_on_a_box,
    _doc,
    _doc_says_working,
    _publisher_state,
)
from tests.test_chats_api import _events

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

CREDIT = "Stopped because the organization ran out of credit."


def _text(entry: dict[str, Any]) -> str | None:
    part = (entry.get("payload") or {}).get("part") or {}
    text = part.get("text")
    return text if isinstance(text, str) else None


async def _rows(chat_id: str) -> list[ChatMessage]:
    async with AsyncSessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == UUID(chat_id))
                    .order_by(ChatMessage.seq)
                )
            )
            .scalars()
            .all()
        )


async def _server_appends(org_id: UUID, chat_id: str) -> list[dict[str, Any]]:
    """Every ``append`` the server put on the chat's channel, as its events."""
    out: list[dict[str, Any]] = []
    for row in await _events(org_id, EventType.DOC_OP):
        if row.entity_id != f"doc:chat:{chat_id}":
            continue
        envelope = row.payload["envelope"]
        if envelope["peer_id"] != SERVER_PEER_ID or envelope["payload"].get("intent") != "append":
            continue
        out.extend(envelope["payload"]["events"])
    return out


async def _working_chat(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin, pod: str
) -> tuple[str, str]:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod=pod)
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "go on", "client_id": f"{pod}-1"}
    )
    assert sent.status_code == 201, sent.text
    return chat["id"], machine_id


async def _end(chat_id: str, reason: chat_end.ChatEndReason) -> None:
    async with AsyncSessionLocal() as session:
        await chat_end.end_chat(session, UUID(chat_id), reason, actor=None)
        await session.commit()


async def test_a_turn_ended_for_credit_leaves_the_reason_on_the_transcript_for_every_reader(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    chat_id, _machine = await _working_chat(client, real_session, org_admin, "pod-note")
    await _doc_says_working(chat_id)

    await _end(chat_id, chat_end.ChatEndReason.CREDITS_EXHAUSTED)

    rows = await _rows(chat_id)
    notes = [row for row in rows if row.role == "system" and _text(row.payload) == CREDIT]
    assert len(notes) == 1, [(row.role, row.kind, _text(row.payload)) for row in rows]
    note = notes[0]
    assert note.seq == rows[-2].seq, "the line follows everything said before the turn ended"
    assert not answers_a_waiting_message(role=note.role, event_id=note.event_id), (
        "the line reports on the machine and leaves a waiting message owed"
    )

    # The window a reader opens on holds it, stamped with the row's sequence.
    window = (await _doc(chat_id)).state["events"]
    in_window = [event for event in window if event.get("event_id") == note.event_id]
    assert [event.get("seq") for event in in_window] == [note.seq]

    # A viewer who was watching folded it from the frame, with the same sequence.
    framed = [
        event
        for event in await _server_appends(org_admin.org_id, chat_id)
        if event.get("event_id") == note.event_id
    ]
    assert [(event.get("seq"), _text(event)) for event in framed] == [(note.seq, CREDIT)]


async def test_an_ending_with_no_turn_running_leaves_no_line(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The negative case: nothing was cut off, so there is nothing to explain."""
    chat_id, _machine = await _working_chat(client, real_session, org_admin, "pod-idle-note")
    before = len(await _rows(chat_id))

    await _end(chat_id, chat_end.ChatEndReason.CREDITS_EXHAUSTED)

    rows = await _rows(chat_id)
    assert len(rows) == before
    assert await _server_appends(org_admin.org_id, chat_id) == []


async def test_a_second_ending_of_the_same_turn_adds_no_second_line(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    chat_id, _machine = await _working_chat(client, real_session, org_admin, "pod-twice")
    await _doc_says_working(chat_id)

    await _end(chat_id, chat_end.ChatEndReason.MACHINE_STOPPED)
    await _end(chat_id, chat_end.ChatEndReason.CREDITS_EXHAUSTED)

    texts = [
        _text(row.payload)
        for row in await _rows(chat_id)
        if row.role == "system" and _text(row.payload) is not None
    ]
    assert texts == ["Stopped because the machine was stopped."]
