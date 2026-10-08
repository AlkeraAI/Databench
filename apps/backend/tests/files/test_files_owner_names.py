"""A row's Owner is a person's name, resolved once for the whole page.

``attrs.owner`` is ``file_nodes.created_by`` — a uuid, which is not a thing a
person can read, and every row of the browser's listing showed one. ``ownerName``
is the label beside it. Two things are pinned here: that the label is the right
one for each of the three cases (a named user, a user who has not named
themselves, and a node the server made), and that a page of rows costs the same
number of statements as a page of one — a name per row would be a statement per
row, which is exactly the cost the lease and star lookups are batched to avoid.
"""

from __future__ import annotations

import uuid

import pytest
from _oracle import probe, quiesce_auth
from alkera_core.db.session import engine
from alkera_core.models.user import User
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: The app and the fixtures share one process-wide async engine; its sync
#: facade is what the statement listener binds to.
SYNC_ENGINE = engine.sync_engine


async def _named(session: AsyncSession, user_id: uuid.UUID, first: str, last: str) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(first_name=first, last_name=last)
    )
    await session.commit()


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def test_a_listing_names_every_owner_it_has_one_for(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """The three cases, on one page: a named user, an unnamed one, and no owner."""
    admin_id = fx.actor_id
    member = files_org.member
    await _named(real_session, admin_id, "Ada", "Lovelace")
    await _named(real_session, member.id, "", "")

    folder = await fx.node(b"owners", kind="folder")
    await fx.node(b"by-admin.txt", parent=folder, created_by=admin_id)
    await fx.node(b"by-member.txt", parent=folder, created_by=member.id)
    await fx.node(b"by-nobody.txt", parent=folder)

    drive = await _drive_id(files_client)
    page = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")
    assert page.status_code == 200, page.text
    rows = {row["name"]: row for row in page.json()["value"]}

    assert rows["by-admin.txt"]["ownerName"] == "Ada Lovelace"
    # No name of their own: the email is what a colleague can recognise them by.
    assert rows["by-member.txt"]["ownerName"] == member.email
    # A node the server made has no owner to name, and the wire says so rather
    # than inventing one.
    assert rows["by-nobody.txt"]["ownerName"] is None
    # The identifier is untouched: the name is only ever the label beside it.
    assert rows["by-admin.txt"]["attrs"]["owner"] == str(admin_id)


async def test_a_single_item_read_names_its_owner_too(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """The detail pane draws from the same field the listing does."""
    await _named(real_session, fx.actor_id, "Grace", "Hopper")
    node = await fx.node(b"detail.txt", created_by=fx.actor_id)
    drive = await _drive_id(files_client)
    response = await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")
    assert response.status_code == 200, response.text
    assert response.json()["ownerName"] == "Grace Hopper"


async def test_a_page_of_rows_costs_no_more_owner_statements_than_a_page_of_one(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
) -> None:
    """One statement resolves every owner on the page, not one per row.

    The two folders differ only in how many rows they hold, and their owners are
    two distinct users, so a per-row lookup would show up here as a difference
    the batched read does not have.
    """
    member = files_org.member
    await _named(real_session, fx.actor_id, "Ada", "Lovelace")
    await _named(real_session, member.id, "Alan", "Turing")

    small = await fx.node(b"small", kind="folder")
    await fx.node(b"one.txt", parent=small, created_by=fx.actor_id)
    large = await fx.node(b"large", kind="folder")
    for index in range(8):
        owner = fx.actor_id if index % 2 else member.id
        await fx.node(f"row{index}.txt".encode(), parent=large, created_by=owner)

    drive = await _drive_id(files_client)

    async def count(folder_id: uuid.UUID) -> int:
        url = f"{BASE}/drives/{drive}/items/{folder_id}/children"
        # Warm the per-connection and per-session caches first: the first request
        # of a process pays for lookups every later one has cached.
        await files_client.get(url)
        # …and start both measurements from the same revocation answer, which
        # ages by the wall clock rather than by anything the page did: see
        # ``quiesce_auth``. Without it a slow runner puts the window between the
        # warm-up and the probe on one side only, and the extra re-check is
        # reported as a page of eight costing more than a page of one.
        quiesce_auth()
        observed = await probe(files_client, SYNC_ENGINE, "GET", url)
        assert observed.status == 200
        return observed.statements

    one = await count(small.id)
    eight = await count(large.id)
    assert one == eight, f"a page of 1 cost {one} statements, a page of 8 cost {eight}"
