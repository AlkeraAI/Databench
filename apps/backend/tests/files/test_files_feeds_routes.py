"""The five feeds: a page, a continuation, and nothing a caller may not read.

Each feed is proven three ways, because those are the three ways a feed goes
wrong. It has to return the rows (the happy page); its marker has to resume
exactly where the previous page stopped, with nothing skipped and nothing
repeated (the continuation); and a node the caller cannot read has to be absent
from the page **while the page stays full**, the filtered-before-
pagination rule, which is the one that turns a short page into a count of what
is hidden.

The node-addressed feed, ``activity``, carries its three "not yours" classes in
``test_files_no_oracle_items.py`` instead, where the shared probe and the
three-class fixture already live.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, FilesOrgFixture
from alkera_core.files.acl_intern import ace_body, body_hash
from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
from alkera_core.files.filters import FLAG_STARRED
from alkera_core.models.files.acl import FileAcl, FileShare
from alkera_core.models.files.history import FileHistory, FileStar
from backend.api.routes.files import feeds as feed_routes
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"


class Feeds:
    """One org, a member who can read one folder, and the ids planted in it."""

    def __init__(
        self,
        client: AsyncClient,
        fx: FilesFixtures,
        session: AsyncSession,
        admin_id: uuid.UUID,
        member_id: uuid.UUID,
    ) -> None:
        self.client = client
        self.fx = fx
        self.session = session
        self.admin_id = admin_id
        self.member_id = member_id
        self.drive_id: uuid.UUID | None = None
        self.folder: Any = None
        self.readable: list[Any] = []

    async def acl(self, grants: list[Grant]) -> uuid.UUID:
        """One interned ACL, written the way a real share writes it."""
        body = ace_body(grants)
        digest = body_hash(body, org_team_id=self.fx.org_team_id)
        existing = await self.session.execute(select(FileAcl).where(FileAcl.body_hash == digest))
        found = existing.scalar_one_or_none()
        if found is not None:
            return found.id
        acl = FileAcl(
            id=uuid.uuid4(),
            org_team_id=self.fx.org_team_id,
            body=body,
            body_hash=digest,
        )
        self.session.add(acl)
        await self.session.flush()
        return acl.id

    def _admin(self, role: str = "manager") -> Grant:
        return Grant(
            principal=Principal(kind="user", id=self.admin_id),
            role=role,
            origin=GrantOrigin.direct(),
        )

    def _member(self, role: str = "writer") -> Grant:
        return Grant(
            principal=Principal(kind="user", id=self.member_id),
            role=role,
            origin=GrantOrigin.direct(),
        )

    async def shared_folder(self, name: bytes = b"shared") -> Any:
        acl_id = await self.acl([self._admin(), self._member()])
        folder = await self.fx.node(name, kind="folder", acl_id=acl_id)
        await self.session.commit()
        return folder

    async def hidden(self, name: bytes) -> Any:
        """A node the member is not named on, beside the folder they hold.

        Inheritance is descent-only, so an unreadable node cannot live *under*
        a folder the caller can read; it lives next to it, under the drive root,
        carrying an ACL that names only the org admin.
        """
        acl_id = await self.acl([self._admin()])
        node = await self.fx.node(name, kind="folder", acl_id=acl_id)
        await self.session.commit()
        return node

    async def readable_node(self, name: bytes) -> Any:
        """One more node the member may read, under the same folder."""
        acl_id = await self.acl([self._admin(), self._member()])
        node = await self.fx.node(name, parent=self.folder, acl_id=acl_id)
        await self.session.commit()
        return node

    async def touched(self, node: Any, *, by: uuid.UUID) -> None:
        """One history row, the shape the library records for an attrs change."""
        self.session.add(
            FileHistory(
                id=uuid.uuid4(),
                org_team_id=self.fx.org_team_id,
                node_id=node.id,
                seq=1,
                kind="attrs",
                acting_principal=by,
            )
        )
        await self.session.commit()

    async def shared_directly(self, node: Any, *, with_whom: uuid.UUID) -> None:
        """A live grant row: what `sharedWithMe` reads, and the only thing it does."""
        self.session.add(
            FileShare(
                id=uuid.uuid4(),
                org_team_id=self.fx.org_team_id,
                node_id=node.id,
                principal_kind="user",
                principal_id=with_whom,
                role="reader",
                granted_by=self.admin_id,
            )
        )
        await self.session.commit()


@pytest_asyncio.fixture
async def feeds(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_on: None,
    fx: FilesFixtures,
) -> Feeds:
    """Four readable nodes named ``hit-*`` under a folder the member may read.

    The caller is a plain member rather than the org admin: an admin holds a
    standing floor over their own drive, so nothing in it could be hidden from
    them and every planted node would be readable.
    """
    state = Feeds(client, fx, real_session, files_org.org.admin_id, files_org.member.id)
    state.folder = await state.shared_folder()
    shared = await state.acl([state._admin(), state._member()])
    for index in range(4):
        state.readable.append(
            await fx.node(f"hit-{index}".encode(), parent=state.folder, acl_id=shared)
        )
    member = await login(client, files_org.member.email, files_org.member_password)
    state.client = member
    drive = await member.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    state.drive_id = uuid.UUID(drive.json()["id"])
    return state


def shape(response: Any) -> tuple[int, tuple[str, ...], bool]:
    """Everything about a page a caller can count."""
    assert response.status_code == 200, response.text
    body = response.json()
    rows = body["value"]
    return (len(rows), tuple(str(row["id"]) for row in rows), body.get("nextMarker") is not None)


async def _star(feeds: Feeds, node: Any) -> None:
    starred = await feeds.client.put(
        f"{BASE}/drives/{feeds.drive_id}/items/{node.id}/star",
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(node.etag)},
    )
    assert starred.status_code in {200, 204}, starred.text


async def _url(feeds: Feeds, feed: str, limit: int) -> str:
    if feed == "search":
        return f"{BASE}/drives/{feeds.drive_id}/search?q=hit-&limit={limit}"
    return f"{BASE}/drives/{feeds.drive_id}/{feed}?limit={limit}"


# --------------------------------------------------------------------------
# search — the one feed with a query
# --------------------------------------------------------------------------


async def test_search_returns_the_matching_nodes(feeds: Feeds) -> None:
    """A page of everything whose name contains the query."""
    found = await feeds.client.get(await _url(feeds, "search", 50))
    count, ids, _ = shape(found)
    assert count == 4
    assert set(ids) == {str(node.id) for node in feeds.readable}


async def test_search_is_case_and_accent_insensitive(feeds: Feeds) -> None:
    """Both sides fold the same way, so an uppercase query still matches."""
    found = await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/search?q=HIT-&limit=50")
    assert shape(found)[0] == 4


async def test_search_refuses_an_empty_query(feeds: Feeds) -> None:
    """A blank query is not "everything": it is a request the library refuses."""
    answer = await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/search?q=%20")
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "files.bad_query"


async def test_search_marker_resumes_without_skipping_or_repeating(feeds: Feeds) -> None:
    """The second page is the rest of the first, in order and exactly once."""
    first = await feeds.client.get(await _url(feeds, "search", 2))
    _, head, more = shape(first)
    assert more is True
    marker = first.json()["nextMarker"]

    second = await feeds.client.get(
        f"{BASE}/drives/{feeds.drive_id}/search?q=hit-&limit=2&marker={marker}"
    )
    _, tail, _ = shape(second)

    assert set(head).isdisjoint(tail)
    assert set(head) | set(tail) == {str(node.id) for node in feeds.readable}


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param(2, id="page-shorter-than-the-readable-set"),
        pytest.param(4, id="page-exactly-the-readable-set"),
        pytest.param(50, id="page-longer-than-everything"),
    ],
)
async def test_search_page_is_identical_with_and_without_hidden_matches(
    feeds: Feeds, limit: int
) -> None:
    """Unreadable matches change neither the rows nor the length of the page.

    The planted names sort *between* the readable ones, so a filter applied
    after the cut would drop rows out of the middle of the first page and
    shorten it — and the length alone would count them.
    """
    before = shape(await feeds.client.get(await _url(feeds, "search", limit)))

    for index in range(4):
        await feeds.hidden(f"hit-{index}z".encode())

    after = shape(await feeds.client.get(await _url(feeds, "search", limit)))
    assert after == before, f"the page changed once hidden matches existed: {before} -> {after}"


async def test_the_search_comparison_would_notice_a_real_change(feeds: Feeds) -> None:
    """The comparison is not vacuous: a *readable* match does move the page."""
    before = shape(await feeds.client.get(await _url(feeds, "search", 50)))
    assert before[0] == 4

    await feeds.readable_node(b"hit-visible")
    after = shape(await feeds.client.get(await _url(feeds, "search", 50)))

    assert after[0] == 5
    assert after != before


# --------------------------------------------------------------------------
# starred
# --------------------------------------------------------------------------


async def test_starred_returns_only_the_starred_nodes(feeds: Feeds) -> None:
    """The star chip forced on: an unstarred sibling is not on the page."""
    await _star(feeds, feeds.readable[0])
    found = await feeds.client.get(await _url(feeds, "starred", 50))
    count, ids, _ = shape(found)
    assert count == 1
    assert ids == (str(feeds.readable[0].id),)


async def test_starred_marker_resumes_without_skipping_or_repeating(feeds: Feeds) -> None:
    for node in feeds.readable:
        await _star(feeds, node)
    first = await feeds.client.get(await _url(feeds, "starred", 2))
    _, head, more = shape(first)
    assert more is True
    marker = first.json()["nextMarker"]

    second = await feeds.client.get(
        f"{BASE}/drives/{feeds.drive_id}/starred?limit=2&marker={marker}"
    )
    _, tail, _ = shape(second)
    assert set(head).isdisjoint(tail)
    assert set(head) | set(tail) == {str(node.id) for node in feeds.readable}


async def test_starred_page_is_identical_with_and_without_hidden_stars(feeds: Feeds) -> None:
    """A node the caller cannot read is absent even when it carries the bit."""
    for node in feeds.readable:
        await _star(feeds, node)
    before = shape(await feeds.client.get(await _url(feeds, "starred", 2)))

    for index in range(3):
        hidden = await feeds.hidden(f"a-hidden-{index}".encode())
        await feeds.session.execute(
            FileHistory.__table__.select().where(FileHistory.node_id == hidden.id)
        )
        hidden.flags = hidden.flags | 1
        await feeds.session.commit()

    after = shape(await feeds.client.get(await _url(feeds, "starred", 2)))
    assert after == before, f"the page changed once hidden stars existed: {before} -> {after}"


# --------------------------------------------------------------------------
# recent
# --------------------------------------------------------------------------


async def _recent_ids(feeds: Feeds) -> set[str]:
    return set(shape(await feeds.client.get(await _url(feeds, "recent", 50)))[1])


async def test_recent_returns_what_this_caller_touched(feeds: Feeds) -> None:
    """`recent` is a projection over the caller's own history rows.

    Measured as a DELTA against the page the fixture already leaves behind:
    reaching the drive provisions the caller's own root and records the create
    that provisioning is, so "what this caller touched" is only ever the rows
    added on top of that — pinning an absolute count would pin the fixture's
    setup instead of the feed.
    """
    before = await _recent_ids(feeds)
    await feeds.touched(feeds.readable[0], by=feeds.member_id)
    assert await _recent_ids(feeds) - before == {str(feeds.readable[0].id)}


async def _touched_repeatedly(feeds: Feeds, node: Any, *, times: int) -> None:
    """`times` distinct history rows on one node: a file edited over and over."""
    for seq in range(2, times + 2):
        feeds.session.add(
            FileHistory(
                id=uuid.uuid4(),
                org_team_id=feeds.fx.org_team_id,
                node_id=node.id,
                seq=seq,
                kind="attrs",
                acting_principal=feeds.member_id,
            )
        )
    await feeds.session.commit()


async def test_recent_lists_a_repeatedly_touched_node_once(feeds: Feeds) -> None:
    """A node is one row however many times its owner touched it.

    "Recent" is a projection over `file_history`, which holds a row per EVENT —
    so the correlated `EXISTS` is the whole thing standing between the feed and
    a page that reads as the same handful of files over and over.
    """
    await _touched_repeatedly(feeds, feeds.readable[0], times=5)
    await feeds.touched(feeds.readable[1], by=feeds.member_id)

    _, ids, _ = shape(await feeds.client.get(await _url(feeds, "recent", 50)))

    assert len(ids) == len(set(ids)), f"the feed repeats rows: {ids}"
    assert ids.count(str(feeds.readable[0].id)) == 1


async def test_recent_pages_never_repeat_a_row_across_the_marker(feeds: Feeds) -> None:
    """Paging the feed walks it once: the second page shares nothing with the first."""
    for node in feeds.readable:
        await _touched_repeatedly(feeds, node, times=3)

    first = await feeds.client.get(await _url(feeds, "recent", 2))
    _, head, more = shape(first)
    assert more, "the fixture did not leave a second page to walk"
    marker = first.json()["nextMarker"]
    _, tail, _ = shape(
        await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/recent?limit=50&marker={marker}")
    )

    assert set(head).isdisjoint(tail), f"a row was served on both pages: {head} / {tail}"
    assert len(tail) == len(set(tail))


async def test_recent_is_per_caller(feeds: Feeds) -> None:
    """Another principal's history row is not this caller's recent."""
    before = await _recent_ids(feeds)
    await feeds.touched(feeds.readable[0], by=feeds.member_id)
    await feeds.touched(feeds.readable[1], by=feeds.admin_id)

    after = await _recent_ids(feeds)
    assert after - before == {str(feeds.readable[0].id)}
    assert str(feeds.readable[1].id) not in after


async def test_recent_page_is_identical_with_and_without_hidden_touches(feeds: Feeds) -> None:
    """A node the caller touched and then lost access to is not on the page."""
    for node in feeds.readable:
        await feeds.touched(node, by=feeds.member_id)
    before = shape(await feeds.client.get(await _url(feeds, "recent", 2)))

    for index in range(3):
        hidden = await feeds.hidden(f"h-{index}".encode())
        await feeds.touched(hidden, by=feeds.member_id)

    after = shape(await feeds.client.get(await _url(feeds, "recent", 2)))
    assert after == before, f"the page changed once hidden touches existed: {before} -> {after}"


async def test_recent_survives_a_row_the_object_fold_hides(feeds: Feeds) -> None:
    """A chat folder warmed ahead of its first message is hidden from a feed,
    and hiding it must not take the rest of the page down with it.

    The fold that hides it hands back fewer items than the page rendered, and
    the feed paired the two by position: the length mismatch raised
    ``ValueError`` and ``recent`` answered 500 with no rows at all. Pairing by
    position is wrong even where the lengths happen to match — every row after
    a hidden one is then served under its neighbour's id.
    """
    acl_id = await feeds.acl([feeds._admin(), feeds._member()])
    warming = await feeds.fx.node(
        b"warming.alkerachat",
        kind="folder",
        parent=feeds.folder,
        acl_id=acl_id,
        subtype="chat",
        target_object_id=uuid.uuid4(),
    )
    await feeds.session.commit()
    await feeds.touched(warming, by=feeds.member_id)
    for node in feeds.readable:
        await feeds.touched(node, by=feeds.member_id)

    rows = await _recent(feeds)

    assert str(warming.id) not in rows, "a chat with no conversation yet is nobody's row"
    for node in feeds.readable:
        assert rows[str(node.id)]["name"] == node.name.decode()


# --------------------------------------------------------------------------
# sharedWithMe
# --------------------------------------------------------------------------


async def test_shared_with_me_returns_the_grant_roots(feeds: Feeds) -> None:
    """The folder the member was handed, not the four children inside it."""
    await feeds.shared_directly(feeds.folder, with_whom=feeds.member_id)
    found = await feeds.client.get(await _url(feeds, "sharedWithMe", 50))
    count, ids, _ = shape(found)
    assert count == 1
    assert ids == (str(feeds.folder.id),)


async def test_shared_with_me_omits_a_grant_to_somebody_else(feeds: Feeds) -> None:
    """A folder shared with the org admin alone is not shared with the member.

    The page keeps its length, because the principal filter is a predicate in
    the rows statement rather than a filter applied after the cut.
    """
    await feeds.shared_directly(feeds.folder, with_whom=feeds.member_id)
    before = shape(await feeds.client.get(await _url(feeds, "sharedWithMe", 1)))

    acl_id = await feeds.acl(
        [
            Grant(
                principal=Principal(kind="user", id=feeds.admin_id),
                role="manager",
                origin=GrantOrigin.direct(),
            )
        ]
    )
    theirs = await feeds.fx.node(b"aaa-theirs", kind="folder", acl_id=acl_id)
    await feeds.session.commit()
    await feeds.shared_directly(theirs, with_whom=feeds.admin_id)

    after = shape(await feeds.client.get(await _url(feeds, "sharedWithMe", 1)))
    assert after == before, f"the page changed once another's grant existed: {before} -> {after}"


# --------------------------------------------------------------------------
# activity — the node-addressed feed
# --------------------------------------------------------------------------


async def test_activity_returns_the_nodes_history(feeds: Feeds) -> None:
    """One row per recorded change, newest first, carrying ids and never names."""
    node = feeds.readable[0]
    await feeds.touched(node, by=feeds.member_id)
    found = await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/items/{node.id}/activity")
    assert found.status_code == 200, found.text
    rows = found.json()["value"]
    assert [row["kind"] for row in rows] == ["attrs"]
    assert rows[0]["nodeId"] == str(node.id)
    assert rows[0]["actingPrincipal"] == str(feeds.member_id)
    assert "name" not in rows[0]


async def test_activity_marker_resumes_without_skipping_or_repeating(feeds: Feeds) -> None:
    """Three rows read two at a time come back once each, newest first."""
    node = feeds.readable[0]
    for seq in range(1, 4):
        feeds.session.add(
            FileHistory(
                id=uuid.uuid4(),
                org_team_id=feeds.fx.org_team_id,
                node_id=node.id,
                seq=seq,
                kind="attrs",
                acting_principal=feeds.member_id,
            )
        )
    await feeds.session.commit()

    url = f"{BASE}/drives/{feeds.drive_id}/items/{node.id}/activity"
    first = await feeds.client.get(f"{url}?limit=2")
    assert first.status_code == 200, first.text
    head = [row["id"] for row in first.json()["value"]]
    marker = first.json()["nextMarker"]
    assert marker is not None

    second = await feeds.client.get(f"{url}?limit=2&marker={marker}")
    assert second.status_code == 200, second.text
    tail = [row["id"] for row in second.json()["value"]]

    assert set(head).isdisjoint(tail)
    assert len(head) + len(tail) == 3


async def test_activity_omits_the_rows_of_an_unreadable_descendant(feeds: Feeds) -> None:
    """``ancestor=true`` walks the subtree, and the subtree is the caller's."""
    node = feeds.readable[0]
    await feeds.touched(node, by=feeds.member_id)
    hidden = await feeds.hidden(b"elsewhere")
    await feeds.touched(hidden, by=feeds.member_id)

    found = await feeds.client.get(
        f"{BASE}/drives/{feeds.drive_id}/items/{feeds.folder.id}/activity?ancestor=true&limit=50"
    )
    assert found.status_code == 200, found.text
    assert str(hidden.id) not in {row["nodeId"] for row in found.json()["value"]}


