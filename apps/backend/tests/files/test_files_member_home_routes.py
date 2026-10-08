"""A plain member owns what they make in their home, through the real routes.

``/home/<member>/`` is granted ``{user: owner}`` and nothing else, and that
grant is a drive default: it lives in the home's interned ACL body and has no
``file_shares`` row. Everything a member creates under it is a fresh node with
no cache of its own, so whether the member can open the folder they just made
depends entirely on the chain read — and on every writer of a cache below the
home carrying the home's grant along. An org admin holds the manager floor on
every node and never notices either gap, which is why the cases here run as
the member, the way the product is used.

Pinned end to end: the folder a member makes in their home answers them (a
read, a child, a tree, the home's listing), one made inside *that* folder does
too, a private home is still opaque to another member of the same org, the
admin still reads everything, a ``Can view`` grant on a home folder reaches
the fresh folders below it — before and after the queued rewrite lands — and
a box holding a member's chat under a lease can plant ``.runtime/agent/``
inside it and keep writing there.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.authz.policies.files import CHAT_RECORD_READ_ONLY
from alkera_core.files import acl, objects_bridge
from alkera_core.files.ids import OperationId
from alkera_core.models.files.ops import FileOp
from alkera_core.models.user import User
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member
from tests.files._files_kit import FilesFixtures, FilesOrgFixture, node_etag

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

Idem = Callable[[], dict[str, str]]


@pytest_asyncio.fixture
async def member_client(
    client: AsyncClient, files_org: FilesOrgFixture, files_on: None
) -> AsyncClient:
    """A plain org member — no admin anywhere — with a live session."""
    return await login(client, files_org.member.email, files_org.member_password)


@dataclass(frozen=True)
class Other:
    """A second plain member of the same org, on their own session."""

    client: AsyncClient
    user: User


@pytest_asyncio.fixture
async def other(
    client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession, files_on: None
) -> Other:
    """A fresh transport-sharing client rather than a second ``login`` on the
    first: logging the other member in on ``member_client`` would take the
    member's session with it."""
    user, password = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    await real_session.commit()
    separate = app_client()
    return Other(client=await login(separate, user.email, password or ""), user=user)


async def _drive(client: AsyncClient) -> tuple[str, str]:
    """``(drive_id, home_id)`` for whoever holds ``client``.

    The drive read is the call that ensures the caller's own home, resolved by
    the caller's identity — the same entrance every client takes.
    """
    drive = await client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    return str(drive.json()["id"]), str(drive.json()["homeId"])


async def _make_folder(
    client: AsyncClient,
    drive_id: str,
    parent_id: str,
    idem: Idem,
    *,
    name: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{parent_id}/children",
        json={"name": name or f"kept-{uuid.uuid4().hex[:8]}", "kind": "folder"},
        headers={**idem(), **(headers or {})},
    )


async def _tree(
    client: AsyncClient,
    drive_id: str,
    top_id: str,
    idem: Idem,
    paths: list[str],
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{top_id}/tree",
        json={"paths": paths},
        headers={**idem(), **(headers or {})},
    )


async def _read(client: AsyncClient, drive_id: str, node_id: str) -> Response:
    return await client.get(f"{BASE}/drives/{drive_id}/items/{node_id}")


async def _listed_ids(client: AsyncClient, drive_id: str, folder_id: str) -> set[str]:
    listed = await client.get(f"{BASE}/drives/{drive_id}/items/{folder_id}/children")
    assert listed.status_code == 200, listed.text
    return {str(row["id"]) for row in listed.json()["value"]}


async def _kept_under_home(
    client: AsyncClient, drive_id: str, home_id: str, idem: Idem
) -> tuple[str, str]:
    """``kept`` under the home and ``inner`` under ``kept`` — a fresh child of a
    fresh child — both made by the member through the route."""
    kept = await _make_folder(client, drive_id, home_id, idem, name="kept")
    assert kept.status_code == 201, kept.text
    inner = await _make_folder(client, drive_id, str(kept.json()["id"]), idem, name="inner")
    assert inner.status_code == 201, inner.text
    return str(kept.json()["id"]), str(inner.json()["id"])


