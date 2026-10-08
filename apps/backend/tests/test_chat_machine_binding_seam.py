"""A chat's machine status has to track the machine.

``resolve_machine_for`` is called once, at chat creation, and its answer is
written into the chat's ``spec``. The browser then prefers that value over
``GET /api/v1/machines/current`` — ``useMachineStatus`` in
``apps/web/src/pages/workspace/chat/ChatPage.tsx`` returns
``chat.machine_status`` whenever the chat has loaded — and disables the
composer for anything but ``ready``.

So the two directions of drift both matter, and both are checked here through
the real routes:

* a chat opened while the box was still booting must become usable once the box
  answers, or the reader is locked out of a chat that is in fact live;
* a chat whose box has stopped answering must stop saying ``ready``, or the
  demo's kill drill shows a healthy banner over a dead machine, the
  silent hang the rehearsal exists to rule out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.authz import agent_headers
from alkera_core.compute.meter import meter_and_cutoff
from alkera_core.compute.provider import ComputeProviderError
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.compute import ComputeAllocation
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider, make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("compute-fleet")]

T0 = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a box that IS beating inside the ready window for the whole case.

    A box reads ``ready`` only while its last heartbeat is newer than
    ``compute_heartbeat_ready_seconds`` — forty-five seconds of WALL CLOCK, read
    live every time a chat is read. Most cases here register a box, beat it once,
    and then drive half a dozen more requests before asking what the chat says;
    on a loaded runner — sixty-four workers, ten seconds a request — the window
    closes between the beat and the question and the chat reads ``unreachable``
    for a box nothing is wrong with. Pinned past any run's length, ``ready``
    means the box beat rather than that the host was quick.

    The cases that are about a box going quiet still get their answer: each ages
    the heartbeat (or moves a frozen clock) PAST whatever this window is, never
    past a hard-coded forty-five seconds, so they cross the boundary here exactly
    as they do at the shipped default.

    Ten minutes, not an hour: the frozen cases move the clock by the window plus
    a second, and a jump past ``auth_token_ttl_seconds`` (1800) drops the session
    cookie the browser is holding — the request then fails as unauthorized rather
    than as anything about a machine.
    """
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


def _browser() -> AsyncClient:
    return app_client(base_url="http://testserver")


_device_tokens: dict[UUID, str] = {}


async def _daemon_headers(org: OrgWithAdmin, *, agent_id: str = "binding-seam") -> dict[str, str]:
    """The box's one device token (minted once per operator, the way
    ``alkera login`` leaves one in ``~/.alkera/auth.yml``) plus the agent
    assertion: a machine verifies only on the credential that registered it."""
    token = _device_tokens.get(org.admin_id)
    if token is None:
        token = await mint_cli_token(
            user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
        )
        _device_tokens[org.admin_id] = token
    return {"Authorization": f"Bearer {token}", **agent_headers(agent_id)}


async def _register(org: OrgWithAdmin, code: str, pod: str) -> str:
    async with _browser() as daemon:
        registered = await daemon.post(
            "/api/v1/machines/register",
            json={
                "provider": "runpod",
                "provider_pod_id": pod,
                "name": "demo-box",
                "machine_type_code": code,
            },
            headers=await _daemon_headers(org),
        )
    assert registered.status_code in (200, 201), registered.text
    return str(registered.json()["id"])


async def _heartbeat(org: OrgWithAdmin, machine_id: str) -> None:
    async with _browser() as daemon:
        beat = await daemon.post(
            f"/api/v1/machines/{machine_id}/heartbeat", headers=await _daemon_headers(org)
        )
    assert beat.status_code == 204, beat.text


async def test_a_chat_opened_before_the_box_answered_becomes_ready_with_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The chat is created while the machine is still ``starting``; once the
    daemon heartbeats, reading the chat again must say ``ready``."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-binding-1")

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "while it boots"})
        assert created.status_code == 201, created.text
        chat_id = created.json()["id"]
        assert created.json()["machine_status"] == "starting"

        await _heartbeat(org_admin, machine_id)

        assert (await browser.get("/api/v1/machines/current")).json()["status"] == "ready"
        reread = await browser.get(f"/api/v1/chats/{chat_id}")
        assert reread.status_code == 200, reread.text
        assert reread.json()["machine_status"] == "ready"


