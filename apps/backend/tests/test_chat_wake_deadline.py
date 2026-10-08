"""A wake no machine takes ends, and a waiting message is not run behind the
person's back.

A chat asked to wake on a box that answers but never takes it read "Waking"
for as long as anyone looked: nothing cleared the wake, the message sat
unanswered, and every later sleep re-took the chat for nobody. Past
``chat_wake_deadline_seconds`` on a machine that answers, the compute sweep
puts the chat back to sleep; a waiting message is marked as never run, with a
line saying so, and the chat reads stopped until something is sent again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ChatMessage, WorkspaceObject
from alkera_core.objects import end_untaken_wakes
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login
from tests.test_chat_machine_binding_seam import _heartbeat
from tests.test_chat_status_read import _status
from tests.test_chat_stop_unserved import _chat_on_a_box, _publisher_state

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

START = datetime(2026, 10, 6, 20, 0, 0, tzinfo=UTC)
NOT_TAKEN = "No machine picked this up. Send it again to retry."


@pytest.fixture(autouse=True)
def _bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_turn_start_seconds", 180)
    monkeypatch.setattr(settings, "chat_wake_deadline_seconds", 300)


async def _spec(chat_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        return dict(chat.spec)


async def _ask_to_wake(chat_id: str) -> None:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        chat.spec = {**chat.spec, "wake_requested_at": datetime.now(UTC).isoformat()}
        await session.commit()


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


def _note(row: ChatMessage) -> str | None:
    text = ((row.payload.get("payload") or {}).get("part") or {}).get("text")
    return text if isinstance(text, str) else None


async def _sweep() -> None:
    async with AsyncSessionLocal() as session:
        await end_untaken_wakes(session)


async def _asleep_chat_asked_to_wake(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    pod: str,
    *,
    message: bool,
) -> tuple[str, str]:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod=pod)
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    await _publisher_state(org_admin, machine_id, chat["id"], "asleep")
    if message:
        sent = await client.post(
            f"/api/v1/chats/{chat['id']}/messages",
            json={"text": "are you there", "client_id": f"{pod}-1"},
        )
        assert sent.status_code == 201, sent.text
    await _ask_to_wake(chat["id"])
    return chat["id"], machine_id


async def test_a_message_no_machine_takes_is_marked_not_run_and_the_chat_says_so(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    with freeze_time(START, real_asyncio=True) as clock:
        chat_id, machine_id = await _asleep_chat_asked_to_wake(
            client, real_session, org_admin, "pod-untaken", message=True
        )
        before = len(await _rows(chat_id))

        clock.tick(timedelta(seconds=299))
        await _heartbeat(org_admin, machine_id)
        await _sweep()
        assert "wake_requested_at" in await _spec(chat_id), "inside the bound the wake stands"

        clock.tick(timedelta(seconds=1))
        await _heartbeat(org_admin, machine_id)
        await _sweep()

        spec = await _spec(chat_id)
        assert "wake_requested_at" not in spec
        assert spec["turn_end_reason"] == "not_taken"
        rows = (await _rows(chat_id))[before:]
        cancelled = [row for row in rows if row.kind == "prompt.cancelled"]
        assert [row.payload["payload"]["reason"] for row in cancelled] == ["not_taken"]
        assert [_note(row) for row in rows if row.role == "system" and _note(row)] == [NOT_TAKEN]

        await login(client, org_admin.admin_email, org_admin.admin_password)
        status = await _status(client, chat_id)
        assert (status["state"], status["reason_code"], status["sentence"]) == (
            "stopped",
            "not_taken",
            NOT_TAKEN,
        )
        read = await client.get(f"/api/v1/chats/{chat_id}")
        assert read.json()["pending_turn"] is False, "no box runs the message behind the person"

        # A second pass finds nothing left to end.
        await _sweep()
        assert len(await _rows(chat_id)) == before + len(rows)


async def test_a_wake_with_nothing_owed_just_goes_back_to_sleep(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    with freeze_time(START, real_asyncio=True) as clock:
        chat_id, machine_id = await _asleep_chat_asked_to_wake(
            client, real_session, org_admin, "pod-viewed", message=False
        )
        before = len(await _rows(chat_id))

        clock.tick(timedelta(seconds=301))
        await _heartbeat(org_admin, machine_id)
        await _sweep()

        spec = await _spec(chat_id)
        assert "wake_requested_at" not in spec
        assert spec.get("turn_end_reason") is None
        assert len(await _rows(chat_id)) == before
        await login(client, org_admin.admin_email, org_admin.admin_password)
        # Nothing ever ran and nothing is owed: there is nothing to say, rather
        # than a chat waking for nobody.
        assert await _status(client, chat_id) is None


async def test_a_machine_that_is_not_answering_keeps_its_wake(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The negative case: a box that stopped beating is the machine's own wait,
    which its own bounds end. Nothing is marked not run while it might still
    come back and take the message."""
    with freeze_time(START, real_asyncio=True) as clock:
        chat_id, _machine = await _asleep_chat_asked_to_wake(
            client, real_session, org_admin, "pod-silent", message=True
        )
        clock.tick(timedelta(seconds=settings.chat_wake_deadline_seconds * 4))
        await _sweep()
        assert "wake_requested_at" in await _spec(chat_id)


