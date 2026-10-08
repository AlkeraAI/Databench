"""What a move does to who can reach the thing that moved.

The product promise is one sentence: permissive permissions bubble down, and
there is never a case where someone has a role on a folder but not on
everything inside it. A move is where that promise is easiest to break,
because a node's effective access is cached on the row (``file_nodes.acl_id``)
while the truth is the ancestor chain — and a move changes the chain without
touching the cache.

So these drive the move through the real route and then ask the *other* person,
through the real route, what they can see. Both directions are pinned, because
a stale cache fails one way in each:

  * into a shared folder — the grant must reach the node the moment it lands,
    or the folder was not really shared;
  * out of one — the grant must stop reaching it, or a move is a way to keep
    access to something that has left the room, which is the dangerous half.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from _files_kit import node_etag
from alkera_core.files import acl
from alkera_core.files.acl import OP_ACL_REWRITE
from alkera_core.files.ids import OperationId
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

API = "/api/v1/files"


def _item_url(node: FileNode) -> str:
    return f"{API}/drives/{node.drive_id}/items/{node.id}"


def _perm_url(node: FileNode) -> str:
    return f"{_item_url(node)}/permissions"


async def _mutate(
    session: AsyncSession, node: FileNode, idem: Callable[[], dict[str, str]]
) -> dict[str, str]:
    return {**idem(), "If-Match": await node_etag(session, node.id)}


async def _share_with(
    client: AsyncClient,
    session: AsyncSession,
    node: FileNode,
    user_id: uuid.UUID,
    idem: Callable[[], dict[str, str]],
    *,
    role: str = "reader",
) -> None:
    made = await client.post(
        _perm_url(node),
        json={"principal": {"kind": "user", "id": str(user_id)}, "role": role},
        headers=await _mutate(session, node, idem),
    )
    assert made.status_code == 201, made.text


async def _move(
    client: AsyncClient,
    session: AsyncSession,
    node: FileNode,
    into: FileNode,
    idem: Callable[[], dict[str, str]],
) -> None:
    moved = await client.patch(
        _item_url(node),
        json={"parentId": str(into.id)},
        headers=await _mutate(session, node, idem),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["parentId"] == str(into.id)


async def _member(client: AsyncClient, files_org: FilesOrgFixture) -> AsyncClient:
    """A second client, signed in as the org's ordinary member."""
    other = app_client()
    return await login(other, files_org.member.email, files_org.member_password)


