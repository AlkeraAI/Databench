"""Opening a chat wakes it: ``POST /api/v1/chats/{id}/wake``.

Only someone who may send in the chat wakes it, once per chat per interval
however often the page asks, and a start the org cannot pay for is refused
with the compute refusal and leaves everything asleep. No message is recorded,
so a wake nobody takes ends with nothing marked as never run. A decision is
filed for a wake that is tried and for a refused caller, never for an open
that changes nothing. A workspace opened with no chat wakes its most recently
active chat the caller may send in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import ComputeAllocation, EventOutbox, WorkspaceObject
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

STATE = "/api/v1/chats/{chat_id}/publisher-state"


async def _registered_machine(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, *, pod: str
) -> tuple[str, dict[str, str]]:
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
    box = {"Authorization": f"Bearer {token}", **agent_headers(machine_id)}
    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    assert beat.status_code in (200, 204), beat.text
    return machine_id, box


async def _spec(chat_id: str | UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(str(chat_id)))
        assert row is not None
        return dict(row.spec)


async def _decisions(org_id: UUID, chat_id: str) -> list[tuple[str, str]]:
    """Every decision on file about the chat, oldest first: (action, effect)."""
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
    return [(r.payload["action"], r.payload["effect"]) for r in rows.scalars()]


async def _slept_chat(
    client: AsyncClient, theirs: AsyncClient, box: dict[str, str], title: str
) -> str:
    """A chat its box opened and then put to sleep."""
    created = await theirs.post("/api/v1/chats", json={"title": title})
    assert created.status_code == 201, created.text
    chat_id = str(created.json()["id"])
    for report in ("publishing", "asleep"):
        moved = await client.put(STATE.format(chat_id=chat_id), json={"state": report}, headers=box)
        assert moved.status_code == 200, moved.text
    assert (await _spec(chat_id)).get("wake_requested_at") is None
    return chat_id


async def test_a_writer_opening_a_slept_chat_wakes_it_once_per_interval(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The first open stamps the wake; an open moments later asks for nothing,
    even after the box has taken the chat and slept it again, because the
    open's wake still stands for the interval."""
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-1")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = await _slept_chat(client, theirs, box, "Notebook")

        before = await _decisions(org_admin.org_id, chat_id)
        first = await theirs.post(f"/api/v1/chats/{chat_id}/wake")
        assert first.status_code == 200, first.text
        assert first.json() == {"outcome": "waking", "machine_unavailable": None}
        tried = await _decisions(org_admin.org_id, chat_id)
        assert tried[len(before) :] == [("send", "allow")], "the wake that was tried is on file"
        assert (await _spec(chat_id)).get("wake_requested_at") is not None
        read = (await theirs.get(f"/api/v1/chats/{chat_id}")).json()
        assert read["session_state"] == "waking"

        # The box takes the chat (clearing the wake) and sleeps it again.
        for report in ("publishing", "asleep"):
            await client.put(STATE.format(chat_id=chat_id), json={"state": report}, headers=box)
        assert (await _spec(chat_id)).get("wake_requested_at") is None

        tried = await _decisions(org_admin.org_id, chat_id)
        second = await theirs.post(f"/api/v1/chats/{chat_id}/wake")
        assert second.status_code == 200, second.text
        assert second.json() == {"outcome": "throttled", "machine_unavailable": None}
        assert (await _spec(chat_id)).get("wake_requested_at") is None, "no second wake"
        assert await _decisions(org_admin.org_id, chat_id) == tried, (
            "a throttled open files nothing"
        )