async def test_a_box_that_takes_the_chat_in_time_keeps_it(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    with freeze_time(START, real_asyncio=True) as clock:
        chat_id, machine_id = await _asleep_chat_asked_to_wake(
            client, real_session, org_admin, "pod-taken", message=True
        )
        clock.tick(timedelta(seconds=120))
        await _heartbeat(org_admin, machine_id)
        await _publisher_state(org_admin, machine_id, chat_id, "publishing")

        clock.tick(timedelta(seconds=600))
        await _heartbeat(org_admin, machine_id)
        await _sweep()
        assert (await _spec(chat_id)).get("turn_end_reason") is None


NO_SLOT = "No slot freed up on its machine. Send it again to retry."


async def test_a_message_queued_for_a_slot_that_never_frees_is_marked_not_run_and_says_so(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: Any
) -> None:
    """A box that has the message but no free slot says ``waiting``. That is
    the box's own wait, so the wake's bound does not end it; past
    ``chat_queue_deadline_seconds`` its own bound does."""
    monkeypatch.setattr(settings, "chat_queue_deadline_seconds", 1800)
    with freeze_time(START, real_asyncio=True) as clock:
        chat_id, machine_id = await _asleep_chat_asked_to_wake(
            client, real_session, org_admin, "pod-queued", message=True
        )
        await _publisher_state(org_admin, machine_id, chat_id, "waiting")
        before = len(await _rows(chat_id))

        clock.tick(timedelta(seconds=1799))
        await _heartbeat(org_admin, machine_id)
        await _sweep()
        assert "slot_wait_at" in await _spec(chat_id), "past the wake's bound, inside the queue's"
        assert len(await _rows(chat_id)) == before

        clock.tick(timedelta(seconds=1))
        await _heartbeat(org_admin, machine_id)
        await _sweep()

        spec = await _spec(chat_id)
        assert "slot_wait_at" not in spec and "wake_requested_at" not in spec
        assert spec["turn_end_reason"] == "no_slot"
        rows = (await _rows(chat_id))[before:]
        cancelled = [row for row in rows if row.kind == "prompt.cancelled"]
        assert [row.payload["payload"]["reason"] for row in cancelled] == ["no_slot"]
        assert [_note(row) for row in rows if row.role == "system" and _note(row)] == [NO_SLOT]

        await login(client, org_admin.admin_email, org_admin.admin_password)
        status = await _status(client, chat_id)
        assert (status["state"], status["reason_code"], status["sentence"]) == (
            "stopped",
            "no_slot",
            NO_SLOT,
        )
        read = await client.get(f"/api/v1/chats/{chat_id}")
        assert read.json()["pending_turn"] is False
