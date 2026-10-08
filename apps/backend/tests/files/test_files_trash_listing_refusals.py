"""The trash lists what the reader may see, and leaves out what they may not.

The listing decides each trashed root in turn. A root the reader cannot read
was recorded as a denial, and recording it rolled the request's transaction
back to free its connection: every entry the page had already loaded expired,
the next attribute read in the async session raised, and the page was a 500.
Once a second person trashed anything, the trash was a permanent 500 for
everyone else, and nothing in it could be restored.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alkera_core.authz import AUTHZ_EVENT_TYPE
from alkera_core.models import EventOutbox
from httpx import AsyncClient
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member
from tests.files._files_kit import FilesOrgFixture, node_etag

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

Idem = Callable[[], dict[str, str]]


async def _member(real_session: AsyncSession, files_org: FilesOrgFixture) -> AsyncClient:
    user, password = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    await real_session.commit()
    return await login(app_client(), user.email, password or "")


async def _trash_a_folder(
    client: AsyncClient, real_session: AsyncSession, idem: Idem
) -> tuple[str, str]:
    """A folder in the caller's own home, made and trashed through the routes."""
    drive = (await client.get(f"{BASE}/drives")).json()
    made = await client.post(
        f"{BASE}/drives/{drive['id']}/items/{drive['homeId']}/children",
        json={"name": f"gone-{uuid.uuid4().hex[:6]}", "kind": "folder"},
        headers=idem(),
    )
    assert made.status_code == 201, made.text
    node_id = str(made.json()["id"])
    trashed = await client.delete(
        f"{BASE}/drives/{drive['id']}/items/{node_id}",
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(node_id))},
    )
    assert trashed.status_code in (200, 202, 204), trashed.text
    return str(drive["id"]), node_id


async def test_a_member_lists_their_own_trash_beside_entries_they_cannot_read(
    files_on: None, files_org: FilesOrgFixture, real_session: AsyncSession, idem: Idem
) -> None:
    first = await _member(real_session, files_org)
    second = await _member(real_session, files_org)
    # Entries the second member cannot read on both sides of their own, so a
    # refusal comes before and after an entry they may see.
    _drive_id, hidden_before = await _trash_a_folder(first, real_session, idem)
    drive_id, own = await _trash_a_folder(second, real_session, idem)
    _drive_id, hidden_after = await _trash_a_folder(first, real_session, idem)

    page = await second.get(f"{BASE}/drives/{drive_id}/trash")
    assert page.status_code == 200, page.text
    listed = {entry["item"]["id"] for entry in page.json()["entries"]}
    assert own in listed
    assert not listed & {hidden_before, hidden_after}

    theirs = await first.get(f"{BASE}/drives/{drive_id}/trash")
    assert theirs.status_code == 200, theirs.text
    listed = {entry["item"]["id"] for entry in theirs.json()["entries"]}
    assert {hidden_before, hidden_after} <= listed
    assert own not in listed


async def test_a_trash_page_leaves_one_decision_row_counting_what_it_left_out(
    files_on: None, files_org: FilesOrgFixture, real_session: AsyncSession, idem: Idem
) -> None:
    """The page is decided as one batch: one ``authz.decision`` row for the
    page, naming the entries the reader was refused, and none per entry."""
    first = await _member(real_session, files_org)
    second = await _member(real_session, files_org)
    _drive_id, hidden = await _trash_a_folder(first, real_session, idem)
    drive_id, own = await _trash_a_folder(second, real_session, idem)
    path = f"{BASE}/drives/{drive_id}/trash"

    def _rows() -> Select[tuple[EventOutbox]]:
        return select(EventOutbox).where(
            EventOutbox.org_id == files_org.org.org_id,
            EventOutbox.type == AUTHZ_EVENT_TYPE,
            EventOutbox.payload["path"].astext == path,
        )

    before = {row.id for row in (await real_session.execute(_rows())).scalars()}
    page = await second.get(path)
    assert page.status_code == 200, page.text
    real_session.expire_all()
    new = [row for row in (await real_session.execute(_rows())).scalars() if row.id not in before]

    assert len(new) == 1, [row.payload for row in new]
    (summary,) = new
    assert summary.payload["batch"] is True
    assert summary.payload["action"] == "read"
    assert summary.payload["resource"] == {"type": "file_node"}
    assert hidden in summary.payload["refused_ids"]
    assert own not in summary.payload["refused_ids"]
    assert summary.payload["allowed"] >= 1


