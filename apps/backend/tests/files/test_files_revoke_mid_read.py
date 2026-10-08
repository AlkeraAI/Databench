"""Access withdrawn *while* a caller is reading, at the three seams it can land.

A page, a listing and a signed URL each hand a caller something that outlives
the decision that produced it: a delta token, a keyset marker, a content URL.
The rule at each seam is the same — the decision is taken again at the edge, so
what was readable a moment ago is not readable now — and each of these tests
holds the seam open and revokes into the gap.

The three shapes the revoke must produce are different on purpose: delta owes
the client a *tombstone* (it has told them the node exists, so it must tell them
it is gone), a children page owes them *silence* (a page it never mentioned the
node in must not start mentioning it), and a signed URL owes them the same
opaque 404 a forged one gets.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"
CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"
HELLO = b"hello, files\n"

#: The name the revoked caller must never see again once their grant is gone.
SECRET_NAME = b"severance-terms.txt"


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch, files_on: None) -> None:
    """Files enabled, a store behind it, and a content origin to mint onto."""
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)


@pytest_asyncio.fixture
async def granted(
    client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> Callable[[FileNode], Any]:
    """Grant the org's plain member ``reader`` on a node and log them in.

    Returns the member's client together with the share id, because withdrawing
    the grant is what every test here does next and the id is how ``revoke``
    addresses it.
    """

    async def grant(node: FileNode) -> tuple[AsyncClient, uuid.UUID]:
        ctx = ActingContext.for_user(
            user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
        )
        async with fx.repo.transaction():
            share = await acl.grant(
                fx.repo, ctx, node, Principal(kind="user", id=files_org.member.id), ROLE_READER
            )
            share_id = share.id
        await fx.repo.session.commit()
        member_client = app_client()
        await login(member_client, files_org.member.email, files_org.member_password)
        return member_client, share_id

    return grant


async def _revoke(
    fx: FilesFixtures, files_org: FilesOrgFixture, node: FileNode, share_id: uuid.UUID
) -> None:
    """Withdraw the grant through the library, the way the share route will.

    Going through ``acl.revoke`` rather than an ``UPDATE`` is the point: the
    revoke also schedules the inherited-ACL descent, and a test that skipped it
    would prove the read path re-decides but not that it re-decides against
    what the *product's* revoke actually leaves behind.
    """
    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        await acl.revoke(fx.repo, ctx, node, share_id)
    await fx.repo.session.commit()
    revoked = await fx.repo.session.execute(select(FileShare).where(FileShare.id == share_id))
    assert revoked.scalar_one().revoked_at is not None


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


# --------------------------------------------------------------------------
# delta: the client was told the node exists, so it must be told it is gone
# --------------------------------------------------------------------------


async def test_a_revoke_between_two_delta_pages_yields_a_name_free_tombstone(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    granted: Any,
) -> None:
    """Page one names the node; the grant is withdrawn; page two must carry it
    as ``{"id", "deleted"}`` and nothing else.

    Both halves matter. Without the tombstone the client's mirror keeps a file
    it may no longer have. With a tombstone that still carried ``name`` or
    ``etag``, the revoke would leak the very thing it was meant to withdraw —
    so the assertion is on the exact key set, not on the presence of ``id``.
    """
    node = await fx.node(SECRET_NAME)
    reader, share_id = await granted(node)
    drive = await _drive_id(reader)

    first = await reader.get(f"{BASE}/drives/{drive}/delta")
    assert first.status_code == 200, first.text
    named = [row for row in first.json()["items"] if row["id"] == str(node.id)]
    assert named and named[0]["deleted"] is False
    assert named[0]["name"] == SECRET_NAME.decode()

    await _revoke(fx, files_org, node, share_id)

    again = await reader.get(f"{BASE}/drives/{drive}/delta")
    assert again.status_code == 200, again.text
    rows = [row for row in again.json()["items"] if row["id"] == str(node.id)]
    assert rows, "the node vanished from delta entirely, leaving a stale mirror"
    assert rows[0] == {"id": str(node.id), "deleted": True}


async def test_a_revoke_tells_only_the_caller_who_lost_the_node(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    granted: Any,
    real_session: AsyncSession,
) -> None:
    """The revoke's tombstone is owed to the caller whose grant it withdrew,
    and to nobody else: a colleague who never had the node reads the very same
    rows and is not told the node exists."""
    node = await fx.node(SECRET_NAME)
    reader, share_id = await granted(node)
    drive = await _drive_id(reader)
    await _revoke(fx, files_org, node, share_id)

    # The reader's page carrying the tombstone proves the revoke row is
    # deliverable, and the boundary only rises, so the colleague's read after
    # it covers the same row.
    again = await reader.get(f"{BASE}/drives/{drive}/delta")
    assert again.status_code == 200, again.text
    assert {"id": str(node.id), "deleted": True} in again.json()["items"]

    colleague, password = await make_member(
        real_session, org_id=files_org.org.org_id, verified=True
    )
    assert password is not None
    other = app_client()
    await login(other, colleague.email, password)
    theirs = await other.get(f"{BASE}/drives/{drive}/delta")
    assert theirs.status_code == 200, theirs.text
    assert str(node.id) not in [row["id"] for row in theirs.json()["items"]]


# --------------------------------------------------------------------------
# children: a page that never named the node must not start naming it
# --------------------------------------------------------------------------


async def test_a_revoke_between_two_children_pages_removes_the_node_silently(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    granted: Any,
) -> None:
    """One page names three children; the grant that opened the folder is
    withdrawn; the next page is the opaque 404 and names none of them.

    The revoke is taken on the *folder* rather than on one child because
    inheritance is the model: a grant on a folder descends, so revoking a
    child's own grant while the parent's still stands correctly leaves the
    child readable. The seam that matters is therefore the one where the
    caller's standing over the whole listing disappears between two pages —
    and the page after it must not answer with a shorter list of survivors,
    which would confirm both the folder and how much was in it.
    """
    folder = await fx.node(b"folder", kind="folder")
    secret = await fx.node(SECRET_NAME, parent=folder)
    sibling_a = await fx.node(b"kept-a.txt", parent=folder)
    sibling_b = await fx.node(b"kept-b.txt", parent=folder)
    reader, folder_share = await granted(folder)
    drive = await _drive_id(reader)

    url = f"{BASE}/drives/{drive}/items/{folder.id}/children"
    before = await reader.get(url)
    assert before.status_code == 200, before.text
    assert sorted(before.json()) == ["nextMarker", "value"]
    assert {row["id"] for row in before.json()["value"]} == {
        str(secret.id),
        str(sibling_a.id),
        str(sibling_b.id),
    }

    await _revoke(fx, files_org, folder, folder_share)

    after = await reader.get(url)
    assert after.status_code == 404, after.text
    assert SECRET_NAME.decode() not in after.text
    assert str(sibling_a.id) not in after.text


async def test_the_revoked_node_is_no_longer_addressable_by_id_either(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    granted: Any,
) -> None:
    """Dropping the node from a listing would be theatre if the caller could
    still fetch it by the id the earlier page gave them."""
    node = await fx.node(SECRET_NAME)
    reader, share_id = await granted(node)
    drive = await _drive_id(reader)
    assert (await reader.get(f"{BASE}/drives/{drive}/items/{node.id}")).status_code == 200

    await _revoke(fx, files_org, node, share_id)

    refused = await reader.get(f"{BASE}/drives/{drive}/items/{node.id}")
    assert refused.status_code == 404
    assert SECRET_NAME.decode() not in refused.text


# --------------------------------------------------------------------------
# the signed URL: valid when handed out, dead when redeemed
# --------------------------------------------------------------------------


async def test_a_revoke_between_the_mint_and_the_redemption_kills_the_url(
    files_client: AsyncClient,
    content_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    granted: Any,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The URL carries a whole credential, so the only thing standing between a
    revoked caller and the bytes is the re-authorization at the content edge.

    The node is not trashed and the token is not spent: the *only* thing that
    changed between the 302 and the redemption is the grant. Remove the
    ``_readable_node`` call in ``content_serve`` and this test serves bytes to
    someone whose access was withdrawn.
    """
    node = await fx.node(SECRET_NAME)
    drive = await fx.drive()
    await real_session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive.id,
        },
    )
    await real_session.commit()
    written = await files_client.put(
        f"{BASE}/drives/{drive.id}/items/{node.id}/content",
        content=HELLO,
        headers={
            **idem(),
            "If-Match": f'"{node.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(HELLO)),
        },
    )
    assert written.status_code in {200, 201}, written.text

    reader, share_id = await granted(node)
    minted = await reader.get(f"{BASE}/drives/{drive.id}/items/{node.id}/content")
    assert minted.status_code == 302, minted.text
    location = str(minted.headers["location"])

    await _revoke(fx, files_org, node, share_id)

    refused = await reader.get(
        location.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST}
    )
    assert refused.status_code == 404
    assert refused.content != HELLO
    assert SECRET_NAME.decode() not in refused.text
