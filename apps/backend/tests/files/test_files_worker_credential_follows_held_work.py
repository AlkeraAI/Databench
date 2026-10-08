"""An org-bound worker credential opens nothing once its box holds no work in the org.

A pool box mints a worker credential for an org only while it holds work
there. The credential is signed for 15 minutes, and the door used to ask
only that the machine credential behind it still stands and may serve the
org, so a box that lost the org's last chat kept a working credential for
the rest of that time. The door now asks the hold again on every request
and every socket keepalive, by the rule the drive admission reads
(``holds_work_in``): a chat of the org bound to the box, a workspace it
holds, or a live lease it is finishing on.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from _files_kit import FilesOrgFixture
from _live_holder import MockHolder
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.machine_refusals import MACHINE_CREDENTIAL_REFUSED
from backend.auth.dependencies import machine_context_if_standing
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client
from tests.files import test_files_machine_credential as machine_credential_tests
from tests.files.test_files_box_lease_follows_binding import _release
from tests.files.test_files_machine_credential import MemberChat, _bind
from tests.test_machine_principal_routes import Box, _box, _chat, _error

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

#: The member's private chat those tests make, as a fixture of this module too.
members_chat = machine_credential_tests.members_chat

MINT = "/api/v1/machines/me/worker-credentials"
LISTING = "/api/v1/chats"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


@pytest_asyncio.fixture
async def pool_box(platform_admin: OrgWithAdmin) -> Box:
    return await _box(platform_admin, tenancy="pool")


@pytest_asyncio.fixture
async def http() -> AsyncIterator[AsyncClient]:
    async with app_client() as client:
        yield client


async def _worker(http: AsyncClient, box: Box, org_id: uuid.UUID) -> dict[str, str]:
    minted = await http.post(MINT, json={"org_id": str(org_id)}, headers=box.headers)
    assert minted.status_code == 201, minted.text
    return {"Authorization": f"Bearer {minted.json()['token']}"}


def _code(response: Response) -> str:
    return str(_error(response)["code"])


async def _socket_rebuilds(box: Box, org_id: uuid.UUID) -> bool:
    """Whether a socket the worker holds on a ticket survives its keepalive."""
    async with AsyncSessionLocal() as session:
        ctx = await machine_context_if_standing(
            session,
            credential_id=box.credential_id,
            machine_id=box.machine_id,
            org_id=org_id,
            org_bound=True,
        )
    return ctx is not None


async def _delete_chat(chat_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE workspace_objects SET deleted_at = 1 WHERE id = :id"),
            {"id": uuid.UUID(chat_id)},
        )
        await session.commit()


async def _lose(chat_id: str, how: str) -> None:
    if how == "moved":
        await _bind(chat_id, str(uuid.uuid4()))
    elif how == "unbound":
        await _bind(chat_id, None)
    else:
        await _delete_chat(chat_id)


@pytest.mark.parametrize("how", ["moved", "unbound", "deleted"])
async def test_a_worker_credential_stops_when_the_box_loses_the_orgs_last_chat(
    http: AsyncClient,
    pool_box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    how: str,
) -> None:
    org_id = files_org.org.org_id
    await _bind(members_chat.chat_id, str(pool_box.machine_id))
    worker = await _worker(http, pool_box, org_id)
    before = await http.get(LISTING, headers=worker)
    assert before.status_code == 200, before.text
    assert await _socket_rebuilds(pool_box, org_id)

    await _lose(members_chat.chat_id, how)
    after = await http.get(LISTING, headers=worker)

    assert after.status_code == 401, after.text
    assert _code(after) == MACHINE_CREDENTIAL_REFUSED
    assert not await _socket_rebuilds(pool_box, org_id)


async def test_a_worker_keeps_its_org_while_another_chat_of_it_stays_on_the_box(
    http: AsyncClient, pool_box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    org_id = files_org.org.org_id
    await _bind(members_chat.chat_id, str(pool_box.machine_id))
    staying = await _chat(files_org.member, machine_id=pool_box.machine_id)
    worker = await _worker(http, pool_box, org_id)

    await _bind(members_chat.chat_id, str(uuid.uuid4()))
    listed = await http.get(LISTING, headers=worker)

    assert listed.status_code == 200, listed.text
    assert [chat["id"] for chat in listed.json()["items"]] == [staying]


async def test_a_worker_finishing_on_its_lease_keeps_its_org_until_the_lease_ends(
    http: AsyncClient,
    pool_box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The chat moved while the worker held its folder: the worker may finish
    the hand-back for as long as the lease lives, and no longer."""
    org_id = files_org.org.org_id
    await _bind(members_chat.chat_id, str(pool_box.machine_id))
    worker = await _worker(http, pool_box, org_id)
    async with app_client() as on_worker:
        on_worker.headers.update(worker)
        holder = MockHolder(on_worker, members_chat.drive, members_chat.node.id)
        taken = await holder.take(real_session, _idem, purpose="chat")
        assert taken.status_code == 200, taken.text
        await _bind(members_chat.chat_id, str(uuid.uuid4()))

        during = await on_worker.get(LISTING)
        released = await _release(on_worker, holder, real_session)
        assert released.status_code < 300, released.text
        after = await on_worker.get(LISTING)

    assert during.status_code == 200, during.text
    assert after.status_code == 401, after.text


async def test_a_box_mid_hand_back_mints_a_worker_and_the_door_admits_it(
    http: AsyncClient,
    pool_box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The mint and the door are one rule. The chat moved off the box while
    it held the folder's lease, and its worker credential ran out (a box
    restarts, or the 15 minutes pass) before the hand-back finished: the box
    mints a fresh worker for the org, and the door admits it, for as long as
    the lease lives. Once the lease is released both refuse."""
    org_id = files_org.org.org_id
    await _bind(members_chat.chat_id, str(pool_box.machine_id))
    first = await _worker(http, pool_box, org_id)
    async with app_client() as on_worker:
        on_worker.headers.update(first)
        holder = MockHolder(on_worker, members_chat.drive, members_chat.node.id)
        taken = await holder.take(real_session, _idem, purpose="chat")
        assert taken.status_code == 200, taken.text
        await _bind(members_chat.chat_id, str(uuid.uuid4()))

        reminted = await http.post(MINT, json={"org_id": str(org_id)}, headers=pool_box.headers)
        assert reminted.status_code == 201, reminted.text
        second = {"Authorization": f"Bearer {reminted.json()['token']}"}
        during = await http.get(LISTING, headers=second)

        released = await _release(on_worker, holder, real_session)
        assert released.status_code < 300, released.text

    refused = await http.post(MINT, json={"org_id": str(org_id)}, headers=pool_box.headers)
    after = await http.get(LISTING, headers=second)
    assert during.status_code == 200, during.text
    assert refused.status_code == 404, refused.text
    assert after.status_code == 401, after.text
