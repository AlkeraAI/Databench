"""Choosing a chat's model, and the stance its session runs in.

Two controls a browser reader never had, driven through the real routes.

The model is the harder half: it is picked from the gateway's catalog, resolved
against that catalog at create time, and stored as the catalog described it —
because the BOX reads the chat row to open the session and cannot look a model
up on a gateway it may not reach. So the tests here pin what the row holds, not
just that the request was accepted.

The gateway is the one collaborator faked: it is a separate process and an
external dependency, reached through ``chat_catalog.fetch_catalog``'s injectable
client. Everything else — Postgres, the routes, the policy, the outbox — is real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import ChatMessage, EventOutbox, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.objects import CHAT_RELAY_ADAPTER, ModelRelay, ModeRelay
from backend.services.chats import catalog as chat_catalog
from backend.services.chats import chat_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio


#: A catalog the way the gateway serves one: two real models and one e2e
#: fixture family, which a human picker must never be offered.
CATALOG: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-4.5",
            "object": "model",
            "display_name": "Claude Opus 4.5",
            "family": "claude",
            "wire": "anthropic",
            "efforts": ["low", "medium", "high"],
            "default_effort": "medium",
            "tier": "frontier",
            "context_window": 200000,
            "max_output_tokens": 64000,
        },
        {
            "id": "gpt-5.2",
            "object": "model",
            "display_name": "GPT-5.2",
            "family": "gpt",
            "wire": "openai",
            "efforts": [],
            "default_effort": None,
            "tier": "standard",
            "context_window": 400000,
            "max_output_tokens": 128000,
        },
        {
            "id": "t-fixture",
            "object": "model",
            "display_name": "e2e fixture",
            "family": "test",
            "wire": "anthropic",
            "efforts": [],
        },
    ],
    "org_flags": {"web_search_enabled": True},
}


def _gateway(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: dict[str, Any] | None = None,
    status_code: int = 200,
    down: bool = False,
    seen: list[httpx.Request] | None = None,
) -> None:
    """Point the catalog read at a scripted gateway instead of a live one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if down:
            raise httpx.ConnectError("no gateway here", request=request)
        return httpx.Response(status_code, json=body if body is not None else CATALOG)

    real = chat_catalog.fetch_catalog

    async def patched(*args: Any, **kwargs: Any) -> chat_catalog.Catalog:
        kwargs.setdefault("client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return await real(*args, **kwargs)

    monkeypatch.setattr(chat_catalog, "fetch_catalog", patched)


async def _chat_row(chat_id: UUID) -> WorkspaceObject:
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, chat_id)
        assert row is not None
        return row


async def _relays(org_id: UUID) -> list[dict[str, Any]]:
    """Every relay body put on a chat channel for this org, oldest first."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == "doc.op")
            .order_by(EventOutbox.id)
        )
        rows = list(result.scalars().all())
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        envelope = payload.get("envelope") or {}
        for event in (envelope.get("payload") or {}).get("events") or []:
            out.append(event)
    return out


# ---------------------------------------------------------------------------
# The catalog a reader picks from
# ---------------------------------------------------------------------------


async def test_the_catalog_is_the_gateways_minus_the_fixture_families(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev gateway accumulates hundreds of e2e fixture models. They are
    routable and entitled — the gateway serves them — and a human picker that
    offered one would start a chat on a model that answers with a script."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.get("/api/v1/me/chat-models")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [m["id"] for m in items] == ["claude-opus-4.5", "gpt-5.2"]
    assert items[0]["efforts"] == ["low", "medium", "high"]
    assert items[0]["default_effort"] == "medium"
    assert items[0]["wire"] == "anthropic"


async def test_the_catalog_read_names_the_caller_to_the_gateway(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway decides the catalog per caller — entitlement, BYOK — so the
    hop has to carry WHO asked. A read that went out unauthenticated (or as the
    backend itself) would serve one org's catalog to another's reader."""
    seen: list[httpx.Request] = []
    _gateway(monkeypatch, seen=seen)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    await client.get("/api/v1/me/chat-models")

    assert len(seen) == 1
    authorization = seen[0].headers.get("authorization", "")
    assert authorization.startswith("Bearer ")
    from alkera_core.auth.tokens import decode_session_token

    claims = decode_session_token(authorization.removeprefix("Bearer "))
    assert claims.email == org_admin.admin_email


