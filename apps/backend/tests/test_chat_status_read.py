"""A chat's read says what its worker reported, not only where it was placed.

A machine row with a fresh heartbeat proves the box's root process reaches the
API and nothing about the chat: a chat read ``ready`` for thirteen minutes
while no turn ran, and ``Working…`` long after its machine was stopped for
credit. Every read of a chat now carries one status written on the server,
decided on the stamp a running turn's worker renews, on how long a sent
message has waited, and on why the last turn ended.

Driven through the real routes on real Postgres, with the clock moved across
each bound while the box keeps beating: the heartbeat stays fresh the whole
time, so a status that changes is one that read the worker's own evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.objects import chat_end
from alkera_core.schemas.realtime import SERVER_PEER_ID
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login
from tests.test_chat_machine_binding_seam import _heartbeat
from tests.test_chat_stop_unserved import (
    _chat_on_a_box,
    _doc,
    _doc_says_working,
    _publisher_state,
    _turn_stamps,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

START = datetime(2026, 10, 6, 19, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_turn_silence_seconds", 120)
    monkeypatch.setattr(settings, "chat_turn_start_seconds", 180)


async def _status(client: AsyncClient, chat_id: str) -> dict[str, Any]:
    """The chat's status as the single read and the listing give it; the two
    must be the same fact."""
    single = await client.get(f"/api/v1/chats/{chat_id}")
    assert single.status_code == 200, single.text
    listed = await client.get("/api/v1/chats", params={"limit": 50})
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["items"] if item["id"] == chat_id)
    assert row["status"] == single.json()["status"], "the list and the chat agree"
    status: dict[str, Any] = single.json()["status"]
    return status


async def _turn(chat_id: str) -> str | None:
    return (await _doc(chat_id)).turn_state


def _words(status: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        status["state"],
        status["reason_code"],
        status["label"],
        status["tone"],
        status["sentence"],
    )


async def test_a_turn_whose_worker_stops_reporting_reads_stalled_while_the_box_still_beats(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    with freeze_time(START, real_asyncio=True) as clock:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-stall")
        await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
        sent = await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "go on", "client_id": "st-1"}
        )
        assert sent.status_code == 201, sent.text
        await _doc_says_working(chat["id"])
        assert _words(await _status(client, chat["id"])) == (
            "working",
            "",
            "Working",
            "info",
            "The agent is working.",
        )

        # One second short of the bound, the box still beating: still working.
        clock.tick(timedelta(seconds=119))
        await _heartbeat(org_admin, machine_id)
        assert (await _status(client, chat["id"]))["state"] == "working"

        # At the bound. The machine's heartbeat is as fresh as it can be, and
        # the chat's machine still reads ready: only the turn's stamp aged.
        clock.tick(timedelta(seconds=1))
        await _heartbeat(org_admin, machine_id)
        read = await client.get(f"/api/v1/chats/{chat['id']}")
        assert read.json()["machine_status"] == "ready"
        stalled = await _status(client, chat["id"])
        assert _words(stalled) == (
            "stalled",
            "turn_silent",
            "Stalled",
            "warning",
            "The agent has stopped reporting progress.",
        )
        assert datetime.fromisoformat(stalled["since"]) == START, "since the stamp went quiet"

        # The worker reports again: the turn is running after all.
        await _doc_says_working(chat["id"])
        assert (await _status(client, chat["id"]))["state"] == "working"


async def test_a_message_a_ready_machine_never_starts_reads_stalled_not_ready(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    with freeze_time(START, real_asyncio=True) as clock:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-idle")
        await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
        assert _words(await _status(client, chat["id"])) == (
            "awake",
            "",
            "Awake",
            "success",
            "Ready for a message.",
        )
        sent = await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "hello?", "client_id": "st-2"}
        )
        assert sent.status_code == 201, sent.text
        assert (await _status(client, chat["id"]))["state"] == "waking"

        clock.tick(timedelta(seconds=179))
        await _heartbeat(org_admin, machine_id)
        assert (await _status(client, chat["id"]))["state"] == "waking"

        # Thirteen minutes on, the supervisor beating all the while.
        clock.tick(timedelta(minutes=13))
        await _heartbeat(org_admin, machine_id)
        stalled = await _status(client, chat["id"])
        assert _words(stalled) == (
            "stalled",
            "turn_not_started",
            "Stalled",
            "warning",
            "demo-box has the message and hasn't started on it.",
        )
        assert datetime.fromisoformat(stalled["since"]) == START, "since the message was sent"


async def test_a_turn_ended_for_credit_reads_stopped_for_credit_until_a_message_is_sent(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    with freeze_time(START, real_asyncio=True) as clock:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-drain")
        await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
        sent = await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "go on", "client_id": "st-3"}
        )
        assert sent.status_code == 201, sent.text
        await _doc_says_working(chat["id"])

        clock.tick(timedelta(seconds=30))
        async with AsyncSessionLocal() as session:
            ended = await chat_end.end_chat(
                session,
                UUID(chat["id"]),
                chat_end.ChatEndReason.CREDITS_EXHAUSTED,
                actor=None,
            )
            await session.commit()
        assert ended.changed

        stopped = await _status(client, chat["id"])
        assert _words(stopped) == (
            "stopped",
            "credits_exhausted",
            "Stopped",
            "warning",
            "Stopped because the organization ran out of credit.",
        )
        assert datetime.fromisoformat(stopped["since"]) == START + timedelta(seconds=30)

        # It says so for as long as nothing is sent, however long that is.
        clock.tick(timedelta(hours=6))
        await _heartbeat(org_admin, machine_id)
        # Six hours outlive the session; the reader signs in again.
        await login(client, org_admin.admin_email, org_admin.admin_password)
        assert (await _status(client, chat["id"]))["reason_code"] == "credits_exhausted"

        # The next message is a new turn owed, not the old one's ending.
        again = await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "again", "client_id": "st-4"}
        )
        assert again.status_code == 201, again.text
        assert (await _status(client, chat["id"]))["state"] == "waking"


async def test_an_ending_with_no_turn_running_does_not_read_stopped(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The negative case: a chat that was idle when its machine was stopped
    lost nothing, so it reads asleep and names no cause."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-quiet")
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "hi", "client_id": "st-5"}
    )
    assert sent.status_code == 201, sent.text
    stopped = await client.post(f"/api/v1/chats/{chat['id']}/stop")
    assert stopped.status_code == 202, stopped.text
    async with AsyncSessionLocal() as session:
        await chat_end.end_chat(
            session, UUID(chat["id"]), chat_end.ChatEndReason.MACHINE_STOPPED, actor=None
        )
        await session.commit()
    status = await _status(client, chat["id"])
    assert (status["state"], status["reason_code"]) == ("asleep", "")


async def test_a_turn_whose_worker_never_reports_again_is_ended_by_the_sweep(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Nothing ended a turn whose worker died under a supervisor that kept
    beating: no ending fires on a ready machine. Past the abandon bound the
    compute sweep ends it, the chat says why, and a box that stamps again
    before the bound keeps its turn."""
    from alkera_core.objects import end_abandoned_turns

    with freeze_time(START, real_asyncio=True) as clock:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-dead")
        await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
        sent = await client.post(
            f"/api/v1/chats/{chat['id']}/messages", json={"text": "go", "client_id": "st-6"}
        )
        assert sent.status_code == 201, sent.text
        await _doc_says_working(chat["id"])

        clock.tick(timedelta(seconds=settings.chat_turn_abandon_seconds - 1))
        await _heartbeat(org_admin, machine_id)
        async with AsyncSessionLocal() as session:
            await end_abandoned_turns(session)
        assert (await _status(client, chat["id"]))["state"] == "stalled"
        assert (await _turn(chat["id"])) == "working"

        clock.tick(timedelta(seconds=1))
        await _heartbeat(org_admin, machine_id)
        async with AsyncSessionLocal() as session:
            await end_abandoned_turns(session)
        assert (await _turn(chat["id"])) == "idle"
        ended = await _status(client, chat["id"])
        assert (ended["state"], ended["reason_code"], ended["sentence"]) == (
            "stopped",
            "turn_lost",
            "Stopped because the agent stopped reporting progress.",
        )
        frames = await _turn_stamps(org_admin.org_id, chat["id"])
        assert [frame["state"] for frame in frames if frame["peer"] == SERVER_PEER_ID] == ["idle"]
        async with AsyncSessionLocal() as session:
            await end_abandoned_turns(session)
        frames = await _turn_stamps(org_admin.org_id, chat["id"])
        assert [frame["state"] for frame in frames if frame["peer"] == SERVER_PEER_ID] == [
            "idle"
        ], "an ended turn is not ended twice"


