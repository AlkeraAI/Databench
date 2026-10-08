"""A chat's records through the real routes: born marked, written by the box alone.

The exemplar shape applied to the chat's own records. A member's chat is
made through ``POST /chats``; the org's box, bound to it and holding its
lease, pushes the transcript into the chat folder the way a checkpoint push
does. Then every person who holds WRITE on that folder — the chat's owner,
a colleague with "Can edit", the org admin with "Full access" — tries to
change the transcript, and each is refused with the record's own code while
the row names the reason. The box, on the same node, writes it — but only on
the request that carries the lease it holds.

Each case pins the status contract AND the ``authz.decision`` row: the DENY
that survived the refused request, the ALLOW that rode the box's own.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import FilesFixtures, FilesOrgFixture
from _live_holder import MockHolder
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.policies.files import CHAT_RECORD_READ_ONLY
from alkera_core.authz.principal import ActingContext
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files import acl
from alkera_core.files.authz.decider import RECORD_BIT
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_MANAGER, ROLE_WRITER
from alkera_core.models import EventOutbox
from alkera_core.models.files.tree import FileNode
from blake3 import blake3
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login, make_member
from tests.files._boxes import registered_box

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
TRANSCRIPT = "chat.jsonl"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


def _code(response: Response) -> str | None:
    body = response.json()
    if isinstance(body.get("code"), str):
        return str(body["code"])
    detail = body.get("detail")
    return str(detail["code"]) if isinstance(detail, dict) and "code" in detail else None


@pytest_asyncio.fixture
async def box(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> AsyncIterator[tuple[AsyncClient, str]]:
    """The org's box, operated by the org admin, on its own box credential."""
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        yield client, machine_id


class Chat:
    def __init__(self, chat_id: str, node: FileNode, owner: AsyncClient) -> None:
        self.chat_id = chat_id
        self.node = node
        self.drive = node.drive_id
        self.owner = owner

    def item(self, node_id: uuid.UUID | None = None) -> str:
        return f"{BASE}/drives/{self.drive}/items/{node_id or self.node.id}"


@pytest_asyncio.fixture
async def chat(files_org: FilesOrgFixture) -> AsyncIterator[Chat]:
    """A member's private chat, made through the route and filed in their home."""
    owner = await login(app_client(), files_org.member.email, files_org.member_password)
    made = await owner.post(
        "/api/v1/chats", json={"title": "Pricing review", "clientId": secrets.token_hex(8)}
    )
    assert made.status_code == 201, made.text
    chat_id = str(made.json()["id"])
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == uuid.UUID(chat_id),
                    FileNode.subtype == "chat",
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        session.expunge(node)
    yield Chat(chat_id, node, owner)
    await owner.aclose()


async def _bind(chat_id: str, machine: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
            ),
            {"id": uuid.UUID(chat_id), "machine": machine},
        )
        await session.commit()


async def _child(parent_id: uuid.UUID, name: str) -> FileNode:
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.parent_id == parent_id,
                    FileNode.name == name.encode(),
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        session.expunge(node)
        return node


async def _etag(node_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as session:
        return str(
            (
                await session.execute(
                    text("SELECT etag FROM file_nodes WHERE id = :node"), {"node": node_id}
                )
            ).scalar_one()
        )


async def _decisions(org_id: uuid.UUID, node_id: uuid.UUID) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "file_node",
                EventOutbox.entity_id == str(node_id),
            )
            .order_by(EventOutbox.id)
        )
        return [dict(row.payload) for row in rows.scalars().all()]


async def _upload(
    client: AsyncClient,
    parent_id: uuid.UUID,
    name: str,
    payload: bytes,
    *,
    headers: dict[str, str],
) -> Response:
    """A new file under ``parent_id``: open, one part, complete — what a push
    does for a file the drive has never seen."""
    opened = await client.post(
        f"{BASE}/uploads",
        json={"declaredSize": len(payload), "name": name, "parentId": str(parent_id)},
        headers={**_idem(), **headers},
    )
    if opened.status_code != 201:
        return opened
    upload_id = opened.json()["uploadId"]
    checksum = blake3(payload).digest().hex()
    # The fence rides every call of a push — the box's client carries it for
    # the life of the lease — and a part landing under a record folder is a
    # write into it like any other.
    part = await client.put(
        f"{BASE}/uploads/{upload_id}/parts/1",
        content=payload,
        headers={**_idem(), **headers, "X-Part-Checksum": checksum},
    )
    assert part.status_code == 200, part.text
    return await client.post(
        f"{BASE}/uploads/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": len(payload), "checksum": checksum}]},
        headers={**_idem(), **headers},
    )


