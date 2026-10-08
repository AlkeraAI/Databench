"""A pool box learns nothing about a drive in an org where it holds no work.

A pool box may be placed in any org, so its credential "serves" every org.
That is a ceiling, not standing: the drive-scoped listings (delta's latest
token, my leases, trash, conflicts) used to answer such a box an empty page
for any org's real drive and the opaque 404 for a made-up id, which told it
which drive ids exist. The box now stands in an org only while it holds work
there (a chat bound to it, a workspace it holds) or is finishing on a live
lease it holds, and every route answers a drive outside that the way it
answers a drive that does not exist.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, FilesOrgFixture, refusal
from _live_holder import MockHolder
from alkera_core.compute.workspace_lease import holds_work_in
from alkera_core.db.session import AsyncSessionLocal
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin
from tests.files import test_files_machine_credential as machine_credential_tests
from tests.files.test_files_machine_credential import (
    BASE,
    Box,
    MemberChat,
    _bind,
)
from tests.test_machine_principal_routes import _box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

#: The member's private chat those tests make, as a fixture of this module too.
members_chat = machine_credential_tests.members_chat

#: The drive-scoped listings a box can name any drive id on.
LISTINGS = [
    pytest.param("/delta?token=latest", id="delta-latest"),
    pytest.param("/leases?mine=true", id="my-leases"),
    pytest.param("/trash", id="trash"),
    pytest.param("/conflicts", id="conflicts"),
]


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


@pytest_asyncio.fixture
async def pool_box(platform_admin: OrgWithAdmin) -> AsyncIterator[Box]:
    """A pool box the platform minted: placeable in any org, holding nothing."""
    made = await _box(platform_admin, tenancy="pool")
    box = Box(made.raw, made.credential_id, str(made.machine_id))
    yield box
    await box.client.aclose()


async def _get(client: AsyncClient, drive: uuid.UUID, listing: str) -> tuple[int, str]:
    response = await client.get(f"{BASE}/drives/{drive}{listing}")
    return response.status_code, refusal(response) if response.status_code == 404 else ""


async def _release_leases(machine_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE file_leases SET released_at = now() "
                "WHERE holder_principal_id = :machine AND released_at IS NULL"
            ),
            {"machine": uuid.UUID(machine_id)},
        )
        await session.commit()


@pytest.mark.parametrize("listing", LISTINGS)
async def test_a_pool_box_holding_nothing_in_an_org_cannot_tell_its_drive_exists(
    pool_box: Box, members_chat: MemberChat, listing: str
) -> None:
    """The org's real drive and a made-up id are one answer, the opaque 404."""
    real = await _get(pool_box.client, members_chat.drive, listing)
    made_up = await _get(pool_box.client, uuid.uuid4(), listing)

    assert made_up == (404, NOT_FOUND)
    assert real == made_up


async def test_a_pool_box_holding_nothing_in_an_org_cannot_address_a_node_on_its_drive(
    pool_box: Box, members_chat: MemberChat
) -> None:
    """The item doors go through the same admission as the listings."""
    response = await pool_box.client.get(members_chat.item)

    assert response.status_code == 404, response.text
    assert refusal(response) == NOT_FOUND


@pytest.mark.parametrize("listing", LISTINGS)
async def test_a_pool_box_with_a_chat_bound_in_the_org_is_answered(
    pool_box: Box, members_chat: MemberChat, listing: str
) -> None:
    await _bind(members_chat.chat_id, pool_box.machine_id)

    status, _ = await _get(pool_box.client, members_chat.drive, listing)

    assert status == 200