async def test_a_member_undoes_their_own_trash_and_a_colleague_cannot(
    files_on: None, files_org: FilesOrgFixture, real_session: AsyncSession, idem: Idem
) -> None:
    """Undoing a trash is restoring what was trashed, and is decided as that.
    The trash operation records no result node, so the undo used to ask the
    drive root for a write, which no member holds: every member was refused
    their own undo with a 403 while the operation said it was undoable."""
    owner = await _member(real_session, files_org)
    colleague = await _member(real_session, files_org)
    drive = (await owner.get(f"{BASE}/drives")).json()
    made = await owner.post(
        f"{BASE}/drives/{drive['id']}/items/{drive['homeId']}/children",
        json={"name": f"undo-{uuid.uuid4().hex[:6]}", "kind": "folder"},
        headers=idem(),
    )
    assert made.status_code == 201, made.text
    node_id = str(made.json()["id"])
    trashed = await owner.delete(
        f"{BASE}/drives/{drive['id']}/items/{node_id}",
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(node_id))},
    )
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["kind"] == "trash"
    undo = f"{BASE}/drives/{drive['id']}/operations/{trashed.json()['id']}/undo"

    refused = await colleague.post(undo, headers={**idem(), "If-Match": "0"})
    undone = await owner.post(undo, headers={**idem(), "If-Match": "0"})

    assert refused.status_code == 404, refused.text
    assert undone.status_code == 202, undone.text
    item = await owner.get(f"{BASE}/drives/{drive['id']}/items/{node_id}")
    assert item.status_code == 200, item.text
    rows = (
        (
            await real_session.execute(
                select(EventOutbox).where(
                    EventOutbox.org_id == files_org.org.org_id,
                    EventOutbox.type == AUTHZ_EVENT_TYPE,
                    EventOutbox.entity_id == node_id,
                    EventOutbox.payload["path"].astext == undo,
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted((r.payload["action"], r.payload["effect"]) for r in rows) == [
        ("restore", "allow"),
        ("restore", "deny"),
    ]


async def _member_and_id(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> tuple[AsyncClient, str]:
    user, password = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    await real_session.commit()
    return await login(app_client(), user.email, password or ""), str(user.id)


async def _make(client: AsyncClient, drive: dict[str, str], parent: str, idem: Idem) -> str:
    made = await client.post(
        f"{BASE}/drives/{drive['id']}/items/{parent}/children",
        json={"name": f"n-{uuid.uuid4().hex[:6]}", "kind": "folder"},
        headers=idem(),
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


async def _share(
    client: AsyncClient,
    real_session: AsyncSession,
    drive: dict[str, str],
    node: str,
    reader: str,
    idem: Idem,
) -> None:
    granted = await client.post(
        f"{BASE}/drives/{drive['id']}/items/{node}/permissions",
        json={"principal": {"kind": "user", "id": reader}, "role": "reader"},
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(node))},
    )
    assert granted.status_code == 201, granted.text


async def _bin(
    client: AsyncClient, real_session: AsyncSession, drive: dict[str, str], node: str, idem: Idem
) -> None:
    trashed = await client.delete(
        f"{BASE}/drives/{drive['id']}/items/{node}",
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(node))},
    )
    assert trashed.status_code in (200, 202, 204), trashed.text


async def test_nested_and_shared_trashed_roots_are_listed_by_what_each_reader_may_read(
    files_on: None, files_org: FilesOrgFixture, real_session: AsyncSession, idem: Idem
) -> None:
    """The shape the trash had when it answered 500 for every member: roots
    trashed inside folders that were trashed after them, roots shared with
    one member, and roots nobody else may read, side by side. Each reader gets
    a 200 and exactly the roots they may read; a member with no share and no
    trash of their own gets an empty page, which is correct, not a filter that
    is too strict."""
    owner, _owner_id = await _member_and_id(real_session, files_org)
    reader, reader_id = await _member_and_id(real_session, files_org)
    bystander, _ = await _member_and_id(real_session, files_org)
    drive = (await owner.get(f"{BASE}/drives")).json()

    outer = await _make(owner, drive, drive["homeId"], idem)
    inner_shared = await _make(owner, drive, outer, idem)
    inner_private = await _make(owner, drive, outer, idem)
    top_shared = await _make(owner, drive, drive["homeId"], idem)
    await _share(owner, real_session, drive, inner_shared, reader_id, idem)
    await _share(owner, real_session, drive, top_shared, reader_id, idem)
    # Inner roots first, then the folder that held them: three separate roots
    # nested in one another, as the captured trash had them.
    for node in (inner_shared, inner_private, outer, top_shared):
        await _bin(owner, real_session, drive, node, idem)

    def listed(page: Any) -> set[str]:
        assert page.status_code == 200, page.text
        return {entry["item"]["id"] for entry in page.json()["entries"]}

    mine = {outer, inner_shared, inner_private, top_shared}
    assert listed(await owner.get(f"{BASE}/drives/{drive['id']}/trash")) >= mine
    assert listed(await reader.get(f"{BASE}/drives/{drive['id']}/trash")) & mine == {
        inner_shared,
        top_shared,
    }
    assert listed(await bystander.get(f"{BASE}/drives/{drive['id']}/trash")) & mine == set()
