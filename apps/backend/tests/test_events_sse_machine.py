"""``GET /api/v1/events`` for a box on its own machine credential.

A box holds no session and belongs to no org; its stream carries the frames
that concern the chats bound to its machine — their doorbells, their folders'
lease and node changes, the connection and membership traffic of the orgs it
holds chats in — and nothing else: another box's chat in the same org, a
platform stream, a frame addressed to a person and any org it does not serve
never reach it. The doorbell that binds a chat to the box is the first frame
it hears about that chat. The credential's standing is re-asked on every
keepalive, so a revoked box is cut within one tick and refused on reconnect,
and the stream is counted under the machine, never under a person.

The frames are read as the server writes them (``tests._sse_reader``), so a
case waits for the frame it is about and ends the stream by disconnecting;
nothing here is decided by the file's wall-clock bound.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alkera_core.auth.machine_token import MACHINE_TOKEN_PREFIX
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType, emit
from alkera_core.models import MachineCredential, User, WorkspaceObject
from backend.services.chats import chat_service
from backend.services.org import teams as team_service
from backend.services.realtime.limits import ConnectionGate
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._sse_reader import open_event_stream
from tests.conftest import OrgWithAdmin, make_member
from tests.test_events_sse import _until, parked_before_the_body
from tests.test_machine_principal_routes import _box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

STREAM = "/api/v1/events"
REFUSED = "machine_credential_refused"


class Box:
    def __init__(self, raw: str, credential_id: UUID, machine_id: str) -> None:
        self.raw = raw
        self.credential_id = credential_id
        self.machine_id = machine_id
        self.headers = {"Authorization": f"Bearer {raw}"}


async def _served_org() -> tuple[UUID, User]:
    tag = secrets.token_hex(4)
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Served Org {tag}",
            admin_email=f"served-{tag}@alkera.dev",
            admin_first_name="Served",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, _password = await make_member(session, org_id=org.id, verified=True)
    return org.id, member


async def _bound_chat(owner: User, machine_id: str | None) -> str:
    async with AsyncSessionLocal() as session:
        chat, _created = await chat_service.create_chat(
            session,
            owner=owner,
            org_id=owner.home_org_team_id,
            title="Quarterly plan",
            client_id=None,
            machine_id=machine_id,
            machine_status="ready" if machine_id else "none",
        )
        await session.commit()
        return str(chat.id)


async def _ring(
    chat_id: str,
    *,
    rebind: bool = False,
    machine: str | None = None,
    access_changed: bool = False,
) -> None:
    """Ring a chat's doorbell as the product does — after moving its binding
    to ``machine`` when ``rebind`` is set (``None`` unbinds it)."""
    async with AsyncSessionLocal() as session:
        if rebind:
            if machine is None:
                await session.execute(
                    text("UPDATE workspace_objects SET spec = spec - 'machine_id' WHERE id = :id"),
                    {"id": UUID(chat_id)},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                        "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                    ),
                    {"id": UUID(chat_id), "machine": machine},
                )
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        await session.refresh(chat)
        await chat_service.announce_chat(
            session, chat=chat, actor=None, access_changed=access_changed
        )
        await session.commit()


async def _emit(org_id: UUID, *, type_: EventType, entity_id: str, visibility: str = "org") -> int:
    async with AsyncSessionLocal() as session:
        row = await emit(
            session,
            org_id=org_id,
            type=type_,
            entity="kb_item",
            entity_id=entity_id,
            version=1,
            visibility=visibility,
            payload=None,
        )
        await session.commit()
        return int(row.id)


@pytest_asyncio.fixture
async def box(org_admin: OrgWithAdmin) -> AsyncIterator[tuple[Box, UUID, User]]:
    """A dedicated box serving a fresh org, with that org's member as the
    owner of the chats it will be bound to."""
    served_org, owner = await _served_org()
    made = await _box(org_admin, tenancy="dedicated", served_org=served_org)
    assert made.raw.startswith(MACHINE_TOKEN_PREFIX)
    yield Box(made.raw, made.credential_id, str(made.machine_id)), served_org, owner


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _ids(frames: list[dict[str, str]]) -> list[str]:
    return [frame["id"] for frame in frames if "id" in frame]


async def test_a_boxs_stream_carries_its_chats_frames_and_no_others(
    realtime_app: FastAPI, box: tuple[Box, UUID, User]
) -> None:
    """Open on the credential, the stream delivers the doorbell of a chat bound
    to the box — and of a chat that becomes bound while the stream is open,
    from the very announcement that binds it — and delivers neither another
    box's chat in the same org, a platform row, a row addressed to a person,
    nor anything from an org the credential does not serve."""
    the_box, served_org, owner = box
    mine = await _bound_chat(owner, the_box.machine_id)
    elsewhere = await _bound_chat(owner, str(uuid4()))
    later = await _bound_chat(owner, None)
    _foreign_org, stranger = await _served_org()
    foreign = await _bound_chat(stranger, the_box.machine_id)
    async with _client(realtime_app) as client:
        async with open_event_stream(realtime_app, client, headers=the_box.headers) as stream:
            await stream.opened()
            await _ring(elsewhere)
            await _emit(served_org, type_=EventType.KB_ITEM_CHANGED, entity_id="platform-row")
            await _emit(
                served_org,
                type_=EventType.CHAT_UPDATED,
                entity_id=mine,
                visibility="platform",
            )
            await _emit(
                served_org,
                type_=EventType.CHAT_UPDATED,
                entity_id=mine,
                visibility=f"user:{owner.id}",
            )
            await _ring(foreign)
            await _ring(mine)
            heard = await stream.wait_for_event(EventType.CHAT_UPDATED.value)
            assert mine in heard["data"]
            await _ring(later, rebind=True, machine=the_box.machine_id)
            bound = await stream.wait_for_event(EventType.CHAT_UPDATED.value)
            assert later in bound["data"], "the announcement that binds a chat reaches the box"
            await stream.quiet()
            named = [frame["data"] for frame in stream.events]
            assert len(named) == 2, stream.events
            assert not any(elsewhere in data or foreign in data for data in named)
            assert all(frame["event"] == EventType.CHAT_UPDATED.value for frame in stream.events)


async def test_a_box_hears_its_orgs_connection_traffic_only_where_it_holds_a_chat(
    realtime_app: FastAPI, box: tuple[Box, UUID, User]
) -> None:
    """Which connections a chat's members may use is org-wide, so the box hears
    a connection or membership move in an org it holds a chat in — and not in
    an org it serves but holds nothing in, nor in one it does not serve."""
    the_box, served_org, owner = box
    await _bound_chat(owner, the_box.machine_id)
    foreign_org, _stranger = await _served_org()
    async with _client(realtime_app) as client:
        async with open_event_stream(realtime_app, client, headers=the_box.headers) as stream:
            await stream.opened()
            await _emit(foreign_org, type_=EventType.TEAM_CONNECTION_UPDATED, entity_id="c-far")
            await _emit(served_org, type_=EventType.TEAM_CONNECTION_UPDATED, entity_id="c-near")
            heard = await stream.wait_for_event(EventType.TEAM_CONNECTION_UPDATED.value)
            assert "c-near" in heard["data"]
            await _emit(served_org, type_=EventType.MEMBERSHIP_CHANGED, entity_id="m-near")
            await stream.wait_for_event(EventType.MEMBERSHIP_CHANGED.value)
            await stream.quiet()
            assert not any("c-far" in frame["data"] for frame in stream.events)


async def test_a_boxs_reconnect_replays_only_its_own_chats_rows(
    realtime_app: FastAPI, box: tuple[Box, UUID, User]
) -> None:
    """The catch-up page is cut by the same predicate as the live frames: rows
    of the box's chats after its cursor come back, across the orgs it holds
    chats in; another box's chat and another org's rows do not."""
    the_box, served_org, owner = box
    mine = await _bound_chat(owner, the_box.machine_id)
    elsewhere = await _bound_chat(owner, str(uuid4()))
    cursor = await _emit(served_org, type_=EventType.KB_ITEM_CHANGED, entity_id="cursor")
    await _ring(elsewhere)
    await _ring(mine)
    await _emit(served_org, type_=EventType.KB_ITEM_CHANGED, entity_id="somebody-elses")
    async with _client(realtime_app) as client:
        async with open_event_stream(
            realtime_app, client, headers={**the_box.headers, "Last-Event-ID": str(cursor)}
        ) as stream:
            await stream.opened()
            await stream.wait_for_event(EventType.CHAT_UPDATED.value)
            await stream.quiet()
            replayed = [frame["data"] for frame in stream.events]
            assert len(replayed) == 1, stream.events
            assert mine in replayed[0]
            assert int(_ids(stream.events)[0]) > cursor


async def test_a_boxs_stream_is_counted_under_the_machine(
    realtime_app: FastAPI, box: tuple[Box, UUID, User]
) -> None:
    the_box, _served_org, owner = box
    await _bound_chat(owner, the_box.machine_id)
    gate = ConnectionGate.sse()
    async with _client(realtime_app) as client:
        async with open_event_stream(realtime_app, client, headers=the_box.headers) as stream:
            await stream.opened()
            assert gate.active_for(f"machine:{the_box.machine_id}") == 1
            assert gate.active_for_principal(f"machine:{the_box.machine_id}") == 1


@pytest.mark.parametrize("how", ["revoke", "release"])
async def test_a_box_whose_standing_ends_is_cut_within_one_keepalive_and_refused_after(
    realtime_app: FastAPI,
    box: tuple[Box, UUID, User],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """The keepalive re-asks the credential's standing: a revoke, or the
    machine leaving the plane, ends the stream with the error frame within one
    tick, and the reconnect is refused at the door with the credential's
    refusal."""
    from alkera_core.models.compute import ComputeAllocation

    monkeypatch.setattr(settings, "realtime_sse_keepalive_seconds", 0.2)
    the_box, _served_org, owner = box
    await _bound_chat(owner, the_box.machine_id)
    async with _client(realtime_app) as client:
        async with open_event_stream(realtime_app, client, headers=the_box.headers) as stream:
            await stream.opened()
            async with AsyncSessionLocal() as db:
                if how == "revoke":
                    credential = await db.get(MachineCredential, the_box.credential_id)
                    assert credential is not None
                    credential.revoked_at = datetime.now(UTC)
                else:
                    machine = await db.get(ComputeAllocation, UUID(the_box.machine_id))
                    assert machine is not None
                    machine.state = "released"
                await db.commit()
            await stream.wait_for(
                lambda s: any(f.get("event") == "error" for f in s.frames),
                what="the error frame that ends a box's stream",
            )
            ended = next(f for f in stream.frames if f.get("event") == "error")
            assert "unauthorized" in ended["data"]
        refused = await client.get(STREAM, headers=the_box.headers)
        assert refused.status_code == 401, refused.text
        assert refused.json()["error"]["code"] == REFUSED


async def test_an_unclaimed_credential_opens_no_stream(
    realtime_app: FastAPI, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A credential that has claimed no machine stands behind nothing: it is
    refused at the door like a revoked one."""
    from backend.services.credentials import machine_credentials as machine_credential_service
    from tests._compute_helpers import make_machine_type

    machine_type = await make_machine_type(real_session)
    _credential, raw = await machine_credential_service.mint(
        real_session,
        org_id=org_admin.org_id,
        created_by=org_admin.admin_id,
        machine_type=machine_type,
        tenancy="pool",
        label="unclaimed",
    )
    await real_session.commit()
    async with _client(realtime_app) as client:
        refused = await client.get(STREAM, headers={"Authorization": f"Bearer {raw}"})
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"]["code"] == REFUSED


async def test_a_box_gone_before_its_body_starts_gives_its_slot_back(
    realtime_app: FastAPI, box: tuple[Box, UUID, User], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box's stream takes its slot in the route, like a person's; a box
    that goes away while the request's session is still committing -- before
    the body runs at all -- gets the slot back instead of holding the
    machine's cap for the life of the process."""
    the_box, _served_org, owner = box
    await _bound_chat(owner, the_box.machine_id)
    gate = ConnectionGate.sse()
    key = f"machine:{the_box.machine_id}"
    async with _client(realtime_app) as client:
        async with parked_before_the_body(
            realtime_app, client, monkeypatch, headers=the_box.headers
        ) as task:
            assert gate.active_for(key) == 1, "the parked request holds its slot"
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await _until(
                lambda: gate.active_for(key) == 0,
                1.0,
                what="the cancelled request frees the machine's slot",
            )