# --------------------------------------------------------------------------
# the member's own folders answer the member
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(1, id="a-folder-made-in-the-home"),
        pytest.param(2, id="a-folder-made-in-a-fresh-folder"),
    ],
)
async def test_a_member_reads_and_writes_the_folder_they_just_made(
    member_client: AsyncClient, idem: Idem, depth: int
) -> None:
    """Every route a client uses on the folder answers its own maker.

    The folder has no cache of its own, so the read, the child, the tree and
    the listing all rest on the chain carrying the home's owner grant down.
    At depth two the parent has no cache either, so nothing between the home
    and the node holds a body at all.
    """
    drive_id, home_id = await _drive(member_client)
    parent_id = home_id
    folder_id = home_id
    for _ in range(depth):
        made = await _make_folder(member_client, drive_id, folder_id, idem)
        assert made.status_code == 201, made.text
        parent_id, folder_id = folder_id, str(made.json()["id"])

    read = await _read(member_client, drive_id, folder_id)
    assert read.status_code == 200, read.text
    assert read.json()["id"] == folder_id

    child = await _make_folder(member_client, drive_id, folder_id, idem, name="child")
    assert child.status_code == 201, child.text

    dropped = await _tree(member_client, drive_id, folder_id, idem, ["dropped/deeper"])
    assert dropped.status_code == 201, dropped.text
    assert [row["name"] for row in dropped.json()] == ["dropped", "deeper"]

    assert folder_id in await _listed_ids(member_client, drive_id, parent_id)
    assert {str(child.json()["id"]), str(dropped.json()[0]["id"])} <= await _listed_ids(
        member_client, drive_id, folder_id
    )


async def test_another_member_of_the_org_gets_the_opaque_404_on_a_private_home_folder(
    member_client: AsyncClient, other: Other, idem: Idem
) -> None:
    """The fix that lets the owner in must not let the whole org in: a home is
    private, and a folder in it stays as opaque to a teammate as the home is."""
    drive_id, home_id = await _drive(member_client)
    kept_id, inner_id = await _kept_under_home(member_client, drive_id, home_id, idem)

    for node_id in (home_id, kept_id, inner_id):
        read = await _read(other.client, drive_id, node_id)
        assert read.status_code == 404, (node_id, read.text)
    planted = await _make_folder(other.client, drive_id, kept_id, idem)
    assert planted.status_code == 404, planted.text
    dropped = await _tree(other.client, drive_id, kept_id, idem, ["dropped"])
    assert dropped.status_code == 404, dropped.text
    assert kept_id in await _listed_ids(member_client, drive_id, home_id)
    assert (await _listed_ids(member_client, drive_id, kept_id)) == {inner_id}


async def test_an_org_admin_still_reads_a_members_fresh_folders(
    member_client: AsyncClient, files_client: AsyncClient, idem: Idem
) -> None:
    """Admin-by-descent is unchanged: the manager floor reaches a member's
    fresh folder exactly as it reaches the home."""
    drive_id, home_id = await _drive(member_client)
    kept_id, inner_id = await _kept_under_home(member_client, drive_id, home_id, idem)

    for node_id in (home_id, kept_id, inner_id):
        read = await _read(files_client, drive_id, node_id)
        assert read.status_code == 200, (node_id, read.text)
    assert (await _listed_ids(files_client, drive_id, kept_id)) == {inner_id}


# --------------------------------------------------------------------------
# a share on a home folder reaches below it, and never costs the owner
# --------------------------------------------------------------------------


async def _grant_can_view(
    client: AsyncClient,
    session: AsyncSession,
    drive_id: str,
    node_id: str,
    principal_id: uuid.UUID,
    idem: Idem,
) -> Response:
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{node_id}/permissions",
        json={"principal": {"kind": "user", "id": str(principal_id)}, "role": "reader"},
        headers={**idem(), "If-Match": await node_etag(session, uuid.UUID(node_id))},
    )


