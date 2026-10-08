"""Dropping something onto a chat files it where the chat's agent runs.

"Put this in the chat" means "give it to the conversation", and a file the
conversation cannot see was not given to it. So a move or a copy aimed at a chat
resolves to the chat's SANDBOX — the one directory the agent runs in, writes to
and reads from — rather than to whichever folder a client happened to name.

``CHAT_SANDBOX_FOLDER`` is the single place that decides which directory that
is: it is the chat folder itself today, and naming a child there re-points every
case below at that child with no other edit. That property — one constant, and
the browser, the fence and the agent's tool binding follow it — is what these
tests pin, which is why each one computes its expected destination from the
constant instead of spelling a folder name.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from _files_kit import delta_until
from alkera_core.authz.principal import ActingContext
from alkera_core.files import acl
from alkera_core.files import namespace as namespace_module
from alkera_core.files.authz.grants import Principal
from alkera_core.files.ids import NodeId
from alkera_core.files.objects_bridge import CHAT_FOLDER_CHILDREN, CHAT_SANDBOX_FOLDER
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: The name of the child a drop lands in, or ``None`` when the sandbox IS the
#: chat folder and a drop therefore lands at the chat's own top level.
SANDBOX_NAME = None if CHAT_SANDBOX_FOLDER is None else CHAT_SANDBOX_FOLDER.decode("utf-8")


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _chat_node(client: AsyncClient, title: str = "Quarterly review") -> str:
    """A chat created through the real route, as its Files node id."""
    response = await client.post("/api/v1/chats", json={"title": title})
    assert response.status_code == 201, response.text
    node_id = response.json()["files_node_id"]
    assert node_id, "a chat created with Files on names its node"
    return str(node_id)


def _etag(item: dict[str, Any]) -> dict[str, str]:
    return {"If-Match": str(item["etag"])}


async def _item(client: AsyncClient, drive: str, node_id: str) -> dict[str, Any]:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _rows(client: AsyncClient, drive: str, node_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}/children")
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()["value"]]


async def _names(client: AsyncClient, drive: str, node_id: str) -> set[str]:
    return {str(row["name"]) for row in await _rows(client, drive, node_id)}


async def _sandbox_of(client: AsyncClient, drive: str, chat: str) -> str:
    """The node id a drop onto ``chat`` must end up under."""
    if SANDBOX_NAME is None:
        return chat
    rows = {str(row["name"]): row for row in await _rows(client, drive, chat)}
    assert SANDBOX_NAME in rows, sorted(rows)
    return str(rows[SANDBOX_NAME]["id"])


async def _home(client: AsyncClient, fx: FilesFixtures) -> FileNode:
    """The caller's home, where a node they OWN is seeded.

    A move that would hand the mover a rung they do not hold on the source is
    the owner's verb, so what a person drops onto their chat is theirs to begin
    with: a stray under the drive root, where an org admin is only floored at
    manager, is not a stand-in for it any more.
    """
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return await fx.folder(uuid.UUID(str(response.json()["homeId"])))


async def _move(
    client: AsyncClient,
    drive: str,
    node_id: str,
    parent_id: str,
    idem: Callable[[], dict[str, str]],
    *,
    conflict: str | None = None,
) -> Any:
    url = f"{BASE}/drives/{drive}/items/{node_id}"
    if conflict is not None:
        url = f"{url}?conflict_behavior={conflict}"
    fetched = await _item(client, drive, node_id)
    return await client.patch(
        url, json={"parentId": parent_id}, headers={**idem(), **_etag(fetched)}
    )


async def test_a_new_chat_holds_no_second_place_for_what_a_person_hands_it(
    files_client: AsyncClient, files_on: None
) -> None:
    """One question, one answer.

    A chat used to be created holding an ``attachments/`` folder as well as the
    sandbox the agent actually reads, so a file could be filed in a place
    nothing ever opened while the person who filed it believed they had handed
    it over. The folders a chat is born with are now the deliverable folder and
    the working one, and the agent's sandbox is where a hand-off lands.
    """
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)

    listed = await _names(files_client, drive, chat)

    assert {name.decode("utf-8") for name in CHAT_FOLDER_CHILDREN} <= listed, listed
    assert "attachments" not in listed, listed


@pytest.mark.parametrize("kind", ["file", "folder"])
async def test_a_node_moved_onto_a_chat_lands_in_the_directory_the_agent_runs_in(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    kind: str,
) -> None:
    """The caller names the chat; the answer names where the node really is.

    A file and a folder take the same path through the move, so both are here:
    the destination is decided by what is being written INTO, never by what is
    being dropped.
    """
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    sandbox = await _sandbox_of(files_client, drive, chat)
    dropped = await fx.node(
        b"budget.csv" if kind == "file" else b"evidence",
        kind=kind,
        parent=await _home(files_client, fx),
    )

    response = await _move(files_client, drive, str(dropped.id), chat, idem)

    assert response.status_code == 200, response.text
    assert response.json()["parentId"] == sandbox, (
        "the move must answer with the folder the node is really in"
    )
    assert dropped.name.decode("utf-8") in await _names(files_client, drive, sandbox)


async def test_a_name_already_taken_in_the_sandbox_is_renamed_not_refused(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A drop onto a chat keeps the ordinary conflict handling of a folder.

    Dropping a second ``notes.md`` onto a chat is what a person does in any
    folder, so the answer is the renamed node — never a collision reported
    against a folder the caller never named.
    """
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    sandbox = await _sandbox_of(files_client, drive, chat)

    home = await _home(files_client, fx)
    first = await fx.node(b"notes.md", parent=home)
    moved = await _move(files_client, drive, str(first.id), chat, idem)
    assert moved.status_code == 200, moved.text

    inbox = await fx.node(b"inbox", kind="folder", parent=home)
    second = await fx.node(b"notes.md", parent=inbox)
    again = await _move(files_client, drive, str(second.id), chat, idem, conflict="rename")

    assert again.status_code == 200, again.text
    assert again.json()["parentId"] == sandbox
    landed = {str(row["id"]): str(row["name"]) for row in await _rows(files_client, drive, sandbox)}
    assert landed[str(first.id)] == "notes.md"
    assert landed[str(second.id)] != "notes.md", landed


