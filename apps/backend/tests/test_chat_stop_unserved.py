"""A Stop on a chat no box holds ends the turn on the server.

The word ``working`` on a chat's document is written by the box running the
turn, and a reader's Stop is a relay that box answers by ending the turn and
writing ``idle``. When no box holds the chat — its machine gone quiet, asleep
or off the plane, the box having put the chat to sleep, or the box having
refused to publish it — the relay reaches no process, and the document went on
saying ``working``: every listing owed a turn, the composer offered Stop and
nothing else, and nothing changed until some box took the chat again.

Now the stop route ends the turn itself in exactly that case, the way the box
would have: the same ``set_meta`` a box sends, persisted to the document's own
columns and carried as the same frame on the chat's channel. Driven through
the real route with a real Postgres behind it; the negative case pins that a
chat whose box is answering is left to that box.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import EventType
from alkera_core.models import RealtimeDoc, WorkspaceObject
from alkera_core.models.compute import ComputeAllocation
from backend.services.realtime.docsync import SERVER_PEER_ID
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin, login
from tests.test_chat_machine_binding_seam import _browser, _daemon_headers, _heartbeat, _register
from tests.test_chats_api import _create_chat, _events

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


@pytest.fixture(autouse=True)
def _hold_the_heartbeat_window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A beating box stays ``ready`` for the length of the case; the case
    about a box going quiet ages its heartbeat past this window explicitly."""
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


async def _doc_says_working(chat_id: str) -> None:
    """The document as a box leaves it mid-turn: the word in the state's meta
    and on the row's own columns, which is what a real turn change writes."""
    at = datetime.now(UTC).isoformat()
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        doc = await session.get(RealtimeDoc, (chat.org_team_id, "chat", chat_id))
        if doc is None:
            doc = RealtimeDoc(org_id=chat.org_team_id, doc_type="chat", doc_id=chat_id, state={})
            session.add(doc)
        meta = dict(doc.state.get("meta") or {})
        meta["turn_state"] = {"state": "working", "at": at}
        meta["turn_state_at"] = at
        doc.state = {**doc.state, "meta": meta}
        doc.turn_state = "working"
        doc.turn_state_at = at
        await session.commit()


async def _doc(chat_id: str) -> RealtimeDoc:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, UUID(chat_id))
        assert chat is not None
        doc = await session.get(RealtimeDoc, (chat.org_team_id, "chat", chat_id))
        assert doc is not None
        return doc


async def _turn_stamps(org_id: UUID, chat_id: str) -> list[dict[str, Any]]:
    """Every ``set_meta`` op carried on the chat's channel that names a turn
    state: ``(peer, seq, state)`` per frame, in order."""
    out: list[dict[str, Any]] = []
    for row in await _events(org_id, EventType.DOC_OP):
        if row.entity_id != f"doc:chat:{chat_id}":
            continue
        envelope = row.payload["envelope"]
        payload = envelope["payload"]
        if payload.get("intent") != "set_meta":
            continue
        turn = (payload.get("meta") or {}).get("turn_state")
        if isinstance(turn, dict):
            out.append(
                {"peer": envelope["peer_id"], "seq": envelope["seq"], "state": turn["state"]}
            )
    return out


async def _age_heartbeat(machine_id: str) -> None:
    async with AsyncSessionLocal() as session:
        alloc = await session.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        alloc.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=20)
        await session.commit()


async def _publisher_state(
    org: OrgWithAdmin, machine_id: str, chat_id: str, state: str, *, kind: str | None = None
) -> None:
    async with _browser() as daemon:
        reported = await daemon.put(
            f"/api/v1/chats/{chat_id}/publisher-state",
            json={
                "state": state,
                **({"reason": "the gateway said no"} if state == "refused" else {}),
                **({"refusal_kind": kind} if kind is not None else {}),
            },
            headers=await _daemon_headers(org, agent_id=machine_id),
        )
    assert reported.status_code == 200, reported.text


async def _chat_on_a_box(
    session: AsyncSession, org: OrgWithAdmin, client: AsyncClient, *, pod: str
) -> tuple[dict[str, Any], str]:
    machine_type = await make_machine_type(session)
    await make_grant(session, org_team_id=org.org_id, machine_type_id=machine_type.id)
    machine_id = await _register(org, machine_type.provider_type_id, pod)
    await _heartbeat(org, machine_id)
    chat = await _create_chat(client, title="stopped from afar")
    assert chat["machine_id"] == machine_id
    return chat, machine_id


