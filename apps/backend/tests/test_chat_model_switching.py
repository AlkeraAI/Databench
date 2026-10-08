"""Moving an open chat between models, through the real routes.

The rule (the owner's): a chat whose history carries reasoning from a model may
not move to a model that cannot read it; a chat with no reasoning yet moves
freely; an effort change on the same model always goes through. The server is
the authority: it derives the chat's reasoning ledger from the turns the box
stamped, refuses a switch the rule rules out with a 409 and writes nothing, and
serves every surface the verdicts (``GET /chats/{id}/model-options``).

The gateway catalog is the one faked collaborator (``chat_catalog.fetch_catalog``'s
injectable client); Postgres, the routes, the policy and the outbox are real.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import (
    ChatMessage,
    EventOutbox,
    MembershipStatus,
    OrgMembership,
    RealtimeDoc,
    User,
    WorkspaceObject,
)
from backend.services.chats import catalog as chat_catalog
from backend.services.chats import chat_service, model_switch
from httpx import AsyncClient
from sqlalchemy import select
from structlog.testing import capture_logs
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = pytest.mark.asyncio


def _model(
    model_id: str, name: str, wire: str, fmt: str | None, reads: list[str]
) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "display_name": name,
        "family": "claude" if wire == "anthropic" else "gpt",
        "wire": wire,
        "efforts": ["low", "high"],
        "default_effort": "high",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "reasoning_format": fmt,
        "reads_reasoning_formats": reads,
    }


OPUS_55 = _model(
    "claude-opus-5.5",
    "Claude Opus 5.5",
    "anthropic",
    "anthropic:claude-opus-5-5",
    ["anthropic:claude-opus-5"],
)
FABLE_51 = _model(
    "claude-fable-5.1",
    "Claude Fable 5.1",
    "anthropic",
    "anthropic:claude-fable-5-1",
    ["anthropic:claude-opus-5-5", "anthropic:claude-opus-5"],
)
OPUS_5 = _model("claude-opus-5", "Claude Opus 5", "anthropic", "anthropic:claude-opus-5", [])
GPT = _model("gpt-5.5", "GPT-5.5", "openai", "openai:gpt-5.5", [])
CATALOG: dict[str, Any] = {"object": "list", "data": [OPUS_55, FABLE_51, OPUS_5, GPT]}


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    real = chat_catalog.fetch_catalog

    async def patched(db: Any, user: Any, org_team_id: Any, **kwargs: Any) -> chat_catalog.Catalog:
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=CATALOG))
        kwargs.setdefault("client", httpx.AsyncClient(transport=transport))
        return await real(db, user, org_team_id, **kwargs)

    monkeypatch.setattr(chat_catalog, "fetch_catalog", patched)


async def _chat(client: AsyncClient, model: str) -> dict[str, Any]:
    response = await client.post("/api/v1/chats", json={"title": "Ops", "model": model})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _ran(
    chat_id: str,
    model_id: str | None,
    *,
    session_id: str | None = None,
    reasoned: bool = True,
) -> None:
    """The box published a reply: stamped with the model that ran it, or not
    at all (a box built before stamps), and with the reasoning part it
    produced unless the model ran with thinking off."""
    event_id = f"m-{uuid4().hex[:8]}"
    sid = session_id or chat_id
    payload: dict[str, Any] = {
        "event_type": "message.created",
        "event_id": event_id,
        "session_id": sid,
        "message_id": event_id,
        "role": "assistant",
    }
    if model_id is not None:
        payload["model"] = {"provider_id": "alkera-anthropic", "model_id": model_id}
    events: list[dict[str, Any]] = [
        {"event_id": event_id, "role": "assistant", "kind": "message.created", "payload": payload}
    ]
    if reasoned:
        part_event = f"p-{uuid4().hex[:8]}"
        events.append(
            {
                "event_id": part_event,
                "role": "assistant",
                "kind": "part.created",
                "payload": {
                    "event_type": "part.created",
                    "event_id": part_event,
                    "session_id": sid,
                    "part": {
                        "type": "reasoning",
                        "part_id": f"prt-{part_event}",
                        "message_id": event_id,
                        "text": "",
                    },
                },
            }
        )
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat_id))
        assert row is not None
        await chat_service.persist_published_events(session, chat=row, events=events)
        await session.commit()


async def _pinned(chat_id: str) -> str | None:
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat_id))
        assert row is not None
        pin = chat_service.chat_spec_of(row).model
        return pin.id if pin else None


async def _changes(chat_id: str) -> int:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(ChatMessage).where(
                ChatMessage.chat_id == UUID(chat_id), ChatMessage.kind == "model.changed"
            )
        )
        return len(list(rows.scalars()))


async def _model_relays(org_id: UUID) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(EventOutbox).where(
                    EventOutbox.org_id == org_id, EventOutbox.type == "doc.op"
                )
            )
        ).scalars()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
            for event in ((payload.get("envelope") or {}).get("payload") or {}).get("events") or []:
                if event.get("kind") == "model":
                    out.append(event)
        return out


async def _switch(client: AsyncClient, chat_id: str, model: str, **extra: Any) -> httpx.Response:
    return await client.put(f"/api/v1/chats/{chat_id}/model", json={"model": model, **extra})


async def test_a_chat_with_no_reasoning_yet_moves_anywhere(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])

    response = await _switch(client, chat["id"], GPT["id"])

    assert response.status_code == 200, response.text
    assert await _pinned(chat["id"]) == GPT["id"]


@pytest.mark.parametrize(
    ("ran", "target", "status", "code"),
    [
        pytest.param(
            "claude-opus-5.5",
            "gpt-5.5",
            409,
            "reasoning_not_readable",
            id="cross-wire-after-a-turn",
        ),
        pytest.param(
            "claude-opus-5.5",
            "claude-opus-5",
            409,
            "reasoning_not_readable",
            id="older-model-cannot-read",
        ),
        pytest.param("claude-opus-5.5", "claude-fable-5.1", 200, None, id="a-model-that-reads-it"),
        pytest.param("claude-opus-5", "claude-opus-5.5", 200, None, id="opus-5.5-reads-opus-5"),
        pytest.param(
            None, "gpt-5.5", 409, "reasoning_not_readable", id="an-unstamped-reply-ran-on-the-pin"
        ),
    ],
)
async def test_a_switch_is_judged_against_the_reasoning_the_history_carries(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    ran: str | None,
    target: str,
    status: int,
    code: str | None,
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, ran or OPUS_55["id"])
    await _ran(chat["id"], ran)

    response = await _switch(client, chat["id"], target)

    assert response.status_code == status, response.text
    if status == 409:
        error = response.json()["error"]
        assert error["code"] == code
        assert "Start a new chat to use" in error["message"]
        # Nothing moved: not the row, not the transcript, not the box.
        assert await _pinned(chat["id"]) == (ran or OPUS_55["id"])
        assert await _changes(chat["id"]) == 0
        assert await _model_relays(org_admin.org_id) == []
    else:
        assert await _pinned(chat["id"]) == target
        relays = await _model_relays(org_admin.org_id)
        assert relays and relays[-1]["ledger"] == [
            next(m["reasoning_format"] for m in CATALOG["data"] if m["id"] == ran)
        ]


async def test_the_refusal_names_the_model_whose_reasoning_blocks_and_the_way_out(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], OPUS_55["id"])

    error = (await _switch(client, chat["id"], GPT["id"])).json()["error"]

    assert error["message"] == (
        "This chat has reasoning from Claude Opus 5.5 that GPT-5.5 can't read. "
        "Start a new chat to use GPT-5.5."
    )
    assert error["details"]["blocking_formats"] == ["anthropic:claude-opus-5-5"]
    assert error["details"]["escape_new_chat_model"] == "gpt-5.5"


async def test_an_effort_change_always_goes_through(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], OPUS_55["id"])
    await _ran(chat["id"], "a-model-the-catalog-no-longer-has")

    response = await _switch(client, chat["id"], OPUS_55["id"], effort="low")

    assert response.status_code == 200, response.text
    assert response.json()["model"]["effort"] == "low"


async def test_a_subagents_turns_are_not_the_chats_history(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], GPT["id"], session_id="a-subagent-session")

    assert (await _switch(client, chat["id"], GPT["id"])).status_code == 200


async def test_a_model_the_catalog_lost_blocks_every_other_target(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """History the server cannot vouch for is never assumed readable."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, FABLE_51["id"])
    await _ran(chat["id"], "claude-retired")

    error = (await _switch(client, chat["id"], OPUS_55["id"])).json()["error"]
    assert error["code"] == "reasoning_not_readable"
    assert error["details"]["blocking_formats"] == ["model:claude-retired"]