async def test_activity_from_the_drive_root_omits_rows_the_caller_cannot_read(
    feeds: Feeds,
) -> None:
    """Every member reads the drive root, and an unreadable node lives UNDER it.

    Anchoring the subtree walk at the root is the shape that reaches another
    member's folders: the anchor's own READ must not stand in for the rows'.
    The page still fills from the readable rows past the hidden ones."""
    drive = await feeds.fx.drive()
    readable = feeds.readable[0]
    await feeds.touched(readable, by=feeds.member_id)
    for index in range(3):
        hidden = await feeds.hidden(f"someone-elses-{index}".encode())
        await feeds.touched(hidden, by=feeds.admin_id)

    found = await feeds.client.get(
        f"{BASE}/drives/{feeds.drive_id}/items/{drive.root_node_id}/activity?ancestor=true&limit=1"
    )
    assert found.status_code == 200, found.text
    rows = found.json()["value"]
    assert [row["nodeId"] for row in rows] == [str(readable.id)]


async def test_a_feed_renders_the_callers_star_and_not_a_colleagues(feeds: Feeds) -> None:
    """`starred` on the wire is one person's bookmark, on a feed as on a listing.

    Both nodes carry the node bit — the summary that says *somebody* starred
    them — and only one carries the caller's own row. A feed that rendered the
    bit would put a colleague's bookmark under this caller's star.
    """
    mine, theirs = feeds.readable[0], feeds.readable[1]
    for node in (mine, theirs):
        node.flags = FLAG_STARRED
    feeds.session.add(
        FileStar(org_team_id=feeds.fx.org_team_id, user_id=feeds.member_id, node_id=mine.id)
    )
    feeds.session.add(
        FileStar(org_team_id=feeds.fx.org_team_id, user_id=feeds.admin_id, node_id=theirs.id)
    )
    await feeds.session.commit()

    found = await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/search?q=hit&limit=10")
    assert found.status_code == 200, found.text
    starred = {row["id"]: row["starred"] for row in found.json()["value"]}

    assert starred[str(mine.id)] is True
    assert starred[str(theirs.id)] is False


