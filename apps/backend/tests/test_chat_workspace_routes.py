"""The workspace a reader keeps beside a chat, driven through the real routes.

Every branch of the contract with the row it does (or does not) leave behind:
the default document a never-saved chat answers, the round-trip, two readers of
one chat keeping independent layouts, the opaque 404 a non-reader gets on BOTH
verbs, the 403 a token gets, and the two refusals that keep the row small and
ids-only. The decision rows are asserted alongside the statuses, because the
status alone cannot tell an allowed read from one that was never decided.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ChatWorkspaceState, EventOutbox, TeamRole, User, WorkspaceObject
from alkera_core.schemas.objects import MAX_WORKSPACE_STATE_BYTES, MAX_WORKSPACE_TABS
from backend.services.credentials import ci_tokens as ci_token_service
from backend.services.credentials import pats as pat_service
from backend.services.credentials import proxy_tokens as proxy_token_service
from backend.services.org import teams as team_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from tests._suite_app import app as fastapi_app
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = pytest.mark.asyncio

AUTHZ_TYPE = "authz.decision"


def _url(chat_id: str | UUID) -> str:
    return f"/api/v1/chats/{chat_id}/workspace"


def _tab(**overrides: Any) -> dict[str, Any]:
    tab: dict[str, Any] = {
        "id": "t1",
        "kind": "file",
        "node_id": str(uuid4()),
        "name": "q3-revenue.html",
        "path": "q3-revenue.html",
        "params": {},
    }
    tab.update(overrides)
    return tab


async def _create_chat(client: AsyncClient, title: str = "Ops") -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


async def _row_count(chat_id: UUID | str | None = None) -> int:
    async with AsyncSessionLocal() as session:
        stmt = select(func.count()).select_from(ChatWorkspaceState)
        if chat_id is not None:
            stmt = stmt.where(ChatWorkspaceState.chat_id == UUID(str(chat_id)))
        return int((await session.execute(stmt)).scalar_one())


async def _decisions(org_id: UUID, entity_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == AUTHZ_TYPE,
                EventOutbox.entity_id == entity_id,
            )
            .order_by(EventOutbox.id)
        )
        return [dict(row.payload) for row in rows.scalars().all()]


def _without_trace(body: dict[str, Any]) -> dict[str, Any]:
    """An error envelope without its per-request trace id, so two refusals can
    be compared for being the same answer."""
    error = {k: v for k, v in dict(body["error"]).items() if k != "trace_id"}
    return {**body, "error": error}


async def _second_reader(
    client: AsyncClient, org_admin: OrgWithAdmin, chat_id: str, role: str = "reader"
) -> tuple[User, str, AsyncClient]:
    """A verified member of the same org, with ``chat_id`` shared to them at
    ``role``, logged in on a client of their own."""
    async with AsyncSessionLocal() as session:
        member, password = await make_member(
            session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
        )
        assert password is not None
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        owner = await session.get(User, org_admin.admin_id)
        assert owner is not None
        await share_chat(session, chat=chat, owner=owner, user=member, role=role)
        email = member.email
    other = app_client()
    await login(other, email, password)
    return member, email, other


# ---------------------------------------------------------------------------
# The default document, and the round trip
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_a_chat_nobody_has_arranged_answers_the_default_document(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A first-time reader is handed a layout, not an error and not a null —
    so the client renders the same shape on the first open as on the tenth."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)

    response = await client.get(_url(chat["id"]))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"]["tabs"] == []
    assert body["state"]["active_tab_id"] is None
    assert body["updated_at"] is None
    assert await _row_count(chat["id"]) == 0


@pytest.mark.usefixtures("files_on")
async def test_a_saved_workspace_comes_back_on_the_next_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The acceptance: the chat stores your open tabs and gives them back."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    tabs = [_tab(id="t1", name="report.html"), _tab(id="t2", kind="files", node_id=None)]

    saved = await client.put(
        _url(chat["id"]), json={"state": {"tabs": tabs, "active_tab_id": "t2"}}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["updated_at"] is not None

    read = await client.get(_url(chat["id"]))
    assert read.status_code == 200, read.text
    state = read.json()["state"]
    assert [tab["id"] for tab in state["tabs"]] == ["t1", "t2"]
    assert state["tabs"][0]["name"] == "report.html"
    assert state["active_tab_id"] == "t2"
    assert read.json()["updated_at"] == saved.json()["updated_at"]


@pytest.mark.usefixtures("files_on")
async def test_a_second_save_replaces_the_layout_rather_than_merging_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Closing a tab is a save with that tab gone; a merge would reopen it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    first = [_tab(id="t1"), _tab(id="t2")]
    await client.put(_url(chat["id"]), json={"state": {"tabs": first, "active_tab_id": "t1"}})

    await client.put(_url(chat["id"]), json={"state": {"tabs": [_tab(id="t2")]}})

    state = (await client.get(_url(chat["id"]))).json()["state"]
    assert [tab["id"] for tab in state["tabs"]] == ["t2"]
    assert state["active_tab_id"] is None
    assert await _row_count(chat["id"]) == 1


@pytest.mark.usefixtures("files_on")
async def test_two_readers_of_one_chat_keep_their_own_tabs(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A shared chat is one conversation and two workspaces: what the second
    reader opens is theirs, and neither reader can see or disturb the other's."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    await client.put(_url(chat["id"]), json={"state": {"tabs": [_tab(id="owner-tab")]}})
    _member, _email, other = await _second_reader(client, org_admin, chat["id"])

    try:
        theirs_before = await other.get(_url(chat["id"]))
        assert theirs_before.status_code == 200, theirs_before.text
        assert theirs_before.json()["state"]["tabs"] == []

        saved = await other.put(_url(chat["id"]), json={"state": {"tabs": [_tab(id="their-tab")]}})
        assert saved.status_code == 200, saved.text

        mine = await client.get(_url(chat["id"]))
        assert [tab["id"] for tab in mine.json()["state"]["tabs"]] == ["owner-tab"]
        theirs = await other.get(_url(chat["id"]))
        assert [tab["id"] for tab in theirs.json()["state"]["tabs"]] == ["their-tab"]
    finally:
        await other.aclose()
    assert await _row_count(chat["id"]) == 2


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["get", "put"])
@pytest.mark.usefixtures("files_on")
async def test_a_member_who_cannot_read_the_chat_gets_the_opaque_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin, verb: str
) -> None:
    """A chat is private until shared, so a stranger to it learns nothing —
    the same 404 an id that never existed gives, on the read AND the write."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with AsyncSessionLocal() as session:
        member, password = await make_member(
            session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
        )
        assert password is not None
        email = member.email
    outsider = app_client()
    await login(outsider, email, password)
    try:
        if verb == "get":
            refused = await outsider.get(_url(chat["id"]))
        else:
            refused = await outsider.put(
                _url(chat["id"]), json={"state": {"tabs": [_tab(id="t1")]}}
            )
        unknown = await outsider.request(
            refused.request.method, _url(uuid4()), json={"state": {"tabs": []}}
        )
    finally:
        await outsider.aclose()

    assert refused.status_code == 404, refused.text
    assert unknown.status_code == 404
    # Everything but the per-request trace id: the two answers must be
    # indistinguishable, or the 404 tells an outsider the chat exists.
    assert _without_trace(refused.json()) == _without_trace(unknown.json())
    assert await _row_count(chat["id"]) == 0
    effects = [
        (row["effect"], row["action"]) for row in await _decisions(org_admin.org_id, chat["id"])
    ]
    assert ("deny", "read") in effects