@pytest.mark.parametrize(
    ("expected", "status"),
    [
        pytest.param("claude-opus-5.5", 200, id="the-model-it-is-on"),
        pytest.param("gpt-5.5", 409, id="a-model-someone-moved-it-off"),
    ],
)
async def test_expected_model_id_refuses_a_switch_raced_by_another(
    client: AsyncClient, org_admin: OrgWithAdmin, expected: str, status: int
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])

    response = await _switch(client, chat["id"], OPUS_5["id"], expected_model_id=expected)

    assert response.status_code == status, response.text
    if status == 409:
        assert response.json()["error"]["code"] == "model_changed"
        assert await _pinned(chat["id"]) == OPUS_55["id"]
        assert await _changes(chat["id"]) == 0


async def test_the_options_read_carries_every_verdict(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], OPUS_55["id"])

    body = (await client.get(f"/api/v1/chats/{chat['id']}/model-options")).json()

    assert body["current"] == {"model_id": "claude-opus-5.5", "effort": "high"}
    assert body["can_switch"] is True
    assert body["applies"] == "next_turn"
    states = {o["model"]["id"]: (o["state"], o["reason_code"]) for o in body["options"]}
    assert states == {
        "claude-opus-5.5": ("current", None),
        "claude-fable-5.1": ("available", None),
        "claude-opus-5": ("unavailable", "reasoning_not_readable"),
        "gpt-5.5": ("unavailable", "reasoning_not_readable"),
    }
    gpt = next(o for o in body["options"] if o["model"]["id"] == "gpt-5.5")
    assert gpt["escape_new_chat_model"] == "gpt-5.5"
    assert gpt["message"].startswith("This chat has reasoning from Claude Opus 5.5")