async def test_the_starred_feed_holds_only_the_callers_own_stars(feeds: Feeds) -> None:
    """The `Starred` surface is the caller's bookmarks, not the drive's."""
    mine, theirs = feeds.readable[0], feeds.readable[1]
    for node in (mine, theirs):
        node.flags = FLAG_STARRED
    feeds.session.add(
        FileStar(org_team_id=feeds.fx.org_team_id, user_id=feeds.member_id, node_id=mine.id)
    )
    feeds.session.add(
        FileStar(org_team_id=feeds.fx.org_team_id, user_id=feeds.admin_id, node_id=theirs.id)
    )
    await feeds.session.commit()

    found = await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/starred?limit=10")
    assert found.status_code == 200, found.text
    rows = found.json()["value"]

    assert [row["id"] for row in rows] == [str(mine.id)]
    assert rows[0]["starred"] is True


# --------------------------------------------------------------------------
# location — the folder a feed row lives in
# --------------------------------------------------------------------------


async def _recent(feeds: Feeds) -> dict[str, Any]:
    """The Recent page, keyed by node id."""
    page = await feeds.client.get(f"{BASE}/drives/{feeds.drive_id}/recent?limit=50")
    assert page.status_code == 200, page.text
    return {str(row["id"]): row for row in page.json()["value"]}