async def test_an_open_past_the_interval_wakes_again(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    from backend.services.chats import WAKE_INTENT_INTERVAL

    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-2")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = await _slept_chat(client, theirs, box, "Again")
        assert (await theirs.post(f"/api/v1/chats/{chat_id}/wake")).json()["outcome"] == "waking"
        for report in ("publishing", "asleep"):
            await client.put(STATE.format(chat_id=chat_id), json={"state": report}, headers=box)
        # The last open is moved back past the interval.
        async with AsyncSessionLocal() as session:
            row = await session.get(WorkspaceObject, UUID(chat_id))
            assert row is not None
            earlier = datetime.now(UTC) - WAKE_INTENT_INTERVAL - timedelta(seconds=1)
            row.spec = {**row.spec, "wake_intent_at": earlier.isoformat()}
            await session.commit()
        again = await theirs.post(f"/api/v1/chats/{chat_id}/wake")
        assert again.json() == {"outcome": "waking", "machine_unavailable": None}
        assert (await _spec(chat_id)).get("wake_requested_at") is not None


async def test_an_open_on_an_awake_chat_asks_for_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-3")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = str((await theirs.post("/api/v1/chats", json={"title": "Up"})).json()["id"])
        await client.put(STATE.format(chat_id=chat_id), json={"state": "publishing"}, headers=box)
        before = await _decisions(org_admin.org_id, chat_id)
        opened = await theirs.post(f"/api/v1/chats/{chat_id}/wake")
        assert opened.json() == {"outcome": "awake", "machine_unavailable": None}
        spec = await _spec(chat_id)
        assert spec.get("wake_requested_at") is None and spec.get("wake_intent_at") is None
        assert await _decisions(org_admin.org_id, chat_id) == before, "an awake chat files nothing"


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize(
    ("role", "status"),
    [
        pytest.param(ROLE_READER, 403, id="a-view-only-reader-does-not-wake-it"),
        pytest.param(ROLE_WRITER, 200, id="an-editor-wakes-it"),
    ],
)
async def test_only_someone_who_may_send_wakes_a_shared_chat(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    role: str,
    status: int,
) -> None:
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-4")
    owner, owner_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    guest, guest_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert owner_password is not None and guest_password is not None
    async with app_client() as mine, app_client() as theirs:
        await login(mine, owner.email, owner_password)
        await login(theirs, guest.email, guest_password)
        chat_id = await _slept_chat(client, mine, box, "Shared")
        row = await real_session.get(WorkspaceObject, UUID(chat_id))
        assert row is not None
        await share_chat(real_session, chat=row, owner=owner, user=guest, role=role)

        before = await _decisions(org_admin.org_id, chat_id)
        opened = await theirs.post(f"/api/v1/chats/{chat_id}/wake")
        assert opened.status_code == status, opened.text
        stamped = (await _spec(chat_id)).get("wake_requested_at") is not None
        assert stamped is (status == 200)
        filed = (await _decisions(org_admin.org_id, chat_id))[len(before) :]
        assert filed == [("send", "allow" if status == 200 else "deny")]


async def test_a_stranger_gets_the_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-5")
    owner, owner_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    other, other_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert owner_password is not None and other_password is not None
    async with app_client() as mine, app_client() as theirs:
        await login(mine, owner.email, owner_password)
        await login(theirs, other.email, other_password)
        chat_id = await _slept_chat(client, mine, box, "Private")
        opened = await theirs.post(f"/api/v1/chats/{chat_id}/wake")
        assert opened.status_code == 404, opened.text
        assert (await _spec(chat_id)).get("wake_requested_at") is None
        missing = await theirs.post(f"/api/v1/chats/{uuid4()}/wake")
        assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# a workspace opened with no chat
# --------------------------------------------------------------------------- #


async def _touch(chat_id: str, at: datetime) -> None:
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat_id))
        assert row is not None
        row.updated_at = at
        await session.commit()