@pytest.mark.usefixtures("files_on")
@pytest.mark.parametrize(
    ("role", "can_switch", "put_status"),
    [
        pytest.param(ROLE_READER, False, 403, id="can-view-sees-why-and-may-not-switch"),
        pytest.param(ROLE_WRITER, True, 200, id="can-edit-may-switch"),
    ],
)
async def test_whoever_may_send_may_switch(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    role: str,
    can_switch: bool,
    put_status: int,
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=row, owner=owner, user=member, role=role)

    async with app_client() as other:
        await login(other, member.email, password)
        options = await other.get(f"/api/v1/chats/{chat['id']}/model-options")
        switched = await _switch(other, chat["id"], FABLE_51["id"])

    assert options.status_code == 200, options.text
    assert options.json()["can_switch"] is can_switch
    assert switched.status_code == put_status, switched.text
    if put_status == 200:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(ChatMessage).where(
                        ChatMessage.chat_id == UUID(chat["id"]), ChatMessage.kind == "model.changed"
                    )
                )
            ).scalars()
            [change] = [r.payload["payload"] for r in rows]
        assert change["decided_by_user_id"] == str(member.id)


async def test_an_unreachable_catalog_lists_nothing_and_never_fails_the_read(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])

    async def down(db: Any, user: Any, org_team_id: Any, **kwargs: Any) -> chat_catalog.Catalog:
        raise chat_catalog.CatalogUnavailableError("down")

    monkeypatch.setattr(chat_catalog, "fetch_catalog", down)
    response = await client.get(f"/api/v1/chats/{chat['id']}/model-options")

    assert response.status_code == 200
    assert response.json()["options"] == []
    assert response.json()["current"]["model_id"] == "claude-opus-5.5"