@pytest.mark.usefixtures("files_on")
async def test_a_refused_write_leaves_a_deny_row_and_no_workspace_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The refusal survives the request's rollback: the row a reviewer reads is
    written on the deny path, and the layout the caller tried to store is not."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with AsyncSessionLocal() as session:
        member, password = await make_member(
            session, org_id=org_admin.org_id, role=TeamRole.MEMBER, verified=True
        )
        assert password is not None
        email = member.email
    outsider = app_client()
    await login(outsider, email, password)
    try:
        refused = await outsider.put(_url(chat["id"]), json={"state": {"tabs": [_tab(id="t1")]}})
    finally:
        await outsider.aclose()

    assert refused.status_code == 404
    assert await _row_count(chat["id"]) == 0
    reads = [
        row for row in await _decisions(org_admin.org_id, chat["id"]) if row["action"] == "read"
    ]
    assert [row["effect"] for row in reads] == ["deny"]


@pytest.mark.usefixtures("files_on")
async def test_an_allowed_read_is_on_record_too(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Every decision is on record, not only the refusals."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)

    assert (await client.get(_url(chat["id"]))).status_code == 200

    rows = [
        row for row in await _decisions(org_admin.org_id, chat["id"]) if row["action"] == "read"
    ]
    assert [row["effect"] for row in rows] == ["allow"]


@pytest.mark.parametrize("kind", ["ci", "proxy", "pat"])
@pytest.mark.usefixtures("files_on")
async def test_a_token_is_refused_because_a_workspace_belongs_to_a_person(
    client: AsyncClient, org_admin: OrgWithAdmin, kind: str
) -> None:
    """Automation has no tabs open. A CI token, a proxy token and a personal
    access token are all refused with 403 rather than silently writing a layout
    nobody will ever see — and the refusal is not a 401: the credential parsed."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    async with AsyncSessionLocal() as session:
        if kind == "ci":
            _row, raw = await ci_token_service.mint(
                session, org_id=org_admin.org_id, created_by_id=org_admin.admin_id, label="ci"
            )
        elif kind == "proxy":
            _row, raw = await proxy_token_service.mint(
                session, org_id=org_admin.org_id, created_by_id=org_admin.admin_id, label="proxy"
            )
        else:
            _row, raw = await pat_service.mint(
                session, org_id=org_admin.org_id, user_id=org_admin.admin_id, label="pat"
            )
        await session.commit()
    bot = AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test")
    try:
        read = await bot.get(_url(chat["id"]), headers={"Authorization": f"Bearer {raw}"})
        write = await bot.put(
            _url(chat["id"]),
            headers={"Authorization": f"Bearer {raw}"},
            json={"state": {"tabs": [_tab(id="t1")]}},
        )
    finally:
        await bot.aclose()

    assert read.status_code == 403, read.text
    assert write.status_code == 403, write.text
    assert read.json()["error"]["code"] == "workspace_needs_a_user_session"
    assert await _row_count(chat["id"]) == 0


# ---------------------------------------------------------------------------
# What may be stored
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("files_on")
async def test_a_workspace_bigger_than_the_ceiling_is_refused_and_stores_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The client trims and retries on this code, so it must be the code a
    17 KiB document actually gets — and the oversized layout must not land."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    fat = [_tab(id="t1", name="x" * (MAX_WORKSPACE_STATE_BYTES + 1_024))]

    refused = await client.put(_url(chat["id"]), json={"state": {"tabs": fat}})

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "workspace_state_too_large"
    assert await _row_count(chat["id"]) == 0


@pytest.mark.usefixtures("files_on")
async def test_a_document_that_just_fits_is_stored(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The ceiling refuses what is over it and nothing else — a document at the
    limit is a working layout, not an error."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    probe = await client.put(_url(chat["id"]), json={"state": {"tabs": [_tab(id="t1", name="")]}})
    assert probe.status_code == 200, probe.text
    overhead = len(str(probe.json()["state"]))
    name = "y" * max(MAX_WORKSPACE_STATE_BYTES - overhead - 256, 64)

    accepted = await client.put(
        _url(chat["id"]), json={"state": {"tabs": [_tab(id="t1", name=name)]}}
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"]["tabs"][0]["name"] == name


@pytest.mark.parametrize(
    ("state", "field"),
    [
        pytest.param(
            {"tabs": [_tab(id="t1", node_id="https://evil.example/steal")]},
            "tabs.0.node_id",
            id="a-url-where-a-node-id-belongs",
        ),
        pytest.param(
            {"tabs": [_tab(id="t1", node_id="../../etc/passwd")]},
            "tabs.0.node_id",
            id="a-relative-path-where-a-node-id-belongs",
        ),
        pytest.param({"tabs": [_tab(id="")]}, "state", id="a-tab-with-no-id"),
        pytest.param({"tabs": [_tab(id="t1"), _tab(id="t1")]}, "state", id="two-tabs-with-one-id"),
        pytest.param(
            {"tabs": [_tab(id="t1")], "active_tab_id": "t9"},
            "state",
            id="an-active-tab-that-is-not-in-the-document",
        ),
        pytest.param(
            {"tabs": [_tab(id=f"t{i}") for i in range(MAX_WORKSPACE_TABS + 1)]},
            "state",
            id="more-tabs-than-a-workspace-holds",
        ),
    ],
)
@pytest.mark.usefixtures("files_on")
async def test_a_document_the_client_could_not_replay_is_refused(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    state: dict[str, Any],
    field: str,
) -> None:
    """The row is replayed by a browser, so anything fetchable — a URL, a
    scheme, a path — is refused where a node id belongs; and a layout whose
    tabs cannot be addressed is refused before it can be read back."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)

    refused = await client.put(_url(chat["id"]), json={"state": state})

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "invalid_workspace_state"
    assert field in error["message"] or field == "state"
    assert await _row_count(chat["id"]) == 0


@pytest.mark.usefixtures("files_on")
async def test_a_tab_kind_this_build_never_heard_of_is_kept(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A newer client and an older server share one document: the server does
    not know every kind and must not drop the ones it does not, or a reader who
    upgrades loses the tabs they opened."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client)
    future = _tab(id="t1", kind="dashboard", params={"panel": "revenue"})

    saved = await client.put(_url(chat["id"]), json={"state": {"tabs": [future]}})

    assert saved.status_code == 200, saved.text
    stored = (await client.get(_url(chat["id"]))).json()["state"]["tabs"][0]
    assert stored["kind"] == "dashboard"
    assert stored["params"] == {"panel": "revenue"}


@pytest.mark.usefixtures("files_on")
async def test_a_chat_in_another_org_is_not_found_and_writes_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Tenancy is the engine's floor, not a WHERE clause: a chat that belongs to
    another org answers the same 404 an unknown id does."""
    async with AsyncSessionLocal() as session:
        other_org, other_admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Other {secrets.token_hex(4)}",
            admin_email=f"other-{secrets.token_hex(4)}@example.com",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="other-pass-12345",
        )
        other_admin.email_verified_at = datetime.now(UTC)
        other_email, other_password = other_admin.email, "other-pass-12345"
        await session.commit()
    stranger = app_client()
    await login(stranger, other_email, other_password)
    try:
        theirs = await _create_chat(stranger, title="Theirs")
    finally:
        await stranger.aclose()
    await login(client, org_admin.admin_email, org_admin.admin_password)

    refused = await client.put(_url(theirs["id"]), json={"state": {"tabs": [_tab(id="t1")]}})

    assert refused.status_code == 404, refused.text
    assert await _row_count(theirs["id"]) == 0
    assert other_org.id != org_admin.org_id