async def _in_folder(feeds: Feeds, folder: bytes, file: bytes) -> tuple[Any, Any]:
    """One readable folder with one readable file in it, touched by the member."""
    acl_id = await feeds.acl([feeds._admin(), feeds._member()])
    parent = await feeds.fx.node(folder, kind="folder", acl_id=acl_id)
    node = await feeds.fx.node(file, parent=parent, acl_id=acl_id)
    await feeds.session.commit()
    await feeds.touched(node, by=feeds.member_id)
    return parent, node


async def test_recent_row_names_the_folder_it_lives_in(feeds: Feeds) -> None:
    """A feed draws from the whole drive, so each row says where it is.

    The id is what a click navigates to and the name is what a person reads;
    both have to be on the row, because a feed has no parent listing to read
    either off.
    """
    reports, report = await _in_folder(feeds, b"Reports", b"qa-report.md")

    row = (await _recent(feeds))[str(report.id)]

    assert row["parentId"] == str(reports.id)
    assert row["parentName"] == "Reports"


async def test_two_same_named_files_are_told_apart_by_their_folders(feeds: Feeds) -> None:
    """The bug this field exists for: `qa-report.md` three times, over and over.

    Two distinct nodes of the same name in different folders are two rows whose
    only difference is the folder they are in, so the folder has to reach the
    page or the two rows are indistinguishable.
    """
    reports, first = await _in_folder(feeds, b"Reports", b"qa-report.md")
    archive, second = await _in_folder(feeds, b"Archive", b"qa-report.md")

    rows = await _recent(feeds)

    assert rows[str(first.id)]["name"] == rows[str(second.id)]["name"]
    assert rows[str(first.id)]["parentName"] == "Reports"
    assert rows[str(second.id)]["parentName"] == "Archive"
    assert {rows[str(first.id)]["parentId"], rows[str(second.id)]["parentId"]} == {
        str(reports.id),
        str(archive.id),
    }