@pytest.mark.parametrize(
    ("capabilities", "applies"),
    [
        pytest.param(["model_switch_v1"], "next_turn", id="a-box-that-moves-a-running-agent"),
        pytest.param([], "after_reopen", id="a-box-that-names-nothing"),
        pytest.param(None, "after_reopen", id="an-older-box-that-sends-no-field"),
    ],
)
async def test_the_options_say_when_a_switch_applies_on_the_chats_box(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    capabilities: list[str] | None,
    applies: str,
) -> None:
    """The box says what its build can do on its heartbeat; the options read
    tells the reader whether a switch runs on the next turn or once the chat's
    agent restarts."""
    chat = await _chat_on_a_box(client, org_admin, real_session, capabilities)

    body = (await client.get(f"/api/v1/chats/{chat['id']}/model-options")).json()

    assert body["applies"] == applies


async def _chat_on_a_box(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    capabilities: list[str] | None,
) -> dict[str, Any]:
    """A chat placed on a registered box whose heartbeat names
    ``capabilities`` (``None``: a build that sends no such field)."""
    from alkera_core.authz import agent_headers
    from tests._compute_helpers import make_grant, make_machine_type
    from tests.conftest import mint_cli_token

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    registered = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": f"pod-{uuid4().hex[:6]}",
            "name": "demo-box",
            "machine_type_code": machine_type.provider_type_id,
        },
        headers={"Authorization": f"Bearer {token}", **agent_headers("booting")},
    )
    assert registered.status_code == 201, registered.text
    machine_id = str(registered.json()["id"])
    beat_body = {} if capabilities is None else {"capabilities": capabilities}
    beat = await client.post(
        f"/api/v1/machines/{machine_id}/heartbeat",
        json=beat_body,
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    )
    assert beat.status_code == 204, beat.text
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    assert chat["machine_id"] == machine_id
    return chat


#: Two plans: the smaller one lacks Fable 5.1 and GPT-5.5.
SMALL_PLAN: dict[str, Any] = {"object": "list", "data": [OPUS_55, OPUS_5]}
LARGE_PLAN: dict[str, Any] = CATALOG


def _plans(monkeypatch: pytest.MonkeyPatch, by_user: dict[UUID, dict[str, Any]]) -> None:
    """The gateway serves each user the models their plan entitles them to."""
    real = chat_catalog.fetch_catalog

    async def patched(db: Any, user: Any, org_team_id: Any, **kwargs: Any) -> chat_catalog.Catalog:
        body = by_user[user.id]
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        kwargs.setdefault("client", httpx.AsyncClient(transport=transport))
        return await real(db, user, org_team_id, **kwargs)

    monkeypatch.setattr(chat_catalog, "fetch_catalog", patched)


