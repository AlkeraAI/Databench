"""One person, two orgs: chats and compute answer in the credential's org.

The subject is ``two_org_identity``: a person (U) whose home is org A and who
also holds a membership in org B. Every case calls the real route with U's B
credential (``token_b``) and org A's objects, and pins the same rule: what a
request reads, writes and creates is in the org its credential names, never
"the person's org". The A objects are U's own wherever that is possible, so a
route that decided by person rather than by org would hand them back.

Each refusal case has its positive control on ``token_a`` (the same person,
the same object, the other credential), so a 404 here is the org deciding and
not a broken fixture. Multi-org is on for every case (``multi_org``): with it
off, a B credential is refused at the door before any route runs.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    ChatMessage,
    ComputeAllocation,
    TeamMembership,
    TeamRole,
    WorkspaceObject,
)
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.objects import chat_spares
from backend.services.chats import chat_service
from backend.services.chats import duplicate as duplicate_service
from backend.services.chats import templates as chat_template_service
from backend.services.compute import service as compute_service
from httpx import Response
from sqlalchemy import func, select, update
from tests._compute_helpers import make_grant, make_machine_type
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import TwoOrg, app_client

# Files on: every chat here is made with its drive node, in whichever org it
# lands, so a person's second org builds its own owner ACL beside the first.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.compute_rows,
    pytest.mark.usefixtures("multi_org", "files_on"),
]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(t: TwoOrg, which: str) -> str:
    return t.token_a if which == "a" else t.token_b


def _org(t: TwoOrg, which: str) -> UUID:
    return t.org_a if which == "a" else t.org_b


async def _call(method: str, path: str, token: str, **kwargs: Any) -> Response:
    headers = {**_bearer(token), **kwargs.pop("headers", {})}
    async with app_client() as client:
        return await client.request(method, path, headers=headers, **kwargs)


# --- seeds -------------------------------------------------------------------


async def _chat(t: TwoOrg, which: str, *, client_id: str | None = None) -> WorkspaceObject:
    """A chat U owns, in org ``which``."""
    async with AsyncSessionLocal() as db:
        chat, _ = await chat_service.create_chat(
            db,
            owner=t.user,
            org_id=_org(t, which),
            title=f"chat in {which}",
            client_id=client_id,
            machine_id=None,
            machine_status="none",
        )
        await db.commit()
        await db.refresh(chat)
        return chat


async def _template(t: TwoOrg, which: str) -> WorkspaceObject:
    """A chat template U owns, in org ``which``."""
    row = WorkspaceObject(
        id=uuid4(),
        org_team_id=_org(t, which),
        logical_id=str(uuid4()),
        namespace=DEFAULT_NAMESPACE,
        type=chat_template_service.TEMPLATE_TYPE,
        title=f"template in {which}",
        version=1,
        status="ready",
        spec={"brief": "start here"},
        owner_user_id=t.user.id,
        visibility_scope="private",
        content_updated_at=datetime.now(UTC).timestamp(),
    )
    async with AsyncSessionLocal() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _make_admin_of_b(t: TwoOrg) -> None:
    """U becomes an admin of org B's root, so B's admin routes admit U and the
    only thing left to decide what they answer about is the org."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TeamMembership)
            .where(TeamMembership.team_id == t.org_b, TeamMembership.user_id == t.user.id)
            .values(role=TeamRole.ADMIN)
        )
        await db.commit()


async def _row_count(model: Any, *where: Any) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )


# --- chats: every chat-scoped route refuses another org's chat ---------------


@dataclass(frozen=True)
class ChatCall:
    method: str
    suffix: str
    body: dict[str, Any] | None = None


#: Every route that names a chat in its path, with a body that would be
#: accepted on the chat's own credential. ``""`` is the chat itself.
CHAT_CALLS = [
    pytest.param(ChatCall("GET", ""), id="read"),
    pytest.param(ChatCall("GET", "/messages"), id="messages"),
    pytest.param(
        ChatCall("POST", "/messages", {"text": "hello", "client_id": "m-1"}), id="post-message"
    ),
    pytest.param(
        ChatCall("POST", "/answer", {"interrupt_id": "ask-1", "option_id": "once"}), id="answer"
    ),
    pytest.param(ChatCall("POST", "/stop"), id="stop"),
    pytest.param(ChatCall("PUT", "/permission-mode", {"mode": "plan"}), id="permission-mode"),
    pytest.param(ChatCall("PUT", "/model", {"model": "claude-opus-4.5"}), id="model"),
    pytest.param(ChatCall("POST", "/promote", {"event_id": "e-1", "title": "Rows"}), id="promote"),
    pytest.param(ChatCall("GET", "/attachments"), id="attachments"),
    pytest.param(ChatCall("GET", "/workspace"), id="workspace"),
    pytest.param(ChatCall("DELETE", ""), id="delete"),
]


