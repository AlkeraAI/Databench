"""A plain member can open the drive: the signposts list, and nothing else leaks.

The org root, ``home/`` and ``Teams/`` carry no grants at all — a grant there
would trickle down and hand every member every drive — so the only way a member
who is not an org admin ever reaches their own home is by listing the root and
following the signpost the server names. Without a rule for traversal-only
containers the decider gave that member no role on the root, so ``GET
/items/<root>`` and its children answered the opaque 404 and the drive had no
entrance: every non-admin's Files surface was empty.

What is pinned here is both halves of that rule, because only the pair is safe:
the containers are listable by any member of the org, *and* a listing of one
returns only the children that member could already reach by some other grant,
so ``home/`` still shows one home to the person who owns it and not the roster
of everyone else's.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from alkera_core.authz.principal import ActingContext
from alkera_core.files import drives
from alkera_core.files.ids import NodeId
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login, make_member
from tests.files._files_kit import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


async def _home_for(
    session: AsyncSession, fx: FilesFixtures, org_id: uuid.UUID, member: User
) -> FileNode:
    """``/home/<member>/`` built the way membership builds it.

    Named from the member's address, because that name is the home's identity:
    a folder under the container with any other name — however it got there —
    is not a home, and the drive read would ensure the real one beside it.
    """
    await fx.drive()
    ctx = ActingContext.for_user(user_id=member.id, org_id=org_id, email=member.email)
    async with fx.repo.transaction():
        home = await drives.ensure_home_folder(fx.repo, ctx, member.id)
    await session.commit()
    return home


@pytest_asyncio.fixture
async def member_client(
    client: AsyncClient, files_org: FilesOrgFixture, files_on: None
) -> AsyncClient:
    """A plain org member — no admin anywhere — with a live session."""
    return await login(client, files_org.member.email, files_org.member_password)


async def test_a_plain_member_lists_the_drive_root(
    member_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """The root answers a member with the three signposts, not a 404.

    This is the entrance: the client resolves every place it shows from this
    one listing, so a 404 here is a Files surface that never loads at all.
    """
    await _home_for(real_session, fx, files_org.org.org_id, files_org.member)

    drive = await member_client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    drive_id = drive.json()["id"]
    root_id = drive.json()["rootId"]

    item = await member_client.get(f"{BASE}/drives/{drive_id}/items/{root_id}")
    assert item.status_code == 200, item.text

    listed = await member_client.get(f"{BASE}/drives/{drive_id}/items/{root_id}/children")
    assert listed.status_code == 200, listed.text
    assert {row["name"] for row in listed.json()["value"]} == {"Shared", "home", "Teams"}


async def test_a_member_sees_only_their_own_home_under_the_container(
    member_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """``home/`` is listable by everyone and still shows one home per member.

    The container is the one place every member's folder is a sibling, so a
    listing that is not cut by readability publishes the whole roster — and
    names each of those folders to a caller who may not open any of them.
    """
    mine = await _home_for(real_session, fx, files_org.org.org_id, files_org.member)
    other, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    await real_session.commit()
    theirs = await _home_for(real_session, fx, files_org.org.org_id, other)

    drive = await member_client.get(f"{BASE}/drives")
    drive_id = drive.json()["id"]
    root_id = drive.json()["rootId"]
    rows = (await member_client.get(f"{BASE}/drives/{drive_id}/items/{root_id}/children")).json()
    container = next(row for row in rows["value"] if row["name"] == "home")

    listed = await member_client.get(f"{BASE}/drives/{drive_id}/items/{container['id']}/children")
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["value"]] == [str(mine.id)]

    # And the folder that did not appear is still refused when addressed by id,
    # so the listing and the direct read agree about what is reachable.
    direct = await member_client.get(f"{BASE}/drives/{drive_id}/items/{theirs.id}")
    assert direct.status_code == 404, direct.text


async def test_a_member_may_not_write_into_a_signpost(
    member_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """Listing a container is not a role on it.

    The rule adds READ and only READ, so the traversal container stays
    ungrantable and unwritable — otherwise every member could plant a folder in
    the org root the moment the entrance opened.
    """
    await _home_for(real_session, fx, files_org.org.org_id, files_org.member)
    drive = await member_client.get(f"{BASE}/drives")
    drive_id = drive.json()["id"]
    root_id = drive.json()["rootId"]

    made = await member_client.post(
        f"{BASE}/drives/{drive_id}/items/{root_id}/children",
        json={"name": "planted", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert made.status_code in (403, 404), made.text
    async with fx.repo.transaction():
        siblings = await fx.repo.siblings(NodeId(uuid.UUID(root_id)))
    assert not [row for row in siblings if bytes(row.name) == b"planted"]


# --------------------------------------------------------------------------
# the signposts take no direct write, from anybody
# --------------------------------------------------------------------------

#: The three traversal-only containers, named the way a caller finds them: the
#: drive root, and the two signposts listed directly under it.
SIGNPOSTS = ("", "home", "Teams")

READONLY_CODE = "files.container_readonly"


async def _drive_ids(client: AsyncClient) -> tuple[str, str, str]:
    """``(drive_id, root_id, home_id)`` for whoever is holding ``client``.

    The drive read is also the call that ensures the caller's own home, so the
    "and the same call into my home succeeds" half of every case below has a
    folder to aim at without building one by hand.
    """
    drive = await client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    body = drive.json()
    return body["id"], body["rootId"], body["homeId"]


async def _signpost_id(client: AsyncClient, drive_id: str, root_id: str, name: str) -> str:
    """The id of one signpost: the root itself, or a container listed under it."""
    if not name:
        return root_id
    rows = (await client.get(f"{BASE}/drives/{drive_id}/items/{root_id}/children")).json()
    return str(next(row for row in rows["value"] if row["name"] == name)["id"])


async def _a_folder_of_mine(client: AsyncClient, drive_id: str, home_id: str) -> tuple[str, int]:
    """A folder inside the caller's own home, with its etag — the thing to move."""
    made = await client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": f"movable-{uuid.uuid4().hex[:8]}", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"]), int(made.json()["etag"])


