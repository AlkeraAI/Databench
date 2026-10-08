"""A chat template's derived member is readable over HTTP.

The ``README.md`` inside a ``.alkerachat.template`` folder holds no stored
bytes: it is rendered from the template's row on every read. The content route
only ever minted a signed URL for a stored head version, so it answered the
opaque 404 that a node nobody may read gets — the README the shipped agent
guidance tells a fresh chat to read FIRST was reachable from no surface at all,
by a human or by a machine.

Every case here drives the real app against real Postgres, so a green case is
the route, the provider registry and the renderer agreeing rather than a
fixture echoing itself.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from _files_kit import refusal
from alkera_core.models import User, WorkspaceObject
from backend.services.objects import object_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: The template a case is created with. The brief is the author's own prose and
#: the README reproduces it verbatim, so an empty or half-rendered answer
#: cannot pass.
TEMPLATE_TITLE = "Weekly revenue"
TEMPLATE_BRIEF = "Revenue is up in every region that reports weekly. Ask which region to chart."
TEMPLATE_SPEC = {"brief": TEMPLATE_BRIEF}


async def _drive_id(client: AsyncClient) -> str:
    return str((await client.get(f"{BASE}/drives")).json()["id"])


async def _template_folder(
    client: AsyncClient, session: AsyncSession, owner_id: UUID
) -> tuple[str, str]:
    """A chat template, as ``(object id, folder node id)``.

    Seeded through the object service rather than the chat-templates route:
    that route cuts a template out of a live chat, and nothing about the
    rendering depends on where the brief came from.
    """
    owner = await session.get(User, owner_id)
    assert owner is not None
    created, _ = await object_service.create_object(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="chat_template",
        title=TEMPLATE_TITLE,
        spec=dict(TEMPLATE_SPEC),
        logical_id=f"t-{uuid.uuid4().hex}",
    )
    await session.commit()
    object_id = str(created.id)

    drive = await _drive_id(client)
    root = str((await client.get(f"{BASE}/drives")).json()["rootId"])
    found = await _find_object_node(client, drive, root, object_id)
    assert found is not None, "a saved template materialises a folder in the drive"
    return object_id, found


async def _find_object_node(
    client: AsyncClient, drive: str, folder: str, object_id: str, depth: int = 4
) -> str | None:
    """The node the object materialised at, found the way a browsing user finds it.

    A template lands under ``/home/<user>/``, not at the drive root, and the
    route does not echo the node id back — so the test walks the tree rather
    than being told where to look, which is also what proves the folder is
    reachable by browsing at all.
    """
    listing = await client.get(f"{BASE}/drives/{drive}/items/{folder}/children")
    assert listing.status_code == 200, listing.text
    rows = listing.json()["value"]
    for row in rows:
        if (row["object"] or {}).get("id") == object_id:
            return str(row["id"])
    if depth <= 0:
        return None
    for row in rows:
        if row["kind"] != "folder" or row["object"]:
            continue
        deeper = await _find_object_node(client, drive, str(row["id"]), object_id, depth - 1)
        if deeper is not None:
            return deeper
    return None


async def _members(client: AsyncClient, folder_id: str) -> dict[str, str]:
    drive = await _drive_id(client)
    listing = await client.get(f"{BASE}/drives/{drive}/items/{folder_id}/children")
    assert listing.status_code == 200, listing.text
    return {str(row["name"]): str(row["id"]) for row in listing.json()["value"]}


def _content(drive: str, node: str) -> str:
    return f"{BASE}/drives/{drive}/items/{node}/content"


async def test_the_readme_of_a_template_folder_is_served_as_prose(
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """The half of a template a person reads comes back as markdown.

    Before the provider registry was wired into the content route this was a
    404: the node carries no head version, and the route could only sign a URL
    for one.
    """
    _, folder = await _template_folder(files_client, real_session, files_org.org.admin_id)
    members = await _members(files_client, folder)
    drive = await _drive_id(files_client)

    response = await files_client.get(_content(drive, members["README.md"]))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    body = response.text
    assert body.startswith(f"# {TEMPLATE_TITLE}\n")
    # The README is only useful if it carries the parts a fresh chat has to act
    # on: what its author asked for, and where the files it starts with live.
    assert TEMPLATE_BRIEF in body
    assert "scratch/" in body


async def test_a_rendered_member_follows_the_row_with_nothing_rewritten(
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """A brief edit changes what the route serves, because nothing was stored.

    This is what separates a rendering from a copy taken at create time: had
    the folder been materialised with bytes, the second read would still hold
    the first brief.
    """
    object_id, folder = await _template_folder(files_client, real_session, files_org.org.admin_id)
    members = await _members(files_client, folder)
    drive = await _drive_id(files_client)
    before = (await files_client.get(_content(drive, members["README.md"]))).text
    assert "Now with a different story." not in before

    stored = await real_session.get(WorkspaceObject, UUID(object_id))
    assert stored is not None
    await object_service.apply_update(
        real_session,
        obj=stored,
        expected_version=stored.version,
        spec={**TEMPLATE_SPEC, "brief": "Now with a different story."},
    )
    await real_session.commit()

    after = await files_client.get(_content(drive, members["README.md"]))
    assert after.status_code == 200, after.text
    assert "Now with a different story." in after.text


async def test_a_download_of_a_rendered_member_is_an_attachment(
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """``?download=1`` saves the file rather than opening a tab, as it does for
    a stored version — the two origins answer the header from one builder."""
    _, folder = await _template_folder(files_client, real_session, files_org.org.admin_id)
    members = await _members(files_client, folder)
    drive = await _drive_id(files_client)

    response = await files_client.get(
        _content(drive, members["README.md"]), params={"download": "1"}
    )

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="README.md"' in disposition


async def test_a_file_node_holding_no_bytes_still_answers_the_opaque_404(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """The rendered path may not become an oracle.

    An ordinary file that was created and never written has no head version
    either. It must keep answering the same 404 a node in another org answers
    — not a typed "no provider for 'bytes'" that would confirm the node is
    there and merely empty.
    """
    empty = await fx.node(b"note.txt")
    drive = await _drive_id(files_client)
    absent = uuid.uuid4()

    mine = await files_client.get(_content(drive, str(empty.id)))
    nobodys = await files_client.get(_content(drive, str(absent)))

    assert mine.status_code == 404, mine.text
    assert refusal(mine) == refusal(nobodys)


async def test_a_derived_member_names_no_object_page_to_open(
    files_client: AsyncClient,
    files_on: None,
    real_session: AsyncSession,
    files_org: FilesOrgFixture,
) -> None:
    """A member is the object rendered as bytes, not the object.

    Its node points at the template the way the folder does, so the item used
    to carry the template's own page in ``object.web_url`` — and a double-click
    on ``README.md`` in the browser landed on the template's page instead of
    opening the file. A member names no destination, so a client opens it the
    way it opens any other file.
    """
    object_id, folder = await _template_folder(files_client, real_session, files_org.org.admin_id)
    drive = await _drive_id(files_client)
    listing = await files_client.get(f"{BASE}/drives/{drive}/items/{folder}/children")
    rows = {str(row["name"]): row for row in listing.json()["value"]}

    container = (await files_client.get(f"{BASE}/drives/{drive}/items/{folder}")).json()
    assert container["object"]["web_url"] == f"/templates/{object_id}", (
        "the folder itself still opens the template"
    )
    readme = rows["README.md"]
    assert readme["object"] is None
    assert readme["capabilities"]["can_download"] is True
