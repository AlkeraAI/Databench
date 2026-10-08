"""The objects routes a box calls, on its own machine credential.

A box reads the result a promotion out of one of its chats created, delivers
the payload behind it and says when the payload is not coming — exactly the
three doors its mirror knocks on — and only for objects whose source chat is
bound to its machine, in an org its credential serves. A result of a chat
another box runs, a result in an org it does not serve, and every other
objects door answer as they would a stranger. Each case pins the status
contract AND the ``authz.decision`` row the route left behind, chain and all.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alkera_core.auth.machine_token import MACHINE_TOKEN_PREFIX
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import EventOutbox, User
from alkera_core.schemas.objects import ResultBlobEnvelope
from backend.services.org import teams as team_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login, make_member
from tests.test_machine_principal_routes import _box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

NOT_FOUND = "Object not found"


class Box:
    def __init__(self, raw: str, credential_id: UUID, machine_id: str) -> None:
        self.raw = raw
        self.credential_id = credential_id
        self.machine_id = machine_id
        self.client = AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {raw}"},
        )


class Served:
    """A fresh org, a verified member with a browser, and the box's chat there."""

    def __init__(self, org_id: UUID, owner: User, browser: AsyncClient) -> None:
        self.org_id = org_id
        self.owner = owner
        self.browser = browser


async def _served_org() -> Served:
    tag = secrets.token_hex(4)
    async with AsyncSessionLocal() as session:
        org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Served Org {tag}",
            admin_email=f"served-{tag}@alkera.dev",
            admin_first_name="Served",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
        member, password = await make_member(session, org_id=org.id, verified=True)
    assert password is not None
    return Served(org.id, member, await login(app_client(), member.email, password))


@pytest_asyncio.fixture
async def box(org_admin: OrgWithAdmin) -> AsyncIterator[tuple[Box, Served]]:
    served = await _served_org()
    made = await _box(org_admin, tenancy="dedicated", served_org=served.org_id)
    assert made.raw.startswith(MACHINE_TOKEN_PREFIX)
    box = Box(made.raw, made.credential_id, str(made.machine_id))
    yield box, served
    await box.client.aclose()
    await served.browser.aclose()


async def _chat(browser: AsyncClient, *, machine_id: str | None) -> str:
    """A chat of the served org, bound to ``machine_id`` — or to nothing.
    Placement binds a new chat to the org's ready box at creation, so the
    binding is written explicitly either way."""
    from sqlalchemy import text

    made = await browser.post("/api/v1/chats", json={"title": "Ops"})
    assert made.status_code == 201, made.text
    chat_id = str(made.json()["id"])
    async with AsyncSessionLocal() as session:
        if machine_id is None:
            await session.execute(
                text("UPDATE workspace_objects SET spec = spec - 'machine_id' WHERE id = :id"),
                {"id": UUID(chat_id)},
            )
        else:
            await session.execute(
                text(
                    "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                    "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                ),
                {"id": UUID(chat_id), "machine": machine_id},
            )
        await session.commit()
    return chat_id


async def _promoted(browser: AsyncClient, chat_id: str) -> dict[str, Any]:
    """A result promoted out of ``chat_id`` by its owner, waiting for its
    payload — the shape the box's mirror is asked to fill."""
    response = await browser.post(
        f"/api/v1/chats/{chat_id}/promote", json={"event_id": f"ev-{uuid4().hex[:6]}", "title": "R"}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _envelope(count: int) -> dict[str, Any]:
    return ResultBlobEnvelope(
        kind="rows",
        columns=["day", "n"],
        rows=[[f"2026-09-{index + 1:02d}", index] for index in range(count)],
        total=count,
    ).model_dump(mode="json")


def _receipt() -> dict[str, Any]:
    return {
        "sql": "SELECT day, n FROM prompts",
        "connection_name": "Tideline Postgres",
        "role": "analytics_readonly",
        "engine": "postgres",
        "executed_at": "2026-09-07T12:00:00+00:00",
        "row_count": 2,
        "duration_ms": 41,
        "params": {},
        "definitions_referenced": [],
    }


async def _decisions(org_id: UUID, object_id: str) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.AUTHZ_DECISION.value,
                EventOutbox.entity == "workspace_object",
                EventOutbox.entity_id == object_id,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str, str]]:
    return [(r.payload["action"], r.payload["effect"], r.payload["reason"]) for r in rows]


def _by_machine(rows: list[EventOutbox], machine_id: str) -> list[EventOutbox]:
    """The rows the box left, apart from the owner's own reads of the result."""
    return [row for row in rows if _chain(row) == [("machine", machine_id)]]


def _chain(row: EventOutbox) -> list[tuple[str, str]]:
    return [(link["kind"], link["id"]) for link in row.actor["chain"]]