async def test_a_gateway_that_is_down_empties_the_picker_rather_than_the_page(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composer already polls out of an empty catalog. A 502 here would
    take the whole chat page down over a picker the reader may not even use."""
    _gateway(monkeypatch, down=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    response = await client.get("/api/v1/me/chat-models")

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_the_catalog_is_not_readable_without_a_session(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/me/chat-models")).status_code == 401


# ---------------------------------------------------------------------------
# The model a chat is pinned to
# ---------------------------------------------------------------------------


async def test_a_created_chat_pins_the_model_as_the_catalog_described_it(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box reads the chat row to open the session, so the row has to carry
    the catalog's FACTS — the wire above all. An id alone would leave the box
    unable to say which provider to file the model under."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post(
        "/api/v1/chats", json={"title": "Ops", "model": "claude-opus-4.5", "effort": "high"}
    )

    assert created.status_code == 201, created.text
    pin = created.json()["model"]
    assert pin["id"] == "claude-opus-4.5"
    assert pin["wire"] == "anthropic"
    assert pin["effort"] == "high"
    assert pin["efforts"] == ["low", "medium", "high"]
    assert pin["context_window"] == 200000
    # And it is what was STORED, not only what the response said.
    row = await _chat_row(UUID(created.json()["id"]))
    spec = chat_service.chat_spec_of(row)
    assert spec.model is not None
    assert spec.model.id == "claude-opus-4.5"
    assert spec.model.wire == "anthropic"


async def test_an_effort_the_model_does_not_offer_falls_back_to_its_default(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale effort from a client that saw an older catalog must not be
    stored: the box would send a variant the provider rejects."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post(
        "/api/v1/chats", json={"model": "claude-opus-4.5", "effort": "ludicrous"}
    )

    assert created.status_code == 201, created.text
    assert created.json()["model"]["effort"] == "medium"


async def test_a_model_with_no_variants_pins_no_effort(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"model": "gpt-5.2", "effort": "high"})

    assert created.status_code == 201, created.text
    assert created.json()["model"]["effort"] is None


@pytest.mark.parametrize(
    "model_id",
    [
        pytest.param("claude-opus-9", id="a model the catalog never offered"),
        pytest.param("t-fixture", id="an e2e fixture family the picker hides"),
    ],
)
async def test_a_model_the_catalog_does_not_offer_is_refused(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
) -> None:
    """A chat pinned to a model the gateway will not serve fails on its first
    turn with nothing to say why — so it is refused at the door instead."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"model": model_id})

    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "model_not_offered"
    # Named, because the reader picked it by display name and the id is what
    # they have to quote to anyone who can help.
    assert model_id in created.text


async def test_a_chat_with_no_pick_is_refused_while_the_gateway_is_down(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader who picked nothing used to get a chat with no pin, which "ran the
    box's own default" — and the box's default was opencode's credential-less
    hosted model: no metering, no billing, no audit, a model nobody chose, while
    the composer showed another. The box now has no default at all, so a chat
    with no model is one that cannot answer; the create is refused with a reason
    the reader can act on (try again) rather than a 201 that lies."""
    _gateway(monkeypatch, down=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "model_catalog_unavailable"
    listed = await client.get("/api/v1/chats")
    assert listed.json()["items"] == [], "no half-made chat was left behind"


async def test_a_pick_the_gateway_cannot_confirm_is_refused_not_dropped(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half, and the one a live drive found: a reader who DID pick a
    model used to get a 201 saying ``model: null``. That reads as "your choice
    was taken" and it was not — the chat runs the box's default, on a model
    nobody chose, until someone reads the row. An unconfirmable pick is
    refused, and the refusal names the model and the reason."""
    _gateway(monkeypatch, down=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"model": "claude-opus-4.5"})

    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "model_catalog_unavailable"
    assert "claude-opus-4.5" in created.text
    # And no half-made chat was left behind for the reader to find later.
    listed = await client.get("/api/v1/chats")
    assert listed.json()["items"] == []


async def test_a_chat_created_with_no_pick_opens_on_the_catalogs_first_model(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pick and no saved default resolves the way the composer's own
    preselection does: the first selectable model, at its default effort — so
    a chat ALWAYS reaches the box with a model the gateway serves."""
    seen: list[httpx.Request] = []
    _gateway(monkeypatch, seen=seen)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 201, created.text
    pin = created.json()["model"]
    assert pin is not None
    assert pin["id"] == "claude-opus-4.5"
    assert pin["effort"] == "medium"
    assert pin["wire"] == "anthropic"
    assert len(seen) == 1, "the catalog was consulted exactly once"


async def test_a_chat_created_with_no_pick_opens_on_the_saved_default(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved preference wins over the catalog's first model — the same
    order ``GET /me/chat-defaults`` resolves in, so the chat opens on exactly
    the model the composer would have shown."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    saved = await client.patch(
        "/api/v1/me/preferences",
        json={"preferences": {"default_chat_model": "gpt-5.2", "default_chat_effort": None}},
    )
    assert saved.status_code in (200, 204), saved.text

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 201, created.text
    pin = created.json()["model"]
    assert pin is not None
    assert pin["id"] == "gpt-5.2"
    assert pin["wire"] == "openai"


async def test_a_catalog_that_offers_nothing_refuses_the_create(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog of e2e fixtures only offers a human nothing; the create says so
    (ask an admin) rather than pinning a fixture or nothing at all."""
    fixtures_only = {**CATALOG, "data": [row for row in CATALOG["data"] if row["family"] == "test"]}
    _gateway(monkeypatch, body=fixtures_only)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 422, created.text
    assert created.json()["error"]["code"] == "no_model_offered"
    listed = await client.get("/api/v1/chats")
    assert listed.json()["items"] == []


# ---------------------------------------------------------------------------
# The Files node a chat IS
# ---------------------------------------------------------------------------


@pytest.fixture
def files_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Files enabled with a filesystem store, so ``create_chat`` mints the
    chat's node in the same transaction production does.

    ``files_enabled`` is off by default, and this module sits outside
    ``apps/backend/tests/files/`` where the package conftest turns it on — so without this
    the drive cases below never execute at all.
    """
    root = tmp_path / "files-store"
    root.mkdir()
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_store_provider", "filesystem")
    monkeypatch.setattr(settings, "files_store_root", root)


@pytest.mark.usefixtures("files_on")
async def test_a_chat_names_the_drive_folder_it_is(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A chat is a folder in the drive, and the box mounts that folder BY ID.

    Rebuilding the path from the chat's title instead would break the moment
    anyone renamed a chat, and would guess wrong outright for two chats with
    the same title — so the id has to be on the wire, and it has to be the
    node that actually projects THIS chat.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    node_id = created["files_node_id"]
    assert node_id is not None
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(FileNode).where(FileNode.target_object_id == UUID(created["id"]))
        )
        node = result.scalars().one()
    assert str(node.id) == node_id
    # …and the listing says the same thing, so a page and a detail view cannot
    # disagree about which folder a chat is.
    listed = (await client.get("/api/v1/chats")).json()["items"]
    assert next(row["files_node_id"] for row in listed if row["id"] == created["id"]) == node_id


@pytest.mark.usefixtures("files_on")
async def test_two_chats_with_the_same_title_name_two_different_folders(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The case rebuilding the path from the title would get wrong.

    Two chats called the same thing are two folders, and each has to name its
    own — which is the whole reason the id is on the wire rather than the
    title. Asserted both ways: the ids differ, and each one resolves back to
    the chat that reported it.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)

    first = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    second = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    assert first["files_node_id"] and second["files_node_id"]
    assert first["files_node_id"] != second["files_node_id"]
    async with AsyncSessionLocal() as session:
        for chat in (first, second):
            node = (
                (
                    await session.execute(
                        select(FileNode).where(FileNode.id == UUID(chat["files_node_id"]))
                    )
                )
                .scalars()
                .one()
            )
            assert str(node.target_object_id) == chat["id"]


async def test_a_chat_with_no_drive_node_names_none(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files can be off, and a chat created before the drive existed never got
    a node. Neither is an error — the id is a convenience for the box, not
    something a chat needs in order to work — so the field is simply null."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    monkeypatch.setattr(settings, "files_enabled", False)

    created = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    assert created["files_node_id"] is None


# ---------------------------------------------------------------------------
# The stance the session runs in
# ---------------------------------------------------------------------------


async def test_a_new_chat_starts_in_default(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """A chat nobody has set a stance for asks before each change, the same
    stance the desktop starts in; it does not refuse every change."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.json()["permission_mode"] == "default"


@pytest.mark.parametrize("picked", ["read_only", "default", "auto", "plan", "bypass"])
async def test_a_chat_opens_in_the_stance_the_create_named(
    client: AsyncClient, org_admin: OrgWithAdmin, picked: str
) -> None:
    """A composer that offers a model and a stance must be able to send both on
    the one request. Creating and then correcting is two states the box can
    observe — and a window in which the first turn runs in a stance nobody
    chose. The row is asserted, not just the response, because the row is what
    the box reads when it opens the session."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops", "permission_mode": picked})

    assert created.status_code == 201, created.text
    assert created.json()["permission_mode"] == picked
    read_back = await client.get(f"/api/v1/chats/{created.json()['id']}")
    assert read_back.json()["permission_mode"] == picked


async def test_a_named_stance_beats_the_saved_default(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The preference seeds a chat nobody chose a stance for; it does not
    override one they did. Without this the control would be advisory."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.patch(
        "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": "plan"}}
    )

    created = await client.post("/api/v1/chats", json={"permission_mode": "read_only"})

    assert created.json()["permission_mode"] == "read_only"


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        pytest.param(None, "default", id="nothing saved — a new chat starts in default"),
        pytest.param("default", "default", id="a saved default lifts the chat off the floor"),
        pytest.param("plan", "plan", id="a saved plan is honoured"),
        pytest.param("bypass", "bypass", id="a saved bypass is honoured"),
        pytest.param(
            "accept_edits", "default", id="a retired word is never saved, so the start is default"
        ),
    ],
)
async def test_a_create_that_names_no_stance_opens_in_the_saved_one_and_every_read_says_so(
    client: AsyncClient, org_admin: OrgWithAdmin, saved: str | None, expected: str
) -> None:
    """The chain a create with no stance resolves through is the reader's saved
    preference, then the new-chat start (``default``) — there is no org-level
    stance in the product, so nothing sits between the two. What matters is that the box reads
    the same answer the create gave: it opens a chat off the LIST (`sync_once`)
    or off the detail read (`reconcile_chat`), never off the create response, so
    a stance the create returned but a read dropped would still open the chat
    at the floor. All three are asserted to say the same word."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    if saved is not None:
        await client.patch(
            "/api/v1/me/preferences", json={"preferences": {"default_permission_mode": saved}}
        )

    created = await client.post("/api/v1/chats", json={"title": "Ops"})

    assert created.status_code == 201, created.text
    assert created.json()["permission_mode"] == expected
    chat_id = created.json()["id"]
    read_back = await client.get(f"/api/v1/chats/{chat_id}")
    assert read_back.json()["permission_mode"] == expected
    listed = (await client.get("/api/v1/chats")).json()["items"]
    assert [item["permission_mode"] for item in listed if item["id"] == chat_id] == [expected]


@pytest.mark.parametrize("picked", ["sideways", "accept_edits", "READ_ONLY", "AUTO"])
async def test_a_stance_no_harness_runs_cannot_be_named_on_a_create(
    client: AsyncClient, org_admin: OrgWithAdmin, picked: str
) -> None:
    """The create door is exactly as narrow as the dedicated route's. If it
    were not, it would be the way around it: a word the box cannot translate
    into a harness stance would open a chat nobody can say the stance of."""
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"permission_mode": picked})

    assert created.status_code == 422, created.text
    listed = await client.get("/api/v1/chats")
    assert listed.json()["items"] == []


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("default", id="default asks before each change"),
        pytest.param("plan", id="plan explores and proposes"),
        pytest.param("auto", id="auto has the judge weigh the middle"),
        pytest.param("bypass", id="bypass asks about nothing"),
    ],
)
async def test_switching_the_stance_records_it_and_relays_it(
    client: AsyncClient, org_admin: OrgWithAdmin, mode: str
) -> None:
    """Both halves matter. The ROW is what the box reads when it opens the
    session, so a chat resumed on a fresh machine comes back in the stance the
    reader left it in; the RELAY is how a box running the chat right now hears
    about it without waiting for a resume.

    Every stance the picker offers is driven here, ``bypass`` included: a mode
    the surface shows and the route 422s is a control that lies, and the reader
    only finds out at the moment they were trying to stop being asked."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    response = await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": mode})

    assert response.status_code == 200, response.text
    assert response.json()["permission_mode"] == mode
    row = await _chat_row(UUID(chat["id"]))
    assert chat_service.chat_spec_of(row).permission_mode == mode

    relays = [r for r in await _relays(org_admin.org_id) if r.get("kind") == "mode"]
    assert len(relays) == 1
    parsed = CHAT_RELAY_ADAPTER.validate_python(relays[0])
    assert isinstance(parsed, ModeRelay)
    assert parsed.mode == mode
    # Stamped from the authenticated caller, never from the body — it is what
    # lets the box attribute the switch.
    assert parsed.user_id == str(org_admin.admin_id)


async def test_a_switch_is_on_the_transcript_naming_who_where_and_from_what(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The change is a transcript entry every reader renders as a card -- the
    web transcript and a linked Slack thread alike -- so it says who changed
    it, that it was on the web, and the stance it replaced. Switching to the
    stance the chat already runs in is no change, and writes nothing."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": "bypass"})
    await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": "bypass"})

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == UUID(chat["id"]), ChatMessage.kind == "mode.changed")
                .order_by(ChatMessage.seq)
            )
        ).scalars()
        changes = [(row.role, row.event_id, row.payload["payload"]) for row in rows]
    assert len(changes) == 1
    role, event_id, change = changes[0]
    assert role == "system"
    assert event_id.startswith("aside-"), "a mode change answers no waiting message"
    assert (change["previous_mode"], change["mode"], change["decided_via"]) == (
        "default",
        "bypass",
        "web",
    )
    assert change["decided_by_user_id"] == str(org_admin.admin_id)
    assert change["decided_by_name"]


async def test_a_read_after_the_switch_reports_the_new_stance(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The browser seeds its pill from a plain read of the chat, so the switch
    has to be visible there and not only on the response that made it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": "plan"})

    assert (await client.get(f"/api/v1/chats/{chat['id']}")).json()["permission_mode"] == "plan"


@pytest.mark.parametrize(
    ("mode", "refusal"),
    [
        pytest.param(
            "read_only", "This workspace is read-only, so it won't run this.", id="read-only"
        ),
        pytest.param(
            "plan", "Plan mode explores and proposes a plan, so it won't run this.", id="plan"
        ),
        pytest.param("default", None, id="default"),
        pytest.param("auto", None, id="auto"),
        pytest.param("bypass", None, id="bypass"),
    ],
)
async def test_the_read_says_whether_an_approval_on_a_write_would_be_acted_on(
    client: AsyncClient, org_admin: OrgWithAdmin, mode: str, refusal: str | None
) -> None:
    """The permission card renders this verdict instead of keeping its own
    stance table, so every read carries it for the stance the chat is in."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    switched = await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": mode})

    assert switched.json()["approval_refusal"] == refusal
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).json()["approval_refusal"] == refusal


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("", id="no mode at all"),
        pytest.param("READ_ONLY", id="a mode spelled differently"),
        pytest.param("accept_edits", id="a retired word no harness runs"),
        pytest.param("sideways", id="not a stance at all"),
    ],
)
async def test_a_stance_no_harness_runs_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, mode: str
) -> None:
    """A stance decides who is asked, so a word the box cannot translate into
    one would leave the chat running under a policy nobody named. The surface
    agreeing is not enough — a hand-rolled request must be refused too, and the
    row must still say what it said before."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    response = await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": mode})

    assert response.status_code == 422, response.text
    row = await _chat_row(UUID(chat["id"]))
    assert chat_service.chat_spec_of(row).permission_mode == "default"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param({"mode": "sideways"}, "a mode no harness runs", id="wrong value"),
        pytest.param({"permission_mode": "plan"}, "the field named wrong", id="wrong field"),
        pytest.param({"mode": ["plan"]}, "a mode that is not a string", id="wrong type"),
        pytest.param({}, "an empty body", id="nothing at all"),
    ],
)
async def test_a_refused_stance_says_which_stances_exist(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict[str, object], reason: str
) -> None:
    """The refusal has to be actionable, not a pydantic dump.

    Every way of getting this body wrong used to come back as the generic
    ``validation_error`` and a list of ``loc``/``msg`` pairs — the operator who
    guessed ``permission_mode`` for the field name read an array to find out the
    field is called ``mode``, and nothing anywhere named the stances that are
    actually accepted. A body with exactly one field has one refusal worth
    giving: this is the name, and these are the words.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    response = await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json=body)

    assert response.status_code == 422, f"{reason}: {response.text}"
    error = response.json()["error"]
    assert error["code"] == "invalid_permission_mode"
    assert "read_only" in error["message"] and "plan" in error["message"]
    assert '"mode"' in error["message"]
    # The pydantic diagnosis is still there; only the headline changed.
    assert error["details"]["errors"]
    # Nothing was written on the way to the refusal.
    row = await _chat_row(UUID(chat["id"]))
    assert chat_service.chat_spec_of(row).permission_mode == "default"