@pytest.mark.parametrize("call", CHAT_CALLS)
async def test_a_b_credential_cannot_reach_the_persons_own_chat_in_a(
    two_org_identity: TwoOrg, call: ChatCall
) -> None:
    """U owns the chat. On the B credential it does not exist, and nothing the
    route would have written lands on it."""
    t = two_org_identity
    chat = await _chat(t, "a")
    messages_before = await _row_count(ChatMessage, ChatMessage.chat_id == chat.id)
    resp = await _call(
        call.method, f"/api/v1/chats/{chat.id}{call.suffix}", t.token_b, json=call.body
    )
    assert resp.status_code == 404, resp.text
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, chat.id)
        assert row is not None
        assert row.deleted_at == 0
        assert row.version == chat.version
        assert row.spec == chat.spec
    assert await _row_count(ChatMessage, ChatMessage.chat_id == chat.id) == messages_before
    assert (
        await _row_count(
            WorkspaceObject,
            WorkspaceObject.type == "result",
            WorkspaceObject.spec["source_chat_id"].as_string() == str(chat.id),
        )
        == 0
    )


async def test_the_same_chat_reads_on_its_own_orgs_credential(two_org_identity: TwoOrg) -> None:
    """The positive control for the table above: the person, the chat, the A
    credential."""
    t = two_org_identity
    chat = await _chat(t, "a")
    resp = await _call("GET", f"/api/v1/chats/{chat.id}", t.token_a)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(chat.id)


@pytest.mark.parametrize(
    ("which", "status"),
    [pytest.param("a", 200, id="agent-on-a"), pytest.param("b", 404, id="agent-on-b")],
)
async def test_an_agent_on_the_persons_b_session_cannot_name_an_a_chat(
    two_org_identity: TwoOrg, which: str, status: int
) -> None:
    """An agent acts inside its person's session and in that session's org. The
    agent on U's A session reads U's A chat; the agent on U's B session, naming
    the very same chat, is told it does not exist."""
    t = two_org_identity
    chat = await _chat(t, "a")
    resp = await _call(
        "GET",
        f"/api/v1/chats/{chat.id}",
        _token(t, which),
        headers=agent_headers(f"agent-{secrets.token_hex(4)}"),
    )
    assert resp.status_code == status, resp.text


@pytest.mark.parametrize("which", ["a", "b"])
async def test_a_chat_listing_holds_only_the_credentials_org(
    two_org_identity: TwoOrg, which: str
) -> None:
    t = two_org_identity
    chat_a, chat_b = await _chat(t, "a"), await _chat(t, "b")
    resp = await _call("GET", "/api/v1/chats", _token(t, which))
    assert resp.status_code == 200, resp.text
    listed = {item["id"] for item in resp.json()["items"]}
    mine, theirs = (chat_a, chat_b) if which == "a" else (chat_b, chat_a)
    assert str(mine.id) in listed
    assert str(theirs.id) not in listed


@pytest.mark.parametrize("which", ["a", "b"])
async def test_a_chat_created_on_a_credential_lands_in_its_org(
    two_org_identity: TwoOrg, which: str
) -> None:
    t = two_org_identity
    resp = await _call("POST", "/api/v1/chats", _token(t, which), json={"title": "new"})
    assert resp.status_code == 201, resp.text
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, UUID(resp.json()["id"]))
    assert row is not None
    assert row.org_team_id == _org(t, which)
    assert row.owner_user_id == t.user.id


async def test_a_client_id_used_in_a_never_hands_the_a_chat_to_b(
    two_org_identity: TwoOrg,
) -> None:
    """A retried create is a read of the chat the id already made, in the
    credential's org: the same id on the B credential makes a B chat."""
    t = two_org_identity
    client_id = f"client-{secrets.token_hex(6)}"
    chat_a = await _chat(t, "a", client_id=client_id)
    resp = await _call(
        "POST", "/api/v1/chats", t.token_b, json={"title": "again", "client_id": client_id}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] != str(chat_a.id)
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, UUID(resp.json()["id"]))
    assert row is not None
    assert row.org_team_id == t.org_b


