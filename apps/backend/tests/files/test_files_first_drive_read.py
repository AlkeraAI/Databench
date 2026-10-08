"""A brand-new org's very first ``GET /files/drives``.

Nothing in this module touches the drive before the route does: there is no
``fx``, no fixture surgery, no seeded node. The org exists, its admin logs in,
and the first thing that ever asks about a drive is the request under test —
which is exactly what the browser does on a person's first visit to ``/files``.

Two things are pinned, because the page depends on both and neither is visible
from the other side of a mock:

* the read *provisions* — a drive that only existed after some other write would
  leave the first visit answering a 404 no product path ever heals;
* the answer is ONE drive object whose ``rootId`` is a node the very next
  request can list. The browser has no node in the URL at ``/files``; the root
  listing is its only way out of the index, and it addresses that listing with
  this field. A list, a snake-cased ``root_id``, or a root that lists nothing
  all leave the page waiting on a request it never makes.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.models.files.stores import FileDrive
from backend.api.routes.files import PREFIX
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

DRIVES = f"{PREFIX}/drives"


async def test_first_read_provisions_the_org_drive(
    files_client: AsyncClient, real_session: AsyncSession, files_on: None
) -> None:
    """No drive row exists until the first read asks for one, and then exactly one does."""
    before = await real_session.scalar(select(func.count()).select_from(FileDrive))

    response = await files_client.get(DRIVES)

    assert response.status_code == 200, response.text
    after = await real_session.scalar(select(func.count()).select_from(FileDrive))
    assert (after or 0) == (before or 0) + 1


async def test_first_read_answers_one_drive_object_not_a_collection(
    files_client: AsyncClient, files_on: None
) -> None:
    """The route answers the caller's one drive. A client that unwrapped a list
    here would read `undefined` for every field and address nothing."""
    body = (await files_client.get(DRIVES)).json()

    assert isinstance(body, dict), body
    # Not a collection envelope wearing an object's clothes, either.
    assert "value" not in body and "entries" not in body


@pytest.mark.parametrize("field", ["id", "orgId", "rootId"])
async def test_the_drive_answer_names_its_ids_in_the_spelling_the_client_reads(
    files_client: AsyncClient, files_on: None, field: str
) -> None:
    """camelCase, non-empty, and a real id. The page reads `rootId` straight off
    this body; a snake-cased or empty one disables the root listing outright, so
    no request is made, nothing fails, and the page waits for ever."""
    body = (await files_client.get(DRIVES)).json()

    assert field in body, body
    assert isinstance(body[field], str) and body[field] != ""
    uuid.UUID(body[field])


async def test_the_root_the_drive_names_is_listable_on_the_first_visit(
    files_client: AsyncClient, files_on: None
) -> None:
    """The second request the browser makes, made the way the browser makes it:
    addressed by the ids the first answer carried. A brand-new org's root holds
    the containers the rail routes to — `/files` lands on `home`."""
    drive = (await files_client.get(DRIVES)).json()

    listing = await files_client.get(
        f"{DRIVES}/{drive['id']}/items/{drive['rootId']}/children", params={"limit": 500}
    )

    assert listing.status_code == 200, listing.text
    names = {row["name"].lower() for row in listing.json()["value"]}
    assert "home" in names, names


async def test_the_drive_read_is_idempotent(files_client: AsyncClient, files_on: None) -> None:
    """A reload must not mint a second drive — the ids are the same both times."""
    first = (await files_client.get(DRIVES)).json()
    second = (await files_client.get(DRIVES)).json()

    assert first["id"] == second["id"]
    assert first["rootId"] == second["rootId"]