async def test_a_named_body_refusal_does_not_leak_onto_its_neighbours(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The naming is per-route, not per-app: a sibling chat route with a bad
    body still answers the generic code. Registering one endpoint must not
    rewrite the refusal every other endpoint gives."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    response = await client.put(f"/api/v1/chats/{chat['id']}/model", json={"effort": "high"})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_chat_in_another_org_cannot_be_put_in_a_stance(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """Deciding how the agent behaves is speaking in the chat, so it is gated
    like speaking — and a chat outside the caller's audience is an opaque 404,
    the same answer every other chat route gives."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {uuid4().hex[:8]}",
        admin_email=f"outsider-{uuid4().hex[:8]}@example.com",
        admin_first_name="Out",
        admin_last_name="Sider",
        admin_password="outsider-pass-12345",
    )
    other_admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    assert other_org.id != org_admin.org_id
    await login(client, other_admin.email, "outsider-pass-12345")

    response = await client.put(
        f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": "default"}
    )

    assert response.status_code == 404, response.text
    row = await _chat_row(UUID(chat["id"]))
    assert chat_service.chat_spec_of(row).permission_mode == "default"


# ---------------------------------------------------------------------------
# Switching the model on an OPEN chat
# ---------------------------------------------------------------------------


async def test_switching_the_model_records_it_and_relays_it(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker on an open chat is not a local preference.

    Both halves matter, the same two the stance switch needs. The ROW is what
    the box reads when it opens the session, so a reload and a resume both come
    back on the model the reader picked; the RELAY is how a box already running
    the chat hears about it in time for the next turn. Without the row the chat
    silently reverts to the workspace default — the reader is shown one model
    over a transcript another one wrote, and billed for the difference.
    """
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops", "model": "gpt-5.2"})).json()

    response = await client.put(
        f"/api/v1/chats/{chat['id']}/model", json={"model": "claude-opus-4.5", "effort": "low"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["model"]["id"] == "claude-opus-4.5"
    row = await _chat_row(UUID(chat["id"]))
    stored = chat_service.chat_spec_of(row).model
    assert stored is not None
    assert stored.id == "claude-opus-4.5"
    # The row carries the catalog's FACTS, not the id alone — the box files the
    # model under a provider from the wire and cannot look it up itself.
    assert stored.wire == "anthropic"
    assert stored.effort == "low"
    assert stored.context_window == 200000

    relays = [r for r in await _relays(org_admin.org_id) if r.get("kind") == "model"]
    assert len(relays) == 1
    parsed = CHAT_RELAY_ADAPTER.validate_python(relays[0])
    assert isinstance(parsed, ModelRelay)
    # The whole pin rides, so the box translates a switch through the same
    # translation it applies to the row it read when it opened the session.
    assert parsed.pin["id"] == "claude-opus-4.5"
    assert parsed.pin["wire"] == "anthropic"
    assert parsed.pin["effort"] == "low"
    # Stamped from the authenticated caller, never from the body.
    assert parsed.user_id == str(org_admin.admin_id)


async def _model_changes(chat_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.chat_id == UUID(chat_id), ChatMessage.kind == "model.changed")
                .order_by(ChatMessage.seq)
            )
        ).scalars()
        return [(row.role, row.event_id, row.payload["payload"]) for row in rows]


async def test_a_model_switch_is_on_the_transcript_naming_who_where_and_from_what(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every reader sees when the chat moved, who moved it, from where and off
    what, in order with the turns around it. A switch onto the model and effort
    the chat already runs is no change, and writes nothing."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops", "model": "gpt-5.2"})).json()

    body = {"model": "claude-opus-4.5", "effort": "low"}
    assert (await client.put(f"/api/v1/chats/{chat['id']}/model", json=body)).status_code == 200
    assert (await client.put(f"/api/v1/chats/{chat['id']}/model", json=body)).status_code == 200

    changes = await _model_changes(chat["id"])
    assert len(changes) == 1
    role, event_id, change = changes[0]
    assert role == "system"
    assert event_id.startswith("aside-"), "a model change answers no waiting message"
    assert (change["previous_model_id"], change["previous_effort"]) == ("gpt-5.2", None)
    assert (change["model_id"], change["effort"], change["display_name"]) == (
        "claude-opus-4.5",
        "low",
        "Claude Opus 4.5",
    )
    assert change["previous_display_name"] == "GPT-5.2"
    assert change["decided_via"] == "web"
    assert change["decided_by_user_id"] == str(org_admin.admin_id)
    assert change["decided_by_name"]


async def test_an_effort_change_alone_is_on_the_transcript(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same model, another effort: the next turn runs differently (and costs
    differently), so the change is recorded, with the model unchanged."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (
        await client.post(
            "/api/v1/chats", json={"title": "Ops", "model": "claude-opus-4.5", "effort": "high"}
        )
    ).json()

    await client.put(
        f"/api/v1/chats/{chat['id']}/model", json={"model": "claude-opus-4.5", "effort": "low"}
    )

    [(_role, _id, change)] = await _model_changes(chat["id"])
    assert (change["previous_model_id"], change["model_id"]) == (
        "claude-opus-4.5",
        "claude-opus-4.5",
    )
    assert (change["previous_effort"], change["effort"]) == ("high", "low")


async def test_a_refused_switch_writes_no_change(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()

    refused = await client.put(f"/api/v1/chats/{chat['id']}/model", json={"model": "no-such"})

    assert refused.status_code == 422
    assert await _model_changes(chat["id"]) == []


async def test_the_model_a_turn_ran_on_is_kept_on_the_transcript(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The box stamps each reply with the model and effort that ran it; the
    stamp lands on the stored row as the box sent it. An older box sends no
    stamp, and its row stores none."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    row = await _chat_row(UUID(chat["id"]))
    stamp = {"provider_id": "alkera-anthropic", "model_id": "claude-opus-4.5", "effort": "low"}
    events = [
        {
            "event_id": "m-new",
            "role": "assistant",
            "kind": "message.created",
            "payload": {
                "event_type": "message.created",
                "event_id": "m-new",
                "session_id": chat["id"],
                "message_id": "msg-1",
                "role": "assistant",
                "model": stamp,
            },
        },
        {
            "event_id": "m-old",
            "role": "assistant",
            "kind": "message.created",
            "payload": {
                "event_type": "message.created",
                "event_id": "m-old",
                "session_id": chat["id"],
                "message_id": "msg-2",
                "role": "assistant",
            },
        },
    ]
    async with AsyncSessionLocal() as session:
        await chat_service.persist_published_events(session, chat=row, events=events)
        await session.commit()
    async with AsyncSessionLocal() as session:
        stored = {
            r.event_id: r.payload["payload"]
            for r in (
                await session.execute(
                    select(ChatMessage).where(ChatMessage.chat_id == UUID(chat["id"]))
                )
            ).scalars()
        }
    assert stored["m-new"]["model"] == stamp
    assert "model" not in stored["m-old"]


async def test_a_read_after_the_switch_reports_the_new_model(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this closes, stated the way the client hit it: pick a model,
    leave the chat, come back — and the chip must still say what they picked
    rather than relabelling their transcript with the workspace default."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    # No pick opens on the workspace default (the catalog's first model)…
    assert chat["model"]["id"] == "claude-opus-4.5"

    await client.put(f"/api/v1/chats/{chat['id']}/model", json={"model": "gpt-5.2"})

    # …and a switch away from it is what the reread reports.
    reread = (await client.get(f"/api/v1/chats/{chat['id']}")).json()
    assert reread["model"] is not None
    assert reread["model"]["id"] == "gpt-5.2"
    assert reread["model"]["display_name"] == "GPT-5.2"


async def test_a_switch_takes_the_new_models_default_effort_not_the_old_ones(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A switch names a MODEL; the variant that rides with it is that model's
    own default. Carrying the previous model's variant across is how a provider
    gets sent an effort it does not offer."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (
        await client.post(
            "/api/v1/chats", json={"title": "Ops", "model": "claude-opus-4.5", "effort": "high"}
        )
    ).json()
    assert chat["model"]["effort"] == "high"

    # gpt-5.2 offers no variants at all, so the old "high" must not survive.
    onto_plain = await client.put(f"/api/v1/chats/{chat['id']}/model", json={"model": "gpt-5.2"})

    assert onto_plain.status_code == 200, onto_plain.text
    assert onto_plain.json()["model"]["effort"] is None

    # And back the other way: no effort named means the model's own default.
    back = await client.put(f"/api/v1/chats/{chat['id']}/model", json={"model": "claude-opus-4.5"})

    assert back.json()["model"]["effort"] == "medium"


@pytest.mark.parametrize(
    "picked",
    [
        pytest.param("t-fixture", id="an e2e fixture family a human is never offered"),
        pytest.param("gpt-9", id="a model no gateway serves"),
        pytest.param("claude-opus-4.5 ", id="an id with a trailing space"),
        pytest.param("CLAUDE-OPUS-4.5", id="an id spelled in the wrong case"),
    ],
)
async def test_a_model_the_catalog_does_not_offer_cannot_be_switched_onto(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    picked: str,
) -> None:
    """The same refusal a create makes, for the same reason: a chat pinned to a
    model the box cannot reach fails on its first turn with nothing to say why.
    And nothing is written — a refused switch leaves the chat where it was."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops", "model": "gpt-5.2"})).json()

    response = await client.put(f"/api/v1/chats/{chat['id']}/model", json={"model": picked})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "model_not_offered"
    stored = chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model
    assert stored is not None
    assert stored.id == "gpt-5.2"
    assert [r for r in await _relays(org_admin.org_id) if r.get("kind") == "model"] == []


