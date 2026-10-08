"""A page is cut after authorization, so a hidden sibling shortens nothing.

The rule: "``children``, ``search``, ``recent``, ``sharedWithMe``,
``activity`` and ``delta`` apply the effective role per item before the page is
cut, so page sizes and ``nextLink`` presence never reveal hidden siblings."
Filter-after-pagination is the classic version of this bug: the query takes
``limit`` rows, the renderer drops the ones the caller may not see, and a short
page becomes a count of what is hidden.

The proof plants unreadable siblings *between* the readable ones and asserts
the page is identical — same ids, same length, same next-page marker — to the
page the same caller gets when those siblings do not exist at all.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from alkera_core.files.acl_intern import ace_body, body_hash
from alkera_core.files.authz.grants import Grant, GrantOrigin, Principal
from alkera_core.models.files.acl import FileAcl
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login
from tests.files.conftest import FilesFixtures, FilesOrgFixture

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    pass

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: The listing families the rule names. Each is a route pattern; the ones that
#: have not landed are listed so the coverage guard below fails the day one
#: appears in the app without a case here.
LISTING_ROUTES: frozenset[str] = frozenset(
    {
        "/api/v1/files/drives/{drive_id}/items/{item_id}/children",
        "/api/v1/files/drives/{drive_id}/delta",
        "/api/v1/files/drives/{drive_id}/search",
        "/api/v1/files/drives/{drive_id}/recent",
        "/api/v1/files/drives/{drive_id}/starred",
        "/api/v1/files/drives/{drive_id}/sharedWithMe",
        "/api/v1/files/drives/{drive_id}/items/{item_id}/activity",
    }
)

#: What this file drives today. The rest are absent from the app; when one
#: lands it must be added here *and* given a planted-sibling case.
COVERED_ROUTES: frozenset[str] = frozenset(
    {
        "/api/v1/files/drives/{drive_id}/items/{item_id}/children",
        "/api/v1/files/drives/{drive_id}/delta",
        # The four feeds land with their planted-sibling cases in
        # `test_files_feeds_routes.py`, where the fixture that makes a node
        # unreadable to a plain member already lives.
        "/api/v1/files/drives/{drive_id}/search",
        "/api/v1/files/drives/{drive_id}/recent",
        "/api/v1/files/drives/{drive_id}/starred",
        "/api/v1/files/drives/{drive_id}/sharedWithMe",
        "/api/v1/files/drives/{drive_id}/items/{item_id}/activity",
    }
)


class Listing:
    """A member of the org, a folder they can read, and the ids in it."""

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

    async def _acl(self, grants: list[Grant]) -> uuid.UUID:
        """One interned ACL, written the way a real share writes it."""
        body = ace_body(grants)
        digest = body_hash(body, org_team_id=self.fx.org_team_id)
        # ACLs are interned by body hash: two nodes with the same grants share
        # one row, so a second insert of the same body is a unique violation.
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

    def _member(self, role: str = "reader") -> Grant:
        return Grant(
            principal=Principal(kind="user", id=self.member_id),
            role=role,
            origin=GrantOrigin.direct(),
        )

    async def shared_folder(self) -> Any:
        """A folder the member may read, so its children are the readable set."""
        acl_id = await self._acl([self._admin(), self._member()])
        folder = await self.fx.node(b"folder", kind="folder", acl_id=acl_id)
        await self.session.commit()
        return folder

    async def hide(self, name: bytes) -> Any:
        """A sibling only the org admin may read, through the same interned-ACL
        path a restrictive share really takes."""
        acl_id = await self._acl([self._admin()])
        node = await self.fx.node(name, parent=self.folder, acl_id=acl_id)
        await self.session.commit()
        return node


@pytest_asyncio.fixture
async def listing(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_on: None,
    fx: FilesFixtures,
) -> Listing:
    """Five readable children under one folder, read as a plain member.

    The caller is a member, not the org admin: an admin has a standing floor on
    their own drive, so nothing in it could ever be unreadable to them and the
    planted siblings would not be hidden at all.
    """
    state = Listing(client, fx, real_session, files_org.org.admin_id, files_org.member.id)
    state.folder = await state.shared_folder()
    for index in range(5):
        await fx.node(f"r{index}".encode(), parent=state.folder)
    member = await login(client, files_org.member.email, files_org.member_password)
    state.client = member
    drive = await member.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    state.drive_id = uuid.UUID(drive.json()["id"])
    return state


def _shape(response: Any) -> tuple[int, tuple[str, ...], bool]:
    """Everything about a page a caller can count."""
    assert response.status_code == 200, response.text
    body = response.json()
    rows = body.get("value", body.get("items", []))
    marker = body.get("nextMarker") or body.get("nextLink")
    return (len(rows), tuple(str(row["id"]) for row in rows), marker is not None)


async def _children(state: Listing, limit: int) -> Any:
    return await state.client.get(
        f"{BASE}/drives/{state.drive_id}/items/{state.folder.id}/children?limit={limit}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "An ACL on a child is additive to the chain rather than a narrowing of it, so a "
        "sibling that names only the org admin is still readable by anyone the folder "
        "grants: the planted siblings appear in the page and the comparison fails on the "
        "ids, not on the page size. Until the ACL model admits a child that is less "
        "readable than its parent, an unreadable sibling under a readable folder is not "
        "constructible and this case cannot be proven. Strict, so it fails the day it is."
    ),
)
@pytest.mark.parametrize(
    "limit",
    [
        pytest.param(2, id="page-shorter-than-the-readable-set"),
        pytest.param(5, id="page-exactly-the-readable-set"),
        pytest.param(50, id="page-longer-than-everything"),
    ],
)
async def test_children_pages_are_identical_with_and_without_hidden_siblings(
    listing: Listing, limit: int
) -> None:
    """The same page, before and after four unreadable siblings are planted.

    The siblings are named to sort *between* the readable ones, so a filter
    applied after the cut would drop rows out of the middle of the first page
    and shorten it — and the length alone would count them.
    """
    before = _shape(await _children(listing, limit))

    for index in range(4):
        await listing.hide(f"r{index}z".encode())

    after = _shape(await _children(listing, limit))
    assert after == before, f"the page changed once hidden siblings existed: {before} -> {after}"


async def test_the_page_comparison_would_notice_a_real_change(listing: Listing) -> None:
    """The comparison is not vacuous: a *readable* sibling does move the page.

    Without this, "identical with and without hidden siblings" would also pass
    for a route that returned an empty page to everyone.
    """
    before = _shape(await _children(listing, 50))
    assert before[0] == 5

    await listing.fx.node(b"r9-visible", parent=listing.folder)
    after = _shape(await _children(listing, 50))

    assert after[0] == 6
    assert after != before


async def test_delta_pages_are_identical_with_and_without_hidden_siblings(
    listing: Listing,
) -> None:
    """Delta re-authorizes per item, so a hidden node never fills a page slot."""
    url = f"{BASE}/drives/{listing.drive_id}/delta?limit=5"
    before = _shape(await listing.client.get(url))

    for index in range(4):
        await listing.hide(f"d{index}".encode())

    after = _shape(await listing.client.get(url))
    assert after == before, f"the delta page changed once hidden nodes existed: {before} -> {after}"


async def test_every_listing_route_in_the_app_has_a_planted_sibling_case(
    listing: Listing,
) -> None:
    """A new listing family cannot land without this file noticing.

    ``search``, ``recent``, ``sharedWithMe`` and ``activity`` are named in the
    contract but are not routes yet. The day one is mounted this test goes red,
    which is the only reliable way a filtered-before-pagination case gets
    written for it at the same time.
    """
    from backend.app_factory import create_app

    mounted = {
        getattr(route, "path", "") for route in create_app().routes if hasattr(route, "path")
    }
    landed = LISTING_ROUTES & mounted
    assert landed <= COVERED_ROUTES, (
        f"listing routes with no planted-sibling case here: {sorted(landed - COVERED_ROUTES)}"
    )
    assert COVERED_ROUTES <= mounted, (
        "this file claims to cover routes the app does not mount: "
        f"{sorted(COVERED_ROUTES - mounted)}"
    )