async def test_a_turn_stamped_again_is_not_ended(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    from alkera_core.objects import end_abandoned_turns

    with freeze_time(START, real_asyncio=True) as clock:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-alive")
        await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
        await _doc_says_working(chat["id"])
        for _ in range(4):
            clock.tick(timedelta(seconds=settings.chat_turn_abandon_seconds // 2))
            await _heartbeat(org_admin, machine_id)
            await _doc_says_working(chat["id"])
            async with AsyncSessionLocal() as session:
                await end_abandoned_turns(session)
            assert await _turn(chat["id"]) == "working"
        assert (await _status(client, chat["id"]))["state"] == "working"


async def test_a_box_that_refuses_a_chat_mid_turn_has_the_turn_ended_for_every_reader(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A box that refuses a chat will not finish its turn. The document kept
    saying working, and only a browser that decided for itself drew it idle;
    every other reader, and every listing, owed the turn for good."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-refuse")
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "go", "client_id": "st-7"}
    )
    assert sent.status_code == 201, sent.text
    await _doc_says_working(chat["id"])

    await _publisher_state(org_admin, machine_id, chat["id"], "refused", kind="not_allowed")

    assert await _turn(chat["id"]) == "idle"
    frames = await _turn_stamps(org_admin.org_id, chat["id"])
    assert [frame["state"] for frame in frames if frame["peer"] == SERVER_PEER_ID] == ["idle"]
    # While the box refuses, that is what the chat says; the ended turn is
    # recorded for when it can run again.
    status = await _status(client, chat["id"])
    assert (status["state"], status["reason_code"]) == ("unavailable", "refused")
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    status = await _status(client, chat["id"])
    assert (status["state"], status["reason_code"], status["sentence"]) == (
        "stopped",
        "refused",
        "Stopped because the machine couldn't run this chat.",
    )


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("workspace_elsewhere", id="a-wait"),
        pytest.param("files_unreachable", id="files-not-reachable-yet"),
        pytest.param(None, id="no-kind-from-an-older-box"),
        pytest.param("from_a_newer_box", id="a-kind-this-server-does-not-know"),
    ],
)
async def test_a_refusal_the_box_waits_out_leaves_the_running_turn_alone(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin, kind: str | None
) -> None:
    """A box that says it is waiting (the workspace is still open on the
    machine before it, the files are not reachable yet) tries again on its own
    and may still finish the turn, so the server does not end it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod=f"pod-wait-{kind}")
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "go", "client_id": "st-8"}
    )
    assert sent.status_code == 201, sent.text
    await _doc_says_working(chat["id"])

    await _publisher_state(org_admin, machine_id, chat["id"], "refused", kind=kind)

    assert await _turn(chat["id"]) == "working"
    frames = await _turn_stamps(org_admin.org_id, chat["id"])
    assert [frame["state"] for frame in frames if frame["peer"] == SERVER_PEER_ID] == []


async def test_a_new_chats_first_message_reads_starting_and_a_slept_chats_reads_waking(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat no box has ever held is being started: there is nothing to wake.
    Once a box has held it and put it to sleep, the next message wakes it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-new")
    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "hello", "client_id": "st-new"}
    )
    assert sent.status_code == 201, sent.text
    first = await _status(client, chat["id"])
    assert (first["state"], first["label"], first["sentence"]) == (
        "starting",
        "Starting",
        "Starting the chat.",
    )

    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    await _publisher_state(org_admin, machine_id, chat["id"], "asleep")
    again = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "again", "client_id": "st-new-2"}
    )
    assert again.status_code == 201, again.text
    assert (await _status(client, chat["id"]))["state"] == "waking"