async def _writes_into(
    client: AsyncClient, drive_id: str, target: str, *, source: str, etag: int
) -> dict[str, Response]:
    """Every route that puts a node into ``target``, run once each.

    One helper for both halves of the rule, so the refusal and the success are
    literally the same five calls with a different destination — a test that
    spelled them twice could pass while the routes diverged.
    """
    key = uuid.uuid4().hex
    return {
        # First, because it is the one call that changes the node the rest use
        # as their source: run after the copy it would collide on the name.
        "move into": await client.patch(
            f"{BASE}/drives/{drive_id}/items/{source}",
            json={"parentId": target},
            headers={"If-Match": f'"{etag}"', "Idempotency-Key": uuid.uuid4().hex},
        ),
        "create folder": await client.post(
            f"{BASE}/drives/{drive_id}/items/{target}/children",
            json={"name": f"planted-{key[:8]}", "kind": "folder"},
            headers={"Idempotency-Key": key},
        ),
        "tree": await client.post(
            f"{BASE}/drives/{drive_id}/items/{target}/tree",
            json={"paths": [f"dropped-{key[:8]}/inner"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ),
        "copy into": await client.post(
            f"{BASE}/drives/{drive_id}/items/{source}/copy",
            json={"parentId": target},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ),
        "upload session": await client.post(
            f"{BASE}/uploads",
            json={"declaredSize": 3, "name": f"note-{key[:8]}.txt", "parentId": target},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ),
    }


@pytest.mark.parametrize("signpost", SIGNPOSTS, ids=["root", "home", "Teams"])
async def test_an_org_admin_may_not_write_directly_into_a_signpost(
    files_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    files_on: None,
    signpost: str,
) -> None:
    """``/``, ``home/`` and ``Teams/`` are signposts, not folders.

    An org admin is ``manager`` on all three by descent, so authorization
    allowed the write and nothing downstream refused it: the live drive grew a
    receipt under ``home/`` and a folder called ``Test`` beside the real homes,
    each of them a node no grant can reach and no listing shows to its owner.
    The rule is the container's, not the role's — the system's own ensure is
    the only writer there — so every one of these answers a named 4xx and
    leaves the container exactly as it found it.
    """
    await fx.drive()
    drive_id, root_id, home_id = await _drive_ids(files_client)
    target = await _signpost_id(files_client, drive_id, root_id, signpost)
    source, etag = await _a_folder_of_mine(files_client, drive_id, home_id)

    async with fx.repo.transaction():
        before = {row.id for row in await fx.repo.siblings(NodeId(uuid.UUID(target)))}

    answers = await _writes_into(files_client, drive_id, target, source=source, etag=etag)
    for route, answer in answers.items():
        assert answer.status_code == 422, f"{route}: {answer.status_code} {answer.text}"
        assert answer.json()["code"] == READONLY_CODE, f"{route}: {answer.text}"

    async with fx.repo.transaction():
        after = {row.id for row in await fx.repo.siblings(NodeId(uuid.UUID(target)))}
    assert after == before, "a refused write still landed a row in the signpost"


async def test_the_same_writes_into_the_callers_own_home_are_accepted(
    files_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    files_on: None,
) -> None:
    """The negative twin: the refusal is the container's, not the write's.

    Without this the five cases above would pass just as well if the routes
    were broken outright, or if the rule had been hung on the wrong condition
    and refused every folder in the drive.
    """
    await fx.drive()
    drive_id, _root_id, home_id = await _drive_ids(files_client)
    source, etag = await _a_folder_of_mine(files_client, drive_id, home_id)
    landing = await files_client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": "landing", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert landing.status_code == 201, landing.text
    target = str(landing.json()["id"])

    answers = await _writes_into(files_client, drive_id, target, source=source, etag=etag)
    assert {route: answer.status_code for route, answer in answers.items()} == {
        "create folder": 201,
        "tree": 201,
        "copy into": 202,
        "upload session": 201,
        "move into": 200,
    }, {route: answer.text for route, answer in answers.items()}


# --------------------------------------------------------------------------
# a tree posted at a signpost is a walk; WRITE is decided where it makes something
# --------------------------------------------------------------------------


async def _tree_at(client: AsyncClient, drive_id: str, top: str, *paths: str) -> Response:
    return await client.post(
        f"{BASE}/drives/{drive_id}/items/{top}/tree",
        json={"paths": list(paths)},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


async def _names_under(fx: FilesFixtures, node_id: str) -> set[bytes]:
    """What a folder holds, read off the table rather than through any decision."""
    async with fx.repo.transaction():
        return {bytes(row.name) for row in await fx.repo.siblings(NodeId(uuid.UUID(node_id)))}


async def test_a_member_walks_a_tree_through_the_signposts_into_their_own_home(
    member_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """A tree at the drive root that only passes through ``home/<me>/`` is a
    traversal, not a write into the root.

    A box pushing a chat folder anchors its skeleton at the root and names the
    folder's full path; the root grants a member nothing but the walk, so
    deciding WRITE there refused every member's push before the leased folder
    was ever reached. WRITE is decided on the folder a new one is actually made
    in — here the member's own home — so the same call plants a nested skeleton
    there, and the root itself takes nothing.
    """
    home = await _home_for(real_session, fx, files_org.org.org_id, files_org.member)
    drive_id, root_id, _home_id = await _drive_ids(member_client)
    mine = f"home/{bytes(home.name).decode()}"
    kept = await member_client.post(
        f"{BASE}/drives/{drive_id}/items/{home.id}/children",
        json={"name": "kept", "kind": "folder"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert kept.status_code == 201, kept.text
    at_root = await _names_under(fx, root_id)

    walked = await _tree_at(member_client, drive_id, root_id, f"{mine}/kept")
    assert walked.status_code == 201, walked.text
    assert walked.json() == []

    planted = await _tree_at(member_client, drive_id, root_id, f"{mine}/new/inner")
    assert planted.status_code == 201, planted.text
    assert [row["name"] for row in planted.json()] == ["new", "inner"]
    assert planted.json()[0]["parentId"] == str(home.id)
    assert await _names_under(fx, str(home.id)) == {b"kept", b"new"}
    assert await _names_under(fx, root_id) == at_root


async def test_a_member_may_not_tree_into_another_members_home_through_the_root(
    member_client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
) -> None:
    """The walk grants nothing below the signpost.

    The first folder a tree would make is decided on the home it lands in, and
    a stranger's home refuses it — otherwise READ at the root would let every
    member plant a folder in every home they can spell.
    """
    await _home_for(real_session, fx, files_org.org.org_id, files_org.member)
    other, _ = await make_member(real_session, org_id=files_org.org.org_id, verified=True)
    theirs = await _home_for(real_session, fx, files_org.org.org_id, other)
    drive_id, root_id, _home_id = await _drive_ids(member_client)
    before = await _names_under(fx, str(theirs.id))

    refused = await _tree_at(
        member_client, drive_id, root_id, f"home/{bytes(theirs.name).decode()}/planted/inner"
    )
    assert refused.status_code in (403, 404), refused.text
    assert await _names_under(fx, str(theirs.id)) == before


@pytest.mark.parametrize("signpost", SIGNPOSTS, ids=["root", "home", "Teams"])
async def test_a_tree_from_the_root_that_plants_directly_under_a_signpost_is_refused(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures, signpost: str
) -> None:
    """An org admin may enter every signpost and make things below them, and
    still a tree whose first new folder would sit directly under one is refused
    by the container's rule — from the root as much as from the signpost itself.
    The walk changed who is asked, not what a signpost takes."""
    drive_id, root_id, _home_id = await _drive_ids(files_client)
    target = await _signpost_id(files_client, drive_id, root_id, signpost)
    before = await _names_under(fx, target)
    prefix = f"{signpost}/" if signpost else ""

    refused = await _tree_at(
        files_client, drive_id, root_id, f"{prefix}planted-{uuid.uuid4().hex[:8]}/inner"
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == READONLY_CODE
    assert await _names_under(fx, target) == before