async def test_a_file_moved_into_a_shared_folder_is_readable_at_once(
    files_on: None,
    client: AsyncClient,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The bubble-down promise, as a person meets it: drag a private file into a
    folder you share with someone, and they can open it — not after a sweeper
    has caught up, but on their very next request."""
    shared = await fx.node(b"shared", kind="folder")
    private = await fx.node(b"draft.txt")
    await _share_with(files_client, real_session, shared, files_org.member.id, idem)
    member = await _member(client, files_org)

    # Before the move it is not theirs to see, and the refusal is the opaque one.
    assert (await member.get(_item_url(private))).status_code == 404

    await _move(files_client, real_session, private, shared, idem)

    landed = await member.get(_item_url(private))
    assert landed.status_code == 200, landed.text
    assert landed.json()["capabilities"]["can_read"] is True


async def test_a_file_moved_out_of_a_shared_folder_stops_being_readable(
    files_on: None,
    client: AsyncClient,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The dangerous half of the same rule. Access that came from a folder must
    leave with the folder: a node that kept the grants of the parent it no
    longer has would make "move it somewhere private" a no-op."""
    shared = await fx.node(b"shared", kind="folder")
    inside = await fx.node(b"note.txt", parent=shared)
    elsewhere = await fx.node(b"elsewhere", kind="folder")
    await _share_with(files_client, real_session, shared, files_org.member.id, idem)
    member = await _member(client, files_org)

    assert (await member.get(_item_url(inside))).status_code == 200

    await _move(files_client, real_session, inside, elsewhere, idem)

    gone = await member.get(_item_url(inside))
    assert gone.status_code == 404, gone.text


async def test_a_direct_grant_survives_a_move_into_a_private_folder(
    files_on: None,
    client: AsyncClient,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A grant made ON the node is the node's own and moves with it: only the
    grants it merely inherited are the ones the chain decides."""
    private = await fx.node(b"elsewhere", kind="folder")
    file_ = await fx.node(b"budget.csv")
    await _share_with(files_client, real_session, file_, files_org.member.id, idem)
    member = await _member(client, files_org)

    assert (await member.get(_item_url(file_))).status_code == 200

    await _move(files_client, real_session, file_, private, idem)

    still = await member.get(_item_url(file_))
    assert still.status_code == 200, still.text
    # The folder it landed in stays the mover's own business.
    assert (await member.get(_item_url(private))).status_code == 404


async def _materialize_acls(fx: FilesFixtures) -> int:
    """Run every queued ACL rewrite to completion, as the worker would.

    Until it has run, a node carries no ``acl_id`` at all and the read path has
    no cache to prefer — so a test that grants and immediately moves proves
    nothing about the state a real drive spends its life in. Draining first is
    what puts the cache *there*, holding the grants of the parent the node is
    about to leave.
    """
    repo = fx.repo
    total = 0
    async with repo.transaction():
        op_ids = list(
            (
                await repo.execute_scoped(
                    select(FileOp.id)
                    .where(FileOp.kind == OP_ACL_REWRITE)
                    .order_by(FileOp.created_at)
                )
            )
            .scalars()
            .all()
        )
    for op_id in op_ids:
        for _ in range(50):
            async with repo.transaction():
                moved = await acl.rewrite(repo, OperationId(op_id), batch=64)
            total += moved
            if moved == 0:
                break
    return total


async def test_a_moved_node_does_not_keep_the_access_its_cached_acl_named(
    files_on: None,
    client: AsyncClient,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The same rule once the cache is really there.

    A node's effective access is materialized onto its row by the rewrite the
    grant queues. This drains that rewrite first, so the node genuinely carries
    an interned ACL naming the folder's grant, and only then moves it out. A
    read that trusted the cached row would hand the reader access to a node
    that has left the folder it was shared through.
    """
    shared = await fx.node(b"shared", kind="folder")
    inside = await fx.node(b"note.txt", parent=shared)
    elsewhere = await fx.node(b"elsewhere", kind="folder")
    await _share_with(files_client, real_session, shared, files_org.member.id, idem)
    await _materialize_acls(fx)
    await real_session.commit()

    cached = await real_session.get(FileNode, inside.id, populate_existing=True)
    assert cached is not None
    assert cached.acl_id is not None, "the rewrite did not materialize an ACL to go stale"
    assert cached.state == "live"

    member = await _member(client, files_org)
    assert (await member.get(_item_url(inside))).status_code == 200

    await _move(files_client, real_session, inside, elsewhere, idem)

    gone = await member.get(_item_url(inside))
    assert gone.status_code == 404, gone.text


async def test_a_whole_subtree_inherits_the_folder_it_was_moved_into(
    files_on: None,
    client: AsyncClient,
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """ "Everything in it" is the literal claim: moving a folder hands the
    destination's principals every node underneath, not just the one that was
    named in the request."""
    shared = await fx.node(b"shared", kind="folder")
    carried = await fx.node(b"carried", kind="folder")
    deep = await fx.node(b"deep.txt", parent=carried)
    await _share_with(files_client, real_session, shared, files_org.member.id, idem)
    member = await _member(client, files_org)

    assert (await member.get(_item_url(deep))).status_code == 404

    await _move(files_client, real_session, carried, shared, idem)

    reached = await member.get(_item_url(deep))
    assert reached.status_code == 200, reached.text
    # And the effective listing says WHERE it comes from, which is the line the
    # share dialog renders as "via shared".
    grants = await member.get(f"{_perm_url(deep)}?effective=true")
    assert grants.status_code == 200, grants.text
    assert [
        (row["principal"]["id"], row["grantingNodeId"])
        for row in grants.json()["value"]
        if row["principal"]["id"] == str(files_org.member.id)
    ] == [(str(files_org.member.id), str(shared.id))]