async def _land_the_rewrite(session: AsyncSession, fx: FilesFixtures, node_id: str) -> int:
    """Run the repair the grant queued for ``node_id``'s subtree, the way the
    worker runs it, until it is done. Returns how many caches it rewrote."""
    ops = (
        (
            await session.execute(
                select(FileOp).where(
                    FileOp.kind == acl.OP_ACL_REWRITE,
                    FileOp.result_node_id == uuid.UUID(node_id),
                    FileOp.state.in_(("queued", "running")),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(ops) == 1, [op.state for op in ops]
    rewritten = 0
    async with fx.repo.transaction():
        while True:
            done = await acl.rewrite(fx.repo, OperationId(ops[0].id))
            rewritten += done
            if done == 0:
                break
    await session.commit()
    return rewritten


@pytest.mark.parametrize(
    "rewritten",
    [
        pytest.param(False, id="while-the-subtree-still-reads-the-chain"),
        pytest.param(True, id="after-the-queued-rewrite-landed"),
    ],
)
async def test_a_can_view_grant_on_a_home_folder_reaches_below_it_and_keeps_the_owner(
    member_client: AsyncClient,
    other: Other,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Idem,
    rewritten: bool,
) -> None:
    """Sharing ``kept`` hands the viewer ``kept`` and everything under it —
    what was there, and what the owner makes afterwards — and leaves the owner
    exactly where they were.

    The grant re-interns ``kept``'s own cache in the request and repairs the
    subtree later; both writers compute the body from the chain, so a chain
    that forgets the home's owner grant would lock the owner out of the folder
    they just shared, first at ``kept`` and then, as the rewrite lands, at
    every folder under it. The viewer is a reader and may not write.
    """
    drive_id, home_id = await _drive(member_client)
    kept_id, inner_id = await _kept_under_home(member_client, drive_id, home_id, idem)
    granted = await _grant_can_view(
        member_client, real_session, drive_id, kept_id, other.user.id, idem
    )
    assert granted.status_code == 201, granted.text
    later = await _make_folder(member_client, drive_id, inner_id, idem, name="later")
    assert later.status_code == 201, later.text
    later_id = str(later.json()["id"])
    if rewritten:
        assert await _land_the_rewrite(real_session, fx, kept_id) >= 1

    for node_id in (kept_id, inner_id, later_id):
        seen = await _read(other.client, drive_id, node_id)
        assert seen.status_code == 200, ("viewer", node_id, seen.text)
        mine = await _read(member_client, drive_id, node_id)
        assert mine.status_code == 200, ("owner", node_id, mine.text)
    assert (await _listed_ids(other.client, drive_id, kept_id)) == {inner_id}
    assert (await _listed_ids(other.client, drive_id, inner_id)) == {later_id}
    unseen = await _read(other.client, drive_id, home_id)
    assert unseen.status_code == 404, unseen.text

    planted = await _make_folder(other.client, drive_id, inner_id, idem)
    assert planted.status_code in (403, 404), planted.text
    owned = await _make_folder(member_client, drive_id, later_id, idem, name="still-mine")
    assert owned.status_code == 201, owned.text


# --------------------------------------------------------------------------
# a box holding a member's chat plants its runtime inside it
# --------------------------------------------------------------------------


async def test_a_member_owned_box_creates_its_runtime_under_the_leased_chat(
    member_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Idem,
) -> None:
    """``work/agent/`` is a fresh child of a fresh child under the chat folder
    the box leases; the box must be able to make it, read it back and keep
    writing under it as the member whose chat it is.

    The chat folder is minted by the objects bridge with a cache of its own,
    but its working folders and everything the box pushes are fresh nodes, so
    this is the two-level case one hop further from the home. The example is
    a plain folder on purpose: the chat's own records (``.runtime/`` among
    them) are born marked and admit only the machine proven to hold the
    lease, which a member's mount is not — that refusal has a case of its own
    below.
    """
    member_client, drive_id, chat_id, fence = await _members_leased_chat(
        member_client, fx, real_session, idem
    )

    skeleton = await _tree(member_client, drive_id, chat_id, idem, ["work/agent"], headers=fence)
    assert skeleton.status_code == 201, skeleton.text
    made: dict[str, Any] = {row["name"]: row for row in skeleton.json()}
    assert set(made) == {"work", "agent"}

    for name in ("work", "agent"):
        read = await _read(member_client, drive_id, str(made[name]["id"]))
        assert read.status_code == 200, (name, read.text)
    working = await _listed_ids(member_client, drive_id, chat_id)
    assert str(made["work"]["id"]) in working
    for folder_id in working:
        read = await _read(member_client, drive_id, folder_id)
        assert read.status_code == 200, (folder_id, read.text)

    session_dir = await _make_folder(
        member_client, drive_id, str(made["agent"]["id"]), idem, name="sessions", headers=fence
    )
    assert session_dir.status_code == 201, session_dir.text
    again = await _tree(
        member_client, drive_id, chat_id, idem, ["work/agent/snapshot"], headers=fence
    )
    assert again.status_code == 201, again.text
    assert [row["name"] for row in again.json()] == ["snapshot"]


async def _members_leased_chat(
    member_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession, idem: Idem
) -> tuple[AsyncClient, str, str, dict[str, str]]:
    """A member's chat, leased by the member's own session as a mount: the
    drive, the chat folder and the fence the mount's writes carry."""
    chat = await member_client.post("/api/v1/chats", json={"clientId": secrets.token_hex(8)})
    assert chat.status_code == 201, chat.text
    async with fx.repo.transaction():
        chat_node = await objects_bridge.live_node_for(fx.repo, uuid.UUID(chat.json()["id"]))
    assert chat_node is not None
    drive_id, chat_id = str(chat_node.drive_id), str(chat_node.id)

    leased = await member_client.post(
        f"{BASE}/drives/{drive_id}/items/{chat_id}/lease",
        json={"instanceId": "box-a", "machineId": "box", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(real_session, chat_node.id)},
    )
    assert leased.status_code == 200, leased.text
    fence: dict[str, str] = {
        "X-Alkera-Lease-Epoch": str(leased.json()["epoch"]),
        "X-Alkera-Lease-Instance": "box-a",
    }
    return member_client, drive_id, chat_id, fence


async def test_a_members_mount_may_raise_the_runtime_folder_but_not_write_inside_it(
    member_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Idem,
) -> None:
    """The one thing a member's own mount of their own chat cannot do inside
    the chat folder: change a record. ``.runtime`` is born a record whoever
    makes it, so the mount may raise the folder at the chat's top (a write in
    the chat folder, which is theirs) and is then refused inside it with the
    record's own code — the writer a record admits is the machine proven to
    hold the lease, and a person's session holding the same lease is not it.
    """
    member_client, drive_id, chat_id, fence = await _members_leased_chat(
        member_client, fx, real_session, idem
    )

    raised = await _tree(member_client, drive_id, chat_id, idem, [".runtime"], headers=fence)
    assert raised.status_code == 201, raised.text
    runtime = raised.json()[0]
    assert runtime["name"] == ".runtime"
    read = await _read(member_client, drive_id, str(runtime["id"]))
    assert read.status_code == 200, read.text

    inside = await _make_folder(
        member_client, drive_id, str(runtime["id"]), idem, name="agent", headers=fence
    )
    assert inside.status_code == 403, inside.text
    assert inside.json()["code"] == CHAT_RECORD_READ_ONLY
    deeper = await _tree(member_client, drive_id, chat_id, idem, [".runtime/agent"], headers=fence)
    assert deeper.status_code == 403, deeper.text
    assert deeper.json()["code"] == CHAT_RECORD_READ_ONLY