@pytest.mark.skipif(
    CHAT_SANDBOX_FOLDER is None,
    reason="the sandbox is the chat folder itself, so there is no child to mint",
)
async def test_a_chat_created_before_it_had_a_sandbox_folder_gets_one_on_the_drop(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """An older chat has no sandbox child to re-target into, so the move mints it.

    It is minted through the seam that mints a new chat's folders, so the one an
    old chat gains answers the same capabilities as the one a chat created today
    was born with — a drop must not produce a second, differently sealed kind of
    working folder.
    """
    fresh = await _chat_node(files_client, title="Made today")
    drive = await _drive_id(files_client)
    born = await _item(files_client, drive, await _sandbox_of(files_client, drive, fresh))

    # Beside the fresh chat, so the two sandboxes inherit the same chain and
    # only the minting can tell them apart; under a folder where the caller's
    # role is smaller, a role refusal would masquerade as a minting difference.
    beside = await fx.folder(uuid.UUID(str((await _item(files_client, drive, fresh))["parentId"])))
    older = await fx.node(
        b"Older.alkerachat",
        kind="folder",
        parent=beside,
        subtype="chat",
        target_object_id=uuid.uuid4(),
    )
    assert await _rows(files_client, drive, str(older.id)) == []

    dropped = await fx.node(b"handover.txt", parent=await _home(files_client, fx))
    response = await _move(files_client, drive, str(dropped.id), str(older.id), idem)

    assert response.status_code == 200, response.text
    minted = await _item(files_client, drive, await _sandbox_of(files_client, drive, str(older.id)))
    assert response.json()["parentId"] == str(minted["id"])
    assert minted["kind"] == "folder"
    assert minted["capabilities"] == born["capabilities"], (
        "a minted sandbox must be the one a fresh chat is created with"
    )
    assert await _names(files_client, drive, str(minted["id"])) == {"handover.txt"}


async def test_a_reader_of_the_chat_may_not_file_anything_into_it(
    files_client: AsyncClient,
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Resolving the destination is not a way past the chat's write rule.

    Whoever may write the chat may put a file in it; somebody who can only view
    it is refused and the node stays where it was. The decision is taken on the
    node the caller named, so a destination resolved underneath it can never be
    the thing that is authorized instead.
    """
    chat_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    shared = await fx.shared()
    theirs = await fx.node(b"theirs.txt", parent=shared)
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    principal = Principal(kind="user", id=files_org.member.id)
    async with fx.repo.transaction():
        chat_node = await fx.repo.node(NodeId(uuid.UUID(chat_id)))
        assert chat_node is not None
        # The member may VIEW the conversation and WRITE a file of their own:
        # everything the move needs except permission to add to the chat.
        await acl.grant(fx.repo, ctx, chat_node, principal, "reader")
        await acl.grant(fx.repo, ctx, theirs, principal, "writer")
    await real_session.commit()
    chat = chat_id
    sandbox = await _sandbox_of(files_client, drive, chat)
    before = await _names(files_client, drive, sandbox)

    reader = await login(client, files_org.member.email, files_org.member_password)
    refused = await _move(reader, drive, str(theirs.id), chat, idem)

    assert refused.status_code == 403, refused.text
    assert (await _item(files_client, drive, str(theirs.id)))["parentId"] == str(shared.id)
    assert await _names(files_client, drive, sandbox) == before


async def test_a_move_too_large_to_run_inline_is_queued_onto_the_same_folder(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queued path resolves its destination too, or a big drop would be the
    one that strays.

    The plan the runner replays is written in the request, so the destination it
    records has to be the resolved one — the runner re-parents onto it verbatim
    and never asks again.
    """
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 0)
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    sandbox = await _sandbox_of(files_client, drive, chat)
    moving = await fx.node(b"corpus", kind="folder", parent=await _home(files_client, fx))
    await fx.node(b"one.txt", parent=moving)

    response = await _move(files_client, drive, str(moving.id), chat, idem)

    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "move"
    # The suite runs queued operations inline, so the subtree has landed by now.
    assert (await _item(files_client, drive, str(moving.id)))["parentId"] == sandbox


async def test_a_copy_onto_a_chat_lands_in_the_same_folder_a_move_does(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Copy and move mean the same thing to the person doing it."""
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    sandbox = await _sandbox_of(files_client, drive, chat)
    source = await fx.node(b"deck.pdf")

    response = await files_client.post(
        f"{BASE}/drives/{drive}/items/{source.id}/copy",
        json={"parentId": chat},
        headers=idem(),
    )

    assert response.status_code in (200, 201, 202), response.text
    assert "deck.pdf" in await _names(files_client, drive, sandbox)


async def test_the_delta_feed_reports_the_node_under_the_folder_it_really_landed_in(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A sync client replays the feed, so the feed must name the real parent.

    A client that took the id the caller sent would materialize the file
    somewhere the server did not put it, and then fight the server about it
    forever.

    The move is read back with ``delta_until``, not one page: the feed holds a
    committed row back while any older write transaction is open in this
    database, and some of those are not the test's (an autovacuum ANALYZE takes
    an xid to write its statistics), so a single page may correctly come back
    empty.
    """
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    sandbox = await _sandbox_of(files_client, drive, chat)
    dropped = await fx.node(b"minutes.md", parent=await _home(files_client, fx))

    start = await files_client.get(f"{BASE}/drives/{drive}/delta?token=latest")
    assert start.status_code == 200, start.text
    token = str(start.json()["deltaLink"])

    moved = await _move(files_client, drive, str(dropped.id), chat, idem)
    assert moved.status_code == 200, moved.text

    items, _ = await delta_until(
        files_client, uuid.UUID(drive), carries=str(dropped.id), token=token
    )
    rows = {str(row["id"]): row for row in items}
    assert rows[str(dropped.id)]["parentId"] == sandbox


async def test_a_move_onto_a_report_folder_is_left_exactly_as_it_was(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Only a chat resolves a destination of its own.

    A replication context is a folder with a defined shape a person does not
    drop things into, and this must not quietly give every object-backed folder
    a landing place of its own.
    """
    report = await fx.node(
        b"Replication.alkerareport",
        kind="folder",
        parent=await fx.shared(),
        subtype="report",
        target_object_id=uuid.uuid4(),
    )
    drive = await _drive_id(files_client)
    dropped = await fx.node(b"stray.txt", parent=await _home(files_client, fx))

    response = await _move(files_client, drive, str(dropped.id), str(report.id), idem)

    assert response.status_code == 200, response.text
    assert response.json()["parentId"] == str(report.id)
    assert "stray.txt" in await _names(files_client, drive, str(report.id))


async def test_the_row_the_move_writes_descends_from_the_folder_it_reported(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The destination is resolved before anything downstream reads it.

    A check taken against the id the caller sent and a write that landed
    somewhere else would disagree about where the node is — the whole class of
    bug this ordering prevents — so the stored path is asserted against the
    parent the response named, not against either id on its own.
    """
    chat = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    dropped = await fx.node(b"ledger.csv", parent=await _home(files_client, fx))

    moved = await _move(files_client, drive, str(dropped.id), chat, idem)
    assert moved.status_code == 200, moved.text

    async def _path(node_id: str) -> str:
        found = await real_session.execute(
            text("SELECT path_ids::text FROM file_nodes WHERE id = :id"),
            {"id": uuid.UUID(node_id)},
        )
        return str(found.scalar_one())

    assert (await _path(str(dropped.id))).startswith(f"{await _path(moved.json()['parentId'])}.")