async def test_a_folder_the_caller_cannot_read_is_not_named(feeds: Feeds) -> None:
    """A grant on one file does not publish the name of the folder above it.

    Sharing a payslip out of somebody's `salaries` folder has to hand over the
    payslip and nothing else — the folder's name is a fact about the folder,
    and the caller holds no rung on it. The row still carries the id it always
    did; what it must not carry is the name.
    """
    hidden_acl = await feeds.acl([feeds._admin()])
    shared_acl = await feeds.acl([feeds._admin(), feeds._member()])
    salaries = await feeds.fx.node(b"salaries", kind="folder", acl_id=hidden_acl)
    payslip = await feeds.fx.node(b"pay.md", parent=salaries, acl_id=shared_acl)
    await feeds.session.commit()
    await feeds.touched(payslip, by=feeds.member_id)

    row = (await _recent(feeds))[str(payslip.id)]

    assert row["parentId"] == str(salaries.id)
    assert row["parentName"] is None


async def test_a_row_at_the_top_of_the_drive_names_the_drive(feeds: Feeds) -> None:
    """The drive root's own name is empty — it is the leading slash of every
    path — so a row filed directly under it is told what the drive is called
    rather than shown nothing at all."""
    await feeds.touched(feeds.folder, by=feeds.member_id)

    row = (await _recent(feeds))[str(feeds.folder.id)]

    assert row["parentName"] == "Files"