async def test_a_box_reads_fills_and_fails_the_results_of_its_own_chats(
    box: tuple[Box, Served], real_session: AsyncSession
) -> None:
    """The three doors the mirror uses, on the credential: the read of a result
    promoted out of a chat bound to the box, the payload delivery that makes it
    ready — receipted under the machine's own chain — and, on a second result,
    the refusal that marks it failed. Every allow is on record under the
    machine's chain with the box's reason."""
    the_box, served = box
    chat_id = await _chat(served.browser, machine_id=the_box.machine_id)
    filled = await _promoted(served.browser, chat_id)
    abandoned = await _promoted(served.browser, chat_id)

    read = await the_box.client.get(f"/api/v1/objects/{filled['id']}")
    assert read.status_code == 200, read.text
    assert read.json()["status"] == "pending_upload"

    delivered = await the_box.client.post(
        f"/api/v1/objects/{filled['id']}/payload",
        json={"envelope": _envelope(2), "receipt": _receipt()},
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["status"] == "ready"
    uploaded_by = delivered.json()["spec"]["receipt"]["principal_chain"]["uploaded_by"]
    assert [(link["kind"], link["id"]) for link in uploaded_by["chain"]] == [
        ("machine", the_box.machine_id)
    ]
    rows = await served.browser.get(f"/api/v1/objects/{filled['id']}/rows")
    assert rows.status_code == 200, rows.text
    assert rows.json()["total"] == 2

    given_up = await the_box.client.post(
        f"/api/v1/objects/{abandoned['id']}/payload/failed",
        json={"reason": "the warehouse refused the query"},
    )
    assert given_up.status_code == 200, given_up.text
    assert given_up.json()["status"] == "failed"

    for object_id, actions in (
        (filled["id"], {"read", "upload_payload"}),
        (abandoned["id"], {"upload_payload"}),
    ):
        decided = _by_machine(await _decisions(served.org_id, object_id), the_box.machine_id)
        assert {row.payload["action"] for row in decided} >= actions
        assert {(row.payload["effect"], row.payload["reason"]) for row in decided} == {
            ("allow", "machine_holds_source_chat")
        }
        for row in decided:
            assert _chain(row) == [("machine", the_box.machine_id)]
            assert row.actor["delegating_user"] is None


async def test_a_result_of_a_chat_another_box_runs_is_not_there_for_the_box(
    box: tuple[Box, Served],
) -> None:
    """Same org, same drive of results — another machine's chat. Read, delivery
    and refusal are all the opaque not-found, each on record under the box's
    chain with the reason that names the binding."""
    the_box, served = box
    chat_id = await _chat(served.browser, machine_id=str(uuid4()))
    result = await _promoted(served.browser, chat_id)
    for method, path, body in (
        ("GET", f"/api/v1/objects/{result['id']}", None),
        (
            "POST",
            f"/api/v1/objects/{result['id']}/payload",
            {"envelope": _envelope(1), "receipt": _receipt()},
        ),
        ("POST", f"/api/v1/objects/{result['id']}/payload/failed", {"reason": "no"}),
    ):
        answer = await the_box.client.request(method, path, json=body)
        assert answer.status_code == 404, (path, answer.text)
        assert answer.json()["error"]["message"] == NOT_FOUND
    fresh = await served.browser.get(f"/api/v1/objects/{result['id']}")
    assert fresh.json()["status"] == "pending_upload", "nothing moved"
    decided = _by_machine(await _decisions(served.org_id, result["id"]), the_box.machine_id)
    assert _effects(decided) == [
        ("read", "deny", "machine_does_not_hold_object"),
        ("upload_payload", "deny", "machine_does_not_hold_object"),
        ("upload_payload", "deny", "machine_does_not_hold_object"),
    ]


async def test_a_chat_unbound_or_a_result_with_no_chat_is_not_the_boxs(
    box: tuple[Box, Served],
) -> None:
    """A chat no box has taken, and a result the owner created by hand (no
    source chat at all), are nobody's for a box."""
    the_box, served = box
    untaken = await _promoted(served.browser, await _chat(served.browser, machine_id=None))
    handmade = await served.browser.post(
        "/api/v1/objects",
        json={
            "type": "result",
            "title": "By hand",
            "spec": {"columns": [{"name": "day"}]},
            "client_id": f"o-{uuid4().hex[:8]}",
        },
    )
    assert handmade.status_code == 201, handmade.text
    for result in (untaken, handmade.json()):
        answer = await the_box.client.get(f"/api/v1/objects/{result['id']}")
        assert answer.status_code == 404, answer.text
        assert _effects(await _decisions(served.org_id, result["id"]))[-1] == (
            "read",
            "deny",
            "machine_does_not_hold_object",
        )


async def test_a_result_of_the_boxs_chat_in_an_org_it_does_not_serve_is_not_there(
    box: tuple[Box, Served],
) -> None:
    """The engine's tenancy floor comes first: a chat bound to this very
    machine in an org the credential does not serve is refused as cross-org,
    recorded under THAT org."""
    the_box, _served = box
    stranger = await _served_org()
    try:
        chat_id = await _chat(stranger.browser, machine_id=the_box.machine_id)
        result = await _promoted(stranger.browser, chat_id)
        answer = await the_box.client.get(f"/api/v1/objects/{result['id']}")
        assert answer.status_code == 404, answer.text
        assert _effects(await _decisions(stranger.org_id, result["id"])) == [
            ("read", "deny", "cross_org")
        ]
    finally:
        await stranger.browser.aclose()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/api/v1/objects", None, id="list"),
        pytest.param(
            "POST",
            "/api/v1/objects",
            {"type": "result", "title": "x", "spec": {}, "client_id": "c1"},
            id="create",
        ),
        pytest.param("PUT", "/api/v1/objects/{object}", {"expected_version": 1}, id="edit"),
        pytest.param("GET", "/api/v1/objects/{object}/rows", None, id="rows"),
        pytest.param("GET", "/api/v1/objects/{object}/export.csv", None, id="export"),
        pytest.param("POST", "/api/v1/objects/{object}/promote/retry", None, id="retry"),
    ],
)
async def test_every_other_objects_door_wants_a_person(
    box: tuple[Box, Served], method: str, path: str, body: Any
) -> None:
    """The box's reach is the three doors and nothing beside them: on its own
    result, every other verb is refused as a credential that is no session."""
    the_box, served = box
    chat_id = await _chat(served.browser, machine_id=the_box.machine_id)
    result = await _promoted(served.browser, chat_id)
    answer = await the_box.client.request(method, path.format(object=result["id"]), json=body)
    assert answer.status_code == 401, (path, answer.text)
    assert await _decisions(served.org_id, result["id"]) == []
