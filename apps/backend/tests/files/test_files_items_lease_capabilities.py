"""The lease rungs travel on the wire, so the surface that draws them can gate.

Acquiring a folder, asking for it back and taking it back are three different
rungs of the same ladder: a reader may ask, a writer may take it out, and only
a manager may take it back. The browser draws one control per rung and has no
role to reason from — it only ever sees the node's capability map — so a rung
the map does not carry is a control that is either permanently refused or
offered and then 403'd.

Both directions are asserted through the real item and listing routes on one
node, with the two callers a rung apart, so a green case is the ladder, the
capability map and the wire agreeing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from httpx import AsyncClient
from tests.conftest import login

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _capabilities(client: AsyncClient, drive: str, node_id: str) -> dict[str, Any]:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}")
    assert response.status_code == 200, response.text
    return dict(response.json()["capabilities"])


async def _grant(
    client: AsyncClient,
    drive: str,
    node_id: str,
    user_id: str,
    role: str,
    idem: Callable[[], dict[str, str]],
) -> None:
    owner_view = (await client.get(f"{BASE}/drives/{drive}/items/{node_id}")).json()
    granted = await client.post(
        f"{BASE}/drives/{drive}/items/{node_id}/permissions",
        json={"principal": {"kind": "user", "id": user_id}, "role": role},
        headers={**idem(), "If-Match": str(owner_view["etag"])},
    )
    assert granted.status_code in (200, 201), granted.text


async def test_a_manager_may_take_a_folder_back_and_a_writer_may_not(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The pair the ladder separates, on ONE folder.

    The org admin is a manager on their own drive by descent, so ``lease_force``
    is theirs; the member holds an explicit ``writer`` grant, which is the rung
    directly below — it carries ``lease`` and ``lease_request`` and stops short
    of the force. Without the negative the positives would pass on a map that
    simply said yes to everything.
    """
    folder = await fx.node(b"held-folder", kind="folder")
    drive = await _drive_id(files_client)

    manager = await _capabilities(files_client, drive, str(folder.id))
    assert manager["can_lease"] is True, "a manager may take the folder out"
    assert manager["can_lease_request"] is True
    assert manager["can_lease_force"] is True, "taking it back is the manager's write"

    await _grant(files_client, drive, str(folder.id), str(files_org.member.id), "writer", idem)
    member_client = await login(client, files_org.member.email, files_org.member_password)
    member = await _capabilities(member_client, drive, str(folder.id))
    assert member["can_write"] is True, "the grant landed; the negative below is the rung"
    assert member["can_lease"] is True, "a writer may take the folder out"
    assert member["can_lease_request"] is True
    assert member["can_lease_force"] is False, "only a manager takes a folder back"


async def test_a_reader_may_only_ask_for_a_folder_back(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The bottom rung. "Ask for it back" is the one lease control a reader
    gets; a reader whose other two flags were also true would be shown an
    acquire form the server answers with a 403."""
    folder = await fx.node(b"someone-elses-folder", kind="folder")
    drive = await _drive_id(files_client)
    await _grant(files_client, drive, str(folder.id), str(files_org.member.id), "reader", idem)

    member_client = await login(client, files_org.member.email, files_org.member_password)
    reader = await _capabilities(member_client, drive, str(folder.id))
    assert reader["can_read"] is True, "the grant landed"
    assert reader["can_lease_request"] is True, "anyone who can read it may ask for it back"
    assert reader["can_lease"] is False
    assert reader["can_lease_force"] is False


async def test_a_listed_row_carries_the_lease_rungs_it_answers_for(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """The context menu is opened on a ROW, not on the node's own read: a page
    that got the lease flags only from the item route would gate every row in a
    listing on a map it never received."""
    parent = await fx.node(b"listing-parent", kind="folder")
    child = await fx.node(b"listed-folder", kind="folder", parent=parent)
    drive = await _drive_id(files_client)

    response = await files_client.get(f"{BASE}/drives/{drive}/items/{parent.id}/children")
    assert response.status_code == 200, response.text
    rows = {str(row["id"]): dict(row) for row in response.json()["value"]}
    row = rows[str(child.id)]
    assert row["capabilities"]["can_lease"] is True
    assert row["capabilities"]["can_lease_request"] is True
    assert row["capabilities"]["can_lease_force"] is True