# --------------------------------------------------------------------------
# what one page costs — the claim a per-row decision cannot hold
# --------------------------------------------------------------------------


async def _more_readable_rows(feeds: Feeds, count: int, *, stem: str) -> list[Any]:
    """`count` more nodes the member may read, each with a history row of theirs.

    One commit for the lot: what these two tests vary is how many candidates
    the feed looks at, and seeding them one transaction at a time would take
    longer than the thing being measured.
    """
    acl_id = await feeds.acl([feeds._admin(), feeds._member()])
    made = [
        await feeds.fx.node(f"{stem}{offset}".encode(), parent=feeds.folder, acl_id=acl_id)
        for offset in range(count)
    ]
    for node in made:
        feeds.session.add(
            FileHistory(
                id=uuid.uuid4(),
                org_team_id=feeds.fx.org_team_id,
                node_id=node.id,
                seq=1,
                kind="attrs",
                acting_principal=feeds.member_id,
            )
        )
    await feeds.session.commit()
    return made


async def _recent_page_cost(feeds: Feeds) -> int:
    """Statements one Recent page costs, after an untimed warm-up.

    The warm-up is the same request: the first one through the app pays for
    connection checkout and for answers this process then caches, neither of
    which is the page.
    """
    from _files_kit import counting

    url = f"{BASE}/drives/{feeds.drive_id}/recent?limit=100"
    assert (await feeds.client.get(url)).status_code == 200
    with counting() as seen:
        assert (await feeds.client.get(url)).status_code == 200
    return len(seen)


