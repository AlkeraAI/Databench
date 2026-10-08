"""A delta page says only what the page that was authorized said.

The route reads one page, authorizes that page's ids in ONE batch, and renders
what comes back. The batch is an await between the rows being read and the page
being rendered, and a mutation can commit inside it — that is the window this
module holds open on purpose.

The rule it pins is that such a row belongs to the NEXT page: never to this
one, and above all never to this one as a deletion. The route used to answer
the question by reading the page a second time after the batch, which cannot
hold — the two reads are two statements under READ COMMITTED, so they take two
snapshots, and what the feed may deliver is decided per snapshot ("strictly
below the oldest in-flight transaction"), not by the outbox being append-only.
A row that crossed that boundary between them was in the second page and in
nobody's authorized set, so it reached the client as ``{"id", "deleted": true}``
— a live node announced as a deletion, which is the one item a sync client
applies by removing the user's local copy.
"""

from __future__ import annotations

from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture, delta_until
from alkera_core.authz.principal import ActingContext
from alkera_core.files.history import emit_node_changed
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.models.files.tree import FileNode
from backend.api.routes.files import delta as delta_route
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _announce(fx: FilesFixtures, org: FilesOrgFixture, node: FileNode) -> None:
    """Emit and commit the outbox row a real mutation would, through the
    library's own emit point rather than a hand-written row."""
    ctx = ActingContext.for_user(
        user_id=org.org.admin_id, org_id=org.org.org_id, email=org.org.admin_email
    )
    async with fx.repo.transaction():
        await emit_node_changed(
            fx.repo,
            ctx,
            node_id=NodeId(node.id),
            drive_id=DriveId(node.drive_id),
            version=1,
        )
    await fx._session.commit()
    # Nothing of this session's may stay open: the feed withholds every row at
    # or above the oldest in-flight transaction in this database, and this test
    # needs the row it just committed to be deliverable.
    await fx._session.rollback()


async def test_a_row_that_commits_while_the_page_is_authorized_is_never_a_tombstone(
    files_on: None,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutation committed inside the page's authorization window is carried by
    a later page, named — and by this page not at all.

    The window is held open at the route's own seam: the batch decision is
    wrapped, so the second node is announced and committed exactly between the
    rows being read and the page being rendered. Both nodes belong to the org
    admin reading the feed, so a tombstone on either is the feed telling a
    client to delete a file it may read — the failure this guards, not a
    stricter reading of the contract.
    """
    drive = await fx.drive()
    early = await fx.node(b"already-announced.txt")
    late = await fx.node(b"landed-mid-page.txt")
    await _announce(fx, files_org, early)

    landed: list[str] = []
    decide = delta_route.readable_ids

    async def _land_a_mutation(repo: Any, ctx: Any, node_ids: Any, **kwargs: Any) -> set[Any]:
        """Commit a second announcement in the window the batch opens, then
        answer exactly what the real decision answers — it forwards the route's
        own call, so nothing about the page's access is faked here."""
        wanted = list(node_ids)
        if wanted and not landed:
            landed.append(str(late.id))
            await _announce(fx, files_org, late)
        return await decide(repo, ctx, wanted, **kwargs)

    monkeypatch.setattr(delta_route, "readable_ids", _land_a_mutation)

    items, link = await delta_until(files_client, drive.id, carries=str(early.id))
    assert landed, "nothing was committed inside the window, so this proves nothing"

    seen = {item["id"]: item for item in items}
    assert [item["id"] for item in items if item["deleted"]] == [], (
        "the feed announced a readable node as deleted — a row that committed "
        "after this page was read was rendered from a read nobody authorized"
    )
    assert seen[str(early.id)]["name"] == "already-announced.txt"

    # ... and it is not lost either: the row waits for a page, it is not dropped.
    if str(late.id) not in seen:
        later, _ = await delta_until(files_client, drive.id, carries=str(late.id), token=link)
        seen.update({item["id"]: item for item in later})
    assert seen[str(late.id)]["deleted"] is False
    assert seen[str(late.id)]["name"] == "landed-mid-page.txt"