async def test_a_workspace_wakes_its_most_recently_active_chat_once_per_interval(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-7")
    async with AsyncSessionLocal() as session:
        # A box that can hold a workspace several chats share.
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.capabilities_json = [BoxCapability.WORKSPACES.value]
        await session.commit()
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        made = await theirs.post("/api/v1/workspaces", json={"title": "Lab"})
        assert made.status_code == 201, made.text
        workspace_id = made.json()["id"]
        wake = f"/api/v1/workspaces/{workspace_id}/wake"

        empty = await theirs.post(wake)
        assert (empty.status_code, empty.content) == (204, b""), empty.text

        chats: list[str] = []
        for title in ("Older", "Newer"):
            created = await theirs.post(
                "/api/v1/chats", json={"title": title, "workspace_id": workspace_id}
            )
            assert created.status_code == 201, created.text
            chats.append(str(created.json()["id"]))
            for report in ("publishing", "asleep"):
                moved = await client.put(
                    STATE.format(chat_id=chats[-1]), json={"state": report}, headers=box
                )
                assert moved.status_code == 200, moved.text
        older, newer = chats
        now = datetime.now(UTC)
        # The chat made first is the one last used.
        await _touch(older, now)
        await _touch(newer, now - timedelta(hours=1))

        opened = await theirs.post(wake)
        assert opened.status_code == 200, opened.text
        assert opened.json() == {"outcome": "waking", "machine_unavailable": None}
        assert (await _spec(older)).get("wake_requested_at") is not None
        assert (await _spec(newer)).get("wake_requested_at") is None
        assert (await _decisions(org_admin.org_id, older))[-1] == ("send", "allow")

        filed = await _decisions(org_admin.org_id, older)
        again = await theirs.post(wake)
        assert again.json() == {"outcome": "throttled", "machine_unavailable": None}
        assert await _decisions(org_admin.org_id, older) == filed
        assert (await _spec(newer)).get("wake_requested_at") is None


@pytest.mark.usefixtures("files_on")
async def test_a_workspace_wakes_nothing_for_someone_who_may_not_send_in_its_chats(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A stranger gets the opaque not-found. A reader of the workspace's only
    chat may not send in it, so nothing is woken for them either."""
    _machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-8")
    owner, owner_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    guest, guest_password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert owner_password is not None and guest_password is not None
    async with app_client() as mine, app_client() as theirs:
        await login(mine, owner.email, owner_password)
        await login(theirs, guest.email, guest_password)
        chat_id = await _slept_chat(client, mine, box, "Shared")
        workspace_id = (await mine.get(f"/api/v1/chats/{chat_id}")).json()["workspace_id"]
        assert workspace_id is not None
        wake = f"/api/v1/workspaces/{workspace_id}/wake"

        assert (await theirs.post(wake)).status_code == 404
        assert (await theirs.post(f"/api/v1/workspaces/{uuid4()}/wake")).status_code == 404

        row = await real_session.get(WorkspaceObject, UUID(chat_id))
        assert row is not None
        await share_chat(real_session, chat=row, owner=owner, user=guest, role=ROLE_READER)
        assert (await theirs.get(f"/api/v1/chats/{chat_id}")).json()["can_send"] is False
        opened = await theirs.post(wake)
        assert opened.status_code in (204, 404), opened.text
        assert (await _spec(chat_id)).get("wake_requested_at") is None

        assert (await mine.post(wake)).json() == {"outcome": "waking", "machine_unavailable": None}


# --------------------------------------------------------------------------- #
# an open-only wake nobody takes
# --------------------------------------------------------------------------- #


async def test_an_open_wake_nobody_takes_marks_no_message_never_run(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The open recorded no message, so when the wake's deadline ends it the
    chat goes back to sleep with nothing in its transcript said to be never
    run. Runs where the untaken-wake sweep exists."""
    wake_reaper = pytest.importorskip("alkera_core.objects.wake_reaper")
    from alkera_core.models import ChatMessage

    machine_id, box = await _registered_machine(client, org_admin, real_session, pod="wake-6")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    async with app_client() as theirs:
        await login(theirs, member.email, password)
        chat_id = await _slept_chat(client, theirs, box, "Idle")
        assert (await theirs.post(f"/api/v1/chats/{chat_id}/wake")).json()["outcome"] == "waking"
        before = await _rows(chat_id, ChatMessage)
        later = datetime.now(UTC) + wake_reaper.wake_deadline() * 2
        async with AsyncSessionLocal() as session:
            # The box still answers at that moment: only an answering machine's
            # untaken wake is the sweep's to end.
            alloc = await session.get(ComputeAllocation, UUID(machine_id))
            assert alloc is not None
            alloc.last_heartbeat_at = later
            await session.commit()
            await wake_reaper.end_untaken_wakes(session, now=later)
        assert (await _spec(chat_id)).get("wake_requested_at") is None, "the wake was ended"
        assert await _rows(chat_id, ChatMessage) == before, "nothing was written to the chat"


async def _rows(chat_id: str, model: Any) -> list[Any]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(model.id).where(model.chat_id == UUID(chat_id)))
        return sorted(str(r) for r in rows.scalars())
