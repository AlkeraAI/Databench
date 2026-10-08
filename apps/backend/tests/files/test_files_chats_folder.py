"""A new chat's folder is filed under ``Chats`` in its owner's home.

Every test here creates the chat the way the product does — the chat route, or
the service the Slack mention uses — and then asks the Files routes where the
node ended up, because "where a chat lands" is a fact a client reads off the
drive and not an argument the bridge was handed.

``Chats`` is an ordinary folder: the member may rename it, move it, fill it or
throw it away, and the next chat then gets a fresh one rather than chasing the
old one around the drive. Chats already filed somewhere else stay where they
are; nothing here moves an existing node.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, delta_until, drive_root_etag
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.drives import CHATS_NAME, HOME_NAME
from alkera_core.files.ids import OrgScope
from alkera_core.files.repo import APP_ROLE, ORG_SETTING, FilesRepo
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from backend.services.chats import chat_service
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


# ---------------------------------------------------------------------------
# reading the tree the way a client does
# ---------------------------------------------------------------------------


async def _drive_and_home(client: AsyncClient) -> tuple[str, str]:
    response = await client.get("/api/v1/files/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["homeId"], "a caller's first drive read ensures their home"
    return str(body["id"]), str(body["homeId"])


async def _item(client: AsyncClient, drive_id: str, item_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/files/drives/{drive_id}/items/{item_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _children(client: AsyncClient, drive_id: str, item_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/files/drives/{drive_id}/items/{item_id}/children")
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()["value"]]


async def _new_chat(client: AsyncClient, title: str) -> str:
    response = await client.post(
        "/api/v1/chats", json={"title": title, "clientId": secrets.token_hex(8)}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _chat_node(client: AsyncClient, drive_id: str, chat_id: str) -> dict[str, Any]:
    """The chat's own node, found by the object it points at rather than by a
    path — the surfaces that reach a chat all hold its node id, and a test that
    walked a path to find it would prove only that the path exists."""
    node_id = await _node_id_of(uuid.UUID(chat_id))
    assert node_id is not None, "a chat created with Files on has a node"
    return await _item(client, drive_id, str(node_id))


async def _node_id_of(object_id: uuid.UUID) -> uuid.UUID | None:
    """The live node the object points at, read outside any request."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM file_nodes WHERE target_object_id = :object "
                    "AND trashed_at IS NULL AND subtype NOT LIKE '%:%'"
                ),
                {"object": object_id},
            )
        ).scalar_one_or_none()
    return uuid.UUID(str(row)) if row is not None else None