async def _overwrite(
    client: AsyncClient, chat: Chat, node: FileNode, payload: bytes, *, headers: dict[str, str]
) -> Response:
    return await client.put(
        f"{chat.item(node.id)}/content",
        content=payload,
        headers={
            **_idem(),
            **headers,
            "If-Match": await _etag(node.id),
            "Content-Type": "application/octet-stream",
        },
    )


class Pushed:
    """The chat as the box left it after its first push: bound, leased, the
    transcript at the top of the folder and a lookalike inside scratch."""

    def __init__(
        self, box: AsyncClient, machine_id: str, holder: MockHolder, record: FileNode
    ) -> None:
        self.box = box
        self.machine_id = machine_id
        self.holder = holder
        self.record = record


@pytest_asyncio.fixture
async def pushed(box: tuple[AsyncClient, str], chat: Chat, real_session: AsyncSession) -> Pushed:
    client, machine_id = box
    await _bind(chat.chat_id, machine_id)
    holder = MockHolder(client, chat.drive, chat.node.id, machine=machine_id)
    taken = await holder.take(real_session, _idem, purpose="chat")
    assert taken.status_code == 200, taken.text
    landed = await _upload(
        client, chat.node.id, TRANSCRIPT, b'{"event_type":"message"}\n', headers=holder.fence
    )
    assert landed.status_code in (200, 201, 202), landed.text
    return Pushed(client, machine_id, holder, await _child(chat.node.id, TRANSCRIPT))


# ------------------------------------------------------------------ birth


async def test_the_transcript_the_box_pushes_is_born_a_record(pushed: Pushed) -> None:
    assert pushed.record.flags & RECORD_BIT, "the mark is set at birth, not by a migration"


async def test_a_record_name_inside_the_working_directory_is_working_material(
    pushed: Pushed, chat: Chat
) -> None:
    """The asymmetric twin: the same name one level down is the agent's own
    file, and a person may edit it."""
    scratch = await _child(chat.node.id, "scratch")
    landed = await _upload(
        pushed.box, scratch.id, "manifest.json", b"{}", headers=pushed.holder.fence
    )
    assert landed.status_code in (200, 201, 202), landed.text
    lookalike = await _child(scratch.id, "manifest.json")
    assert not lookalike.flags & RECORD_BIT

    edited = await _overwrite(chat.owner, chat, lookalike, b'{"mine": true}', headers={})
    assert edited.status_code in (200, 201), edited.text


async def test_the_runtime_directory_and_what_the_box_puts_in_it_are_records(
    pushed: Pushed, chat: Chat
) -> None:
    """The skeleton route makes ``.runtime/agent`` (a push names the folders
    it needs, every segment a folder); the upload lands the database inside
    it. Every level carries the mark: the top from its name, the rest from
    the folder above."""
    made = await pushed.box.post(
        f"{chat.item()}/tree",
        json={"paths": [".runtime/agent"]},
        headers={**_idem(), **pushed.holder.fence},
    )
    assert made.status_code == 201, made.text
    runtime = await _child(chat.node.id, ".runtime")
    agent = await _child(runtime.id, "agent")
    landed = await _upload(pushed.box, agent.id, "agent.db", b"sqlite", headers=pushed.holder.fence)
    assert landed.status_code in (200, 201, 202), landed.text
    database = await _child(agent.id, "agent.db")
    assert runtime.flags & RECORD_BIT and agent.flags & RECORD_BIT and database.flags & RECORD_BIT

    refused = await _overwrite(chat.owner, chat, database, b"forged", headers={})
    assert refused.status_code == 403, refused.text
    assert _code(refused) == CHAT_RECORD_READ_ONLY


# ------------------------------------------------------------ the people


Person = Callable[[FilesOrgFixture, FilesFixtures, Chat], Awaitable[AsyncClient]]


async def _the_owner(files_org: FilesOrgFixture, fx: FilesFixtures, chat: Chat) -> AsyncClient:
    return chat.owner