async def _spare(t: TwoOrg, which: str) -> WorkspaceObject:
    async with AsyncSessionLocal() as db:
        chat, _ = await chat_service.create_chat(
            db,
            owner=t.user,
            org_id=_org(t, which),
            title=None,
            client_id=None,
            machine_id=None,
            machine_status="none",
            spare=True,
        )
        await db.commit()
        await db.refresh(chat)
        return chat


async def test_a_spare_warmed_in_a_is_neither_stamped_nor_reaped_from_b(
    two_org_identity: TwoOrg,
) -> None:
    """One spare per person in each org, and it belongs to the org it was
    warmed in: the B page's heartbeat (B has no box, so it warms nothing) leaves
    A's spare exactly as it stood."""
    t = two_org_identity
    spare = await _spare(t, "a")
    resp = await _call("POST", "/api/v1/chats/spare", t.token_b)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "none"
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, spare.id)
        assert row is not None
        assert row.deleted_at == 0
        assert row.spec == spare.spec


async def test_a_create_on_b_never_claims_the_spare_warmed_in_a(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    spare = await _spare(t, "a")
    resp = await _call(
        "POST", "/api/v1/chats", t.token_b, json={"title": "first", "claim_spare": True}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] != str(spare.id)
    async with AsyncSessionLocal() as db:
        made = await db.get(WorkspaceObject, UUID(resp.json()["id"]))
        assert made is not None
        assert made.org_team_id == t.org_b
        standing = await chat_spares.find_spare(db, owner_id=t.user.id, org_id=t.org_a)
        assert standing is not None
        assert standing.id == spare.id
        assert await chat_spares.find_spare(db, owner_id=t.user.id, org_id=t.org_b) is None


async def _make_admin(t: TwoOrg, org_id: UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TeamMembership)
            .where(TeamMembership.team_id == org_id, TeamMembership.user_id == t.user.id)
            .values(role=TeamRole.ADMIN)
        )
        await db.commit()