async def _live_children(
    session: AsyncSession, org_id: uuid.UUID, parent_id: uuid.UUID
) -> list[FileNode]:
    """Every live child of a node, read under the Files role the product uses."""
    repo = FilesRepo(session, OrgScope(org_team_id=org_id))
    await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": ORG_SETTING, "value": str(org_id)},
    )
    rows = (
        (
            await session.execute(
                repo.select_nodes().where(
                    FileNode.parent_id == parent_id, FileNode.trashed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    await session.execute(text("SET LOCAL ROLE NONE"))
    return list(rows)


# ---------------------------------------------------------------------------
# where a new chat lands
# ---------------------------------------------------------------------------


async def test_a_new_chat_is_filed_under_chats_in_its_owners_home(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """``home/<me>/Chats/<title>.alkerachat`` — asserted by walking up from the
    node the chat actually got, not by trusting the path the server printed."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Kickoff")

    node = await _chat_node(files_client, drive_id, chat_id)
    assert node["name"].endswith(".alkerachat")
    chats = await _item(files_client, drive_id, str(node["parentId"]))
    assert chats["name"] == "Chats"
    assert chats["kind"] == "folder"
    assert chats["parentId"] == home_id

    home = await _item(files_client, drive_id, home_id)
    container = await _item(files_client, drive_id, str(home["parentId"]))
    assert container["name"] == HOME_NAME.decode()


async def test_a_second_chat_reuses_the_one_chats_folder(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """Two chats, one folder: the second create finds the first's folder rather
    than making a second one beside it."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    first = await _chat_node(files_client, drive_id, await _new_chat(files_client, "One"))
    second = await _chat_node(files_client, drive_id, await _new_chat(files_client, "Two"))

    assert first["parentId"] == second["parentId"]
    under_home = await _live_children(real_session, files_org.org.org_id, uuid.UUID(home_id))
    assert [bytes(row.name) for row in under_home if bytes(row.name) == CHATS_NAME] == [CHATS_NAME]


async def test_a_member_gets_their_own_chats_folder(
    client: AsyncClient,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """A plain member is not the org admin and does not share her home: their
    chat is under a ``Chats`` folder in the home that is theirs."""
    await fx.drive()
    _, admin_home = await _drive_and_home(files_client)
    await _new_chat(files_client, "Admin's")

    member = await login(client, files_org.member.email, files_org.member_password)
    drive_id, home_id = await _drive_and_home(member)
    assert home_id != admin_home
    node = await _chat_node(member, drive_id, await _new_chat(member, "Member's"))
    chats = await _item(member, drive_id, str(node["parentId"]))
    assert chats["name"] == "Chats"
    assert chats["parentId"] == home_id


async def test_a_chat_started_outside_a_request_lands_there_too(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """The Slack mention opens a chat through ``chat_service.create_chat`` with
    no HTTP request around it. It is the same service the route calls, so the
    folder is the same one — the placement belongs to the create, not to the
    surface that asked for it."""
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    owner = (
        await real_session.execute(select(User).where(User.id == files_org.org.admin_id))
    ).scalar_one()
    chat, created = await chat_service.create_chat(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="From Slack",
        client_id=secrets.token_hex(8),
        machine_id=None,
        machine_status="none",
    )
    assert created
    await real_session.commit()

    node = await _chat_node(files_client, drive_id, str(chat.id))
    chats = await _item(files_client, drive_id, str(node["parentId"]))
    assert chats["name"] == "Chats"
    assert chats["parentId"] == home_id


async def test_a_saved_query_still_lands_directly_in_the_home(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """Only a chat is filed under ``Chats``. Another object type keeps the home
    it has always had — the negative case that would pass either way if the
    bridge had simply moved every object one level down."""
    from backend.services.objects import object_service

    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    owner = (
        await real_session.execute(select(User).where(User.id == files_org.org.admin_id))
    ).scalar_one()
    obj, created = await object_service.create_object(
        real_session,
        owner=owner,
        type="query",
        title="Revenue",
        spec={},
        org_id=owner.home_org_team_id,
    )
    assert created
    await real_session.commit()

    node_id = await _node_id_of(obj.id)
    assert node_id is not None
    node = await _item(files_client, drive_id, str(node_id))
    assert node["parentId"] == home_id


# ---------------------------------------------------------------------------
# a folder the member owns, and what happens when it is gone
# ---------------------------------------------------------------------------


async def test_the_chats_folder_is_an_ordinary_folder(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """No marker, no signpost, no flags of its own: the owner may create inside
    it and rename it through the same routes every other folder takes, and a
    write into it is not refused the way a write into ``home/`` is."""
    await fx.drive()
    drive_id, _ = await _drive_and_home(files_client)
    node = await _chat_node(files_client, drive_id, await _new_chat(files_client, "Kickoff"))
    chats_id = str(node["parentId"])
    assert (await _item(files_client, drive_id, chats_id))["name"] == "Chats"

    made = await files_client.post(
        f"/api/v1/files/drives/{drive_id}/items/{chats_id}/children",
        json={"name": "notes", "kind": "folder"},
        headers=idem(),
    )
    assert made.status_code == 201, made.text

    chats = await _item(files_client, drive_id, chats_id)
    renamed = await files_client.patch(
        f"/api/v1/files/drives/{drive_id}/items/{chats_id}",
        json={"name": "Conversations"},
        headers={"If-Match": str(chats["etag"]), **idem()},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Conversations"

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT traversal_only, flags FROM file_nodes WHERE id = :id"),
                {"id": uuid.UUID(chats_id)},
            )
        ).one()
    assert row.traversal_only is False
    assert row.flags == 0


@pytest.mark.parametrize(
    "gone",
    ["renamed", "moved", "trashed", "deleted"],
    ids=["renamed", "moved", "trashed", "deleted"],
)
async def test_a_chats_folder_that_is_gone_is_made_again(
    gone: str,
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """Renamed, moved away, thrown in the trash or deleted outright — in every
    case the next chat gets a fresh ``Chats`` directly under the home, and the
    chat that was already in the old folder stays in the old folder.

    Nothing is resurrected: the trashed folder stays trashed, and the renamed
    one keeps its new name.
    """
    await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    first = await _chat_node(files_client, drive_id, await _new_chat(files_client, "Before"))
    chats_id = str(first["parentId"])
    chats = await _item(files_client, drive_id, chats_id)
    headers = {"If-Match": str(chats["etag"]), **idem()}

    if gone == "renamed":
        moved = await files_client.patch(
            f"/api/v1/files/drives/{drive_id}/items/{chats_id}",
            json={"name": "Conversations"},
            headers=headers,
        )
        assert moved.status_code == 200, moved.text
    elif gone == "moved":
        holder = await files_client.post(
            f"/api/v1/files/drives/{drive_id}/items/{home_id}/children",
            json={"name": "Archive", "kind": "folder"},
            headers=idem(),
        )
        assert holder.status_code == 201, holder.text
        moved = await files_client.patch(
            f"/api/v1/files/drives/{drive_id}/items/{chats_id}",
            json={"parentId": str(holder.json()["id"])},
            headers=headers,
        )
        assert moved.status_code == 200, moved.text
    elif gone == "trashed":
        trashed = await files_client.delete(
            f"/api/v1/files/drives/{drive_id}/items/{chats_id}", headers=headers
        )
        assert trashed.status_code in (200, 202, 204), trashed.text
    else:
        trashed = await files_client.delete(
            f"/api/v1/files/drives/{drive_id}/items/{chats_id}", headers=headers
        )
        assert trashed.status_code in (200, 202, 204), trashed.text
        # Gone for good: emptying the trash purges the row, so the next chat
        # cannot find the folder even among the dead.
        emptied = await files_client.post(
            f"/api/v1/files/drives/{drive_id}/trash/empty",
            headers={"If-Match": await drive_root_etag(real_session, await fx.drive()), **idem()},
        )
        assert emptied.status_code in (200, 202, 204), emptied.text
        async with AsyncSessionLocal() as session:
            still = (
                await session.execute(
                    text("SELECT count(*) FROM file_nodes WHERE id = :id"),
                    {"id": uuid.UUID(chats_id)},
                )
            ).scalar_one()
        assert still == 0, "the purge is what makes this case different from `trashed`"

    second = await _chat_node(files_client, drive_id, await _new_chat(files_client, "After"))
    fresh_id = str(second["parentId"])
    assert fresh_id != chats_id, "the folder that is gone is not the one the new chat went into"
    fresh = await _item(files_client, drive_id, fresh_id)
    assert fresh["name"] == "Chats"
    assert fresh["parentId"] == home_id

    under_home = await _live_children(real_session, files_org.org.org_id, uuid.UUID(home_id))
    assert [row.id for row in under_home if bytes(row.name) == CHATS_NAME] == [
        uuid.UUID(fresh_id)
    ], "exactly one live Chats folder, and it is the new one"

    if gone in {"renamed", "moved"}:
        # The chats already filed in the old folder are still filed there.
        kept = await _item(files_client, drive_id, chats_id)
        assert kept["id"] == chats_id
        assert [row["id"] for row in await _children(files_client, drive_id, chats_id)] == [
            first["id"]
        ]


# ---------------------------------------------------------------------------
# what a syncing client is told
# ---------------------------------------------------------------------------


async def test_the_delta_feed_reports_the_chat_folder_under_chats(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    settle: Callable[[], Awaitable[None]],
) -> None:
    """A client that syncs by delta learns the new shape from the feed alone:
    the ``Chats`` folder arrives as a child of the home and the chat as a child
    of ``Chats`` — no listing walk, and no node reported under a parent the
    client has not been told about.

    The feed is read with ``delta_until``, not one page: it holds a committed
    row back while any older write transaction is open in this database, and
    not all of those are the test's (an autovacuum ANALYZE takes an xid), so a
    single page may correctly come back without the chat yet."""
    drive = await fx.drive()
    drive_id, home_id = await _drive_and_home(files_client)
    latest = await files_client.get(f"/api/v1/files/drives/{drive.id}/delta?token=latest")
    assert latest.status_code == 200, latest.text
    link = str(latest.json()["deltaLink"])

    chat_id = await _new_chat(files_client, "Kickoff")
    node = await _chat_node(files_client, drive_id, chat_id)

    await settle()
    delivered, _ = await delta_until(
        files_client, uuid.UUID(drive_id), carries=str(node["id"]), token=link
    )
    items = {str(row["id"]): row for row in delivered}
    chats_id = str(node["parentId"])
    assert items[str(node["id"])]["parentId"] == chats_id
    assert chats_id in items, "the folder the chat is reported under is reported too"
    assert items[chats_id]["name"] == "Chats"
    assert items[chats_id]["parentId"] == home_id


async def test_the_delta_feed_delivers_a_change_once_the_test_s_own_writer_settles(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
    settle: Callable[[], Awaitable[None]],
) -> None:
    """The feed's no-gaps rule withholds a row while an OLDER write transaction
    is still open in the same database, and the test process is such a writer.

    An uncommitted write on ``real_session`` takes an xid before the chat is
    created; on that page the change is correctly held back, which is the whole
    point of the rule. Once the sibling settles, the same read — no retry, no
    second token — delivers it. Take the ``settle`` away and the first half of
    this test is what the rest of the family sees.
    """
    await fx.drive()
    drive_id, _home_id = await _drive_and_home(files_client)
    latest = await files_client.get(f"/api/v1/files/drives/{drive_id}/delta?token=latest")
    assert latest.status_code == 200, latest.text
    link = str(latest.json()["deltaLink"])

    # A sibling write whose xid predates the change: the condition the feed
    # holds rows back for, and the one the fixtures must clear before a read.
    await real_session.execute(text("CREATE TEMP TABLE _delta_sibling (marker int)"))
    held = (
        await real_session.execute(text("SELECT pg_current_xact_id_if_assigned()::text"))
    ).scalar_one()
    assert held is not None, "the sibling session really is holding a write xid"

    chat_id = await _new_chat(files_client, "Held back")
    node = await _chat_node(files_client, drive_id, chat_id)

    withheld = await files_client.get(f"/api/v1/files/drives/{drive_id}/delta?token={link}")
    assert withheld.status_code == 200, withheld.text
    assert str(node["id"]) not in {str(row["id"]) for row in withheld.json()["items"]}, (
        "a row committed under an older open write must not be delivered — "
        "delivering it is the gap the feed exists to prevent"
    )

    await settle()
    delivered = await files_client.get(f"/api/v1/files/drives/{drive_id}/delta?token={link}")
    assert delivered.status_code == 200, delivered.text
    assert str(node["id"]) in {str(row["id"]) for row in delivered.json()["items"]}


# ---------------------------------------------------------------------------
# where a deleted chat's folder goes
# ---------------------------------------------------------------------------


async def _trash_page(client: AsyncClient, drive_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/files/drives/{drive_id}/trash")
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()["entries"]]


async def _node_row(node_id: uuid.UUID) -> Any:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT trashed_at, trash_op_id, target_object_id FROM file_nodes WHERE id = :n"
                ),
                {"n": node_id},
            )
        ).one()


async def test_a_deleted_chats_folder_is_in_the_trash_exactly_once(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """Deleting a chat puts its folder in the bin the member can actually see.

    The tombstone used to stamp ``trashed_at`` on the chat's node with a bare
    UPDATE and nothing else — no trash op, so the Trash listing (which is a
    join through ``file_trash_ops``) could never show it, no ``purge_after``, so
    the retention window never started and the sweeper never claimed it, and
    nothing below the root was swept, so every file the conversation held stayed
    live under a trashed parent. The folder was invisible AND immortal.

    One entry, carrying the chat's own title, with a window on it.
    """
    await fx.drive()
    drive_id, _ = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Kickoff")
    node = await _chat_node(files_client, drive_id, chat_id)
    node_id = uuid.UUID(str(node["id"]))

    deleted = await files_client.delete(f"/api/v1/chats/{chat_id}")
    assert deleted.status_code == 204, deleted.text

    page = await _trash_page(files_client, drive_id)
    entries = [row for row in page if row["item"]["id"] == str(node_id)]
    assert len(entries) == 1, page
    assert entries[0]["item"]["name"] == node["name"]
    assert entries[0]["item"]["name"].startswith("Kickoff")
    assert entries[0]["purgeAfter"] is not None

    row = await _node_row(node_id)
    assert row.trashed_at is not None
    assert row.trash_op_id is not None, "the listing is a join through the trash op"


async def test_a_deleted_chats_folder_restores_as_a_plain_folder(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """Restore brings the tree back, and what comes back is an ordinary folder.

    The chat itself is a tombstone and is not coming back, so the node must not
    keep pointing at it: a restored folder that still named a deleted chat would
    be a door onto rows the product has already retired, and the Files page
    would render a conversation nobody can open.
    """
    await fx.drive()
    drive_id, _ = await _drive_and_home(files_client)
    chat_id = await _new_chat(files_client, "Kickoff")
    node = await _chat_node(files_client, drive_id, chat_id)
    node_id = uuid.UUID(str(node["id"]))
    parent_id = str(node["parentId"])

    deleted = await files_client.delete(f"/api/v1/chats/{chat_id}")
    assert deleted.status_code == 204, deleted.text
    entry = next(
        row
        for row in await _trash_page(files_client, drive_id)
        if row["item"]["id"] == str(node_id)
    )

    restored = await files_client.post(
        f"/api/v1/files/drives/{drive_id}/trash/{entry['trashOpId']}/restore",
        json={},
        headers={"Idempotency-Key": secrets.token_hex(8), "If-Match": "1"},
    )
    assert restored.status_code == 200, restored.text

    row = await _node_row(node_id)
    assert row.trashed_at is None
    assert row.trash_op_id is None
    assert row.target_object_id is None, "a deleted chat's folder comes back as a folder"
    back = await _item(files_client, drive_id, str(node_id))
    assert back["kind"] == "folder"
    assert back["parentId"] == parent_id
    assert not [
        row
        for row in await _trash_page(files_client, drive_id)
        if row["item"]["id"] == str(node_id)
    ]
