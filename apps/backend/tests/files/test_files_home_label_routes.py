"""A member's home reads as its owner's current name, through the real routes.

A home is stored under its owner's id; what every surface shows for it is the
``home`` facet — the owner's id and their display name, resolved when the
payload is rendered — and, for a row inside a home, the ``pathHome`` facet that
names the home its path runs through. Pinned here end to end: the item read,
the listing, search, Recent and Shared with me all carry the facets; another
member reads the OWNER's name, not their own; the name follows a profile edit
at once; a changed address and name leave nothing of the old ones in any
response; a member with no name reads as the neutral fallback, never their
address; and the home's own name cannot be renamed away from its id.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio
from alkera_core.models.user import User
from backend.services.files.items import HOME_FALLBACK_NAME
from backend.services.identity import users as user_service
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client, login, make_member
from tests.files._files_kit import FilesOrgFixture, node_etag

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

Idem = Callable[[], dict[str, str]]


@dataclass(frozen=True)
class Member:
    client: AsyncClient
    user: User


@pytest_asyncio.fixture
async def owner(
    client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession, files_on: None
) -> Member:
    """The member whose home is under test, named, with a recognisable address."""
    token = uuid.uuid4().hex[:8]
    user, password = await make_member(
        real_session,
        org_id=files_org.org.org_id,
        email=f"robin+tideline{token}@example.test",
        first_name="Dana",
        last_name=f"Owner{token}",
        verified=True,
    )
    await real_session.commit()
    return Member(client=await login(app_client(), user.email, password or ""), user=user)


@pytest_asyncio.fixture
async def viewer(
    client: AsyncClient, files_org: FilesOrgFixture, real_session: AsyncSession, files_on: None
) -> Member:
    """A second plain member of the same org, on a session of their own."""
    user, password = await make_member(
        real_session,
        org_id=files_org.org.org_id,
        first_name="Vic",
        last_name="Viewer",
        verified=True,
    )
    await real_session.commit()
    return Member(client=await login(app_client(), user.email, password or ""), user=user)


async def _drive(client: AsyncClient) -> tuple[str, str]:
    drive = await client.get(f"{BASE}/drives")
    assert drive.status_code == 200, drive.text
    return str(drive.json()["id"]), str(drive.json()["homeId"])


async def _ok(response: Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


@dataclass(frozen=True)
class Home:
    drive_id: str
    home_id: str
    folder_id: str
    folder_name: str


async def _home_with_a_folder(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> Home:
    """The owner's home, a folder in it, and a ``Can view`` grant on the home
    for ``viewer``, all through the routes."""
    drive_id, home_id = await _drive(owner.client)
    folder_name = f"Quarterly{uuid.uuid4().hex[:6]}"
    made = await owner.client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/children",
        json={"name": folder_name, "kind": "folder"},
        headers=idem(),
    )
    assert made.status_code == 201, made.text
    granted = await owner.client.post(
        f"{BASE}/drives/{drive_id}/items/{home_id}/permissions",
        json={"principal": {"kind": "user", "id": str(viewer.user.id)}, "role": "reader"},
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(home_id))},
    )
    assert granted.status_code in (200, 201), granted.text
    return Home(drive_id, home_id, str(made.json()["id"]), folder_name)


def _label(user: User) -> str:
    return f"{user.first_name} {user.last_name}"


async def _surfaces(client: AsyncClient, home: Home) -> dict[str, dict[str, object]]:
    """Every read a Files screen draws a home or a row in it from."""
    base = f"{BASE}/drives/{home.drive_id}"
    return {
        "home": await _ok(await client.get(f"{base}/items/{home.home_id}")),
        "folder": await _ok(await client.get(f"{base}/items/{home.folder_id}")),
        "children": await _ok(await client.get(f"{base}/items/{home.home_id}/children")),
        "search": await _ok(await client.get(f"{base}/search", params={"q": home.folder_name})),
    }


def _row(page: dict[str, object], node_id: str) -> dict[str, object]:
    rows = page["value"]
    assert isinstance(rows, list)
    found = [row for row in rows if row["id"] == node_id]
    assert len(found) == 1, f"{node_id} not in {[row['id'] for row in rows]}"
    row = found[0]
    assert isinstance(row, dict)
    return row


async def test_the_owner_reads_their_home_as_their_name_on_every_surface(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> None:
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    seen = await _surfaces(owner.client, home)
    label = _label(owner.user)
    facet = {"node_id": home.home_id, "owner_id": str(owner.user.id), "owner_name": label}

    assert seen["home"]["home"] == {**facet, "schema_version": "1.0.0", "metadata": {}}
    assert seen["home"]["name"] == str(owner.user.id), "stored under the id, never the address"
    assert seen["home"]["pathHome"] == seen["home"]["home"]
    capabilities = seen["home"]["capabilities"]
    assert isinstance(capabilities, dict) and capabilities["can_rename"] is False

    assert seen["folder"]["home"] is None, "a folder in a home is not itself a home"
    assert seen["folder"]["pathHome"] == seen["home"]["home"]
    assert _row(seen["children"], home.folder_id)["pathHome"] == seen["home"]["home"]
    hit = _row(seen["search"], home.folder_id)
    assert hit["pathHome"] == seen["home"]["home"]
    assert hit["parentName"] == label, "a row directly in a home is located by the home's label"

    recent = await _ok(await owner.client.get(f"{BASE}/drives/{home.drive_id}/recent"))
    assert _row(recent, home.folder_id)["parentName"] == label


async def test_another_member_reads_the_owners_name_not_their_own(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> None:
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    seen = await _surfaces(viewer.client, home)
    shared = await _ok(await viewer.client.get(f"{BASE}/drives/{home.drive_id}/sharedWithMe"))

    for body in (seen["home"], _row(shared, home.home_id)):
        facet = body["home"]
        assert isinstance(facet, dict)
        assert facet["owner_id"] == str(owner.user.id)
        assert facet["owner_name"] == _label(owner.user)
        assert _label(viewer.user) not in str(facet)
    folder_facet = seen["folder"]["pathHome"]
    assert isinstance(folder_facet, dict)
    assert folder_facet["owner_name"] == _label(owner.user)


async def test_an_address_and_name_change_leaves_nothing_of_the_old_ones(
    owner: Member,
    viewer: Member,
    files_client: AsyncClient,
    real_session: AsyncSession,
    idem: Idem,
) -> None:
    """The customer hand-over: the account's address and name are replaced,
    and no response any reader gets names the old ones — not the home's name,
    not its label, not a path, not a location."""
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    old_email = owner.user.email
    old_local = old_email.rpartition("@")[0]
    old_last = owner.user.last_name
    user = await real_session.get(User, owner.user.id)
    assert user is not None
    await user_service.update_profile(
        real_session,
        user,
        email=f"customer{uuid.uuid4().hex[:8]}@customer.test",
        first_name="Casey",
        last_name="Customer",
    )
    await real_session.commit()

    for reader in (viewer.client, files_client):
        seen = await _surfaces(reader, home)
        seen["sharedWithMe"] = await _ok(
            await reader.get(f"{BASE}/drives/{home.drive_id}/sharedWithMe")
        )
        for surface, body in seen.items():
            text = str(body)
            assert old_local not in text, f"{surface} still names the old address"
            assert old_last not in text, f"{surface} still names the old name"
        facet = seen["home"]["home"]
        assert isinstance(facet, dict) and facet["owner_name"] == "Casey Customer"


async def test_a_member_with_no_name_reads_as_the_fallback_never_their_address(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> None:
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    user = await real_session.get(User, owner.user.id)
    assert user is not None
    user.first_name = ""
    user.last_name = ""
    await real_session.commit()

    seen = await _surfaces(viewer.client, home)
    facet = seen["home"]["home"]
    assert isinstance(facet, dict) and facet["owner_name"] == HOME_FALLBACK_NAME
    # What a home is CALLED on every surface — its label, the label of the
    # home a path runs through, a row's location, the stored name — never the
    # address. (The owner column is a different label: an unnamed person is
    # identified there by their current address, as everywhere else.)
    local = owner.user.email.rpartition("@")[0]
    labels = [
        seen["home"]["home"],
        seen["home"]["pathHome"],
        seen["home"]["name"],
        seen["folder"]["pathHome"],
        _row(seen["search"], home.folder_id)["parentName"],
    ]
    assert not [label for label in labels if local in str(label)]
    assert _row(seen["search"], home.folder_id)["parentName"] == HOME_FALLBACK_NAME


async def test_a_home_cannot_be_renamed_away_from_its_id(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> None:
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    refused = await owner.client.patch(
        f"{BASE}/drives/{home.drive_id}/items/{home.home_id}",
        json={"name": "My stuff"},
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(home.home_id))},
    )
    assert refused.status_code == 422, refused.text
    kept = await _ok(await owner.client.get(f"{BASE}/drives/{home.drive_id}/items/{home.home_id}"))
    assert kept["name"] == str(owner.user.id)


async def test_the_trash_says_where_a_row_was_by_the_homes_label(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> None:
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    trashed = await owner.client.delete(
        f"{BASE}/drives/{home.drive_id}/items/{home.folder_id}",
        headers={**idem(), "If-Match": await node_etag(real_session, uuid.UUID(home.folder_id))},
    )
    assert trashed.status_code in (200, 202, 204), trashed.text
    page = await _ok(await owner.client.get(f"{BASE}/drives/{home.drive_id}/trash"))
    entries = page["entries"]
    assert isinstance(entries, list)
    [entry] = [row for row in entries if row["item"]["id"] == home.folder_id]
    assert entry["originalPath"] == f"/home/{_label(owner.user)}"
    assert entry["item"]["pathHome"]["owner_name"] == _label(owner.user)


async def test_a_copy_of_a_home_is_an_ordinary_folder(
    owner: Member, viewer: Member, real_session: AsyncSession, idem: Idem
) -> None:
    """The viewer copies the owner's home into their own: the copy carries the
    owner's id as its name and none of a home's identity."""
    home = await _home_with_a_folder(owner, viewer, real_session, idem)
    _drive_id, viewer_home = await _drive(viewer.client)
    copied = await viewer.client.post(
        f"{BASE}/drives/{home.drive_id}/items/{home.home_id}/copy",
        json={"parentId": viewer_home},
        headers=idem(),
    )
    assert copied.status_code in (200, 201, 202), copied.text
    listed = await _ok(
        await viewer.client.get(f"{BASE}/drives/{home.drive_id}/items/{viewer_home}/children")
    )
    rows = listed["value"]
    assert isinstance(rows, list)
    [copy] = [row for row in rows if row["name"] == str(owner.user.id)]
    assert copy["id"] != home.home_id
    assert copy["home"] is None
    assert copy["subtype"] != "home"
    path_home = copy["pathHome"]
    assert isinstance(path_home, dict) and path_home["owner_id"] == str(viewer.user.id)
