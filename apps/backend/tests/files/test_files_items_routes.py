"""The status contract of the drive and item routes, one case per status.

Every test here drives the real app through ``ASGITransport`` against real
Postgres and a filesystem store under ``tmp_path``, so a green case is the
route, the library, the policy and the migration agreeing — not a mock echoing
its own script back.

The isolation cases are the point of the file: the three "not yours" classes
are asserted *byte-identical*, because a body, a code or a status that differed
between them would be an oracle for a node the caller may not know exists.
"""

from __future__ import annotations

import base64
import unicodedata
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
from _files_kit import NOT_FOUND, counting, node_etag, refusal
from alkera_core.authz.headers import agent_headers
from alkera_core.config import settings
from alkera_core.files import names
from alkera_core.files import namespace as namespace_module
from alkera_core.files.surfaces import encode_for_surface
from alkera_core.models import TeamMembership, TeamRole, User
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.files.item import Item, ObjectFacet
from alkera_core.temporal import WorkflowType
from alkera_core.verification import is_blocked
from backend.services.objects import object_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import login, make_member

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [
    pytest.mark.asyncio,
    # Every case builds its own org, drive and subtree and asserts only on the
    # rows it created, so the cases may land on different workers.
    pytest.mark.spread,
]

BASE = "/api/v1/files"


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _root_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    return str(response.json()["rootId"])


async def _shared_id(client: AsyncClient) -> str:
    """``/Shared`` — the top-of-drive folder a caller may actually write into.

    The root is a traversal-only signpost and refuses every direct write, so a
    test that wants a parent near the top of the tree asks for this one.
    """
    drive = str((await client.get(f"{BASE}/drives")).json()["id"])
    root = await _root_id(client)
    rows = (await client.get(f"{BASE}/drives/{drive}/items/{root}/children")).json()
    return str(next(row for row in rows["value"] if row["name"] == "Shared")["id"])


def _etag(item: dict[str, object]) -> dict[str, str]:
    return {"If-Match": str(item["etag"])}


async def _chat_node(client: AsyncClient) -> tuple[str, str]:
    """A chat created through the real route, as ``(chat id, Files node id)``."""
    response = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["files_node_id"], "a chat created with Files on names its node"
    return str(body["id"]), str(body["files_node_id"])


async def _template_object(session: AsyncSession, owner_id: uuid.UUID) -> str:
    """A chat template, as its object id.

    Seeded through the object service rather than a route: a template is minted
    by saving a chat, and this case only needs the row and the folder the
    bridge files for it.

    The service does not echo a Files node id the way the chat route does, so
    the node is found the way a browsing client finds it: in the listing of the
    folder templates live in, by the object the facet names.
    """
    owner = await session.get(User, owner_id)
    assert owner is not None
    created, _ = await object_service.create_object(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="chat_template",
        title="Revenue by region",
        spec={"title": "Revenue by region", "brief": "Pull the revenue numbers."},
        logical_id="t-1",
    )
    await session.commit()
    return str(created.id)