async def test_a_switch_the_gateway_cannot_confirm_is_refused_not_dropped(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway that is DOWN must not read as "no pick". A 200 saying the chat
    moved, over a chat that did not, is the one answer nobody can act on — and
    the two refusals are told apart, because "retry the same pick" and "pick
    another" are different instructions."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    _gateway(monkeypatch, down=True)

    response = await client.put(
        f"/api/v1/chats/{chat['id']}/model", json={"model": "claude-opus-4.5"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "model_catalog_unavailable"
    # The chat stays on the pin it was created with — nothing was dropped.
    kept = chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model
    assert kept is not None and kept.id == chat["model"]["id"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"model": ""}, id="an empty id"),
        pytest.param({}, id="no model field at all"),
        pytest.param({"effort": "low"}, id="an effort with no model"),
    ],
)
async def test_a_body_naming_no_model_cannot_switch_a_chat(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    """A create may decline to pick — the box then runs its own default. A
    SWITCH that pins nothing would report a move that never happened."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops", "model": "gpt-5.2"})).json()

    assert (await client.put(f"/api/v1/chats/{chat['id']}/model", json=body)).status_code == 422
    stored = chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model
    assert stored is not None
    assert stored.id == "gpt-5.2"


async def test_switching_the_model_leaves_the_stance_alone(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two controls sit on the same spec dict and are written by the same
    lock-and-merge. A switch that dropped the other one would silently lift a
    read-only chat out of read-only, which is the expensive direction."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    await client.put(f"/api/v1/chats/{chat['id']}/permission-mode", json={"mode": "plan"})

    switched = await client.put(
        f"/api/v1/chats/{chat['id']}/model", json={"model": "claude-opus-4.5"}
    )

    assert switched.json()["permission_mode"] == "plan"
    assert chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).permission_mode == "plan"


async def test_a_chat_in_another_org_cannot_be_moved_onto_a_model(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing what answers is speaking in the chat, so it is gated like
    speaking — and a chat outside the caller's audience is an opaque 404, the
    same answer every other chat route gives."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops", "model": "gpt-5.2"})).json()

    _, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {uuid4().hex[:8]}",
        admin_email=f"outsider-{uuid4().hex[:8]}@example.com",
        admin_first_name="Out",
        admin_last_name="Sider",
        admin_password="outsider-pass-12345",
    )
    other_admin.email_verified_at = datetime.now(UTC)
    await real_session.commit()
    await login(client, other_admin.email, "outsider-pass-12345")

    response = await client.put(
        f"/api/v1/chats/{chat['id']}/model", json={"model": "claude-opus-4.5"}
    )

    assert response.status_code == 404, response.text
    stored = chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model
    assert stored is not None
    assert stored.id == "gpt-5.2"


# ---------------------------------------------------------------------------
# A chat from before every row carried a model
# ---------------------------------------------------------------------------


async def _strip_pin(chat_id: UUID) -> None:
    """Leave the row the way a writer from before the pin existed left it."""
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, chat_id)
        assert row is not None
        row.spec = {**row.spec, "model": None}
        await session.commit()


async def _model_relays(org_id: UUID) -> list[dict[str, Any]]:
    return [r for r in await _relays(org_id) if r.get("kind") == "model"]


async def test_a_legacy_chat_is_pinned_on_its_first_read_the_way_a_create_is(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that names no model is not a chat the box can run a turn on, and
    it must not stay that way until somebody opens the picker: the first read
    resolves the pin the way a create that named none does — the owner's saved
    default, else the catalog's first — persists it on the row (what the box
    reads at open) and relays it (what a box already running the chat hears).
    """
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    saved = await client.patch(
        "/api/v1/me/preferences",
        json={"preferences": {"default_chat_model": "gpt-5.2", "default_chat_effort": None}},
    )
    assert saved.status_code in (200, 204), saved.text
    await _strip_pin(UUID(chat["id"]))

    read = await client.get(f"/api/v1/chats/{chat['id']}")

    assert read.status_code == 200, read.text
    assert read.json()["model"]["id"] == "gpt-5.2"
    assert read.json()["model"]["wire"] == "openai"
    stored = chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model
    assert stored is not None
    assert stored.id == "gpt-5.2"
    relays = await _model_relays(org_admin.org_id)
    assert len(relays) == 1
    parsed = CHAT_RELAY_ADAPTER.validate_python(relays[0])
    assert isinstance(parsed, ModelRelay)
    assert parsed.pin["id"] == "gpt-5.2"
    # Once: the next read finds the pin on the row and resolves nothing.
    again = await client.get(f"/api/v1/chats/{chat['id']}")
    assert again.json()["model"]["id"] == "gpt-5.2"
    assert len(await _model_relays(org_admin.org_id)) == 1


async def test_the_chat_list_pins_a_legacy_chat_the_way_the_box_discovers_it(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The box finds the chats bound to it through the LIST, not through a
    read of each — so a legacy row has to be pinned there too, or the box
    opens it unpinned and refuses its first turn."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    await _strip_pin(UUID(chat["id"]))

    listed = await client.get("/api/v1/chats")

    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["items"] if item["id"] == chat["id"])
    assert row["model"]["id"] == "claude-opus-4.5"
    assert row["model"]["effort"] == "medium"
    stored = chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model
    assert stored is not None
    assert stored.id == "claude-opus-4.5"
    assert len(await _model_relays(org_admin.org_id)) == 1


async def test_a_legacy_chat_stays_readable_and_unpinned_while_the_catalog_is_down(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read is not the place to refuse: the chat still opens, the row is
    left as it was (nothing is guessed), and the picker is the way out — the
    refusal, if it comes, is the box's, at the turn, with the reason in the
    chat."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Ops"})).json()
    await _strip_pin(UUID(chat["id"]))
    _gateway(monkeypatch, down=True)

    read = await client.get(f"/api/v1/chats/{chat['id']}")

    assert read.status_code == 200, read.text
    assert read.json()["model"] is None
    assert chat_service.chat_spec_of(await _chat_row(UUID(chat["id"]))).model is None
    assert not await _model_relays(org_admin.org_id)


# ---------------------------------------------------------------------------
# An effort named without a model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("effort", "pinned"),
    [
        pytest.param("high", "high", id="an effort the resolved model offers is kept"),
        pytest.param("ultra", "medium", id="one it does not offer falls back to its default"),
    ],
)
async def test_an_effort_named_without_a_model_rides_the_model_that_resolves(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
    pinned: str,
) -> None:
    """The composer names the effort off its preselected model and can send
    the pick before the catalog it names the model from has loaded, so the
    body carries an effort and no model. The effort rides the model the
    server resolves — when that model offers it — rather than being dropped
    for the default under a chip that promised another."""
    _gateway(monkeypatch)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    created = await client.post("/api/v1/chats", json={"title": "Ops", "effort": effort})

    assert created.status_code == 201, created.text
    pin = created.json()["model"]
    assert pin["id"] == "claude-opus-4.5"
    assert pin["effort"] == pinned
    stored = chat_service.chat_spec_of(await _chat_row(UUID(created.json()["id"]))).model
    assert stored is not None
    assert stored.effort == pinned