async def _register_box(t: TwoOrg, which: str) -> UUID:
    """U, made an admin of the org, registers a box on that org's credential."""
    await _make_admin(t, _org(t, which))
    async with AsyncSessionLocal() as db:
        mt = await make_machine_type(db)
        await make_grant(db, org_team_id=_org(t, which), machine_type_id=mt.id)
    resp = await _call(
        "POST",
        "/api/v1/machines/register",
        _token(t, which),
        headers=agent_headers(f"sess-box-{which}"),
        json={
            "provider": "runpod",
            "provider_pod_id": f"pod-{secrets.token_hex(4)}",
            "name": f"{which}-box",
            "machine_type_code": mt.provider_type_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _standing_spares(user_id: UUID) -> dict[UUID, WorkspaceObject]:
    """Every spare the person holds, by org: read without ``find_spare`` so a
    second spare in one org would show rather than raise."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(WorkspaceObject).where(
                    WorkspaceObject.owner_user_id == user_id,
                    WorkspaceObject.type == "chat",
                    WorkspaceObject.deleted_at == 0,
                    WorkspaceObject.spec["spare"].as_boolean().is_(True),
                )
            )
        ).scalars()
        spares: dict[UUID, WorkspaceObject] = {}
        for row in rows:
            assert row.org_team_id not in spares, "at most one spare per org"
            spares[row.org_team_id] = row
        return spares


async def _warm_in_both(t: TwoOrg) -> dict[UUID, WorkspaceObject]:
    for token in (t.token_a, t.token_b):
        resp = await _call("POST", "/api/v1/chats/spare", token)
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "warm"
    return await _standing_spares(t.user.id)


async def test_a_person_keeps_one_spare_in_each_org_on_that_orgs_box(
    two_org_identity: TwoOrg,
) -> None:
    """Warming on A and on B leaves two spares, each in its own org and on its
    own org's box; another beat in either org stamps its own and warms nothing
    new."""
    t = two_org_identity
    box_a, box_b = await _register_box(t, "a"), await _register_box(t, "b")
    spares = await _warm_in_both(t)
    assert set(spares) == {t.org_a, t.org_b}
    assert spares[t.org_a].spec["machine_id"] == str(box_a)
    assert spares[t.org_b].spec["machine_id"] == str(box_b)
    before = {org: row.id for org, row in spares.items()}
    again = await _warm_in_both(t)
    assert {org: row.id for org, row in again.items()} == before


@pytest.mark.parametrize("claimer", ["a", "b"])
async def test_a_claim_takes_the_spare_of_its_own_org_and_leaves_the_others(
    two_org_identity: TwoOrg, claimer: str
) -> None:
    t = two_org_identity
    await _register_box(t, "a")
    await _register_box(t, "b")
    spares = await _warm_in_both(t)
    mine, other = _org(t, claimer), _org(t, "b" if claimer == "a" else "a")
    resp = await _call(
        "POST", "/api/v1/chats", _token(t, claimer), json={"title": "first", "claim_spare": True}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == str(spares[mine].id), "the claim took this org's spare"
    left = await _standing_spares(t.user.id)
    assert set(left) == {other}
    assert left[other].id == spares[other].id
    assert left[other].spec == spares[other].spec, "the other org's spare is untouched"


async def test_signing_out_reaps_the_spare_of_every_org_the_session_switched_to(
    two_org_identity: TwoOrg,
) -> None:
    """A sign-out ends the refresh family, which follows the browser through
    every org it switched to: nobody is on any of those pages any more."""
    t = two_org_identity
    await _register_box(t, "a")
    await _register_box(t, "b")
    assert len(await _warm_in_both(t)) == 2
    out = await _call("POST", "/api/v1/auth/logout", t.token_b)
    assert out.status_code == 200, out.text
    assert await _standing_spares(t.user.id) == {}


async def test_a_chat_is_duplicated_only_inside_the_org_the_copy_is_made_in(
    two_org_identity: TwoOrg,
) -> None:
    """A copy lands in the source's org, and a copy made for another org is
    refused rather than filed under the person's home."""
    t = two_org_identity
    chat_a = await _chat(t, "a")
    async with AsyncSessionLocal() as db:
        source = await db.get(WorkspaceObject, chat_a.id)
        assert source is not None
        with pytest.raises(ValueError, match="inside its own org"):
            await duplicate_service.duplicate_folder_object(
                db, source=source, owner=t.user, org_id=t.org_b, title="copy"
            )
        made = await duplicate_service.duplicate_folder_object(
            db, source=source, owner=t.user, org_id=t.org_a, title="copy"
        )
        assert made.org_team_id == t.org_a
        await db.rollback()


# --- templates ------------------------------------------------------------------


TEMPLATE_CALLS = [
    pytest.param("GET", None, id="read"),
    pytest.param("PUT", {"expected_version": 1, "title": "taken"}, id="update"),
    pytest.param("DELETE", None, id="delete"),
]


@pytest.mark.parametrize(("method", "body"), TEMPLATE_CALLS)
async def test_a_b_credential_cannot_reach_the_persons_template_in_a(
    two_org_identity: TwoOrg, method: str, body: dict[str, Any] | None
) -> None:
    t = two_org_identity
    template = await _template(t, "a")
    resp = await _call("" + method, f"/api/v1/chat-templates/{template.id}", t.token_b, json=body)
    assert resp.status_code == 404, resp.text
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, template.id)
        assert row is not None
        assert (row.title, row.version, row.deleted_at) == (template.title, 1, 0)
    control = await _call("GET", f"/api/v1/chat-templates/{template.id}", t.token_a)
    assert control.status_code == 200, control.text


@pytest.mark.parametrize("which", ["a", "b"])
async def test_a_template_listing_holds_only_the_credentials_org(
    two_org_identity: TwoOrg, which: str
) -> None:
    t = two_org_identity
    template_a, template_b = await _template(t, "a"), await _template(t, "b")
    resp = await _call("GET", "/api/v1/chat-templates", _token(t, which))
    assert resp.status_code == 200, resp.text
    listed = {item["id"] for item in resp.json()["items"]}
    mine, theirs = (template_a, template_b) if which == "a" else (template_b, template_a)
    assert str(mine.id) in listed
    assert str(theirs.id) not in listed


# --- compute ----------------------------------------------------------------------


async def _allocation(t: TwoOrg, which: str, *, session_id: str = "") -> ComputeAllocation:
    """A live session allocation U holds in org ``which``."""
    async with AsyncSessionLocal() as db:
        mt = await make_machine_type(db)
        alloc = ComputeAllocation(
            user_id=t.user.id,
            org_team_id=_org(t, which),
            machine_type_id=mt.id,
            lifecycle="session",
            state="ready",
            ssh_public_key="ssh-ed25519 AAAA test",
            ssh_private_key_enc="sealed",
            project_path="/work",
            session_id=session_id,
            price_per_minute_nanos=0,
            true_cost_per_minute_nanos=0,
        )
        db.add(alloc)
        await db.commit()
        await db.refresh(alloc)
        return alloc


@pytest.mark.parametrize("which", ["a", "b"])
async def test_an_allocation_listing_holds_only_the_credentials_org(
    two_org_identity: TwoOrg, which: str
) -> None:
    """The person rents machines in both orgs; each org's session lists its own."""
    t = two_org_identity
    alloc_a, alloc_b = await _allocation(t, "a"), await _allocation(t, "b")
    resp = await _call("GET", "/api/v1/compute/allocations", _token(t, which))
    assert resp.status_code == 200, resp.text
    listed = {row["id"] for row in resp.json()}
    mine, theirs = (alloc_a, alloc_b) if which == "a" else (alloc_b, alloc_a)
    assert str(mine.id) in listed
    assert str(theirs.id) not in listed


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        pytest.param("GET", "", id="read"),
        pytest.param("DELETE", "", id="release"),
    ],
)
async def test_a_b_credential_cannot_reach_the_persons_allocation_in_a(
    two_org_identity: TwoOrg, method: str, suffix: str
) -> None:
    t = two_org_identity
    alloc = await _allocation(t, "a")
    resp = await _call(method, f"/api/v1/compute/allocations/{alloc.id}{suffix}", t.token_b)
    assert resp.status_code == 404, resp.text
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, alloc.id)
    assert row is not None and row.state == "ready"
    control = await _call("GET", f"/api/v1/compute/allocations/{alloc.id}", t.token_a)
    assert control.status_code == 200, control.text