async def _shared_with_member(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> tuple[dict[str, Any], User, str]:
    """The admin's chat, shared Can edit with a member who may switch it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    owner = await real_session.get(User, org_admin.admin_id)
    row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    await share_chat(real_session, chat=row, owner=owner, user=member, role=ROLE_WRITER)
    return chat, member, password


@pytest.mark.usefixtures("files_on")
async def test_a_collaborator_on_a_larger_plan_cannot_move_the_chat_past_the_owners(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner pays for every turn, so the owner's plan is the list: a
    collaborator on a larger plan sees the owner's models, is told whose they
    are, and a switch to a model only their own plan has is refused with
    nothing written."""
    chat, member, password = await _shared_with_member(client, org_admin, real_session)
    _plans(monkeypatch, {org_admin.admin_id: SMALL_PLAN, member.id: LARGE_PLAN})

    async with app_client() as other:
        await login(other, member.email, password)
        options = await other.get(f"/api/v1/chats/{chat['id']}/model-options")
        refused = await _switch(other, chat["id"], FABLE_51["id"])
        allowed = await _switch(other, chat["id"], OPUS_5["id"])
    owners_view = await client.get(f"/api/v1/chats/{chat['id']}/model-options")

    assert options.status_code == 200, options.text
    assert [o["model"]["id"] for o in options.json()["options"]] == [OPUS_55["id"], OPUS_5["id"]]
    assert options.json()["billed_to_owner"] is True
    assert owners_view.json()["billed_to_owner"] is False
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "model_not_offered"
    assert allowed.status_code == 200, allowed.text
    assert await _pinned(chat["id"]) == OPUS_5["id"]
    assert await _changes(chat["id"]) == 1


@pytest.mark.usefixtures("files_on")
async def test_a_collaborator_on_a_smaller_plan_may_move_the_chat_to_the_owners_models(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: the collaborator's own plan lacks Fable 5.1, the
    owner's has it, and the owner pays, so the collaborator may move the
    owner's chat there."""
    chat, member, password = await _shared_with_member(client, org_admin, real_session)
    _plans(monkeypatch, {org_admin.admin_id: LARGE_PLAN, member.id: SMALL_PLAN})

    async with app_client() as other:
        await login(other, member.email, password)
        options = await other.get(f"/api/v1/chats/{chat['id']}/model-options")
        switched = await _switch(other, chat["id"], FABLE_51["id"])

    assert FABLE_51["id"] in [o["model"]["id"] for o in options.json()["options"]]
    assert switched.status_code == 200, switched.text
    assert await _pinned(chat["id"]) == FABLE_51["id"]


@pytest.mark.parametrize(
    "model_id",
    [pytest.param("claude-opus-5.5", id="stamped"), pytest.param(None, id="unstamped")],
)
async def test_a_reply_that_produced_no_reasoning_leaves_nothing_to_lose(
    client: AsyncClient, org_admin: OrgWithAdmin, model_id: str | None
) -> None:
    """A model run with thinking off (Haiku at effort None) answers with no
    reasoning block: the chat has no reasoning history and moves anywhere."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], model_id, reasoned=False)

    options = await client.get(f"/api/v1/chats/{chat['id']}/model-options")
    response = await _switch(client, chat["id"], GPT["id"])

    states = {o["model"]["id"]: o["state"] for o in options.json()["options"]}
    assert states[GPT["id"]] == "available"
    assert response.status_code == 200, response.text


async def test_one_turn_with_reasoning_is_enough_to_hold_it(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Thinking off on one turn does not erase what an earlier turn produced."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], OPUS_55["id"], reasoned=True)
    await _ran(chat["id"], OPUS_55["id"], reasoned=False)

    response = await _switch(client, chat["id"], GPT["id"])

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "reasoning_not_readable"


async def test_models_refused_for_one_reason_share_one_line(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Every model the chat's reasoning rules out carries the same group line,
    which names the source but not the model, so a picker lists them under it
    once; each still has its own sentence."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _ran(chat["id"], OPUS_55["id"])

    options = (await client.get(f"/api/v1/chats/{chat['id']}/model-options")).json()["options"]

    refused = [o for o in options if o["state"] == "unavailable"]
    assert {o["model"]["id"] for o in refused} == {OPUS_5["id"], GPT["id"]}
    assert {o["group_message"] for o in refused} == {
        "This chat has reasoning from Claude Opus 5.5 that these models can't read. "
        "Use them in a new chat."
    }
    assert len({o["message"] for o in refused}) == 2
    assert all(o["group_message"] is None for o in options if o["state"] != "unavailable")


@pytest.mark.parametrize("effort", ["ultra", "none", ""])
async def test_an_effort_the_model_does_not_offer_is_refused_and_stores_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, effort: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])

    response = await _switch(client, chat["id"], OPUS_5["id"], effort=effort)
    options = await client.get(f"/api/v1/chats/{chat['id']}/model-options")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "effort_not_offered"
    assert await _pinned(chat["id"]) == OPUS_55["id"]
    assert options.json()["current"]["effort"] == "high"
    assert await _changes(chat["id"]) == 0


async def _turn_state(chat_id: str, state: str) -> None:
    """The chat's document says a turn is ``state`` (``working`` / ``idle``)."""
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        doc = await session.get(RealtimeDoc, (chat.org_team_id, "chat", chat_id))
        if doc is None:
            session.add(
                RealtimeDoc(
                    org_id=chat.org_team_id, doc_type="chat", doc_id=chat_id, turn_state=state
                )
            )
        else:
            doc.turn_state = state
        await session.commit()


@pytest.mark.parametrize(
    ("state", "status"),
    [
        pytest.param("working", 409, id="a-turn-still-working-holds-its-reasoning"),
        pytest.param("idle", 200, id="an-idle-chat-with-no-reasoning-moves"),
    ],
)
async def test_a_switch_made_mid_turn_counts_the_reasoning_the_turn_is_writing(
    client: AsyncClient, org_admin: OrgWithAdmin, state: str, status: int
) -> None:
    """The running turn has not published its reasoning yet, so the ledger the
    transcript gives is empty; judged on that alone, the switch would let the
    next turn drop what this one writes. The pin's format counts while the
    document says a turn is working, in the switch and in the options alike."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    await _turn_state(chat["id"], state)

    options = (await client.get(f"/api/v1/chats/{chat['id']}/model-options")).json()
    response = await _switch(client, chat["id"], GPT["id"])

    assert response.status_code == status, response.text
    gpt = next(o for o in options["options"] if o["model"]["id"] == GPT["id"])
    if status == 409:
        assert response.json()["error"]["code"] == "reasoning_not_readable"
        assert response.json()["error"]["details"]["blocking_formats"] == [
            OPUS_55["reasoning_format"]
        ]
        assert gpt["state"] == "unavailable"
        assert await _pinned(chat["id"]) == OPUS_55["id"]
    else:
        assert gpt["state"] == "available"


async def test_reasoning_no_model_can_be_named_for_blocks_every_move(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A chat with no pin (one started before chats were pinned) whose history
    holds a reasoning reply nobody stamped: the server cannot say which model
    wrote it, so no model is vouched to read it, and the refusal says so in
    words."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat["id"]))
        assert row is not None
        row.spec = {k: v for k, v in (row.spec or {}).items() if k != "model"}
        await session.commit()
    assert await _pinned(chat["id"]) is None
    await _ran(chat["id"], None)

    options = (await client.get(f"/api/v1/chats/{chat['id']}/model-options")).json()
    response = await _switch(client, chat["id"], GPT["id"])

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "reasoning_not_readable"
    assert error["message"] == (
        "This chat has reasoning from an earlier model that GPT-5.5 can't read. "
        "Start a new chat to use GPT-5.5."
    )
    assert {o["state"] for o in options["options"]} == {"unavailable"}


@pytest.mark.parametrize(
    ("capabilities", "reasoned", "target", "status"),
    [
        pytest.param([], True, FABLE_51["id"], 409, id="an-older-box-keeps-a-reasoned-chat"),
        pytest.param(None, True, FABLE_51["id"], 409, id="a-box-that-sends-no-field-too"),
        pytest.param([], True, OPUS_55["id"], 200, id="an-effort-change-on-it-still-goes"),
        pytest.param([], False, GPT["id"], 200, id="an-older-box-moves-a-chat-with-none"),
        pytest.param(
            ["model_switch_v1"], True, FABLE_51["id"], 200, id="a-box-that-replays-moves-it"
        ),
    ],
)
async def test_a_box_that_reopens_the_agent_to_switch_keeps_a_chat_with_reasoning(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    capabilities: list[str] | None,
    reasoned: bool,
    target: str,
    status: int,
) -> None:
    """An older box applies a switch by reopening the agent, which carries no
    reasoning across models: a move the rule calls available would still drop
    the chat's reasoning there, so it is refused while the box holds the chat.
    An effort change on the same model, or a chat with no reasoning yet, is
    not."""
    chat = await _chat_on_a_box(client, org_admin, real_session, capabilities)
    if reasoned:
        await _ran(chat["id"], OPUS_55["id"])

    options = (await client.get(f"/api/v1/chats/{chat['id']}/model-options")).json()
    extra = {"effort": "low"} if target == OPUS_55["id"] else {}
    response = await _switch(client, chat["id"], target, **extra)

    assert response.status_code == status, response.text
    state = next(o["state"] for o in options["options"] if o["model"]["id"] == target)
    if status == 409:
        assert response.json()["error"]["code"] == "reasoning_not_readable"
        assert state == "unavailable"
    else:
        assert state in ("available", "current")


async def _leave_org(user_id: UUID, org_id: UUID) -> None:
    async with AsyncSessionLocal() as session:
        membership = await session.scalar(
            select(OrgMembership).where(
                OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_id
            )
        )
        assert membership is not None
        membership.status = MembershipStatus.DEACTIVATED
        await session.commit()


@pytest.mark.usefixtures("files_on")
async def test_a_chat_whose_owner_left_the_org_lists_no_models_and_switches_nowhere(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    """The owner pays, and an owner gone from the org has no plan there to
    vouch for a model: the options read is an empty list (never a failure),
    and a switch is refused, at the send gate for a chat nobody may drive on
    their money now, and by the planner itself when asked directly."""
    chat, member, password = await _shared_with_member(client, org_admin, real_session)
    await _leave_org(org_admin.admin_id, org_admin.org_id)

    async with app_client() as other:
        await login(other, member.email, password)
        options = await other.get(f"/api/v1/chats/{chat['id']}/model-options")
        refused = await _switch(other, chat["id"], OPUS_5["id"])

    assert options.status_code == 200, options.text
    assert options.json()["options"] == []
    assert options.json()["billed_to_owner"] is True
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "payer_lost_access"
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, UUID(chat["id"]))
        switcher = await session.get(User, member.id)
        assert row is not None and switcher is not None
        with pytest.raises(model_switch.ModelSwitchRefusedError) as planned:
            await model_switch.plan_switch(
                session, row, switcher, model_id=OPUS_5["id"], effort=None
            )
    assert (planned.value.status, planned.value.code) == (422, "model_not_offered")


async def test_a_switch_and_a_refusal_each_leave_a_structured_log(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _chat(client, OPUS_55["id"])
    with capture_logs() as applied:
        assert (await _switch(client, chat["id"], FABLE_51["id"])).status_code == 200
    await _ran(chat["id"], FABLE_51["id"])
    with capture_logs() as refused:
        assert (await _switch(client, chat["id"], GPT["id"])).status_code == 409

    moved = [e for e in applied if e["event"] == "chat.model_switch.applied"]
    assert [(e["chat_id"], e["model"], e["previous_model"], e["via"]) for e in moved] == [
        (chat["id"], FABLE_51["id"], OPUS_55["id"], "web")
    ]
    kept = [e for e in refused if e["event"] == "chat.model_switch.refused"]
    assert [(e["chat_id"], e["model"], e["status"], e["code"]) for e in kept] == [
        (chat["id"], GPT["id"], 409, "reasoning_not_readable")
    ]
    assert kept[0]["switcher_id"] == str(org_admin.admin_id)