async def _granted(
    files_org: FilesOrgFixture, fx: FilesFixtures, chat: Chat, *, user_id: uuid.UUID, role: str
) -> None:
    """The chat's owner shares its folder at ``role``."""
    ctx = ActingContext.for_user(
        user_id=files_org.member.id, org_id=files_org.org.org_id, email=files_org.member.email
    )
    async with fx.repo.transaction():
        await acl.grant(fx.repo, ctx, chat.node, Principal(kind="user", id=user_id), role)
    await fx.repo.session.commit()


async def _a_colleague_with_edit(
    files_org: FilesOrgFixture, fx: FilesFixtures, chat: Chat
) -> AsyncClient:
    async with AsyncSessionLocal() as session:
        colleague, password = await make_member(session, org_id=files_org.org.org_id, verified=True)
    await _granted(files_org, fx, chat, user_id=colleague.id, role=ROLE_WRITER)
    return await login(app_client(), colleague.email, password or "")


async def _the_org_admin_with_full_access(
    files_org: FilesOrgFixture, fx: FilesFixtures, chat: Chat
) -> AsyncClient:
    await _granted(files_org, fx, chat, user_id=files_org.org.admin_id, role=ROLE_MANAGER)
    return await login(app_client(), files_org.org.admin_email, files_org.org.admin_password)


PEOPLE = [
    pytest.param(_the_owner, id="the-chats-owner"),
    pytest.param(_a_colleague_with_edit, id="a-colleague-with-can-edit"),
    pytest.param(_the_org_admin_with_full_access, id="the-org-admin-with-full-access"),
]


async def _attempt(verb: str, client: AsyncClient, chat: Chat, node: FileNode) -> Response:
    if verb == "overwrite":
        return await _overwrite(client, chat, node, b"forged\n", headers={})
    if verb == "rename":
        return await client.patch(
            chat.item(node.id),
            json={"name": "chat.old.jsonl"},
            headers={**_idem(), "If-Match": await _etag(node.id)},
        )
    if verb == "move":
        scratch = await _child(chat.node.id, "scratch")
        return await client.patch(
            chat.item(node.id),
            json={"parentId": str(scratch.id)},
            headers={**_idem(), "If-Match": await _etag(node.id)},
        )
    assert verb == "trash"
    return await client.delete(
        chat.item(node.id), headers={**_idem(), "If-Match": await _etag(node.id)}
    )


VERBS = ["overwrite", "rename", "move", "trash"]


@pytest.mark.parametrize("person", PEOPLE)
@pytest.mark.parametrize("verb", VERBS)
async def test_a_person_who_holds_write_on_the_chat_is_refused_its_record_with_the_code(
    pushed: Pushed,
    chat: Chat,
    files_org: FilesOrgFixture,
    fx: FilesFixtures,
    person: Person,
    verb: str,
) -> None:
    """Every person who can write in the chat folder — the owner most of all —
    is refused the transcript, visibly and with the record's own code; the
    DENY row names ``chat_record`` and shows the lease was not held. The
    bytes and the name are exactly as the box left them afterwards."""
    client = await person(files_org, fx, chat)
    before = await _child(chat.node.id, TRANSCRIPT)

    response = await _attempt(verb, client, chat, pushed.record)

    assert response.status_code == 403, response.text
    assert _code(response) == CHAT_RECORD_READ_ONLY
    after = await _child(chat.node.id, TRANSCRIPT)
    assert (after.etag, after.parent_id, after.head_version_id) == (
        before.etag,
        before.parent_id,
        before.head_version_id,
    )

    denies = [
        row
        for row in await _decisions(files_org.org.org_id, pushed.record.id)
        if row["effect"] == "deny"
    ]
    assert denies, "the refusal is on record"
    latest = denies[-1]
    assert latest["reason"] == "chat_record"
    assert latest["error_code"] == CHAT_RECORD_READ_ONLY
    assert latest["as_not_found"] is False
    assert latest["attrs"]["holds_lease"] is False
    assert "record" in latest["attrs"]["flags"]
    assert TRANSCRIPT not in str(latest["attrs"])


async def test_a_person_still_reads_the_record(pushed: Pushed, chat: Chat) -> None:
    """Read-only means read: the owner lists it and opens its metadata, and
    the listing row offers none of the writes."""
    read = await chat.owner.get(chat.item(pushed.record.id))
    assert read.status_code == 200, read.text
    capabilities = read.json()["capabilities"]
    assert capabilities["can_write"] is False
    assert capabilities["can_rename"] is False
    assert capabilities["can_delete"] is False
    listed = await chat.owner.get(f"{chat.item()}/children")
    assert listed.status_code == 200, listed.text
    assert TRANSCRIPT in {item["name"] for item in listed.json()["value"]}


