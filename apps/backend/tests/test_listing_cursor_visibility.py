"""A listing's cursor never names a row the caller may not read.

The chats, objects and chat-template listings page the org's rows newest first
and cut each page to what the caller may read. When the cut ran AFTER the page
was taken, two things leaked: the cursor was minted from the last row fetched
(so walking ``?limit=1`` handed a member the id and creation time of every
private chat in the org, one cursor at a time), and a page cut to nothing still
said "there is more" (so the page count itself told the member how many hidden
rows sat between theirs).

Each case builds two members of one org, interleaves their private rows, and
walks the listing as the member with fewer of them at ``limit=1``. The walk
must yield exactly that member's rows, in order, in exactly as many pages as
there are rows — and every cursor handed out, decoded with the listing's own
codec, must name one of those rows.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.models import WorkspaceObject
from backend.services.chats import templates as chat_template_service
from backend.services.objects.object_service import cursor_scope, decode_cursor
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio]

RESULT_SPEC: dict[str, Any] = {"columns": [{"name": "day", "label": "Day"}, {"name": "n"}]}


async def _chat(client: AsyncClient, title: str) -> str:
    made = await client.post("/api/v1/chats", json={"title": title})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


async def _result(client: AsyncClient, title: str) -> str:
    made = await client.post(
        "/api/v1/objects",
        json={"type": "result", "title": title, "spec": RESULT_SPEC, "client_id": uuid4().hex},
    )
    assert made.status_code == 201, made.text
    assert made.json()["visibility_scope"] == "private"
    return str(made.json()["id"])


async def _template(client: AsyncClient, title: str) -> str:
    chat_id = await _chat(client, title)
    saved = await client.post(
        "/api/v1/chat-templates",
        json={"source_chat_id": chat_id, "brief": title},
        headers={"Idempotency-Key": secrets.token_hex(8)},
    )
    assert saved.status_code in (200, 201), saved.text
    return str(saved.json()["id"])


Maker = Callable[[AsyncClient, str], Awaitable[str]]

#: (listing path, query, cursor scope type, how a row of it is made)
LISTINGS: dict[str, tuple[str, dict[str, str], str | None, Maker]] = {
    "chats": ("/api/v1/chats", {}, "chat", _chat),
    "objects-of-type-chat": ("/api/v1/objects", {"type": "chat"}, "chat", _chat),
    "objects-untyped": ("/api/v1/objects", {}, None, _result),
    "chat-templates": (
        "/api/v1/chat-templates",
        {},
        chat_template_service.TEMPLATE_TYPE,
        _template,
    ),
}


async def _walk(
    client: AsyncClient, path: str, query: dict[str, str]
) -> tuple[list[list[str]], list[str]]:
    """Every page of the listing at ``limit=1``, and every cursor handed out."""
    pages: list[list[str]] = []
    cursors: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        params = {**query, "limit": "1", **({"cursor": cursor} if cursor else {})}
        resp = await client.get(path, params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pages.append([str(item["id"]) for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages, cursors
        cursors.append(cursor)
    raise AssertionError("the walk did not end")


async def _two_members(
    real_session: AsyncSession, org: OrgWithAdmin
) -> tuple[AsyncClient, AsyncClient]:
    clients = []
    for _ in range(2):
        user, password = await make_member(real_session, org_id=org.org_id, verified=True)
        assert password is not None
        clients.append(await login(app_client(), user.email, password))
    return clients[0], clients[1]


@pytest.mark.parametrize("listing", list(LISTINGS))
async def test_walking_a_listing_reveals_only_the_callers_own_rows(
    listing: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    path, query, scope_type, make = LISTINGS[listing]
    hoarder, walker = await _two_members(real_session, org_admin)
    # Interleaved, newest last: hidden rows sit before, between and after the
    # walker's own, so a page boundary lands on a hidden row whichever way the
    # listing is cut.
    mine: list[str] = []
    theirs: list[str] = []
    for index in range(3):
        theirs.append(await make(hoarder, f"Hidden {index}a"))
        mine.append(await make(walker, f"Mine {index}"))
        theirs.append(await make(hoarder, f"Hidden {index}b"))
    theirs.append(await make(hoarder, "Hidden last"))

    pages, cursors = await _walk(walker, path, query)

    seen = [row for page in pages for row in page]
    assert seen == list(reversed(mine)), "the walk is the walker's rows, newest first"
    assert len(pages) == len(mine), "one page per visible row: no empty page betrays a gap"
    assert all(len(page) == 1 for page in pages)
    scope = cursor_scope(org_team_id=org_admin.org_id, type=scope_type)
    named = {str(decode_cursor(cursor, scope=scope)[1]) for cursor in cursors}
    assert named <= set(mine), f"a cursor names a row the walker may not read: {named - set(mine)}"
    assert not named & set(theirs)
    for client in (hoarder, walker):
        await client.aclose()


async def test_a_walk_under_a_long_run_of_hidden_rows_still_ends_on_the_last_visible_row(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """More hidden rows than any one batch holds, between and after the
    walker's two: the listing keeps reading until it has the page it owes (or
    the end), rather than handing back a short page and a cursor into the dark."""
    hoarder, walker = await _two_members(real_session, org_admin)
    older = await _chat(walker, "Mine old")
    for index in range(12):
        await _chat(hoarder, f"Hidden {index}")
    newer = await _chat(walker, "Mine new")
    for index in range(12):
        await _chat(hoarder, f"Hidden late {index}")

    pages, cursors = await _walk(walker, "/api/v1/chats", {})

    assert pages == [[newer], [older]]
    scope = cursor_scope(org_team_id=org_admin.org_id, type="chat")
    assert [str(decode_cursor(c, scope=scope)[1]) for c in cursors] == [newer]
    for client in (hoarder, walker):
        await client.aclose()


async def test_a_spare_is_not_a_page_boundary_for_the_person_it_is_warmed_for(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A spare chat is cut from a person's listing like a hidden row, so it
    must not be where their cursor points either."""
    _hoarder, walker = await _two_members(real_session, org_admin)
    older = await _chat(walker, "Mine old")
    spare = await _chat(walker, "Warming")
    newer = await _chat(walker, "Mine new")
    row = await real_session.get(WorkspaceObject, UUID(spare))
    assert row is not None
    await real_session.execute(
        update(WorkspaceObject)
        .where(WorkspaceObject.id == row.id)
        .values(spec={**(row.spec or {}), "spare": True})
    )
    await real_session.commit()

    pages, cursors = await _walk(walker, "/api/v1/chats", {})

    assert pages == [[newer], [older]]
    scope = cursor_scope(org_team_id=org_admin.org_id, type="chat")
    assert [str(decode_cursor(c, scope=scope)[1]) for c in cursors] == [newer]
    await walker.aclose()


async def test_the_scan_ceiling_ends_a_page_without_a_cursor_into_hidden_rows(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read a page makes past hidden rows is bounded. A page that reaches
    the bound hands back what it found with a cursor from its last READABLE
    row, and a page that found nothing ends the listing — it never mints a
    cursor from the hidden row it stopped on."""
    from alkera_core.config import settings

    hoarder, walker = await _two_members(real_session, org_admin)
    older = await _chat(walker, "Mine old")
    hidden = [await _chat(hoarder, f"Hidden {index}") for index in range(6)]
    newer = await _chat(walker, "Mine new")
    monkeypatch.setattr(settings, "objects_list_scan_ceiling", 2)

    pages, cursors = await _walk(walker, "/api/v1/chats", {})

    assert pages[0] == [newer]
    assert all(row in (newer, older) for page in pages for row in page)
    scope = cursor_scope(org_team_id=org_admin.org_id, type="chat")
    named = {str(decode_cursor(c, scope=scope)[1]) for c in cursors}
    assert named <= {newer, older}
    assert not named & set(hidden)
    for client in (hoarder, walker):
        await client.aclose()