async def test_a_chats_node_says_what_it_is_and_where_it_opens(
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """A row-backed node is not its contents: somebody who opens the chat's row
    in Files expects the chat.

    The node carries that destination itself — the ``object`` facet names the
    type and the page — so a client follows one field and needs no table of its
    own mapping object kinds to routes. Without it the facet reads
    ``{"type": "", "web_url": null}`` and the row is inert: nothing tells a
    client that this folder is a conversation or where it opens.

    The chat's node is a **folder** — ``<Title>.alkerachat``, holding the
    working directory a run leases — and so is a chat template's, which holds
    the files and the brief it hands the next conversation. That is exactly why
    the facet is keyed on the object behind the node and never on the node's
    kind: ``kind`` no longer distinguishes the object types from each other at
    all, and a client that switched on it would list a conversation instead of
    opening it.
    """
    chat_id, node_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    response = await files_client.get(f"{BASE}/drives/{drive}/items/{node_id}")
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["kind"] == "folder", "a chat owns a working directory; its node is a folder"
    # The facet also names the node the chat's files live at — its working
    # directory, a child of this folder — so a client opens the files there.
    files_node = item["object"]["metadata"].pop("files_node_id")
    children = await files_client.get(f"{BASE}/drives/{drive}/items/{node_id}/children")
    assert files_node in {row["id"] for row in children.json()["value"]}
    assert item["object"] == {
        "schema_version": "1.1.0",
        "metadata": {},
        "type": "chat",
        "id": chat_id,
        "title": "Quarterly review",
        "web_url": f"/chat/{chat_id}",
        "app_url": None,
    }

    template_id = await _template_object(real_session, files_org.org.admin_id)
    # Each folder-backed kind is filed where its kind says: a chat under
    # `Chats`, a template under `Chat Templates`, both directly under the home.
    home = str((await files_client.get(f"{BASE}/drives")).json()["homeId"])
    places = await files_client.get(f"{BASE}/drives/{drive}/items/{home}/children")
    assert places.status_code == 200, places.text
    templates = next(row for row in places.json()["value"] if row["name"] == "Chat Templates")
    listing = await files_client.get(f"{BASE}/drives/{drive}/items/{templates['id']}/children")
    assert listing.status_code == 200, listing.text
    rows = [
        row for row in listing.json()["value"] if (row["object"] or {}).get("id") == template_id
    ]
    assert rows, "the template's node is in the drive the chat's node is in"
    row = rows[0]
    assert row["kind"] == "folder", "a template hands a new chat its files; its node is a folder"
    # A template's facet names its scratch the way a chat's names its working
    # directory: the node a new chat copies from, a child of the template's folder.
    template_scratch = row["object"]["metadata"].pop("files_node_id")
    scratch_rows = await files_client.get(f"{BASE}/drives/{drive}/items/{row['id']}/children")
    assert scratch_rows.status_code == 200, scratch_rows.text
    assert template_scratch in {child["id"] for child in scratch_rows.json()["value"]}
    assert row["object"] == {
        "schema_version": "1.1.0",
        "metadata": {},
        "type": "chat_template",
        "id": template_id,
        "title": "Revenue by region",
        # A template is neither a conversation nor an object page: it opens on
        # the surface a person starts a chat from.
        "web_url": f"/templates/{template_id}",
        "app_url": None,
    }


async def test_an_object_node_carries_the_objects_live_title_not_its_folder_name(
    files_client: AsyncClient, files_on: None
) -> None:
    """The row must render the CHAT, and the node's name cannot supply it.

    A chat's node is named ``<Title>.alkerachat`` — minted from the title the
    day it was created, a uuid when the chat was untitled — and is a filesystem
    name from then on. Renaming the chat rewrites the row and never the folder,
    so a surface that renders the node name shows a person a string they never
    typed for a conversation they have already named. The facet therefore
    carries the title the object has NOW, and a rename lands on the next read.
    """
    chat_id, node_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    item = (await files_client.get(f"{BASE}/drives/{drive}/items/{node_id}")).json()
    home = str(item["parentId"])

    # The name is the folder's; the title is the chat's, and they are read apart.
    assert item["name"].endswith(".alkerachat")
    assert item["object"]["title"] == "Quarterly review"
    assert ".alkerachat" not in item["object"]["title"]

    renamed = await files_client.put(
        f"/api/v1/objects/{chat_id}",
        json={"title": "Redshift cold starts", "expected_version": 1},
    )
    assert renamed.status_code == 200, renamed.text

    # The node itself has not moved or been renamed — only the title it reports.
    after = (await files_client.get(f"{BASE}/drives/{drive}/items/{node_id}")).json()
    assert after["name"] == item["name"], "renaming a chat does not rename its folder"
    assert after["object"]["title"] == "Redshift cold starts"

    # And the listing agrees with the direct read, in the same one statement it always took.
    listing = await files_client.get(f"{BASE}/drives/{drive}/items/{home}/children")
    assert listing.status_code == 200, listing.text
    row = next(r for r in listing.json()["value"] if r["id"] == node_id)
    assert row["object"]["title"] == "Redshift cold starts"


async def test_the_titles_for_a_page_are_one_statement_however_many_rows(
    files_client: AsyncClient, files_on: None
) -> None:
    """The title read is batched for the page, not issued per row.

    A statement per row is the cost every other lookup on this route — owners,
    leases — is batched to avoid, and it is the failure mode a facet enrichment
    invites. Three object rows must cost exactly what one costs.
    """
    drive = await _drive_id(files_client)
    first_id, node_id = await _chat_node(files_client)
    home = str(
        (await files_client.get(f"{BASE}/drives/{drive}/items/{node_id}")).json()["parentId"]
    )
    listing = f"{BASE}/drives/{drive}/items/{home}/children"

    with counting() as one_row:
        page = await files_client.get(listing)
    assert page.status_code == 200, page.text
    assert [(r["object"] or {}).get("title") for r in page.json()["value"]] == ["Quarterly review"]

    for _ in range(2):
        await _chat_node(files_client)

    with counting() as three_rows:
        page = await files_client.get(listing)
    assert page.status_code == 200, page.text
    titles = [(r["object"] or {}).get("title") for r in page.json()["value"]]
    assert titles == ["Quarterly review"] * 3, "every chat on the page carries its title"
    assert len(three_rows) == len(one_row), (
        "the titles for a page are resolved in one statement, not one per row"
    )
    assert first_id


async def test_the_same_destination_arrives_in_the_listing(
    files_client: AsyncClient, files_on: None
) -> None:
    """One builder, one facet: a client that discovers the chat by browsing its
    parent gets the destination a direct read gives, not a thinner item."""
    chat_id, node_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    item = (await files_client.get(f"{BASE}/drives/{drive}/items/{node_id}")).json()
    parent = item["parentId"]
    listing = await files_client.get(f"{BASE}/drives/{drive}/items/{parent}/children")
    assert listing.status_code == 200, listing.text
    rows = {row["id"]: row for row in listing.json()["value"]}
    assert rows[node_id]["object"]["web_url"] == f"/chat/{chat_id}"
    assert rows[node_id]["object"]["type"] == "chat"


# ---------------------------------------------------------------------------
# the flag
# ---------------------------------------------------------------------------


async def test_dark_deployment_is_indistinguishable_from_an_empty_one(
    files_client: AsyncClient, files_off: None
) -> None:
    """With ``files_enabled`` off the surface is still in the schema but every
    route answers the opaque 404 — the same body a nonexistent node gets."""
    assert settings.files_enabled is False
    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


async def test_a_dark_deployment_answers_every_caller_the_same_404(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_off: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dark is decided before the credential is, so who asks cannot change the
    answer.

    Three callers the product answers three different ways on a live
    deployment — nobody at all, an account whose verification grace has lapsed,
    and a member in good standing — are byte-identical here. A 401 tells an
    anonymous prober the path is a real route behind auth; a 403
    ``email_verification_required`` tells a squatter the surface exists and is
    merely gated; and a dark route whose status moves with the caller is not
    dark at all.
    """
    assert settings.files_enabled is False
    blocked, blocked_password = await make_member(
        real_session, org_id=files_org.org.org_id, verified=False
    )
    await real_session.commit()
    # No grace left, so the verification gate is the 403 the kill switch has to
    # get in front of rather than a window that happens to still be open.
    monkeypatch.setattr(settings, "email_verification_grace_period_days", 0)
    assert is_blocked(blocked), "the gate this case is about must actually be armed"

    anonymous = await client.get(f"{BASE}/drives")
    lapsed_client = await login(client, blocked.email, blocked_password or "")
    lapsed = await lapsed_client.get(f"{BASE}/drives")
    member_client = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    member = await member_client.get(f"{BASE}/drives")

    answers = [(reply.status_code, refusal(reply)) for reply in (anonymous, lapsed, member)]
    assert answers == [(404, NOT_FOUND)] * 3


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


async def test_drive_route_returns_the_one_org_drive(
    files_client: AsyncClient, files_on: None
) -> None:
    response = await files_client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert uuid.UUID(body["rootId"])
    # Asking twice is asking about the same drive: the ensure is idempotent.
    again = await files_client.get(f"{BASE}/drives")
    assert again.json()["id"] == body["id"]


async def test_item_payload_carries_etag_ctag_capabilities_and_no_path_key(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """The wire item's identity fields are all present and ``path`` is null —
    a client that keyed on the path would break on every rename."""
    node = await fx.node(b"report.txt")
    drive = await _drive_id(files_client)
    response = await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["etag"] and item["ctag"]
    assert item["capabilities"]["can_read"] is True
    assert item["path"] is None
    assert item["pathBytes"]


async def test_path_form_resolves_three_deep_with_percent_encoded_bytes(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """A three-deep path resolves, and a non-ASCII segment survives the URL:
    the route decodes the escape the library's own posix surface encoder
    produced, so the bytes it looks up are the bytes the tree stores."""
    a = await fx.node(b"a", kind="folder")
    b = await fx.node("café".encode(), kind="folder", parent=a)
    leaf = await fx.node(b"deep.txt", parent=b)
    drive = await _drive_id(files_client)
    segment = quote(encode_for_surface("café".encode(), "posix"))
    response = await files_client.get(f"{BASE}/drives/{drive}/root:/a/{segment}/deep.txt")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(leaf.id)


async def test_path_form_for_a_missing_path_is_the_opaque_404(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    await fx.drive()
    drive = await _drive_id(files_client)
    response = await files_client.get(f"{BASE}/drives/{drive}/root:/nowhere/at/all")
    assert response.status_code == 404
    assert refusal(response) == NOT_FOUND


# ---------------------------------------------------------------------------
# the three "not yours" classes
# ---------------------------------------------------------------------------


async def test_the_three_not_yours_classes_are_byte_identical(
    client: AsyncClient,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """A nonexistent id, another org's real node, and a well-formed id nobody
    owns must be indistinguishable on status, body and code."""
    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    # The same factory the caller's own tree came from, pointed at their org.
    theirs = type(fx)(real_session, other_org.id, other_admin.id)
    their_node = await theirs.node(b"secret.txt")

    mine = await login(client, files_org.org.admin_email, files_org.org.admin_password)
    drive = await _drive_id(mine)
    answers = []
    for probe in (uuid.uuid4(), their_node.id, uuid.uuid4()):
        response = await mine.get(f"{BASE}/drives/{drive}/items/{probe}")
        answers.append((response.status_code, refusal(response)))
    assert answers[0] == answers[1] == answers[2] == (404, NOT_FOUND)


# ---------------------------------------------------------------------------
# children: filters, markers, creation
# ---------------------------------------------------------------------------


async def test_children_ands_two_filters_and_round_trips_a_marker(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    """Two filters narrow together (not either-or), and paging with the marker
    the first page returned yields the rest with no duplicate and no miss."""
    folder = await fx.node(b"box", kind="folder")
    for index in range(4):
        await fx.node(f"big{index}.bin".encode(), parent=folder, size=5_000)
    await fx.node(b"small.bin", parent=folder, size=1)
    await fx.node(b"a-folder", kind="folder", parent=folder, size=9_000)
    drive = await _drive_id(files_client)
    base = f"{BASE}/drives/{drive}/items/{folder.id}/children"

    both = await files_client.get(f"{base}?kind=file&sizeMin=1000")
    assert both.status_code == 200, both.text
    names = sorted(item["name"] for item in both.json()["value"])
    assert names == ["big0.bin", "big1.bin", "big2.bin", "big3.bin"]

    first = await files_client.get(f"{base}?kind=file&sizeMin=1000&limit=2")
    page_one = first.json()
    assert len(page_one["value"]) == 2
    assert page_one["nextMarker"]
    second = await files_client.get(
        f"{base}?kind=file&sizeMin=1000&limit=2&marker={page_one['nextMarker']}"
    )
    page_two = second.json()
    seen = [item["id"] for item in page_one["value"]] + [item["id"] for item in page_two["value"]]
    assert len(seen) == len(set(seen)) == 4


async def test_children_refuses_an_unknown_filter_and_an_over_cap_limit(
    files_client: AsyncClient, files_on: None, fx: FilesFixtures
) -> None:
    folder = await fx.node(b"box2", kind="folder")
    drive = await _drive_id(files_client)
    base = f"{BASE}/drives/{drive}/items/{folder.id}/children"
    misspelled = await files_client.get(f"{base}?mimeclass=image")
    assert misspelled.status_code == 422
    assert misspelled.json()["code"] == "files.unknown_filter"
    too_big = await files_client.get(f"{base}?limit=1001")
    assert too_big.status_code == 422
    assert too_big.json()["code"] == "files.bad_limit"


async def test_create_child_returns_201_and_the_item(
    files_client: AsyncClient, files_on: None, idem: Callable[[], dict[str, str]]
) -> None:
    drive = await _drive_id(files_client)
    root = await _shared_id(files_client)
    response = await files_client.post(
        f"{BASE}/drives/{drive}/items/{root}/children",
        json={"name": "notes", "kind": "folder"},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "notes"
    assert body["kind"] == "folder"
    assert body["capabilities"]["can_write"] is True


async def test_create_child_on_a_live_duplicate_is_409(
    files_client: AsyncClient, files_on: None, idem: Callable[[], dict[str, str]]
) -> None:
    """The refusal is the partial unique index, so it holds even against a
    writer that took the name between the check and the insert."""
    drive = await _drive_id(files_client)
    root = await _shared_id(files_client)
    body = {"name": "twice", "kind": "folder"}
    first = await files_client.post(
        f"{BASE}/drives/{drive}/items/{root}/children", json=body, headers=idem()
    )
    assert first.status_code == 201
    second = await files_client.post(
        f"{BASE}/drives/{drive}/items/{root}/children", json=body, headers=idem()
    )
    assert second.status_code == 409, second.text


async def test_create_child_in_another_normalization_form_is_409(
    files_client: AsyncClient, files_on: None, idem: Callable[[], dict[str, str]]
) -> None:
    """``é`` as one code point and as ``e`` plus a combining accent are one name
    on a Mac: the second create is the same 409 an exact duplicate gets."""
    drive = await _drive_id(files_client)
    root = await _shared_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{root}/children"
    first = await files_client.post(
        url,
        json={"name": unicodedata.normalize("NFC", "é"), "kind": "folder"},
        headers=idem(),
    )
    assert first.status_code == 201, first.text
    second = await files_client.post(
        url,
        json={"name": unicodedata.normalize("NFD", "é"), "kind": "folder"},
        headers=idem(),
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "files.exists"


#: The names the drive refuses, and the code each one leaves as. The long one is
#: one byte past ``names.NAME_MAX_BYTES``, grown from it rather than spelled.
_REFUSED_NAMES = [
    pytest.param("a/b", "files.invalid_name.separator", id="separator"),
    pytest.param("a" * (names.NAME_MAX_BYTES + 1), "files.invalid_name.too_long", id="too-long"),
    pytest.param("a\tb", "files.invalid_name.control", id="control"),
    pytest.param("notes ", "files.invalid_name.surrounding_space", id="trailing-space"),
    pytest.param("..", "files.invalid_name.dot", id="dotdot"),
    pytest.param(".", "files.invalid_name.dot", id="dot"),
    pytest.param("a\x00b", "files.invalid_name.nul", id="nul"),
    pytest.param("", "files.invalid_name.empty", id="empty"),
]


@pytest.mark.parametrize(("name", "code"), _REFUSED_NAMES)
async def test_create_child_with_a_refused_name_is_422_naming_the_rule(
    files_client: AsyncClient,
    files_on: None,
    idem: Callable[[], dict[str, str]],
    name: str,
    code: str,
) -> None:
    """RED before the translation: ``names.validate`` raises a plain
    ``ValueError``, which no handler maps — so a folder named with a ``/`` or
    300 characters came back as an opaque 500 and the person was invited to
    retry something that can never succeed."""
    drive = await _drive_id(files_client)
    root = await _shared_id(files_client)

    answer = await files_client.post(
        f"{BASE}/drives/{drive}/items/{root}/children",
        json={"name": name, "kind": "folder"},
        headers=idem(),
    )

    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code


@pytest.mark.parametrize(("name", "code"), _REFUSED_NAMES)
async def test_rename_to_a_refused_name_is_422_and_changes_nothing(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    name: str,
    code: str,
) -> None:
    """The same rule on the same node reached the other way round."""
    node = await fx.node(f"keep-{code}.txt".encode())
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await files_client.get(url)).json()

    answer = await files_client.patch(
        url, json={"name": name}, headers={**idem(), **_etag(fetched)}
    )

    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == code
    assert (await files_client.get(url)).json()["name"] == f"keep-{code}.txt"


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        pytest.param("post", "/children", {"name": "x"}, id="create-child"),
        pytest.param("post", "/tree", {"paths": ["x"]}, id="create-tree"),
        pytest.param("patch", "", {"name": "x"}, id="patch"),
        pytest.param("delete", "", None, id="delete"),
        pytest.param("post", "/copy", {"parentId": str(uuid.uuid4())}, id="copy"),
        pytest.param("put", "/star", None, id="star"),
        pytest.param("delete", "/star", None, id="unstar"),
    ],
)
async def test_every_non_get_without_an_idempotency_key_is_428(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    """428, not 400: the request is otherwise valid and becomes acceptable the
    moment the header is added."""
    node = await fx.node(b"keyed.txt", kind="folder")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}{suffix}"
    call = getattr(files_client, method)
    response = await (call(url, json=body) if body is not None else call(url))
    assert response.status_code == 428, response.text


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        pytest.param("patch", "", {"name": "renamed"}, id="patch"),
        pytest.param("delete", "", None, id="delete"),
        pytest.param("put", "/star", None, id="star"),
    ],
)
async def test_every_mutation_without_if_match_is_428(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    method: str,
    suffix: str,
    body: dict[str, object] | None,
) -> None:
    node = await fx.node(b"unmatched.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}{suffix}"
    call = getattr(files_client, method)
    headers = idem()
    response = await (
        call(url, json=body, headers=headers) if body is not None else call(url, headers=headers)
    )
    assert response.status_code == 428, response.text
    assert response.json()["code"] == "files.if_match_required"
    # The refusal has to say where the value comes from, or the caller reads it
    # as a bug in their request rather than a header they never knew to send.
    assert "etag" in response.json()["message"].lower()


@pytest.mark.parametrize(
    "by_path", [pytest.param(False, id="by-id"), pytest.param(True, id="by-path")]
)
async def test_a_read_returns_the_etag_as_a_header_if_match_takes_verbatim(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    by_path: bool,
) -> None:
    """Every mutation on a node demands its etag back in `If-Match`, and HTTP
    says that value lives in `ETag`. Digging it out of a JSON field instead is a
    step nobody expects, so the read serves it both ways - and the header's own
    spelling has to be one `If-Match` accepts, not merely one that looks right.
    """
    node = await fx.node(b"etag-header.txt")
    drive = await _drive_id(files_client)
    url = (
        f"{BASE}/drives/{drive}/root:/{quote(node.name.decode())}"
        if by_path
        else f"{BASE}/drives/{drive}/items/{node.id}"
    )
    read = await files_client.get(url)
    assert read.status_code == 200, read.text
    header = read.headers.get("ETag")
    assert header == f'"{read.json()["etag"]}"'

    # The proof it is the right spelling: hand the header straight back.
    starred = await files_client.put(
        f"{BASE}/drives/{drive}/items/{node.id}/star",
        headers={**idem(), "If-Match": header},
    )
    assert starred.status_code == 200, starred.text


async def test_a_malformed_if_match_is_422_not_412(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A header the caller got *wrong* is not the same as one that is merely
    stale: the first can never succeed on retry, the second can."""
    node = await fx.node(b"badmatch.txt")
    drive = await _drive_id(files_client)
    response = await files_client.patch(
        f"{BASE}/drives/{drive}/items/{node.id}",
        json={"name": "x"},
        headers={**idem(), "If-Match": '"not-a-number"'},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "files.bad_if_match"


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


async def test_patch_renames_and_a_stale_if_match_is_412_that_changed_nothing(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The precondition rides inside the UPDATE, so the losing caller does not
    half-apply: the name is exactly what the winner left."""
    node = await fx.node(b"before.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await files_client.get(url)).json()

    renamed = await files_client.patch(
        url, json={"name": "after.txt"}, headers={**idem(), **_etag(fetched)}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "after.txt"

    stale = await files_client.patch(
        url, json={"name": "third.txt"}, headers={**idem(), **_etag(fetched)}
    )
    assert stale.status_code == 412, stale.text
    assert (await files_client.get(url)).json()["name"] == "after.txt"


async def test_patch_moves_by_parent_id(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    target = await fx.node(b"dest", kind="folder")
    moving = await fx.node(b"moves.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{moving.id}"
    fetched = (await files_client.get(url)).json()
    response = await files_client.patch(
        url, json={"parentId": str(target.id)}, headers={**idem(), **_etag(fetched)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["parentId"] == str(target.id)


async def test_moving_a_folder_into_its_own_subtree_is_409(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The cycle guard is inside the UPDATE, so a move that was legal when the
    caller looked is still refused if another move made it a cycle first."""
    parent = await fx.node(b"outer", kind="folder")
    child = await fx.node(b"inner", kind="folder", parent=parent)
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{parent.id}"
    fetched = (await files_client.get(url)).json()
    response = await files_client.patch(
        url, json={"parentId": str(child.id)}, headers={**idem(), **_etag(fetched)}
    )
    assert response.status_code == 409, response.text
    assert "cycle" in response.json()["code"]


# ---------------------------------------------------------------------------
# delete, star, copy, tree
# ---------------------------------------------------------------------------


async def test_a_manager_trashes_the_node_it_created(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Trashing is a writer-rung action, so the org admin — a manager on their
    own drive by descent — bins their own upload without an owner grant. The
    proof is the row, not the status: a 200 over a node still live would be a
    lie the client would show as a deletion."""
    node = await fx.node(b"doomed.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await files_client.get(url)).json()
    assert fetched["capabilities"]["can_delete"] is True
    # …and the undoable one only. The purge stays on the owner rung.
    assert fetched["capabilities"]["can_purge"] is False

    response = await files_client.delete(url, headers={**idem(), **_etag(fetched)})
    assert response.status_code == 200, response.text
    reloaded = await real_session.get(FileNode, node.id)
    assert reloaded is not None
    await real_session.refresh(reloaded)
    assert reloaded.trashed_at is not None


async def test_a_permanent_delete_without_the_owner_role_is_a_403_that_purged_nothing(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The same caller, the same node, one query parameter apart: the purge is
    the owner's and a manager is refused it. The caller can read the node, so
    the refusal is allowed to be visible — an opaque 404 would tell them less
    than they already know."""
    node = await fx.node(b"unpurgeable.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await files_client.get(url)).json()
    assert fetched["capabilities"]["can_read"] is True

    response = await files_client.delete(
        f"{url}?permanent=true", headers={**idem(), **_etag(fetched)}
    )
    assert response.status_code == 403, response.text
    # And the refusal was a refusal: the row is there, not half-purged.
    reloaded = await real_session.get(FileNode, node.id)
    assert reloaded is not None and reloaded.trashed_at is None


async def test_a_reader_cannot_trash(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The rung below the writer's. The reader is a real grant rather than a
    bare member, so the 403 is the ladder refusing an action they can see —
    not the opaque 404 a caller with no access to the node would get."""
    node = await fx.node(b"not-yours.txt")
    drive = await _drive_id(files_client)
    # Every mutation carries an If-Match, the grant POST included, so the
    # setup asks the node for its etag before it shares it.
    owner_view = (await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")).json()
    granted = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**idem(), **_etag(owner_view)},
    )
    assert granted.status_code in (200, 201), granted.text

    member = await login(client, files_org.member.email, files_org.member_password)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await member.get(url)).json()
    assert fetched["capabilities"]["can_read"] is True
    assert fetched["capabilities"]["can_delete"] is False

    response = await member.delete(url, headers={**idem(), **_etag(fetched)})
    assert response.status_code == 403, response.text
    reloaded = await real_session.get(FileNode, node.id)
    assert reloaded is not None and reloaded.trashed_at is None


async def test_star_then_unstar_round_trips_through_the_starred_filter(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The star is asserted through the *filter* rather than a column read: the
    bit is only useful if a listing can find it."""
    folder = await fx.node(b"starbox", kind="folder")
    node = await fx.node(b"starred.txt", parent=folder)
    drive = await _drive_id(files_client)
    item_url = f"{BASE}/drives/{drive}/items/{node.id}"
    listing_url = f"{BASE}/drives/{drive}/items/{folder.id}/children?starred=true"

    assert (await files_client.get(listing_url)).json()["value"] == []
    fetched = (await files_client.get(item_url)).json()
    starred = await files_client.put(f"{item_url}/star", headers={**idem(), **_etag(fetched)})
    assert starred.status_code == 200, starred.text
    assert [row["id"] for row in (await files_client.get(listing_url)).json()["value"]] == [
        str(node.id)
    ]

    unstarred = await files_client.delete(
        f"{item_url}/star", headers={**idem(), **_etag(starred.json())}
    )
    assert unstarred.status_code == 200, unstarred.text
    assert (await files_client.get(listing_url)).json()["value"] == []


async def test_the_listing_reports_the_callers_star_on_each_item(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """``starred`` rides the ordinary listing, so the page can draw Star /
    Remove star without a second request per row.

    Read off an *unfiltered* listing on purpose: the ``starred=true`` chip
    already narrows, so a field that was never rendered would still let that
    listing look right. Here the same three states — never starred, starred,
    unstarred — are three different values of one field on one row.
    """
    folder = await fx.node(b"wirebox", kind="folder")
    node = await fx.node(b"wired.txt", parent=folder)
    drive = await _drive_id(files_client)
    item_url = f"{BASE}/drives/{drive}/items/{node.id}"
    listing_url = f"{BASE}/drives/{drive}/items/{folder.id}/children"

    async def starred_column() -> list[bool]:
        page = await files_client.get(listing_url)
        assert page.status_code == 200, page.text
        return [row["starred"] for row in page.json()["value"]]

    fetched = (await files_client.get(item_url)).json()
    assert await starred_column() == [False]

    starred = await files_client.put(f"{item_url}/star", headers={**idem(), **_etag(fetched)})
    assert starred.status_code == 200, starred.text
    assert await starred_column() == [True]

    unstarred = await files_client.delete(
        f"{item_url}/star", headers={**idem(), **_etag(starred.json())}
    )
    assert unstarred.status_code == 200, unstarred.text
    assert await starred_column() == [False]


async def test_another_members_star_is_invisible_on_this_callers_listing(
    files_client: AsyncClient,
    client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A star is one person's bookmark, and the node bit is not it.

    The admin's star sets the shared bit on the node, so a wire that rendered
    that bit would hand the member somebody else's bookmark. The member is
    raised to org admin first so they can list the folder at all — otherwise the
    404 would answer the question for the wrong reason.
    """
    membership = (
        await real_session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == files_org.org.org_id,
                TeamMembership.user_id == files_org.member.id,
            )
        )
    ).scalar_one()
    await membership_service.change_role(real_session, membership, TeamRole.ADMIN)
    await real_session.commit()

    folder = await fx.node(b"sharedbox", kind="folder")
    node = await fx.node(b"shared.txt", parent=folder)
    drive = await _drive_id(files_client)
    listing_url = f"{BASE}/drives/{drive}/items/{folder.id}/children"

    fetched = (await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")).json()
    starred = await files_client.put(
        f"{BASE}/drives/{drive}/items/{node.id}/star", headers={**idem(), **_etag(fetched)}
    )
    assert starred.status_code == 200, starred.text
    assert [row["starred"] for row in (await files_client.get(listing_url)).json()["value"]] == [
        True
    ]

    stranger = await login(client, files_org.member.email, files_org.member_password)
    seen = await stranger.get(listing_url)
    assert seen.status_code == 200, seen.text
    assert [row["id"] for row in seen.json()["value"]] == [str(node.id)]
    assert [row["starred"] for row in seen.json()["value"]] == [False]


async def test_copy_returns_202_with_an_operation(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Always 202, whatever the size: one client progress path, and the server
    stays free to move the work off the request."""
    source = await fx.node(b"copied.txt")
    target = await fx.node(b"into", kind="folder")
    drive = await _drive_id(files_client)
    response = await files_client.post(
        f"{BASE}/drives/{drive}/items/{source.id}/copy",
        json={"parentId": str(target.id)},
        headers=idem(),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["kind"] == "copy"


async def test_tree_creates_a_skeleton_and_a_replay_creates_nothing_more(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Idempotent by construction: a re-drop of the same directory walks into
    the folders that exist rather than making second copies of them."""
    root = await fx.node(b"drop", kind="folder")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{root.id}/tree"
    paths = {"paths": ["a/b/c", "a/b/d"]}
    first = await files_client.post(url, json=paths, headers=idem())
    assert first.status_code == 201, first.text
    assert sorted(item["name"] for item in first.json()) == ["a", "b", "c", "d"]

    second = await files_client.post(url, json=paths, headers=idem())
    assert second.status_code == 201
    assert second.json() == []
    listed = await files_client.get(f"{BASE}/drives/{drive}/items/{root.id}/children")
    assert [row["name"] for row in listed.json()["value"]] == ["a"]


async def test_a_move_over_the_inline_threshold_is_202_with_the_operation(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    nudge_recorder: Any,
) -> None:
    """Over the threshold the tree has not moved yet, so the answer is the
    queued operation and where to poll it — never the unmoved node with a 200,
    which would say the move had already happened."""
    # The suite runs queued operations inline (FILES_INLINE_OPERATIONS, so a
    # route test can complete an upload with no Temporal worker); this test is
    # about the deployment where a runner owns the move, so it pins the flag off.
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 0)
    target = await fx.node(b"big-dest", kind="folder")
    moving = await fx.node(b"big", kind="folder")
    await fx.node(b"big.txt", parent=moving)
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{moving.id}"
    fetched = (await files_client.get(url)).json()

    response = await files_client.patch(
        url, json={"parentId": str(target.id)}, headers={**idem(), **_etag(fetched)}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    assert body["kind"] == "move"
    assert body["driveId"] == drive
    assert response.headers["Location"] == f"{BASE}/drives/{drive}/operations/{body['id']}"
    # The node is still where it was: the runner owns the move, not the request.
    assert (await files_client.get(url)).json()["parentId"] == fetched["parentId"]
    # ...and the request told the worker to run it NOW, keyed by the operation
    # so a retry of the same click attaches to the run rather than doubling it.
    nudges = nudge_recorder.for_workflow(WorkflowType.FILES_LARGE_MOVE.value)
    assert [n.workflow_id for n in nudges] == [f"files.large_move:{body['id']}"]
    assert nudges[0].args == (body["id"], str(fx.org_team_id))


@pytest.mark.parametrize(
    ("cap", "children", "expected"),
    [
        pytest.param(2, 2, 200, id="exactly-the-cap-moves-inline"),
        pytest.param(2, 3, 202, id="one-over-the-cap-is-an-operation"),
    ],
)
async def test_the_inline_move_cap_counts_children_not_the_folder(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
    children: int,
    expected: int,
) -> None:
    """The move budget reads "Move (small) — ≤ 10,000 nodes", and the nodes it means are the
    ones the rewrite touches beneath the folder. Counting the moved folder as
    one of its own children made the last folder that fits refuse itself, so
    the boundary is asserted from both sides at a cap small enough to seed."""
    # The suite runs queued operations inline (FILES_INLINE_OPERATIONS); this
    # test claims which path the route took, so the runner is pinned off and a
    # 202 leaves the subtree where it was.
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", cap)
    target = await fx.node(b"dest", kind="folder")
    moving = await fx.node(b"moved", kind="folder")
    for index in range(children):
        await fx.node(f"child-{index}.txt".encode(), parent=moving)
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{moving.id}"
    fetched = (await files_client.get(url)).json()

    response = await files_client.patch(
        url, json={"parentId": str(target.id)}, headers={**idem(), **_etag(fetched)}
    )

    assert response.status_code == expected, response.text
    body = response.json()
    if expected == 200:
        # Inline, the PATCH itself is the move: it answers the item, reparented.
        assert body["parentId"] == str(target.id), body
        assert (await files_client.get(url)).json()["parentId"] == str(target.id)
    else:
        # Over the cap the rewrite is a queued operation, so the answer is the
        # operation and where to read it — not the item. How soon the subtree
        # actually moves is the runner's business (a deployment that runs
        # queued work inline finishes it inside this request), so the claim
        # here is about which path the route took.
        assert body["kind"] == "move", body
        assert response.headers["Location"].endswith(f"/operations/{body['id']}")
    moved_now = (await files_client.get(url)).json()["parentId"]
    # A 200 moved it; a 202 has not moved it yet, and saying otherwise either
    # way is the bug this pins.
    assert (moved_now == str(target.id)) is (expected == 200)


async def test_an_agent_is_the_first_toucher_of_a_fresh_org_drive(
    files_client: AsyncClient, files_on: None
) -> None:
    """F-191: the agent assertion gets the policy's answer, never a 500.

    ``GET /drives`` on an org that has no drive yet runs
    ``ensure_org_drive -> _ensure_root -> _announce``, which writes the org's
    very first history row. An agent's principal id is its chat session id and
    is not required to be a UUID, so a history writer that parsed it as one
    took down every Files route for an agent in a fresh org.
    """
    response = await files_client.get(f"{BASE}/drives", headers=agent_headers("chat-01.session"))
    assert response.status_code != 500, response.text
    assert response.status_code == 200, response.text
    body = response.json()
    assert uuid.UUID(body["id"])
    # The drive the agent's first touch built is the one everybody else gets.
    plain = await files_client.get(f"{BASE}/drives")
    assert plain.json()["id"] == body["id"]


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------

#: The copied bytes are read back through the content domain, which refuses a
#: request arriving under any other Host — so the test names it explicitly.
_COPY_HOST = "files.localhost:8000"
_COPY_ORIGIN = f"http://{_COPY_HOST}"
_COPY_BYTES = b"the bytes a copy must not lose\n"


async def _children_named(
    client: AsyncClient, drive: str, parent: str
) -> dict[str, dict[str, Any]]:
    listed = await client.get(f"{BASE}/drives/{drive}/items/{parent}/children")
    assert listed.status_code == 200, listed.text
    return {str(item["name"]): item for item in listed.json()["value"]}


async def _served_bytes(client: AsyncClient, drive: str, node_id: str) -> bytes:
    """Mint a content URL for ``node_id`` and follow it onto the content host."""
    minted = await client.get(f"{BASE}/drives/{drive}/items/{node_id}/content")
    assert minted.status_code == 302, minted.text
    location = str(minted.headers["location"])
    served = await client.get(location.removeprefix(_COPY_ORIGIN), headers={"Host": _COPY_HOST})
    assert served.status_code == 200, served.text
    return bytes(served.content)


async def test_a_copy_reaches_done_and_the_copied_tree_holds_the_same_bytes(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 202 from ``copy`` has to end in a copied tree, not in ``failed``.

    The route queues the operation the runner picks up, so what it writes onto
    that row *is* the copy: an operation started with no plan is a 202 that
    lies, and the runner it hands to has nothing to run. The assertions are
    therefore at the far end — the operation's terminal state, the copied node
    under the destination, and the bytes that node serves back through the
    content route — because every one of those is silent when the plan is
    missing while the 202 itself looks exactly the same.
    """
    monkeypatch.setattr(settings, "files_content_base_url", _COPY_ORIGIN)
    drive_row = await fx.drive()
    # `ensure_org_drive` writes quota 0, so the content PUT below would be a
    # 507 until provisioning lands; the test raises it to the configured default.
    await real_session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive_row.id,
        },
    )
    await real_session.commit()
    drive = await _drive_id(files_client)
    source = await fx.node(b"src", kind="folder")
    leaf = await fx.node(b"note.txt", parent=source)
    dest = await fx.node(b"dest", kind="folder")
    written = await files_client.put(
        f"{BASE}/drives/{drive}/items/{leaf.id}/content",
        content=_COPY_BYTES,
        headers={
            **idem(),
            "If-Match": f'"{leaf.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(_COPY_BYTES)),
        },
    )
    assert written.status_code == 201, written.text

    response = await files_client.post(
        f"{BASE}/drives/{drive}/items/{source.id}/copy",
        json={"parentId": str(dest.id)},
        headers=idem(),
    )

    assert response.status_code == 202, response.text
    op_id = str(response.json()["id"])
    finished = await files_client.get(f"{BASE}/drives/{drive}/operations/{op_id}")
    assert finished.status_code == 200, finished.text
    assert finished.json()["state"] == "done", finished.text
    copied_root = (await _children_named(files_client, drive, str(dest.id)))["src"]
    assert copied_root["id"] != str(source.id)
    copied_leaf = (await _children_named(files_client, drive, str(copied_root["id"])))["note.txt"]
    assert copied_leaf["id"] != str(leaf.id)
    assert await _served_bytes(files_client, drive, str(copied_leaf["id"])) == _COPY_BYTES
    # The source still serves its own bytes: a copy shares the object, it does
    # not move it.
    assert await _served_bytes(files_client, drive, str(leaf.id)) == _COPY_BYTES


# ---------------------------------------------------------------------------
# the trash is an operation, and the operation is the undo handle
# ---------------------------------------------------------------------------


async def test_trashing_answers_an_operation_whose_undo_puts_the_node_back(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Trashing is undoable, so the DELETE hands back the handle it is undone
    with. The proof is the round trip rather than the body: the operation the
    response names is POSTed to ``…/operations/{id}/undo`` and the node comes
    back live. Drop the ``op=`` argument from the route's ``trash`` call and no
    inverse is recorded, so the undo answers 400 and the node stays binned.
    """
    node = await fx.node(b"undoable.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await files_client.get(url)).json()

    trashed = await files_client.delete(url, headers={**idem(), **_etag(fetched)})
    assert trashed.status_code == 200, trashed.text
    operation = trashed.json()
    assert operation["kind"] == "trash"
    assert operation["state"] == "done"
    assert operation["undoableUntil"] is not None
    assert trashed.headers["Location"].endswith(f"/operations/{operation['id']}")

    binned = await real_session.get(FileNode, node.id)
    assert binned is not None
    await real_session.refresh(binned)
    assert binned.trashed_at is not None

    undone = await files_client.post(
        f"{BASE}/drives/{drive}/operations/{operation['id']}/undo",
        headers={**idem(), "If-Match": "0"},
    )
    assert undone.status_code == 202, undone.text
    await real_session.refresh(binned)
    assert binned.trashed_at is None


async def test_path_bytes_names_the_node_once(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """F-297: `repo.chain` returns the ancestors AND the node, so appending the
    node's own name again read `/probe/a.txt` back as `/probe/a.txt/a.txt` — a
    path no client could resolve and the path form 404s on."""
    folder = await fx.node(b"probe", kind="folder")
    leaf = await fx.node(b"a.txt", parent=folder)
    drive = await _drive_id(files_client)

    item = (await files_client.get(f"{BASE}/drives/{drive}/items/{leaf.id}")).json()

    assert item["pathBytes"] == "/probe/a.txt"
    # And the path the item advertises is the path the path form resolves.
    by_path = await files_client.get(f"{BASE}/drives/{drive}/root:/probe/a.txt")
    assert by_path.status_code == 200, by_path.text
    assert by_path.json()["id"] == str(leaf.id)
    assert by_path.json()["pathBytes"] == "/probe/a.txt"


async def _home_node(client: AsyncClient, fx: FilesFixtures) -> FileNode:
    """The caller's OWN ``/home/<me>`` — the one folder nobody else can read.

    ``/Shared`` carries a drive default that makes every member a reader, so a
    test about what an outsider may learn has to seed somewhere an outsider
    starts with nothing.
    """
    drive = (await client.get(f"{BASE}/drives")).json()
    assert drive["homeId"], "the caller has a home"
    return await fx.folder(uuid.UUID(str(drive["homeId"])))


# ---------------------------------------------------------------------------
# a path names only the folders the caller may read
# ---------------------------------------------------------------------------


async def test_a_single_file_grant_reveals_no_ancestor_names(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """One grant on one file is one file — not the shape of the drive above it.

    A folder's name is a fact about the folder, so a caller who may not read
    the folder may not learn what it is called. Sharing ``pay.csv`` out of
    ``/Shared/salaries/2026`` handed the grantee ``salaries`` and ``2026`` in
    ``pathBytes`` — on the item read and on the feed that exists to show them
    what was shared — while every other route answered 404 for both folders.

    RED before the chain was decided: ``pathBytes`` read
    ``/Shared/salaries/2026/pay.csv`` for a caller whose parent read is a 404.
    """
    drive = await _drive_id(files_client)
    home = await _home_node(files_client, fx)
    salaries = await fx.node(b"salaries", kind="folder", parent=home)
    year = await fx.node(b"fy-hidden", kind="folder", parent=salaries)
    leaf = await fx.node(b"pay.csv", parent=year)

    owner_view = (await files_client.get(f"{BASE}/drives/{drive}/items/{leaf.id}")).json()
    assert owner_view["pathBytes"].endswith("/salaries/fy-hidden/pay.csv")
    granted = await files_client.post(
        f"{BASE}/drives/{drive}/items/{leaf.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**idem(), **_etag(owner_view)},
    )
    assert granted.status_code in (200, 201), granted.text
    # The owner's own answer, taken before the login below: `login` re-cookies
    # the one client, so everything the owner is asked has to be asked first.
    owner_content = (
        await files_client.get(f"{BASE}/drives/{drive}/items/{leaf.id}/content")
    ).status_code

    member = await login(client, files_org.member.email, files_org.member_password)
    read = await member.get(f"{BASE}/drives/{drive}/items/{leaf.id}")
    assert read.status_code == 200, read.text
    item = read.json()
    assert item["capabilities"]["can_read"] is True
    assert item["pathBytes"] == "pay.csv"
    assert "salaries" not in read.text
    # "fy-hidden" cannot occur inside a uuid or a timestamp; "2026" could, and did.
    assert "fy-hidden" not in read.text

    # Everything above it stays the opaque 404 it already was.
    assert (await member.get(f"{BASE}/drives/{drive}/items/{year.id}")).status_code == 404
    assert (await member.get(f"{BASE}/drives/{drive}/items/{year.id}/children")).status_code == 404

    # And the feed that exists to show them what was shared says the same.
    feed = await member.get(f"{BASE}/drives/{drive}/sharedWithMe")
    assert feed.status_code == 200, feed.text
    rows = {row["id"]: row for row in feed.json()["value"]}
    assert str(leaf.id) in rows, feed.text
    assert rows[str(leaf.id)]["pathBytes"] == "pay.csv"
    assert "salaries" not in feed.text

    # A cut path is not a cut grant: the content route answers the grantee
    # exactly what it answered the owner, so nothing was taken away with the
    # names.
    theirs = await member.get(f"{BASE}/drives/{drive}/items/{leaf.id}/content")
    assert theirs.status_code == owner_content, theirs.text


async def test_a_shared_folders_rows_name_no_folder_above_it(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A grant on a folder publishes that folder and what is under it.

    The listing is the surface that pays for this once per page rather than
    once per row, so it gets its own case: every row of ``2026`` is rendered
    from the same chain, and the cut has to survive being computed once and
    shared across the page.

    RED: every row's ``pathBytes`` read ``/Shared/salaries/2026/<name>``.
    """
    drive = await _drive_id(files_client)
    home = await _home_node(files_client, fx)
    salaries = await fx.node(b"salaries", kind="folder", parent=home)
    year = await fx.node(b"2026", kind="folder", parent=salaries)
    await fx.node(b"pay.csv", parent=year)
    await fx.node(b"bonus.csv", parent=year)

    owner_view = (await files_client.get(f"{BASE}/drives/{drive}/items/{year.id}")).json()
    granted = await files_client.post(
        f"{BASE}/drives/{drive}/items/{year.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**idem(), **_etag(owner_view)},
    )
    assert granted.status_code in (200, 201), granted.text

    member = await login(client, files_org.member.email, files_org.member_password)
    folder = await member.get(f"{BASE}/drives/{drive}/items/{year.id}")
    assert folder.status_code == 200, folder.text
    # The shared folder is the top of what they can see, so it is the first
    # segment — and `salaries` is not a segment at all.
    assert folder.json()["pathBytes"] == "2026"

    listed = await member.get(f"{BASE}/drives/{drive}/items/{year.id}/children")
    assert listed.status_code == 200, listed.text
    assert sorted(row["pathBytes"] for row in listed.json()["value"]) == [
        "2026/bonus.csv",
        "2026/pay.csv",
    ]
    assert "salaries" not in listed.text


async def test_a_readable_chain_keeps_the_path_the_path_form_resolves(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """The cut is a cut, not a rewrite.

    A caller who may read the whole chain is answered the drive-absolute path
    they can hand straight back to the path form. The mount and the push
    address a folder by the path the wire gave them, so a path that lost its
    leading slash for a caller who lost nothing would file a chat's work at the
    drive root.
    """
    drive = await _drive_id(files_client)
    shared = await fx.shared()
    folder = await fx.node(b"papers", kind="folder", parent=shared)
    leaf = await fx.node(b"draft.txt", parent=folder)

    item = (await files_client.get(f"{BASE}/drives/{drive}/items/{leaf.id}")).json()
    assert item["pathBytes"] == "/Shared/papers/draft.txt"

    by_path = await files_client.get(f"{BASE}/drives/{drive}/root:{item['pathBytes']}")
    assert by_path.status_code == 200, by_path.text
    assert by_path.json()["id"] == str(leaf.id)
    assert by_path.json()["pathBytes"] == "/Shared/papers/draft.txt"

    listed = (await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")).json()
    assert [row["pathBytes"] for row in listed["value"]] == ["/Shared/papers/draft.txt"]


async def test_a_symlink_reads_back_the_kind_the_tree_stores(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """F-293: the wire vocabulary is the stored one, 1:1 — a `canonical` link
    read back as `external` cannot be handed to a create that takes the kind."""
    node = await fx.node(
        b"link", kind="symlink", symlink_target=b"/Shared/x", symlink_kind="canonical"
    )
    drive = await _drive_id(files_client)

    item = (await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")).json()

    assert item["symlink"]["kind"] == "canonical"
    assert item["symlink"]["target"] == "/Shared/x"


async def test_a_special_node_keeps_its_kind_and_carries_its_facet(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
) -> None:
    """F-293: rendering a special node as `file` tells a materializer to create
    a regular file where a device node belongs, and drops the `rdev` it needs."""
    node = await fx.node(b"null", kind="special", subtype="chardev", rdev=8_631)
    drive = await _drive_id(files_client)

    item = (await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")).json()

    assert item["kind"] == "special"
    assert item["special"]["type"] == "chardev"
    assert item["special"]["rdev"] == 8_631
    # A special node holds no bytes, so it never carries the content facet.
    assert item["file"] is None


async def test_the_attrs_a_read_renders_are_the_attrs_a_patch_accepts(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """F-297: `AttrsFacet` renders `mtime` as a datetime while `AttrsPatch` took
    snake_case `mtime_ns`, so a client could not send back what it had just
    read. The round trip is the contract: read the facet, edit one field, PATCH
    that same spelling, read the same value again."""
    node = await fx.node(b"stat.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    rendered = (await files_client.get(url)).json()["attrs"]
    assert "mtime" in rendered and "mtime_ns" not in rendered

    patched = await files_client.patch(
        url,
        json={"attrs": {"mtime": "2026-01-01T12:00:00Z", "mode": 33188}},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["attrs"]["mtime"] == "2026-01-01T12:00:00Z"
    assert patched.json()["attrs"]["mode"] == 33188


# ---------------------------------------------------------------------------
# the single-item read carries the head version and the caller's own star
# ---------------------------------------------------------------------------


_HEAD_BYTES = b'{"the": "bytes a single read must describe"}\n'


async def _raise_quota(session: AsyncSession, drive_id: uuid.UUID) -> None:
    """`ensure_org_drive` writes quota 0, so a content PUT would be a 507."""
    await session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive_id,
        },
    )
    await session.commit()


async def test_a_single_read_carries_the_head_versions_facts_and_the_callers_star(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Both single-item forms describe the bytes the node actually holds.

    A client decides whether to re-download from ``contentHash``, whether to
    render inline from ``mimeType``, and whether to serve at all from
    ``scanState``; a page draws Star / Remove star from ``starred``. All four
    were schema defaults because the statement that loads the node never
    reached the head version or the stars table — so the assertions here are
    the *committed* values, which no default can match, and the path form is
    asserted beside the id form because they are two renderings of one node.
    """
    drive_row = await fx.drive()
    await _raise_quota(real_session, drive_row.id)
    drive = await _drive_id(files_client)
    folder = await fx.node(b"headbox", kind="folder")
    node = await fx.node(b"head.json", parent=folder)
    item_url = f"{BASE}/drives/{drive}/items/{node.id}"

    bare = (await files_client.get(item_url)).json()
    # No committed bytes yet, so the facet is at its schema defaults — which
    # is exactly what the read below must NOT still be answering with.
    assert bare["file"]["content_hash"] == ""
    assert bare["file"]["mime_type"] == "application/octet-stream"
    assert bare["file"]["scan_state"] == "pending"
    assert bare["starred"] is False

    written = await files_client.put(
        f"{item_url}/content",
        content=_HEAD_BYTES,
        headers={
            **idem(),
            "If-Match": f'"{node.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(_HEAD_BYTES)),
        },
    )
    assert written.status_code == 201, written.text
    head = (
        await real_session.execute(
            text(
                "SELECT v.content_hash, v.mime_sniffed, v.scan_state, v.size_bytes "
                "FROM file_versions v JOIN file_nodes n ON n.head_version_id = v.id "
                "WHERE n.id = :id"
            ),
            {"id": node.id},
        )
    ).one()

    fetched = (await files_client.get(item_url)).json()
    starred = await files_client.put(f"{item_url}/star", headers={**idem(), **_etag(fetched)})
    assert starred.status_code == 200, starred.text

    read = (await files_client.get(item_url)).json()
    assert read["starred"] is True
    assert read["file"]["content_hash"] == head.content_hash
    assert read["file"]["mime_type"] == head.mime_sniffed
    assert read["file"]["scan_state"] == head.scan_state
    assert read["file"]["size"] == head.size_bytes

    by_path = await files_client.get(f"{BASE}/drives/{drive}/root:/headbox/head.json")
    assert by_path.status_code == 200, by_path.text
    assert by_path.json()["file"] == read["file"]
    assert by_path.json()["starred"] is True


async def test_a_listed_row_carries_the_head_versions_hash_like_a_single_read(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A page of a folder describes each row's bytes, not just its size.

    `alkera files pull` decides whether to re-download a file by comparing the
    BLAKE3 on disk against the listed row's `content_hash`: the walk lists a
    folder once and never reads its children one at a time, because a read per
    row is what the listing budget exists to forbid. A page that answered the
    schema default `""` therefore told the pull that every file on disk
    differed, and a second pull of an unchanged tree fetched every byte again.

    So the claim is equality with the single read, which is the rendering that
    was already right — and it is asserted against the *committed* version row,
    so a page that quietly went back to the default cannot pass by matching a
    single read that regressed with it.
    """
    await _raise_quota(real_session, (await fx.drive()).id)
    drive = await _drive_id(files_client)
    folder = await fx.node(b"listbox", kind="folder")
    node = await fx.node(b"listed.json", parent=folder)
    item_url = f"{BASE}/drives/{drive}/items/{node.id}"

    listed_bare = (
        await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")
    ).json()
    assert listed_bare["value"][0]["file"]["content_hash"] == ""

    written = await files_client.put(
        f"{item_url}/content",
        content=_HEAD_BYTES,
        headers={
            **idem(),
            "If-Match": f'"{node.etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(_HEAD_BYTES)),
        },
    )
    assert written.status_code == 201, written.text
    head = (
        await real_session.execute(
            text(
                "SELECT v.content_hash, v.mime_sniffed, v.scan_state, v.size_bytes "
                "FROM file_versions v JOIN file_nodes n ON n.head_version_id = v.id "
                "WHERE n.id = :id"
            ),
            {"id": node.id},
        )
    ).one()

    page = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")
    assert page.status_code == 200, page.text
    row = page.json()["value"][0]
    assert row["id"] == str(node.id)
    assert row["file"]["content_hash"] == head.content_hash
    assert row["file"]["size"] == head.size_bytes
    assert row["file"]["mime_type"] == head.mime_sniffed
    assert row["file"]["scan_state"] == head.scan_state
    # The two renderings of one node agree; neither is the schema default.
    assert row["file"] == (await files_client.get(item_url)).json()["file"]


async def test_a_page_costs_the_same_statements_however_many_rows_carry_bytes(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The heads ride one statement for the whole page, not one per row.

    Rendering a row's head is only affordable if it is batched: the listing's
    whole contract is that a page costs what a page costs whatever is in it.
    Ten files with committed bytes must therefore cost exactly what one does.
    """
    await _raise_quota(real_session, (await fx.drive()).id)
    drive = await _drive_id(files_client)

    async def _commit(parent: Any, name: bytes) -> None:
        node = await fx.node(name, parent=parent)
        written = await files_client.put(
            f"{BASE}/drives/{drive}/items/{node.id}/content",
            content=_HEAD_BYTES + name,
            headers={
                **idem(),
                "If-Match": f'"{node.etag}"',
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(_HEAD_BYTES + name)),
            },
        )
        assert written.status_code == 201, written.text

    small = await fx.node(b"onefile", kind="folder")
    await _commit(small, b"a.bin")
    big = await fx.node(b"tenfiles", kind="folder")
    for index in range(10):
        await _commit(big, f"f{index}.bin".encode())

    async def _statements(folder_id: Any) -> int:
        counter = _StatementCounter()
        event.listen(Engine, "before_cursor_execute", counter)
        try:
            response = await files_client.get(
                f"{BASE}/drives/{drive}/items/{folder_id}/children?limit=500"
            )
        finally:
            event.remove(Engine, "before_cursor_execute", counter)
        assert response.status_code == 200, response.text
        assert len(response.json()["value"]) >= 1
        return counter.count

    one = await _statements(small.id)
    ten = await _statements(big.id)
    assert ten == one, f"a page of 10 rows cost {ten} statements, a page of 1 cost {one}"


class _StatementCounter:
    """A `before_cursor_execute` listener that counts the statements it sees."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.count += 1


async def test_a_colleagues_star_is_not_this_readers_star_on_a_single_read(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A star belongs to one person. The node also carries a "somebody starred
    it" bit, and answering the wire field from that bit would put a colleague's
    bookmark under this reader's Starred — so the owner stars the node and the
    reader, who can see it, must still read ``starred: false``."""
    node = await fx.node(b"whose-star.txt")
    drive = await _drive_id(files_client)
    item_url = f"{BASE}/drives/{drive}/items/{node.id}"
    owner_view = (await files_client.get(item_url)).json()
    granted = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "reader"},
        headers={**idem(), **_etag(owner_view)},
    )
    assert granted.status_code in (200, 201), granted.text
    # The grant bumped the node's etag, and the star is a mutation like
    # any other: the precondition is the view AFTER the share, not before it.
    shared_view = (await files_client.get(item_url)).json()
    starred = await files_client.put(f"{item_url}/star", headers={**idem(), **_etag(shared_view)})
    assert starred.status_code == 200, starred.text
    assert starred.json()["starred"] is True

    member = await login(client, files_org.member.email, files_org.member_password)
    theirs = await member.get(item_url)
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["capabilities"]["can_read"] is True
    assert theirs.json()["starred"] is False


async def test_a_single_read_still_costs_one_node_statement(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The head version and the star are folded into the statement that loads
    the node, not fetched after it. Counted rather than reasoned about: a
    second query would render the same body, so only the statement log tells
    the two implementations apart."""
    node = await fx.node(b"counted.txt")
    drive = await _drive_id(files_client)
    item_url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await files_client.get(item_url)).json()
    starred = await files_client.put(f"{item_url}/star", headers={**idem(), **_etag(fetched)})
    assert starred.status_code == 200, starred.text

    with counting() as seen:
        read = await files_client.get(item_url)
    assert read.status_code == 200, read.text
    assert read.json()["starred"] is True
    reads = [line for line in seen if "FROM file_nodes" in line and "file_stars" in line]
    assert len(reads) == 1, "\n".join(seen)
    assert "file_versions" in reads[0], reads[0]
    assert not [line for line in seen if line.strip().startswith("SELECT file_stars")], (
        "the star must ride the node statement, not a second query"
    )


# ---------------------------------------------------------------------------
# the items wire: the symlink kind, the xattrs and the exact mtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "expected", "target"),
    [
        pytest.param("relative", "relative", "../sibling", id="relative"),
        pytest.param("canonical", "canonical", "/sibling", id="canonical"),
        pytest.param(None, "relative", "../sibling", id="omitted-defaults-to-relative"),
    ],
)
async def test_a_created_symlink_reads_back_the_kind_the_body_named(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    sent: str | None,
    expected: str,
    target: str,
) -> None:
    """F-311: the wire took `symlinkKind` and the create dropped it, so a
    `canonical` link — the one a materializer rewrites to the local root — came
    back `relative` and would have been recreated pointing at the wrong tree."""
    folder = await fx.node(b"links", kind="folder")
    drive = await _drive_id(files_client)
    body: dict[str, Any] = {
        "name": f"to-sibling-{sent or 'bare'}",
        "kind": "symlink",
        "symlinkTarget": target,
    }
    if sent is not None:
        body["symlinkKind"] = sent

    made = await files_client.post(
        f"{BASE}/drives/{drive}/items/{folder.id}/children", json=body, headers=idem()
    )

    assert made.status_code == 201, made.text
    assert made.json()["symlink"] == {
        **made.json()["symlink"],
        "target": target,
        "kind": expected,
    }
    read = await files_client.get(f"{BASE}/drives/{drive}/items/{made.json()['id']}")
    assert read.json()["symlink"]["kind"] == expected


@pytest.mark.parametrize(
    ("kind", "target", "made"),
    [
        pytest.param("relative", "sibling.txt", True, id="a-sibling"),
        pytest.param("relative", "../to-the-root", True, id="up-to-the-root"),
        pytest.param("relative", "inner/../sibling.txt", True, id="a-climb-that-stays"),
        pytest.param("host", "/etc/passwd", False, id="a-host-path"),
        pytest.param("host", "/opt/alkera-home/auth.yml", False, id="a-box-host-path"),
        pytest.param(None, "/etc/passwd", False, id="an-absolute-path-with-no-kind"),
        pytest.param("relative", "../../etc/passwd", False, id="climbing-out"),
        pytest.param("relative", "../../../../opt/alkera-home", False, id="climbing-far-out"),
        pytest.param("relative", "inner/../../../x", False, id="a-nested-escape"),
        pytest.param("canonical", "/a/../../x", False, id="an-org-path-climbing-out"),
    ],
)
async def test_a_symlink_out_of_the_drive_is_refused_and_one_inside_is_made(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
    kind: str | None,
    target: str,
    made: bool,
) -> None:
    """A link is stored for every machine that later pulls or mounts the
    drive. One whose target leaves the drive is refused with a 422 that says
    so, and nothing appears in the folder; one that stays inside is made."""
    folder = await fx.node(b"links", kind="folder")
    drive = await _drive_id(files_client)
    body: dict[str, Any] = {"name": "the-link", "kind": "symlink", "symlinkTarget": target}
    if kind is not None:
        body["symlinkKind"] = kind

    answer = await files_client.post(
        f"{BASE}/drives/{drive}/items/{folder.id}/children", json=body, headers=idem()
    )

    children = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")
    names = [child["name"] for child in children.json()["value"]]
    if made:
        assert answer.status_code == 201, answer.text
        assert names == ["the-link"]
        return
    assert answer.status_code == 422, answer.text
    assert answer.json()["code"] == "files.link_outside_tree", answer.text
    assert "inside the drive" in answer.json()["message"]
    assert names == []


async def test_a_symlink_kind_the_tree_does_not_store_is_refused(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """`internal` was the 1.0.0 wire spelling. The wire is the stored vocabulary
    now, so the old word is a 422 from the body model rather than a link stored
    under a kind no materializer knows."""
    folder = await fx.node(b"strictlinks", kind="folder")
    drive = await _drive_id(files_client)

    refused = await files_client.post(
        f"{BASE}/drives/{drive}/items/{folder.id}/children",
        json={
            "name": "bad",
            "kind": "symlink",
            "symlinkTarget": "../x",
            "symlinkKind": "internal",
        },
        headers=idem(),
    )

    assert refused.status_code == 422, refused.text
    listed = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")
    assert listed.json()["value"] == []


async def test_the_attrs_patch_round_trips_user_xattrs(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """F-294/F-311: the push's attrs PATCH carried `user.*` xattrs the library
    refused outright, so a repo's recorded origin never survived a push.

    The value is arbitrary bytes, so the assertion is on the base64 the client
    sent coming back unchanged — a re-encode or a repr would not."""
    node = await fx.node(b"provenance.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    raw = b"github.com/acme/repo\x00\xff"
    encoded = base64.b64encode(raw).decode("ascii")

    patched = await files_client.patch(
        url,
        json={"attrs": {"xattrs": {"user.alkera.git.origin": encoded}}},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["attrs"]["xattrs"] == {"user.alkera.git.origin": encoded}
    read = await files_client.get(url)
    assert read.json()["attrs"]["xattrs"] == {"user.alkera.git.origin": encoded}


async def test_the_attrs_facet_carries_the_exact_nanoseconds_beside_the_iso_mtime(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """F-309: only the RFC 3339 `mtime` was rendered, and a float second cannot
    hold nanoseconds — a client that rebuilt them from the date wrote back a
    different time and every pulled file looked modified.

    The stamp used here is deliberately one a double cannot represent: the
    assertion is that the integer survives, and it fails against any value
    derived from the rendered `mtime`."""
    node = await fx.node(b"exact.txt")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    exact = 1_767_268_800_123_456_789

    patched = await files_client.patch(
        url,
        json={"attrs": {"mtimeNs": exact}},
        headers={**idem(), "If-Match": await node_etag(real_session, node.id)},
    )
    assert patched.status_code == 200, patched.text

    read = (await files_client.get(url)).json()["attrs"]
    assert read["mtimeNs"] == exact
    # The date is still there for the readers that want one, and it is the same
    # instant to the precision a datetime can carry.
    from_iso = int(datetime.fromisoformat(read["mtime"]).timestamp() * 1_000_000_000)
    assert abs(from_iso - exact) < 1_000
    assert from_iso != exact, "a float second cannot hold this stamp — that is the point"


# ---------------------------------------------------------------------------
# the move destination, and the fence on creation
# ---------------------------------------------------------------------------


async def test_a_move_into_a_folder_the_caller_cannot_see_is_the_opaque_not_found(
    client: AsyncClient,
    files_client: AsyncClient,
    files_on: None,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The destination is authorized too. A writer on the node being moved has
    no say over where it lands: grafting it into a folder they hold no role on
    would plant content there and — because a move re-derives the subtree's
    inherited ACEs from its new parent — hand that folder's principals a role
    on it. The refusal is the opaque 404, because naming a real folder must not
    tell a caller it exists.

    RED before the destination resolve: the move answered 200 and the row's
    parent_id had changed."""
    node = await fx.node(b"grafted.txt")
    secret = await fx.node(b"restricted", kind="folder")
    drive = await _drive_id(files_client)

    owner_view = (await files_client.get(f"{BASE}/drives/{drive}/items/{node.id}")).json()
    granted = await files_client.post(
        f"{BASE}/drives/{drive}/items/{node.id}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.member.id)}, "role": "writer"},
        headers={**idem(), **_etag(owner_view)},
    )
    assert granted.status_code in (200, 201), granted.text

    member = await login(client, files_org.member.email, files_org.member_password)
    url = f"{BASE}/drives/{drive}/items/{node.id}"
    fetched = (await member.get(url)).json()
    assert fetched["capabilities"]["can_write"] is True
    # The destination is invisible to them, which is the whole point.
    assert (await member.get(f"{BASE}/drives/{drive}/items/{secret.id}")).status_code == 404

    response = await member.patch(
        url,
        json={"parentId": str(secret.id)},
        headers={**idem(), **_etag(fetched)},
    )
    assert response.status_code == 404, response.text
    assert refusal(response) == NOT_FOUND
    # And nothing moved.
    reloaded = await real_session.get(FileNode, node.id)
    assert reloaded is not None
    await real_session.refresh(reloaded)
    assert reloaded.parent_id != secret.id


# ---------------------------------------------------------------------------
# the idempotency key, spent rather than discarded
# ---------------------------------------------------------------------------


async def test_the_same_create_key_twice_makes_one_folder_and_replays_its_bytes(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A retry after a commit-ambiguous timeout is the same request, so the
    client sends the same key. The second call must perform no second
    effect and answer the first call's exact bytes — the id above all, because
    that is the one thing the client has already written down.

    RED while the key was parsed and thrown away: the second POST answered 201
    with a *different* id and the folder had two children called ``twice``."""
    folder = await fx.node(b"retried", kind="folder")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{folder.id}"
    key = idem()

    first = await files_client.post(
        f"{url}/children",
        json={"kind": "folder", "name": "twice"},
        headers={**key, **_etag((await files_client.get(url)).json())},
    )
    assert first.status_code == 201, first.text

    second = await files_client.post(
        f"{url}/children",
        json={"kind": "folder", "name": "twice"},
        headers={**key, **_etag((await files_client.get(url)).json())},
    )
    assert second.status_code == 201, second.text
    assert second.content == first.content
    # One effect, not two — the tree is the assertion, not the echo.
    listed = (await files_client.get(f"{url}/children")).json()
    assert [row["name"] for row in listed["value"]] == ["twice"]


async def test_a_different_body_under_a_spent_create_key_is_the_422(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The stored answer belongs to the request that earned it. Replaying a key
    with a different body would hand a caller the answer to a question nobody
    asked, so it is refused rather than served."""
    folder = await fx.node(b"reused", kind="folder")
    drive = await _drive_id(files_client)
    url = f"{BASE}/drives/{drive}/items/{folder.id}"
    key = idem()

    first = await files_client.post(
        f"{url}/children",
        json={"kind": "folder", "name": "first"},
        headers={**key, **_etag((await files_client.get(url)).json())},
    )
    assert first.status_code == 201, first.text

    reused = await files_client.post(
        f"{url}/children",
        json={"kind": "folder", "name": "second"},
        headers={**key, **_etag((await files_client.get(url)).json())},
    )
    assert reused.status_code == 422, reused.text
    assert reused.json()["code"] == "files.idempotency_mismatch"
    listed = (await files_client.get(f"{url}/children")).json()
    assert [row["name"] for row in listed["value"]] == ["first"]


async def test_a_listed_row_and_a_created_child_carry_the_path_once(
    files_on: None, files_client: AsyncClient, fx: FilesFixtures, idem: Any
) -> None:
    """The children listing, the create answer and the tree answer built each row's
    chain as the parent's chain plus the parent again — and ``authorize`` already
    hands a chain that ends with the parent — so every listed path read
    ``/probe/probe/a.txt``. A single read of the same node said ``/probe/a.txt``."""
    folder = await fx.node(b"probe", kind="folder")
    await fx.node(b"a.txt", parent=folder)
    drive = await _drive_id(files_client)

    listed = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}/children")
    assert listed.status_code == 200, listed.text
    assert [row["pathBytes"] for row in listed.json()["value"]] == ["/probe/a.txt"]

    created = await files_client.post(
        f"{BASE}/drives/{drive}/items/{folder.id}/children",
        json={"name": "sub", "kind": "folder"},
        headers=idem(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["pathBytes"] == "/probe/sub"

    grown = await files_client.post(
        f"{BASE}/drives/{drive}/items/{folder.id}/tree",
        json={"paths": ["deeper/still"]},
        headers=idem(),
    )
    assert grown.status_code in (200, 201), grown.text
    made = {row["name"]: row for row in grown.json()}
    assert made["still"]["pathBytes"] == "/probe/deeper/still"


async def test_a_single_read_survives_a_fold_that_hides_the_item(
    fx: FilesFixtures, real_session: AsyncSession
) -> None:
    """The object fold is a listing's: it drops a spare chat's folder, and a
    folder whose object row is gone. A read by id is not a listing — dropping
    the only row there left nothing to return and the route answered 500."""
    from backend.services.files.items import with_object_facet, with_object_titles

    node = await fx.node(b"spare.alkerachat", kind="folder")
    item = Item(
        id=str(node.id),
        kind="folder",
        name="spare.alkerachat",
        object=ObjectFacet(type="chat", id=str(uuid.uuid4())),
    )

    assert await with_object_titles(real_session, [item]) == [], "a listing still hides it"
    assert await with_object_facet(real_session, item) == item, "a read by id gets it unfolded"


# ---------------------------------------------------------------------------
# the lease names its holder the way a row names its owner
# ---------------------------------------------------------------------------


async def _hold(
    client: AsyncClient,
    drive: str,
    node_id: uuid.UUID,
    session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Take a mount lease on ``node_id`` through the real lease route."""
    granted = await client.post(
        f"{BASE}/drives/{drive}/items/{node_id}/lease",
        json={"instanceId": "instance-a", "machineId": "MacBook Pro", "purpose": "mount"},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )
    assert granted.status_code == 200, granted.text


async def _rename_user(session: AsyncSession, user_id: uuid.UUID, first: str, last: str) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(first_name=first, last_name=last)
    )
    await session.commit()


async def test_a_lease_names_its_holder_the_way_a_row_names_its_owner(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """``lease.holder`` reaches a reader as a person's name, not as a uuid.

    The holder is the one field of the facet that goes into a sentence — "in use
    by … on …" — and it crossed as the holder's principal id, so every badge
    that rendered it put 36 characters of hex in front of a person. The id is
    what the server fences a write on; the name is what the sentence needs, and
    it is filled in beside the owner's name from the statement the page already
    pays for.
    """
    await _rename_user(real_session, files_org.org.admin_id, "Ada", "Lovelace")
    folder = await fx.node(b"models", kind="folder")
    drive = await _drive_id(files_client)
    await _hold(files_client, drive, folder.id, real_session, idem)

    read = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}")
    assert read.status_code == 200, read.text
    assert read.json()["lease"]["holder"] == "Ada Lovelace"
    assert read.json()["lease"]["machine"] == "MacBook Pro"

    # The listing row is where a badge is actually drawn, so it is named too.
    parent = str(read.json()["parentId"])
    page = await files_client.get(f"{BASE}/drives/{drive}/items/{parent}/children")
    assert page.status_code == 200, page.text
    row = next(r for r in page.json()["value"] if r["id"] == str(folder.id))
    assert row["lease"]["holder"] == "Ada Lovelace"
    # A file under the mount carries the same facet, and the same name.
    inside = await fx.node(b"weights.bin", parent=folder)
    child = await files_client.get(f"{BASE}/drives/{drive}/items/{inside.id}")
    assert child.status_code == 200, child.text
    assert child.json()["lease"]["holder"] == "Ada Lovelace"


async def test_a_holder_who_has_not_named_themselves_is_named_by_email(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The fallback the Owner column already takes: an email address is
    something a colleague can recognise, and it is true — where a blank name
    would leave the badge reading "In use by  on MacBook Pro"."""
    await _rename_user(real_session, files_org.org.admin_id, "", "")
    folder = await fx.node(b"unnamed", kind="folder")
    drive = await _drive_id(files_client)
    await _hold(files_client, drive, folder.id, real_session, idem)

    read = await files_client.get(f"{BASE}/drives/{drive}/items/{folder.id}")
    assert read.status_code == 200, read.text
    assert read.json()["lease"]["holder"] == files_org.org.admin_email


async def test_naming_the_holder_costs_the_page_no_statement_per_row(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """The holders of a page are named in the statement the owners already run.

    A name per leased row would be a statement per row — the cost the lease and
    owner lookups are batched to avoid — so six rows under a lease must cost
    exactly what one row under the same lease costs.
    """
    await _rename_user(real_session, files_org.org.admin_id, "Grace", "Hopper")
    leased = await fx.node(b"mounted", kind="folder")
    one = await fx.node(b"one", kind="folder", parent=leased)
    await fx.node(b"only.txt", parent=one)
    many = await fx.node(b"many", kind="folder", parent=leased)
    for index in range(6):
        await fx.node(f"f{index}.txt".encode(), parent=many)
    drive = await _drive_id(files_client)
    await _hold(files_client, drive, leased.id, real_session, idem)

    async def _page(folder_id: uuid.UUID) -> tuple[int, list[dict[str, Any]]]:
        with counting() as seen:
            response = await files_client.get(
                f"{BASE}/drives/{drive}/items/{folder_id}/children?limit=500"
            )
        assert response.status_code == 200, response.text
        return len(seen), list(response.json()["value"])

    single, one_row = await _page(one.id)
    six, six_rows = await _page(many.id)

    holders = {row["lease"]["holder"] for row in one_row + six_rows}
    assert holders == {"Grace Hopper"}, "every row under the mount names the holder"
    assert six == single, f"six rows cost {six} statements, one row cost {single}"