@pytest.mark.parametrize("listing", LISTINGS)
async def test_a_pool_box_finishing_on_its_lease_after_the_chat_moved_stands_until_it_lets_go(
    pool_box: Box,
    members_chat: MemberChat,
    real_session: AsyncSession,
    listing: str,
) -> None:
    """The chat moved to another box while this one held its folder: the box
    may still finish its hand-back, so it keeps standing in the org exactly as
    long as its lease is live, and loses it when the lease ends."""
    await _bind(members_chat.chat_id, pool_box.machine_id)
    holder = MockHolder(pool_box.client, members_chat.drive, members_chat.node.id)
    taken = await holder.take(real_session, _idem, purpose="chat")
    assert taken.status_code == 200, taken.text
    await _bind(members_chat.chat_id, str(uuid.uuid4()))

    during, _ = await _get(pool_box.client, members_chat.drive, listing)
    await _release_leases(pool_box.machine_id)
    after = await _get(pool_box.client, members_chat.drive, listing)

    assert during == 200
    assert after == (404, NOT_FOUND)


async def test_a_dedicated_box_still_reaches_the_drive_of_the_org_it_serves(
    platform_admin: OrgWithAdmin, files_org: FilesOrgFixture, members_chat: MemberChat
) -> None:
    """A dedicated box's assignment is its standing: the listings answer it
    with no chat bound, as before."""
    made = await _box(platform_admin, tenancy="dedicated", served_org=files_org.org.org_id)
    box = Box(made.raw, made.credential_id, str(made.machine_id))
    try:
        status, _ = await _get(box.client, members_chat.drive, "/trash")
    finally:
        await box.client.aclose()

    assert status == 200


# --------------------------------------------------------------------------- #
# holds_work_in, the rule the drive admission reads
# --------------------------------------------------------------------------- #


async def _lease(
    session: AsyncSession, chat: MemberChat, *, holder: str, released: bool, expired: bool
) -> None:
    await session.execute(
        text(
            "UPDATE file_leases SET holder_principal_id = :holder, "
            "released_at = CASE WHEN :released THEN now() ELSE NULL END, "
            "expires_at = CASE WHEN :expired THEN now() - interval '1 second' "
            "ELSE now() + interval '5 minutes' END "
            "WHERE node_id = :node"
        ),
        {
            "holder": uuid.UUID(holder),
            "released": released,
            "expired": expired,
            "node": chat.node.id,
        },
    )
    await session.commit()


@pytest.mark.parametrize(
    ("same_box", "released", "expired", "stands"),
    [
        pytest.param(True, False, False, True, id="its-live-lease"),
        pytest.param(True, True, False, False, id="its-released-lease"),
        pytest.param(True, False, True, False, id="its-lapsed-lease"),
        pytest.param(False, False, False, False, id="another-boxs-live-lease"),
    ],
)
async def test_only_a_live_lease_the_box_holds_gives_it_standing_without_a_chat(
    pool_box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    same_box: bool,
    released: bool,
    expired: bool,
    stands: bool,
) -> None:
    await _bind(members_chat.chat_id, pool_box.machine_id)
    holder = MockHolder(pool_box.client, members_chat.drive, members_chat.node.id)
    taken = await holder.take(real_session, _idem, purpose="chat")
    assert taken.status_code == 200, taken.text
    await _bind(members_chat.chat_id, None)
    async with AsyncSessionLocal() as session:
        await _lease(
            session,
            members_chat,
            holder=pool_box.machine_id if same_box else str(uuid.uuid4()),
            released=released,
            expired=expired,
        )
        found = await holds_work_in(
            session, machine_id=pool_box.machine_id, org_id=files_org.org.org_id
        )

    assert found is stands


async def test_a_box_stands_in_no_other_org_for_the_work_it_holds_in_one(
    pool_box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    await _bind(members_chat.chat_id, pool_box.machine_id)
    async with AsyncSessionLocal() as session:
        here = await holds_work_in(
            session, machine_id=pool_box.machine_id, org_id=files_org.org.org_id
        )
        elsewhere = await holds_work_in(
            session, machine_id=pool_box.machine_id, org_id=uuid.uuid4()
        )

    assert (here, elsewhere) == (True, False)


async def test_a_machine_id_that_is_not_a_uuid_holds_nothing() -> None:
    async with AsyncSessionLocal() as session:
        assert await holds_work_in(session, machine_id="box-7", org_id=uuid.uuid4()) is False