async def test_a_session_machine_is_reused_only_inside_its_org(two_org_identity: TwoOrg) -> None:
    """A provision retry gets the session's live machine back, but a session id
    the person used in A names nothing in B."""
    t = two_org_identity
    session_id = f"sess-{secrets.token_hex(4)}"
    alloc = await _allocation(t, "a", session_id=session_id)
    async with AsyncSessionLocal() as db:
        found = {
            which: await compute_service.live_allocation_for_session(
                db,
                user_id=t.user.id,
                org_team_id=_org(t, which),
                session_id=session_id,
                machine_type_id=alloc.machine_type_id,
            )
            for which in ("a", "b")
        }
    assert found["a"] is not None and found["a"].id == alloc.id
    assert found["b"] is None


async def test_a_machine_heartbeat_for_an_a_machine_is_refused_on_b(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    alloc = await _allocation(t, "a")
    resp = await _call("POST", f"/api/v1/machines/{alloc.id}/heartbeat", t.token_b)
    assert resp.status_code == 404, resp.text
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, alloc.id)
    assert row is not None and row.last_heartbeat_at is None


async def _register_box_in_b(t: TwoOrg) -> UUID:
    """U, an admin of B, registers a box on the B session; its id."""
    await _make_admin_of_b(t)
    async with AsyncSessionLocal() as db:
        mt = await make_machine_type(db)
        await make_grant(db, org_team_id=t.org_b, machine_type_id=mt.id)
    resp = await _call(
        "POST",
        "/api/v1/machines/register",
        t.token_b,
        headers=agent_headers("sess-box"),
        json={
            "provider": "runpod",
            "provider_pod_id": f"pod-{secrets.token_hex(4)}",
            "name": "b-box",
            "machine_type_code": mt.provider_type_id,
        },
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def test_a_box_registered_on_b_is_bs_workspace_machine(two_org_identity: TwoOrg) -> None:
    """The daemon registers its box on the operator's session: the machine is
    the org's that session is in, admitted on that org's grant."""
    t = two_org_identity
    machine_id = await _register_box_in_b(t)
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, machine_id)
    assert row is not None and row.org_team_id == t.org_b


async def test_a_spare_warmed_on_b_is_bs_chat_on_bs_box(two_org_identity: TwoOrg) -> None:
    """The page's heartbeat on the B session warms a chat in B, placed on B's
    box: the spare's org is the request's, never the person's home."""
    t = two_org_identity
    machine_id = await _register_box_in_b(t)
    resp = await _call("POST", "/api/v1/chats/spare", t.token_b)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "warm"
    async with AsyncSessionLocal() as db:
        spare = await chat_spares.find_spare(db, owner_id=t.user.id, org_id=t.org_b)
    assert spare is not None
    assert spare.org_team_id == t.org_b
    assert spare.spec["machine_id"] == str(machine_id)
