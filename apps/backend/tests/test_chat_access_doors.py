"""Every door that makes the node DO something in a chat takes the send rung.

A message is not the only way to drive a chat's agent: answering an ask,
stopping a turn and switching the permission stance all change what the agent
does next, and each is relayed to the node. They are gated by the same
``SEND`` decision a message takes (``chat.access``), so a viewer or a commenter
who may watch the conversation cannot make the node act, an unshared member is
told the chat does not exist — byte-identical to the answer for an id nobody
holds — and an editor passes every one of them. Each refusal is on record as
an ``authz.decision`` row that survives the refused request's rollback.

``test_chat_send_rung.py`` pins the message door; this file pins the rest, so
a door added or loosened alone fails here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_COMMENTER, ROLE_READER, ROLE_WRITER
from alkera_core.models import EventOutbox, User, WorkspaceObject
from httpx import AsyncClient
from sqlalchemy import select
from tests.chat_shares import files_on, share_chat  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


def _answer(client: AsyncClient, chat_id: str) -> Any:
    return client.post(
        f"/api/v1/chats/{chat_id}/answer",
        json={"interrupt_id": "ask-nobody-raised", "option_id": "allow"},
    )


def _stop(client: AsyncClient, chat_id: str) -> Any:
    return client.post(f"/api/v1/chats/{chat_id}/stop")


def _mode(client: AsyncClient, chat_id: str) -> Any:
    return client.put(f"/api/v1/chats/{chat_id}/permission-mode", json={"mode": "plan"})


#: Each door, and what an EDITOR gets past the gate: an answer to an ask nobody
#: raised is the ask lookup's own 404 (the gate was passed, the ask was not
#: found); a stop and a mode switch land.
DOORS = [
    pytest.param(_answer, 404, "ask_not_found", id="answer"),
    pytest.param(_stop, 202, None, id="stop"),
    pytest.param(_mode, 200, None, id="permission-mode"),
]


def _without_trace(body: dict[str, Any]) -> dict[str, Any]:
    """The error body minus the per-request trace id, the one field that may
    legitimately differ between two answers that must otherwise be the same."""
    error = dict(body["error"])
    error.pop("trace_id", None)
    return {**body, "error": error}


async def _send_decisions(org_id: UUID, chat_id: UUID) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(EventOutbox)
                .where(
                    EventOutbox.org_id == org_id,
                    EventOutbox.type == "authz.decision",
                    EventOutbox.entity_id == str(chat_id),
                )
                .order_by(EventOutbox.id)
            )
        ).scalars()
    return [
        (row.payload["effect"], row.payload["reason"])
        for row in rows
        if row.payload["action"] == "send"
    ]


async def _chat_shared_at(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any, role: str | None
) -> tuple[dict[str, Any], AsyncClient]:
    """The admin's chat and a logged-in member holding ``role`` on it
    (``None``: no share at all)."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await client.post("/api/v1/chats", json={"title": "Quarterly review"})
    assert created.status_code == 201, created.text
    chat: dict[str, Any] = created.json()
    owner = await real_session.get(User, org_admin.admin_id)
    row = await real_session.get(WorkspaceObject, UUID(chat["id"]))
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert password is not None
    if role is not None:
        await share_chat(real_session, chat=row, owner=owner, user=member, role=role)
    other = app_client()
    await login(other, member.email, password)
    return chat, other


@pytest.mark.parametrize(("door", "passed_status", "passed_code"), DOORS)
@pytest.mark.parametrize(
    "role",
    [
        pytest.param(ROLE_READER, id="can-view"),
        pytest.param(ROLE_COMMENTER, id="can-comment"),
    ],
)
async def test_a_viewer_reads_the_chat_and_cannot_make_the_node_act(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    role: str,
    door: Any,
    passed_status: int,
    passed_code: str | None,
) -> None:
    del passed_status, passed_code
    chat, viewer = await _chat_shared_at(client, org_admin, real_session, role)
    async with viewer:
        assert (await viewer.get(f"/api/v1/chats/{chat['id']}")).status_code == 200
        response = await door(viewer, chat["id"])
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert (error["code"], error["message"]) == (
        chat_policy.SEND_DENIED_CODE,
        chat_policy.SEND_DENIED_MESSAGE,
    )
    assert await _send_decisions(org_admin.org_id, UUID(chat["id"])) == [
        ("deny", "send_rung_required")
    ]
    # Nothing changed: the stance a viewer asked for did not land.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"/api/v1/chats/{chat['id']}")).json()["permission_mode"] == chat[
        "permission_mode"
    ]


@pytest.mark.parametrize(("door", "passed_status", "passed_code"), DOORS)
async def test_an_editor_passes_every_door(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    door: Any,
    passed_status: int,
    passed_code: str | None,
) -> None:
    chat, editor = await _chat_shared_at(client, org_admin, real_session, ROLE_WRITER)
    async with editor:
        response = await door(editor, chat["id"])
    assert response.status_code == passed_status, response.text
    if passed_code is not None:
        assert response.json()["error"]["code"] == passed_code
    # The allow is filed in the request's own transaction, so it lands with a
    # door that landed and rolls back with the ask lookup's 404 — which is
    # itself the proof the gate was passed: a refused editor never reaches it.
    assert await _send_decisions(org_admin.org_id, UUID(chat["id"])) == (
        [] if passed_status >= 400 else [("allow", "writer_may_send")]
    )


@pytest.mark.parametrize(("door", "passed_status", "passed_code"), DOORS)
async def test_an_unshared_member_gets_the_answer_a_guessed_id_gets(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: Any,
    door: Any,
    passed_status: int,
    passed_code: str | None,
) -> None:
    """A chat nobody shared with the caller and an id nobody holds are the same
    404 with the same body, on every door: neither the read nor any action
    confirms that a chat exists."""
    del passed_status, passed_code
    chat, member = await _chat_shared_at(client, org_admin, real_session, None)
    async with member:
        unshared = await door(member, chat["id"])
        guessed = await door(member, str(uuid4()))
    assert unshared.status_code == guessed.status_code == 404
    assert _without_trace(unshared.json()) == _without_trace(guessed.json())
    assert unshared.json()["error"]["message"] == chat_policy.NOT_FOUND
    assert await _send_decisions(org_admin.org_id, UUID(chat["id"])) == [
        ("deny", "not_in_audience")
    ]