async def test_a_chat_stops_saying_ready_when_its_box_stops_answering(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The kill drill: the machine was ready when the chat opened, then the
    daemon died. Reading the chat again must not still claim ``ready``."""
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-binding-2")
    await _heartbeat(org_admin, machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "before the kill"})
        assert created.status_code == 201, created.text
        chat_id = created.json()["id"]
        assert created.json()["machine_status"] == "ready"

        # The daemon is gone: its last heartbeat ages past the liveness window.
        async with AsyncSessionLocal() as db:
            alloc = await db.get(ComputeAllocation, UUID(machine_id))
            assert alloc is not None
            # Aged past whatever the window is, so the kill drill still kills
            # the box when the window is held open above.
            alloc.last_heartbeat_at = datetime.now(UTC) - timedelta(
                seconds=settings.compute_heartbeat_ready_seconds + 60
            )
            await db.commit()

        assert (await browser.get("/api/v1/machines/current")).json()["status"] == "unreachable"
        reread = await browser.get(f"/api/v1/chats/{chat_id}")
        assert reread.status_code == 200, reread.text
        assert reread.json()["machine_status"] == "unreachable"


async def test_a_chat_whose_box_is_gone_is_rebound_on_its_next_message(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A chat bound to a machine that no longer exists is moved to a live one.

    The binding used to be made once, at create, and a box is a thing that
    sleeps, is replaced, or dies — so a chat whose box went away read as
    waiting for the rest of its life with nothing willing to serve it (a daemon
    serves only chats bound to the machine it registered as). Sending a message
    is the moment that has to be fixed: the reader is waiting for an answer.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    first = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-1")
    await _heartbeat(org_admin, first)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "outlives its box"})
        assert created.status_code == 201, created.text
        chat_id = created.json()["id"]
        assert created.json()["machine_id"] == first

        # The box is gone for good, and a fresh one has taken its place.
        async with AsyncSessionLocal() as db:
            alloc = await db.get(ComputeAllocation, UUID(first))
            assert alloc is not None
            alloc.state = "released"
            await db.commit()
        stranded = await browser.get(f"/api/v1/chats/{chat_id}")
        assert stranded.json()["machine_status"] == "stranded"

        second = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-2")
        await _heartbeat(org_admin, second)

        sent = await browser.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "are you there?", "client_id": "rebind-1"},
        )
        assert sent.status_code == 201, sent.text

        reread = await browser.get(f"/api/v1/chats/{chat_id}")
        assert reread.json()["machine_id"] == second
        assert reread.json()["machine_status"] == "ready"


async def test_a_chat_whose_box_is_alive_is_left_where_it_is(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The negative twin: sending is not a reason to move a working chat.

    A rebind that fired on every message would move a chat off the box that is
    mid-turn on it, which loses the turn and the box's local state with it.
    """
    machine_type = await make_machine_type(real_session)
    # Room for two at once: the point of this test is that a SECOND live box
    # exists and the chat still does not move to it.
    await make_grant(
        real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id, ceiling=2
    )
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-3")
    await _heartbeat(org_admin, machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "still running"})
        chat_id = created.json()["id"]

        # A second, more recently heard from box exists: placement would pick it
        # if it were asked, and it must not be asked.
        newer = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-4")
        await _heartbeat(org_admin, newer)

        sent = await browser.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "still here", "client_id": "rebind-2"},
        )
        assert sent.status_code == 201, sent.text

        assert (await browser.get(f"/api/v1/chats/{chat_id}")).json()["machine_id"] == machine_id


async def test_a_chat_with_no_live_machine_to_move_to_keeps_the_box_it_had(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Nothing to rebind to is not a reason to forget the binding.

    Clearing it would lose which box last ran the chat, which is the only thing
    that says where its state was — and the reader is told the chat is waiting
    (``stranded``) because that box is gone, which needs the box's id kept.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-5")
    await _heartbeat(org_admin, machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "nowhere to go"})
        chat_id = created.json()["id"]

        async with AsyncSessionLocal() as db:
            alloc = await db.get(ComputeAllocation, UUID(machine_id))
            assert alloc is not None
            alloc.state = "released"
            await db.commit()

        sent = await browser.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "anyone?", "client_id": "rebind-3"},
        )
        assert sent.status_code == 201, sent.text

        reread = await browser.get(f"/api/v1/chats/{chat_id}")
        assert reread.json()["machine_id"] == machine_id
        assert reread.json()["machine_status"] == "stranded"


async def test_rebinding_clears_the_old_boxs_publishing_refusal(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A refusal belongs to the machine that said it.

    Carried across a rebind it would light a banner about a box that is no
    longer serving the chat, and the reader would be told to fetch an admin for
    a machine that is already gone.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    first = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-6")
    await _heartbeat(org_admin, first)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "refused then moved"})
        chat_id = created.json()["id"]

    # Only the chat's own machine may say it cannot publish, so the box asserts
    # the id it registered as rather than the seam's usual agent name.
    async with _browser() as daemon:
        refused = await daemon.put(
            f"/api/v1/chats/{chat_id}/publisher-state",
            json={"state": "refused", "reason": "this workspace cannot write it"},
            headers=await _daemon_headers(org_admin, agent_id=first),
        )
    assert refused.status_code == 200, refused.text

    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(first))
        assert alloc is not None
        alloc.state = "released"
        await db.commit()
    second = await _register(org_admin, machine_type.provider_type_id, "pod-rebind-7")
    await _heartbeat(org_admin, second)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        sent = await browser.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "try again", "client_id": "rebind-4"},
        )
        assert sent.status_code == 201, sent.text

        reread = await browser.get(f"/api/v1/chats/{chat_id}")
        assert reread.json()["machine_id"] == second
        assert reread.json()["machine_status"] == "ready"
        assert reread.json()["machine_refusal_reason"] is None


