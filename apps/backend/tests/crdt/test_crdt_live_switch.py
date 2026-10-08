"""Turning live editing off, for one org or the deployment, and back on.

Against real Postgres, real Files, real sandbox workers and, for the tab, a
real server over a real socket: the org's own setting wins over the
deployment's in both directions; switching off writes back the typing every
session holds, tells an open tab (which falls back to showing the file another
way), refuses new live opens and a box's text-peer read and submit (the box
then uploads the file), never refuses an update a tab already holding the
file sends before it is told, and switching back on opens the file live where
its session stands. Only platform staff read the switch and only platform
admins change it, each decision on record.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuditLog, EventOutbox, OrgAuditEvent
from backend.services.crdt import sweeper
from backend.services.crdt import switch as live_switch
from backend.services.crdt.registry import CrdtRegistry
from backend.services.org import settings as org_settings_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.crdt_world import Person
from tests.crdt.file_world import SOURCE, FileWorld, drive_text, file_world
from tests.crdt.test_crdt_gateway import Tab
from tests.crdt.test_crdt_text_peers import _Docs, _holder, _open, _peer_rows, _tab, _type
from tests.test_ws_gateway import connect, logged_in

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

ADMIN = "/admin/v1/orgs"


async def _set_override(org_id: uuid.UUID, enabled: bool | None) -> None:
    async with AsyncSessionLocal() as db:
        await org_settings_service.set_live_editing(db, org_id, enabled)
        await db.commit()


# --------------------------------------------------------------------------- #
# what the switch answers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("deployment", "override", "on"),
    [
        pytest.param(True, None, True, id="follows-a-deployment-that-is-on"),
        pytest.param(False, None, False, id="follows-a-deployment-that-is-off"),
        pytest.param(True, False, False, id="the-org-off-wins-over-the-deployment-on"),
        pytest.param(False, True, True, id="the-org-on-wins-over-the-deployment-off"),
        pytest.param(True, True, True, id="the-org-on-with-the-deployment-on"),
        pytest.param(False, False, False, id="the-org-off-with-the-deployment-off"),
    ],
)
async def test_the_org_s_own_setting_wins_over_the_deployment_s_in_either_direction(
    deployment: bool, override: bool | None, on: bool
) -> None:
    assert live_switch.resolve_live_editing(deployment, override) is on


@pytest.mark.parametrize(
    ("doc_type", "switched"),
    [
        pytest.param("file", True, id="a-co-edited-file"),
        pytest.param("notebook", True, id="a-notebook"),
        pytest.param("chat_draft", False, id="a-chat-draft"),
    ],
)
async def test_only_the_document_types_that_rest_in_a_source_are_switched(
    doc_type: str, switched: bool
) -> None:
    found = CrdtRegistry().get(doc_type)
    assert found is not None
    assert live_switch.covers(found) is switched


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def test_a_replica_reads_the_org_s_setting_and_keeps_it_only_briefly(
    org_admin: OrgWithAdmin,
) -> None:
    """The answer is the org's stored setting; a replica keeps it for the
    cache window (so a socket's tick does not read it every time), reads it
    again past the window, and at once after it forgets the org (the replica
    that changed it). The chat's draft is served whatever the switch says."""
    clock = _Clock()
    switch = live_switch.LiveSwitch(AsyncSessionLocal, deployment=lambda: True, clock=clock)
    draft_type = CrdtRegistry().get("chat_draft")
    assert draft_type is not None
    assert await switch.on(org_admin.org_id) is True

    await _set_override(org_admin.org_id, False)
    assert await switch.on(org_admin.org_id) is True  # still the cached answer
    clock.now += live_switch.SWITCH_CACHE_SECONDS - 0.01
    assert await switch.on(org_admin.org_id) is True
    clock.now += 0.01
    assert await switch.on(org_admin.org_id) is False
    assert await switch.serves(org_admin.org_id, draft_type) is True

    await _set_override(org_admin.org_id, None)
    assert await switch.on(org_admin.org_id) is False  # cached
    switch.forget(org_admin.org_id)
    assert await switch.on(org_admin.org_id) is True


# --------------------------------------------------------------------------- #
# writing back an org's sessions when it is switched off
# --------------------------------------------------------------------------- #


async def test_switching_off_writes_back_every_session_holding_typing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """With nothing else writing back (the store's own passes are off), the
    switch-off pass puts each session's typing on the drive; a session whose
    write back is parked (refused earlier) is left to the unsaved sweep and
    counted as such, never closed."""
    typed = await file_world(real_session, org_admin, name="typed.py")
    parked = await file_world(real_session, org_admin, name="parked.py")
    async with _Docs() as docs:
        for fw, tab in ((typed, _tab(8101)), (parked, _tab(8102))):
            await _open(docs, real_session, fw, tab)
            await _type(docs, real_session, fw, tab, 0, "# typed\n")
        await sweeper.park(
            docs.session_factory, parked.ref, reason="no_writer", now=datetime.now(UTC)
        )
        done = await live_switch.write_back_org(docs, org_admin.org_id)
    assert (done.written, done.left) == (1, 1)
    assert await drive_text(real_session, typed) == "# typed\n" + SOURCE
    assert await drive_text(real_session, parked) == SOURCE


# --------------------------------------------------------------------------- #
# the box's text peer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("deployment", "override", "live"),
    [
        pytest.param(True, False, False, id="the-org-switched-off"),
        pytest.param(False, None, False, id="the-deployment-switched-off"),
        pytest.param(False, True, True, id="the-org-switched-on-against-the-deployment"),
        pytest.param(True, None, True, id="on"),
    ],
)
async def test_while_off_a_box_is_told_live_false_and_its_edit_is_not_merged(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    deployment: bool,
    override: bool | None,
    live: bool,
) -> None:
    """Through the routes, as the folder's holder: while off, the read and
    the submit both answer ``live: false`` naming why (the box writes the
    file the ordinary way) and nothing is merged into the document."""
    fw = await file_world(real_session, org_admin)
    holder = await _holder(real_session, client, fw)
    async with _Docs() as docs:
        await _open(docs, real_session, fw, _tab(8201))
    await _set_override(org_admin.org_id, override)
    from backend.authz import decide_on_record
    from backend.services.realtime import runtime
    from tests.conftest import fastapi_app as app

    monkeypatch.setattr(settings, "realtime_listener_enabled", False)
    monkeypatch.setattr(settings, "live_editing_enabled", deployment)
    started = await runtime.start(app, decide=decide_on_record)
    try:
        read = await holder.live_text(fw.node_id)
        sent = await holder.submit_text(fw.node_id, SOURCE + "# agent\n")
    finally:
        await runtime.stop(app, started)
    assert read.status_code == 200, read.text
    assert sent.status_code == 200, sent.text
    assert read.json()["live"] is live and sent.json()["live"] is live
    merged = await _peer_rows(real_session, fw)
    if live:
        assert sent.json()["text"] == SOURCE + "# agent\n"
        assert len(merged) == 1
        return
    assert read.json()["reason"] == sent.json()["reason"] == "live_editing_off"
    assert merged == []