async def test_a_recent_page_costs_the_same_over_four_candidates_as_over_a_hundred(
    feeds: Feeds,
) -> None:
    """The feed decides its candidate window in ONE batch, not one row at a time.

    A decision per candidate was the node, its chain, its drive and its grants
    — plus the pair of statements that steps out of the tenant role — for every
    row the page looked at, readable or not. At the published page size that is
    four hundred candidates, so the feed took seconds while its siblings
    answered in tens of milliseconds.

    Stated as an equality rather than a ceiling, because that is the claim no
    per-row pattern can satisfy: whatever one page costs, it must cost the same
    when there are twenty-five times as many rows to look past.
    """
    await _more_readable_rows(feeds, 4, stem="few-")
    small = await _recent_page_cost(feeds)

    added = await _more_readable_rows(feeds, 100, stem="many-")
    assert len(added) == 100

    large = await _recent_page_cost(feeds)

    assert large == small, (
        f"the page cost {large} statements over 104 candidates and {small} over 4 — "
        "a query per candidate"
    )


async def test_a_recent_page_records_no_decision_row_per_candidate(feeds: Feeds) -> None:
    """A pollable GET decides once, not once per row it renders or skips.

    Deciding each candidate through ``enforce()`` committed an ``authz.decision``
    row per candidate on EVERY request, so any member could grow the platform
    audit lane without bound by leaving the Files tab open — and the portal
    re-issues this exact request on every cache invalidation.
    """
    await _more_readable_rows(feeds, 40, stem="audited-")
    # A candidate the member is NOT named on. Its refusal is the case that
    # wrote its row in a committed session of its own, so it survived even the
    # rollback of the request that was refused.
    await feeds.touched(await feeds.hidden(b"audited-hidden"), by=feeds.member_id)

    url = f"{BASE}/drives/{feeds.drive_id}/recent?limit=100"
    assert (await feeds.client.get(url)).status_code == 200
    before = await _file_node_read_decisions(feeds)

    page = await feeds.client.get(url)
    assert page.status_code == 200, page.text
    assert len(page.json()["value"]) >= 40

    assert await _file_node_read_decisions(feeds) == before


