"""The batched item read a folder drop finishes with.

Through the real app, real Postgres and a filesystem store: the page must be
the same items the single read renders, in the order asked, silent about the
ids the caller may not see, and it must cost statements per PAGE rather than
per id -- the reason it exists.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from _files_kit import NOT_FOUND, counting, refusal
from backend.api.routes.files.items import MAX_LOOKUP_IDS
from httpx import AsyncClient
from tests.conftest import login

if TYPE_CHECKING:
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


async def _drive(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _lookup(client: AsyncClient, drive: str, ids: list[str]) -> tuple[int, dict]:
    response = await client.post(f"{BASE}/drives/{drive}/items/lookup", json={"ids": ids})
    return response.status_code, response.json()


async def test_the_page_is_the_single_reads_items_in_the_order_asked(
    files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """Etag, name, parent, path and star: what the single read says, byte for
    byte, and in the caller's order rather than the database's."""
    drive = await _drive(files_client)
    shared = await fx.shared()
    files = [await fx.node(f"drop-{n}.txt".encode(), parent=shared) for n in range(5)]
    singles = {}
    for node in files:
        one = await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")
        assert one.status_code == 200, one.text
        singles[str(node.id)] = one.json()
    asked = [str(files[3].id), str(files[0].id), str(files[4].id)]
    status, page = await _lookup(files_client, drive, asked)
    assert status == 200, page
    assert [item["id"] for item in page["value"]] == asked
    for item in page["value"]:
        assert item == singles[item["id"]]


async def test_an_id_the_caller_may_not_see_is_left_out_without_a_trace(
    files_client: AsyncClient,
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """A member asks for a file in the admin's home beside one they may read:
    the page is exactly the readable one, identical to a page that never named
    the other -- an unreadable id and an invented id answer the same."""
    drive = await _drive(files_client)
    shared = await fx.shared()
    readable = await fx.node(b"everyone.txt", parent=shared)
    home_id = uuid.UUID(str((await files_client.get(f"{BASE}/drives")).json()["homeId"]))
    private = await fx.node(b"mine.txt", parent=await fx.folder(home_id))
    member = await login(client, files_org.member.email, files_org.member_password)
    with_private = await _lookup(member, drive, [str(readable.id), str(private.id)])
    with_invented = await _lookup(member, drive, [str(readable.id), str(uuid.uuid4())])
    alone = await _lookup(member, drive, [str(readable.id)])
    assert with_private == with_invented == alone
    assert [item["id"] for item in alone[1]["value"]] == [str(readable.id)]


@pytest.mark.parametrize(
    "named",
    [
        pytest.param(str(uuid.uuid4()), id="a-drive-that-is-not-this-orgs"),
        pytest.param("not-a-drive", id="not-an-id-this-server-issues"),
    ],
)
async def test_a_drive_that_is_not_this_orgs_is_simply_not_there(
    files_client: AsyncClient, fx: FilesFixtures, named: str
) -> None:
    """The opaque 404 every Files route answers, and it is decided before the
    body is looked at: a stranger sending nonsense must not be told which of
    the two was wrong, or the 422 becomes the oracle the 404 denies."""
    node = await fx.node(b"here.txt", parent=await fx.shared())
    refused = await files_client.post(
        f"{BASE}/drives/{named}/items/lookup", json={"ids": [str(node.id)]}
    )
    assert refused.status_code == 404, refused.text
    assert refusal(refused) == NOT_FOUND
    # The same drive id with a body the schema refuses answers identically, so
    # there is nothing to compare between the two requests.
    malformed = await files_client.post(f"{BASE}/drives/{named}/items/lookup", json={"ids": []})
    assert (malformed.status_code, refusal(malformed)) == (404, NOT_FOUND)

    status, page = await _lookup(files_client, await _drive(files_client), [str(node.id)])
    assert status == 200
    assert [item["id"] for item in page["value"]] == [str(node.id)]


@pytest.mark.parametrize(
    ("ids", "status"),
    [
        pytest.param([], 422, id="no-ids"),
        pytest.param([str(uuid.uuid4())] * (MAX_LOOKUP_IDS + 1), 422, id="one-over-the-cap"),
        pytest.param(["not-an-id"], 422, id="not-an-id"),
        pytest.param([str(uuid.uuid4())] * MAX_LOOKUP_IDS, 200, id="at-the-cap"),
    ],
)
async def test_the_body_is_bounded_and_typed(
    files_client: AsyncClient, ids: list[str], status: int
) -> None:
    drive = await _drive(files_client)
    got, _ = await _lookup(files_client, drive, ids)
    assert got == status


async def test_a_duplicated_id_is_answered_once(
    files_client: AsyncClient, fx: FilesFixtures
) -> None:
    drive = await _drive(files_client)
    node = await fx.node(b"twice.txt", parent=await fx.shared())
    _, page = await _lookup(files_client, drive, [str(node.id), str(node.id)])
    assert [item["id"] for item in page["value"]] == [str(node.id)]


async def test_the_page_costs_statements_per_page_not_per_id(
    files_client: AsyncClient, fx: FilesFixtures
) -> None:
    """Forty ids in one folder cost the same statements as one: a read that
    scaled with the ids would just be the five hundred GETs in one envelope."""
    drive = await _drive(files_client)
    shared = await fx.shared()
    nodes = [await fx.node(f"cost-{n}.txt".encode(), parent=shared) for n in range(40)]
    with counting() as one:
        status, _ = await _lookup(files_client, drive, [str(nodes[0].id)])
        assert status == 200
    with counting() as forty:
        status, page = await _lookup(files_client, drive, [str(node.id) for node in nodes])
        assert status == 200
    assert len(page["value"]) == 40
    assert len(forty) == len(one), f"{len(one)} statements for one id, {len(forty)} for forty"