# --------------------------------------------------------------------------- #
# an open tab, over a real socket
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _tab_on(server: str, fw: FileWorld, person: Person) -> AsyncIterator[Tab]:
    async with AsyncExitStack() as stack:
        client = await logged_in(person.user.email, person.password)
        stack.push_async_callback(client.aclose)
        sock = await stack.enter_async_context(connect(server, client))
        yield Tab(sock, fw.ref.channel, container="content")


async def _switch(staff: OrgWithAdmin, org_id: uuid.UUID, enabled: bool | None) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        client = await logged_in(staff.admin_email, staff.admin_password)
        stack.push_async_callback(client.aclose)
        answer = await client.put(f"{ADMIN}/{org_id}/live-editing", json={"enabled": enabled})
    assert answer.status_code == 200, answer.text
    return dict(answer.json())


async def _until(read: Callable[[], Any], expected: object, seconds: float = 20) -> object:
    seen: object = None
    for _ in range(int(seconds / 0.25)):
        seen = await read()
        if seen == expected:
            return seen
        await asyncio.sleep(0.25)
    return seen


def _hello_payload() -> dict[str, Any]:
    return {"proto": 1, "loro": "1.16.4", "doc_schema": 1}


async def test_an_open_tab_is_told_its_typing_is_saved_and_new_opens_are_refused(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab typing in a file when its org is switched off: what it typed
    reaches the drive, the tab is told on its socket's next tick that the
    channel is not served live (it shows the file another way), a fresh
    subscribe is refused the same way, and switching back on opens the file
    live again with the typing in it."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 1)
    fw = await file_world(real_session, org_admin)
    typed = "# typed\n" + SOURCE
    async with _tab_on(uvicorn_server, fw, fw.world.owner) as tab:
        assert await tab.sock.subscribe(fw.ref.channel) is True
        await tab.hello()
        assert (await tab.type(0, "# typed\n"))["kind"] == "ack"

        answer = await _switch(platform_admin, org_admin.org_id, False)
        assert (answer["enabled"], answer["override"]) == (False, False)
        assert isinstance(answer["sessions_written"], int)

        told = await tab.sock.recv_until(lambda f: f["t"] == "error", seconds=15)
        assert (told["code"], told["channel"]) == ("crdt_unsupported", fw.ref.channel)
        assert await _until(lambda: drive_text(real_session, fw), typed) == typed
        refused = await tab.sock.subscribe_error(fw.ref.channel)
        assert refused["code"] == "crdt_unsupported"

        answer = await _switch(platform_admin, org_admin.org_id, None)
        assert (answer["enabled"], answer["override"]) == (True, None)
        assert answer["sessions_written"] is None
        assert await tab.sock.subscribe(fw.ref.channel) is True
        tab.peer = None
        tab.epoch = 0
        await tab.hello()
        assert tab.peer is not None and tab.peer.text == typed


async def test_a_tab_already_holding_the_file_is_not_refused_what_it_sends_before_it_is_told(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between the switch and the tab's next tick the tab may still be
    typing: that update is the person's and is taken (and written back),
    while a hello asking to open the file again is refused in band."""
    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 300)
    fw = await file_world(real_session, org_admin)
    async with _tab_on(uvicorn_server, fw, fw.world.owner) as tab:
        assert await tab.sock.subscribe(fw.ref.channel) is True
        await tab.hello()
        await _switch(platform_admin, org_admin.org_id, False)

        assert (await tab.type(0, "# late\n"))["kind"] == "ack"
        await tab.sock.send(tab.envelope("hello", _hello_payload()))
        refused = await tab.sock.next_doc("error")
        assert refused["payload"]["code"] == "crdt_unsupported"
        assert refused["payload"]["reason"] == "live_editing_off"
    expected = "# late\n" + SOURCE
    assert await _until(lambda: drive_text(real_session, fw), expected) == expected


# --------------------------------------------------------------------------- #
# who may read and flip it
# --------------------------------------------------------------------------- #


async def _decisions(org_id: uuid.UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "platform_org_live_editing",
                EventOutbox.entity_id == str(org_id),
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def test_support_reads_the_switch_and_is_refused_a_change_each_on_record(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    read = await client.get(f"{ADMIN}/{org_admin.org_id}/live-editing")
    assert read.status_code == 200, read.text
    assert read.json() == {
        "org_id": str(org_admin.org_id),
        "enabled": True,
        "override": None,
        "deployment_default": True,
        "sessions_written": None,
        "sessions_left_unsaved": None,
    }
    refused = await client.put(f"{ADMIN}/{org_admin.org_id}/live-editing", json={"enabled": False})
    assert refused.status_code == 403, refused.text
    rows = await _decisions(org_admin.org_id)
    assert [(r.payload["effect"], r.payload["reason"]) for r in rows[-2:]] == [
        ("allow", "staff_reads"),
        ("deny", "platform_admin_required"),
    ]
    assert all(r.visibility == "platform" for r in rows)
    async with AsyncSessionLocal() as db:
        assert await live_switch.live_editing_override(db, org_admin.org_id) is None


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_an_org_admin_cannot_reach_its_own_org_s_switch(
    client: AsyncClient, org_admin: OrgWithAdmin, method: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    answer = await client.request(
        method,
        f"{ADMIN}/{org_admin.org_id}/live-editing",
        json={"enabled": False} if method == "PUT" else None,
    )
    assert answer.status_code == 403, answer.text
    async with AsyncSessionLocal() as db:
        assert await live_switch.live_editing_override(db, org_admin.org_id) is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        pytest.param({}, 422, id="the-key-is-required"),
        pytest.param({"enabled": "off"}, 422, id="not-a-boolean"),
        pytest.param({"enabled": 0}, 422, id="a-number-is-not-a-boolean"),
        pytest.param({"enabled": None}, 200, id="null-clears"),
    ],
)
async def test_a_change_names_the_setting_explicitly(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    body: dict[str, object],
    status: int,
) -> None:
    await _set_override(org_admin.org_id, False)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    answer = await client.put(f"{ADMIN}/{org_admin.org_id}/live-editing", json=body)
    assert answer.status_code == status, answer.text
    async with AsyncSessionLocal() as db:
        stored = await live_switch.live_editing_override(db, org_admin.org_id)
    assert stored is (None if status == 200 else False)


async def test_an_admin_s_change_is_audited_for_the_org_and_the_platform(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    off = await client.put(f"{ADMIN}/{org_admin.org_id}/live-editing", json={"enabled": False})
    cleared = await client.put(f"{ADMIN}/{org_admin.org_id}/live-editing", json={"enabled": None})
    assert off.status_code == cleared.status_code == 200
    assert off.json()["enabled"] is False and cleared.json()["enabled"] is True
    async with AsyncSessionLocal() as db:
        org_rows = (
            await db.execute(
                select(OrgAuditEvent)
                .where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action.like("live_editing.%"),
                )
                .order_by(OrgAuditEvent.created_at)
            )
        ).scalars()
        platform_rows = (
            await db.execute(
                select(AuditLog).where(AuditLog.path == f"{ADMIN}/{org_admin.org_id}/live-editing")
            )
        ).scalars()
        actions = [(row.action, row.detail) for row in org_rows]
        admin_writes = [(row.method, row.status_code) for row in platform_rows]
    assert [action for action, _ in actions] == [
        "live_editing.org_switch_set",
        "live_editing.org_switch_cleared",
    ]
    assert actions[0][1]["enabled"] is False
    assert admin_writes == [("PUT", 200), ("PUT", 200)]
    decisions = await _decisions(org_admin.org_id)
    assert [d.payload["reason"] for d in decisions[-2:]] == ["admin_changes", "admin_changes"]


async def test_a_deployment_switched_off_reads_as_its_default(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_editing_enabled", False)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    before = (await client.get(f"{ADMIN}/{org_admin.org_id}/live-editing")).json()
    on = (
        await client.put(f"{ADMIN}/{org_admin.org_id}/live-editing", json={"enabled": True})
    ).json()
    assert (before["enabled"], before["deployment_default"]) == (False, False)
    assert (on["enabled"], on["override"], on["deployment_default"]) == (True, True, False)


async def test_an_unknown_org_is_not_found(
    client: AsyncClient, platform_admin: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    unknown = uuid.uuid4()
    answer = await client.put(f"{ADMIN}/{unknown}/live-editing", json={"enabled": False})
    assert answer.status_code == 404, answer.text