# --------------------------------------------------------------- the box


async def test_the_box_holding_the_lease_rewrites_the_record_and_the_allow_is_on_record(
    pushed: Pushed, chat: Chat, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    written = await pushed.holder.push(
        real_session, _idem, pushed.record.id, b'{"event_type":"message"}\n{"event_type":"next"}\n'
    )
    assert written.status_code in (200, 201), written.text
    after = await _child(chat.node.id, TRANSCRIPT)
    assert after.head_version_id != pushed.record.head_version_id

    allows = [
        row
        for row in await _decisions(files_org.org.org_id, pushed.record.id)
        if row["effect"] == "allow" and row["action"] == "write"
    ]
    assert allows, "the write rode the box's request"
    assert allows[-1]["attrs"]["holds_lease"] is True
    assert "record" in allows[-1]["attrs"]["flags"]


async def test_the_chats_own_box_without_its_lease_headers_is_a_reader_of_the_record(
    pushed: Pushed, chat: Chat, files_org: FilesOrgFixture
) -> None:
    """Being the machine the chat runs on is not holding the lease: the same
    box, on a request that carries no fence, is refused exactly as a person
    is. The rule is the LIVE lease, resolved from the rows, never the binding
    alone."""
    response = await _overwrite(pushed.box, chat, pushed.record, b"unfenced\n", headers={})
    assert response.status_code == 403, response.text
    assert _code(response) == CHAT_RECORD_READ_ONLY
    denies = [
        row
        for row in await _decisions(files_org.org.org_id, pushed.record.id)
        if row["effect"] == "deny"
    ]
    assert denies[-1]["reason"] == "chat_record"
    assert denies[-1]["attrs"]["holds_lease"] is False


async def _trash_op_of(client: AsyncClient, chat: Chat, node_id: uuid.UUID) -> str:
    page = await client.get(f"{BASE}/drives/{chat.drive}/trash")
    assert page.status_code == 200, page.text
    entry = next(row for row in page.json()["entries"] if row["item"]["id"] == str(node_id))
    return str(entry["trashOpId"])


async def test_a_trashed_record_is_restored_by_the_box_and_not_by_a_person(
    pushed: Pushed, chat: Chat, files_org: FilesOrgFixture
) -> None:
    """Trashing rides WRITE and restoring rides RESTORE; both are the box's.
    A person bringing a record back would be putting words back into the
    chat's mouth that the box had taken out — refused with the same code,
    on record under the same reason. The box, fenced, restores it."""
    trashed = await pushed.box.delete(
        chat.item(pushed.record.id),
        headers={**_idem(), **pushed.holder.fence, "If-Match": await _etag(pushed.record.id)},
    )
    assert trashed.status_code in (200, 202), trashed.text
    op_id = await _trash_op_of(chat.owner, chat, pushed.record.id)

    refused = await chat.owner.post(
        f"{BASE}/drives/{chat.drive}/trash/{op_id}/restore",
        json={},
        headers={**_idem(), "If-Match": "1"},
    )
    assert refused.status_code == 403, refused.text
    assert _code(refused) == CHAT_RECORD_READ_ONLY
    denies = [
        row
        for row in await _decisions(files_org.org.org_id, pushed.record.id)
        if row["effect"] == "deny" and row["action"] == "restore"
    ]
    assert denies and denies[-1]["reason"] == "chat_record"

    restored = await pushed.box.post(
        f"{BASE}/drives/{chat.drive}/trash/{op_id}/restore",
        json={},
        headers={**_idem(), **pushed.holder.fence, "If-Match": "1"},
    )
    assert restored.status_code == 200, restored.text
    back = await _child(chat.node.id, TRANSCRIPT)
    assert back.id == pushed.record.id and back.flags & RECORD_BIT


async def test_a_box_that_is_not_the_chats_is_told_nothing_of_the_record(
    pushed: Pushed, chat: Chat, real_session: AsyncSession, files_org: FilesOrgFixture
) -> None:
    """A second box of the same org, proven and fenced on its own lease over
    some other folder, is turned away before the record rule is reached: the
    opaque not-found the chat's binding gives every stranger box."""
    token, other_machine = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(other_machine)},
    ) as other:
        response = await _overwrite(
            other, chat, pushed.record, b"theirs\n", headers=pushed.holder.fence
        )
    assert response.status_code == 404, response.text