async def test_a_transient_failure_answers_rather_than_shortening_the_page(
    feeds: Feeds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal the feed did not mean is answered, never rendered as absence.

    ``except (FilesError, HTTPException): continue`` swallowed the whole Files
    vocabulary around the decision, so a ``Conflict`` or a store failure made
    the row vanish from the page — indistinguishable from a node the caller may
    not read, and on a feed there is no sibling listing to notice it against.

    A page with rows in it is the control, so what this pins is the difference
    the failure makes: the same request that answers 200 with rows answers the
    error's own status when the decision cannot be made, and never a short 200.
    """
    from alkera_core.files.errors import Conflict

    await _more_readable_rows(feeds, 3, stem="transient-")
    url = f"{BASE}/drives/{feeds.drive_id}/recent?limit=100"

    healthy = await feeds.client.get(url)
    assert healthy.status_code == 200, healthy.text
    assert len(healthy.json()["value"]) >= 3

    async def busy(*_args: Any, **_kwargs: Any) -> Any:
        raise Conflict("files.busy", "the drive is busy")

    monkeypatch.setattr(feed_routes, "decided_by_id", busy)

    refused = await feeds.client.get(url)

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "files.busy"


async def _file_node_read_decisions(feeds: Feeds) -> int:
    """How many ``file_node`` READ decisions this org has on record.

    A session of its own: a refusal commits its decision row outside the
    request's transaction, and this suite shares one database with every other
    worker — so the count is read fresh and narrowed to this org.
    """
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models.event_outbox import EventOutbox

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(EventOutbox).where(
                        EventOutbox.org_id == feeds.fx.org_team_id,
                        EventOutbox.type == "authz.decision",
                    )
                )
            )
            .scalars()
            .all()
        )
    return sum(
        1
        for row in rows
        if (row.payload or {}).get("action") == "read"
        and ((row.payload or {}).get("resource") or {}).get("type") == "file_node"
    )