async def test_a_new_chat_takes_the_orgs_live_box_when_the_preferred_provider_has_none(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The shape every deployment is in today, end to end through the routes.

    Web chats are placed on ``settings.compute_web_chat_provider`` — the
    container service — and nothing provisions one yet, so the org's registered,
    heartbeating box is the only machine there is. A chat that comes back with
    ``machine_status: "none"`` over a box that is answering is a dead banner
    over live compute: the composer is disabled and the reader is told there is
    no workspace while one sits there idle.
    """
    assert settings.compute_web_chat_provider != "runpod", (
        "this test proves the FALLBACK; it says nothing if the box's own "
        "provider is the preferred one"
    )
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-fallback-1")
    await _heartbeat(org_admin, machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "the only box there is"})

    assert created.status_code == 201, created.text
    assert created.json()["machine_id"] == machine_id
    assert created.json()["machine_status"] == "ready"


async def test_a_chat_orphaned_while_its_box_was_reaped_runs_again_when_the_box_returns(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The whole failure, from the reap to the answer.

    A box whose pod the meter decided was gone loses its row, and a chat opened
    while it was gone binds to nothing. When the same box registers again it
    must come back as the SAME machine — so the chat that was bound to it is
    live again with no rebind at all — and the chat opened in the gap must find
    it on its next message, the moment a reader is waiting for one.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-reaped-1")
    await _heartbeat(org_admin, machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        before = await browser.post("/api/v1/chats", json={"title": "opened on the box"})
        assert before.json()["machine_id"] == machine_id

        # The meter asks the provider about the pod, is told it is gone, and
        # stops the machine. The chat now has nowhere to run.
        async with AsyncSessionLocal() as db:
            alloc = await db.get(ComputeAllocation, UUID(machine_id))
            assert alloc is not None
            alloc.state = "released"
            alloc.released_at = datetime.now(UTC)
            alloc.terminated_reason = "provider_gone"
            await db.commit()

        orphan = await browser.post("/api/v1/chats", json={"title": "opened in the gap"})
        assert orphan.status_code == 201, orphan.text
        assert orphan.json()["machine_id"] is None
        assert orphan.json()["machine_status"] == "none"
        orphan_id = orphan.json()["id"]

        back = await _register(org_admin, machine_type.provider_type_id, "pod-reaped-1")
        assert back == machine_id, "the same pod must take its own row back"
        await _heartbeat(org_admin, machine_id)

        # The chat that was bound to it never lost its machine.
        reopened = await browser.get(f"/api/v1/chats/{before.json()['id']}")
        assert reopened.json()["machine_status"] == "ready"

        sent = await browser.post(
            f"/api/v1/chats/{orphan_id}/messages",
            json={"text": "are you there?", "client_id": "reaped-1"},
        )
        assert sent.status_code == 201, sent.text
        reread = await browser.get(f"/api/v1/chats/{orphan_id}")
        assert reread.json()["machine_id"] == machine_id
        assert reread.json()["machine_status"] == "ready"


async def test_a_chat_on_a_registered_box_keeps_its_binding_across_a_meter_pass(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The flap, from the chat's side.

    The box registered itself, so the provider has never heard of its pod and
    says so. The meter used to take that answer, stop the box, and leave every
    chat on it reading ``none`` until the daemon's next heartbeat answered 404
    and it registered again — a dead banner over live compute, once a minute.
    A registered box is metered from its own heartbeat: the pass leaves the
    chat where it was, ready, on the same machine, and the org still has it.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-metered-1")
    await _heartbeat(org_admin, machine_id)

    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        created = await browser.post("/api/v1/chats", json={"title": "on a self-registered box"})
        assert created.status_code == 201, created.text
        assert created.json()["machine_id"] == machine_id
        assert created.json()["machine_status"] == "ready"

        provider = FakeProvider(
            status_errors={"pod-metered-1": ComputeProviderError("no such pod", status_code=404)}
        )
        async with AsyncSessionLocal() as db:
            await meter_and_cutoff(db, provider=provider)

        reread = await browser.get(f"/api/v1/chats/{created.json()['id']}")
        assert reread.json()["machine_id"] == machine_id
        assert reread.json()["machine_status"] == "ready"
        current = await browser.get("/api/v1/machines/current")
        assert current.status_code == 200, current.text
        assert (current.json()["machine_id"], current.json()["status"]) == (machine_id, "ready")


async def test_a_message_to_a_box_that_stopped_answering_is_refused_not_swallowed(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The other half of the kill drill: the chat must REFUSE the message.

    Reading ``unreachable`` is only half the honesty. The row a killed daemon
    leaves behind still says ``ready`` in the database for as long as the meter
    takes to reap it, so the send path — which relays the prompt at a machine id
    and then returns — happily accepts the message, clears the composer and
    leaves the reader waiting on a process that no longer exists.

    The clock is the only thing that moves here: same org, same box, same chat,
    same row. One second inside the liveness window the message lands; one
    second past it the same message is a 409 naming the box, and the transcript
    is untouched — a refused send must not leave a prompt in the chat that
    nothing will ever answer.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    window = settings.compute_heartbeat_ready_seconds

    # Register, heartbeat and log in inside the freeze: each mints a JWT, and a
    # token issued on the real clock has an `iat` in the future once T0 passes.
    # `real_asyncio` keeps the event loop on the real monotonic clock: a frozen
    # one never reaches a `call_later` deadline, so the first request in here to
    # arm one — a catalog fetch's timeout, a pool's acquire — waits forever.
    with freeze_time(T0, real_asyncio=True) as frozen:
        machine_id = await _register(org_admin, machine_type.provider_type_id, "pod-quiet-1")
        await _heartbeat(org_admin, machine_id)
        async with _browser() as browser:
            await login(browser, org_admin.admin_email, org_admin.admin_password)
            created = await browser.post("/api/v1/chats", json={"title": "into the void"})
            assert created.status_code == 201, created.text
            chat_id = created.json()["id"]
            assert created.json()["machine_id"] == machine_id

            frozen.move_to(T0 + timedelta(seconds=window - 1))
            accepted = await browser.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={"text": "while it is alive", "client_id": "quiet-fresh"},
            )
            assert accepted.status_code == 201, accepted.text
            alive = await browser.get(f"/api/v1/chats/{chat_id}")
            assert alive.json()["machine_status"] == "ready"
            seq_before = alive.json()["last_seq"]

            frozen.move_to(T0 + timedelta(seconds=window + 1))
            quiet = await browser.get(f"/api/v1/chats/{chat_id}")
            assert quiet.json()["machine_status"] == "unreachable"
            assert quiet.json()["machine_id"] == machine_id
            refused = await browser.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={"text": "anybody home?", "client_id": "quiet-stale"},
            )
            assert refused.status_code == 409, refused.text
            body = refused.json()["error"]
            assert body["code"] == "machine_unreachable"
            assert body["details"]["machineId"] == machine_id
            assert (await browser.get(f"/api/v1/chats/{chat_id}")).json()["last_seq"] == seq_before

            # The daemon comes back. Nothing else changed, and the same message
            # is accepted again: the refusal tracked the heartbeat, not a latch.
            await _heartbeat(org_admin, machine_id)
            resumed = await browser.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={"text": "anybody home?", "client_id": "quiet-stale"},
            )
            assert resumed.status_code == 201, resumed.text


async def test_a_chat_whose_box_went_quiet_moves_to_one_that_is_answering(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The resume path: a quiet box is stranded the same way a dead row is.

    A box that stops heartbeating is unserveable even though its row is still on
    the plane, so the rebind has to judge it by the heartbeat too. When the org
    has another box that IS answering, the reader gets the answer rather than
    the refusal above — the chat simply moves.
    """
    machine_type = await make_machine_type(real_session)
    await make_grant(
        real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id, ceiling=2
    )
    window = settings.compute_heartbeat_ready_seconds

    # `real_asyncio`: see the case above — a frozen loop clock never reaches a
    # `call_later` deadline, and the request that arms one never returns.
    with freeze_time(T0, real_asyncio=True) as frozen:
        first = await _register(org_admin, machine_type.provider_type_id, "pod-quiet-2")
        await _heartbeat(org_admin, first)
        async with _browser() as browser:
            await login(browser, org_admin.admin_email, org_admin.admin_password)
            created = await browser.post("/api/v1/chats", json={"title": "moves on"})
            assert created.status_code == 201, created.text
            chat_id = created.json()["id"]
            assert created.json()["machine_id"] == first

            # The first box goes quiet; a second one is up and beating.
            frozen.move_to(T0 + timedelta(seconds=window + 1))
            second = await _register(org_admin, machine_type.provider_type_id, "pod-quiet-3")
            await _heartbeat(org_admin, second)

            sent = await browser.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={"text": "are you there?", "client_id": "quiet-move"},
            )
            assert sent.status_code == 201, sent.text
            reread = await browser.get(f"/api/v1/chats/{chat_id}")
            assert reread.json()["machine_id"] == second
            assert reread.json()["machine_status"] == "ready"
