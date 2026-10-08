"""A listed row's capabilities are its own, never the folder it was listed in.

The decider reads a node's OWN ``flags`` and never its chain's, so two rows of
one page can disagree with their parent in both directions: a chat folder is
sealed (``NO_DOWNLOAD``) inside a home that is not, and the ``outputs/``
deliverable folder inside that sealed chat is downloadable again because the
artifact exception exists precisely so a report a member asked for is a report
they can take. A page that stamped every row with the listed folder's access
answered the opposite of what the content route, the download operation and the
archive walk all answer — so the UI offered Download on a sealed chat and hid
it on the deliverable, and ``alkera files pull`` pruned exactly the wrong half.

Both directions are pinned here, through the real routes: the chat and its
working folders are created by the chat route itself, so the flags under test
are the ones production stamps rather than ones a fixture wrote by hand.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _chat_node(client: AsyncClient) -> str:
    """A chat created through the real route, as its Files node id."""
    response = await client.post("/api/v1/chats", json={"title": "My analysis"})
    assert response.status_code == 201, response.text
    node_id = response.json()["files_node_id"]
    assert node_id, "a chat created with Files on names its node"
    return str(node_id)


async def _item(client: AsyncClient, drive: str, node_id: str) -> dict:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _rows(client: AsyncClient, drive: str, node_id: str) -> dict[str, dict]:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}/children")
    assert response.status_code == 200, response.text
    return {str(row["id"]): dict(row) for row in response.json()["value"]}


async def test_a_listed_chat_folder_reports_the_no_download_its_own_read_reports(
    files_client: AsyncClient, files_on: None
) -> None:
    """Browsing the home a chat lives in must not advertise Download on it.

    The home grants ``EXPORT`` and the chat node does not, so this is the exact
    pair a page that borrowed the parent's access got backwards — and the row is
    what the portal's context menu and the CLI's pull prune both read.
    """
    node_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)

    chat = await _item(files_client, drive, node_id)
    home_id = str(chat["parentId"])
    home = await _item(files_client, drive, home_id)
    assert home["capabilities"]["can_download"] is True, (
        "the home folder itself is downloadable; without that the pair below proves nothing"
    )
    assert chat["capabilities"]["can_download"] is False, "a chat's bytes never leave as a file"

    row = (await _rows(files_client, drive, home_id))[node_id]
    assert row["capabilities"]["can_download"] is False, (
        "the listed row must answer what its own read answers, not its parent's"
    )
    assert row["capabilities"]["can_read"] is True, "the row is still readable in the listing"


async def test_the_folders_inside_a_chat_are_downloadable_though_the_chat_is_not(
    files_client: AsyncClient, files_on: None
) -> None:
    """Walking into a chat lists ordinary files.

    The chat folder is un-exportable — neither the conversation nor a zip of it
    leaves as a file — but what it holds is working material: the one working
    directory it produced everything in. That is theirs to take, and the
    listing has to say so, because the row is what the portal's menu and the
    CLI's pull prune both read.
    """
    from alkera_core.files.objects_bridge import CHAT_SANDBOX_FOLDER

    assert CHAT_SANDBOX_FOLDER is not None
    working = CHAT_SANDBOX_FOLDER.decode("utf-8")
    node_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)

    rows = await _rows(files_client, drive, node_id)
    by_name = {str(row["name"]): row for row in rows.values()}
    assert set(by_name) == {working}, by_name.keys()

    assert by_name[working]["capabilities"]["can_download"] is True, (
        "the working directory is the one thing a sealed chat still hands back"
    )
    # The pair the whole rule rests on: the container refuses, its contents do not.
    chat = await _item(files_client, drive, node_id)
    assert chat["capabilities"]["can_download"] is False