async def _asked_and_working(client: AsyncClient, chat: dict[str, Any]) -> None:
    """The chat's state at the moment of the Stop: a message asked, the
    document saying a turn is working, and every read owing a turn."""
    sent = await client.post(
        f"/api/v1/chats/{chat['id']}/messages", json={"text": "go on", "client_id": "s-1"}
    )
    assert sent.status_code == 201, sent.text
    await _doc_says_working(chat["id"])
    before = await client.get(f"/api/v1/chats/{chat['id']}")
    assert before.json()["pending_turn"] is True


@pytest.mark.parametrize(
    "why",
    [
        pytest.param("no-machine", id="bound-to-nothing"),
        pytest.param("stopped-beating", id="box-gone-quiet"),
        pytest.param("asleep", id="box-put-the-chat-to-sleep"),
        pytest.param("refused", id="box-cannot-publish"),
    ],
)
async def test_a_stop_on_a_chat_no_box_holds_ends_the_turn(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin, why: str
) -> None:
    """Whatever left the chat without a box, the reader's Stop returns the
    composer: the document says ``idle`` on its columns and in its state, the
    read no longer owes a turn, and the frame every open viewer folds is the
    same ``set_meta`` the columns were written from, under the server's own
    peer id at the sequence the write took."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    if why == "no-machine":
        chat = await _create_chat(client, title="stopped from afar")
        assert chat["machine_id"] is None
        await _asked_and_working(client, chat)
    else:
        # The message goes first, while the box answers: a send to a box that
        # has gone quiet is refused outright, so the turn a Stop finds hanging
        # is one that began on a box which then stopped, slept or refused.
        chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod=f"pod-{why}")
        await _asked_and_working(client, chat)
        if why == "stopped-beating":
            await _age_heartbeat(machine_id)
        else:
            # A refusal of a final kind: a wait leaves the turn running.
            kind = "not_allowed" if why == "refused" else None
            await _publisher_state(org_admin, machine_id, chat["id"], why, kind=kind)
        if why == "refused":
            # A box that finally refuses the chat has its turn ended by the
            # server at once (test_chat_status_read); the Stop then finds
            # nothing to end.
            assert (await _doc(chat["id"])).turn_state == "idle"
            stopped = await client.post(f"/api/v1/chats/{chat['id']}/stop")
            assert stopped.status_code == 202, stopped.text
            stamps = await _turn_stamps(org_admin.org_id, chat["id"])
            assert [stamp["state"] for stamp in stamps] == ["idle"], "ended once, by the refusal"
            return
        assert (await client.get(f"/api/v1/chats/{chat['id']}")).json()["pending_turn"] is True

    stopped = await client.post(f"/api/v1/chats/{chat['id']}/stop")
    assert stopped.status_code == 202, stopped.text

    after = await client.get(f"/api/v1/chats/{chat['id']}")
    assert after.status_code == 200, after.text
    assert after.json()["pending_turn"] is False, "the composer comes back"
    doc = await _doc(chat["id"])
    assert doc.turn_state == "idle"
    assert doc.state["meta"]["turn_state"]["state"] == "idle"
    stamps = await _turn_stamps(org_admin.org_id, chat["id"])
    assert stamps == [{"peer": SERVER_PEER_ID, "seq": doc.seq, "state": "idle"}], (
        "what was persisted is what was broadcast, once"
    )


async def test_a_stop_on_a_chat_whose_box_is_answering_leaves_the_turn_to_the_box(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: a box that is beating and publishing the chat
    hears the relay and ends the turn itself. The server writes nothing to
    the document — a second writer racing the box over the turn's state is
    exactly what the box-owns-the-word rule exists to rule out."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat, machine_id = await _chat_on_a_box(real_session, org_admin, client, pod="pod-awake")
    await _publisher_state(org_admin, machine_id, chat["id"], "publishing")
    await _asked_and_working(client, chat)

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    doc = await _doc(chat["id"])
    assert doc.turn_state == "working", "the box's word stands until the box takes it back"
    assert await _turn_stamps(org_admin.org_id, chat["id"]) == []
    after = await client.get(f"/api/v1/chats/{chat['id']}")
    assert after.json()["pending_turn"] is True


async def test_a_stop_on_a_chat_whose_document_is_already_idle_writes_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """There is no turn to end: the document is left alone and no frame goes
    out, so a Stop pressed twice, or pressed on a chat that finished on its
    own, does not restamp a turn that is not running."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = await _create_chat(client, title="already quiet")
    await _asked_and_working(client, chat)
    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202
    first = await _doc(chat["id"])

    assert (await client.post(f"/api/v1/chats/{chat['id']}/stop")).status_code == 202

    again = await _doc(chat["id"])
    assert (again.seq, again.turn_state) == (first.seq, "idle")
    assert len(await _turn_stamps(org_admin.org_id, chat["id"])) == 1
