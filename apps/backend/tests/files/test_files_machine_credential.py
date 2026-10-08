"""A box on its own machine credential, through the real Files routes.

The credential is the bearer; there is no user behind the request and no
drive of the box's own. The box addresses the drive of an org its credential
serves, and inside it reaches exactly the folders of the chats bound to its
machine — the chat folder and what is staged under it — as the lease holder,
the writer and the reader of their bytes. Everything else on the drive, a
drive of an org it does not serve, and every door that wants a person answer
the opaque not-found; a revoked credential is refused at the door. Every
case pins the status contract AND the ``authz.decision`` row the route left
behind, chain and all.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from _files_kit import NOT_FOUND, FilesOrgFixture, refusal
from _live_holder import MockHolder
from alkera_core.auth import decode_session_token
from alkera_core.auth.machine_token import MACHINE_TOKEN_PREFIX
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, MachineCredential
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.files.tree import FileNode
from backend.services.org import teams as team_service
from blake3 import blake3
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token
from tests.files.test_files_content_routes import (  # noqa: F401 -- content_on is a fixture
    CONTENT_HOST,
    CONTENT_ORIGIN,
    content_on,
)
from tests.files.test_files_lease_routes import _upload
from tests.test_machine_principal_routes import _box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"
OPAQUE = NOT_FOUND
REFUSED = "machine_credential_refused"


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


class Box:
    """A platform box dedicated to the Files org: its client on the credential,
    the machine it holds, and the credential behind it."""

    def __init__(self, raw: str, credential_id: uuid.UUID, machine_id: str) -> None:
        self.raw = raw
        self.credential_id = credential_id
        self.machine_id = machine_id
        self.client = AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {raw}"},
        )


@pytest_asyncio.fixture
async def box(platform_admin: OrgWithAdmin, files_org: FilesOrgFixture) -> AsyncIterator[Box]:
    """A box the platform minted, holding a live machine, dedicated to the
    Files org — the shape the console and ``/machines/claim`` leave."""
    made = await _box(platform_admin, tenancy="dedicated", served_org=files_org.org.org_id)
    box = Box(made.raw, made.credential_id, str(made.machine_id))
    assert box.raw.startswith(MACHINE_TOKEN_PREFIX)
    yield box
    await box.client.aclose()


class MemberChat:
    def __init__(self, chat_id: str, node: FileNode, member: AsyncClient) -> None:
        self.chat_id = chat_id
        self.node = node
        self.drive = node.drive_id
        self.member = member

    @property
    def item(self) -> str:
        return f"{BASE}/drives/{self.drive}/items/{self.node.id}"


@pytest_asyncio.fixture
async def members_chat(files_org: FilesOrgFixture) -> AsyncIterator[MemberChat]:
    """A member's private chat, made through the route and filed in their home."""
    member = await login(app_client(), files_org.member.email, files_org.member_password)
    made = await member.post(
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
    yield MemberChat(chat_id, node, member)
    await member.aclose()


async def _bind(chat_id: str, machine: str | None) -> None:
    """Set the chat row's machine binding, as placement and rebinding do."""
    async with AsyncSessionLocal() as session:
        if machine is None:
            await session.execute(
                text("UPDATE workspace_objects SET spec = spec - 'machine_id' WHERE id = :id"),
                {"id": uuid.UUID(chat_id)},
            )
        else:
            await session.execute(
                text(
                    "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                    "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                ),
                {"id": uuid.UUID(chat_id), "machine": machine},
            )
        await session.commit()


async def _decisions(org_id: uuid.UUID, node_id: uuid.UUID) -> list[EventOutbox]:
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
        return list(rows.scalars().all())


def _effects(rows: list[EventOutbox]) -> set[tuple[str, str, str]]:
    return {(r.payload["action"], r.payload["effect"], r.payload["reason"]) for r in rows}


def _chain(row: EventOutbox) -> list[tuple[str, str]]:
    return [(link["kind"], link["id"]) for link in row.actor["chain"]]


async def _lease_row(node_id: uuid.UUID) -> Any:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT holder_kind, holder_principal_id, machine_id, epoch FROM file_leases "
                    "WHERE node_id = :node AND released_at IS NULL ORDER BY acquired_at DESC"
                ),
                {"node": node_id},
            )
        ).first()


async def _redeem(client: AsyncClient, location: str, fence: dict[str, str]) -> Any:
    """Follow a minted URL onto the content origin, carrying the lease fence:
    the box's sealed chat folder opens its bytes only to the holder of its
    lease, on the origin exactly as on the API host."""
    return await client.get(
        location.removeprefix(CONTENT_ORIGIN), headers={"Host": CONTENT_HOST, **fence}
    )


# =========================================================================== #
# the folder of a chat bound to the box
# =========================================================================== #


@pytest.mark.usefixtures("content_on")
async def test_a_box_leases_reads_writes_and_pulls_its_chats_folder_on_its_credential(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """The whole of the box's job on one folder, on the credential alone: the
    item read says it may lease and may not share; the lease is granted to
    the MACHINE — the same holder a box on its operator's session fences as
    — the ``uploads/`` staging is created and filled under the fence, and the
    bytes come back through the content origin as the box that minted them.
    Every allow is on record under the machine's own chain, with the box's
    reason, and nothing was refused."""
    await _bind(members_chat.chat_id, box.machine_id)

    read = await box.client.get(members_chat.item)
    assert read.status_code == 200, read.text
    capabilities = read.json()["capabilities"]
    assert capabilities["can_lease"] is True
    assert capabilities["can_share"] is False

    holder = MockHolder(box.client, members_chat.drive, members_chat.node.id, machine="typed-name")
    taken = await holder.take(real_session, _idem, purpose="chat")
    assert taken.status_code == 200, taken.text
    lease = await _lease_row(members_chat.node.id)
    assert lease is not None
    assert (lease.holder_kind, str(lease.holder_principal_id)) == ("machine", box.machine_id)
    assert lease.machine_id == box.machine_id, "the badge is the proven machine, not the typed name"

    staged = await box.client.post(
        f"{members_chat.item}/children",
        json={"name": "uploads", "kind": "folder"},
        headers={**_idem(), **holder.fence},
    )
    assert staged.status_code == 201, staged.text
    uploads_id = uuid.UUID(staged.json()["id"])
    finished = await _upload(
        box.client, uploads_id, "notes.txt", b"from the box", _idem, fence=holder.fence
    )
    assert finished.status_code == 202, finished.text
    listed = await box.client.get(f"{BASE}/drives/{members_chat.drive}/items/{uploads_id}/children")
    assert listed.status_code == 200, listed.text
    (file_row,) = [row for row in listed.json()["value"] if row["name"] == "notes.txt"]
    file_id = uuid.UUID(file_row["id"])

    minted = await box.client.get(
        f"{BASE}/drives/{members_chat.drive}/items/{file_id}/content",
        headers=holder.fence,
        follow_redirects=False,
    )
    assert minted.status_code == 302, minted.text
    served = await _redeem(box.client, str(minted.headers["location"]), holder.fence)
    assert served.status_code == 200, served.text
    assert served.content == b"from the box"
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert rows, "every decision on the folder is on record"
    assert {row.payload["effect"] for row in rows} == {"allow"}
    assert {row.payload["reason"] for row in rows} == {"machine_holds_chat"}
    assert {"read", "lease", "write"} <= {row.payload["action"] for row in rows}
    for row in rows:
        assert _chain(row) == [("machine", box.machine_id)]
        assert row.actor["delegating_user"] is None


async def test_the_lease_a_box_took_on_its_session_is_the_same_lease_on_its_credential(
    members_chat: MemberChat, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """A box switching from its operator's session to its own credential is the
    same holder: the lease it took as an agent on the session it registered
    with resumes at the same epoch on the credential, so the switch loses no
    folder and fences no write of the box's own. The operator's session
    reaches only its own org's drive, so this is the org's own box: minted by
    the org's admin, serving the org."""
    own = await _box(files_org.org, tenancy="dedicated", served_org=files_org.org.org_id)
    box = Box(own.raw, own.credential_id, str(own.machine_id))
    await _bind(members_chat.chat_id, box.machine_id)
    token = await mint_cli_token(
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_team_id=files_org.org.org_id,
    )
    async with AsyncSessionLocal() as session:
        machine = await session.get(ComputeAllocation, uuid.UUID(box.machine_id))
        assert machine is not None
        machine.registered_jti = decode_session_token(token).jti
        await session.commit()
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(box.machine_id)},
    ) as on_session:
        first = MockHolder(
            on_session, members_chat.drive, members_chat.node.id, instance="box-1", machine="b"
        )
        taken = await first.take(real_session, _idem, purpose="chat")
        assert taken.status_code == 200, taken.text
    again = MockHolder(
        box.client, members_chat.drive, members_chat.node.id, instance="box-1", machine="b"
    )
    try:
        resumed = await again.take(real_session, _idem, purpose="chat")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["epoch"] == taken.json()["epoch"], "one holder, one lease, one epoch"
    finally:
        await box.client.aclose()


async def test_a_box_reads_its_chat_with_the_drive_its_folder_is_on(
    box: Box, members_chat: MemberChat
) -> None:
    """A box has no drive of its own and addresses every node by drive and
    node, so the chat it reads on its credential says which drive its folder
    is on — the served org's, never the operator's."""
    await _bind(members_chat.chat_id, box.machine_id)
    read = await box.client.get(f"/api/v1/chats/{members_chat.chat_id}")
    assert read.status_code == 200, read.text
    assert read.json()["files_node_id"] == str(members_chat.node.id)
    assert read.json()["files_drive_id"] == str(members_chat.drive)
    listed = await box.client.get("/api/v1/chats")
    assert listed.status_code == 200, listed.text
    (row,) = [item for item in listed.json()["items"] if item["id"] == members_chat.chat_id]
    assert (row["files_node_id"], row["files_drive_id"]) == (
        str(members_chat.node.id),
        str(members_chat.drive),
    )


# =========================================================================== #
# everything the box does not hold
# =========================================================================== #


async def test_a_box_is_told_nothing_is_there_for_another_boxs_chat(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """Same org, same drive, a proven box — and a chat bound to another machine.
    The read and the lease are the opaque not-found, and each refusal is on
    record under the box's chain with the reason that tells an operator it was
    the binding, not the credential."""
    await _bind(members_chat.chat_id, str(uuid.uuid4()))
    read = await box.client.get(members_chat.item)
    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE
    taken = await MockHolder(box.client, members_chat.drive, members_chat.node.id).take(
        real_session, _idem, purpose="chat"
    )
    assert taken.status_code == 404, taken.text
    assert refusal(taken) == OPAQUE
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert _effects(rows) == {
        ("read", "deny", "machine_mismatch"),
        ("lease", "deny", "machine_mismatch"),
    }
    for row in rows:
        assert _chain(row) == [("machine", box.machine_id)]


async def test_a_box_holds_no_rung_on_a_chat_no_box_has_been_bound_to(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    await _bind(members_chat.chat_id, None)
    read = await box.client.get(members_chat.item)
    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert _effects(rows) == {("read", "deny", "machine_does_not_run")}


async def test_a_box_is_told_nothing_is_there_outside_a_chat_folder(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """The member's home — the folder the chat is filed under — is not a chat's
    folder: the box is refused it opaquely, on record with the reason that
    names the boundary, however the ladder would have read it."""
    await _bind(members_chat.chat_id, box.machine_id)
    home = members_chat.node.parent_id
    assert home is not None
    read = await box.client.get(f"{BASE}/drives/{members_chat.drive}/items/{home}")
    assert read.status_code == 404, read.text
    assert refusal(read) == OPAQUE
    rows = await _decisions(files_org.org.org_id, home)
    assert _effects(rows) == {("read", "deny", "machine_outside_chat")}


async def test_a_box_may_not_share_its_own_chats_folder(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """The box can read the folder, so the refusal is the visible one — and it
    names the box, not a missing rung."""
    await _bind(members_chat.chat_id, box.machine_id)
    shared = await box.client.post(
        f"{members_chat.item}/permissions",
        json={"principal": {"kind": "user", "id": str(files_org.org.admin_id)}, "role": "reader"},
        headers={**_idem(), "If-Match": f'"{members_chat.node.etag}"'},
    )
    assert shared.status_code == 403, shared.text
    assert shared.json()["code"] == "files.forbidden"
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert ("share", "deny", "machine_may_not_share") in _effects(rows)


async def test_a_box_has_no_drive_of_its_own_and_reaches_no_drive_it_does_not_serve(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """``GET /files/drives`` names "the caller's drive", which a box has none
    of; a drive of an org the credential does not serve is the same opaque
    not-found a nonexistent drive gets, decided before any node is read, so
    nothing about that org is left on record under the box's name — and the
    drive of the org it does serve keeps answering."""
    await _bind(members_chat.chat_id, box.machine_id)
    own = await box.client.get(f"{BASE}/drives")
    assert own.status_code == 404, own.text
    assert refusal(own) == OPAQUE
    assert await _decisions(files_org.org.org_id, members_chat.node.id) == [], (
        "a box that named no chat is refused before any node is decided on"
    )

    tag = secrets.token_hex(4)
    async with AsyncSessionLocal() as session:
        foreign_org, _admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Unserved Org {tag}",
            admin_email=f"unserved-{tag}@alkera.dev",
            admin_first_name="Unserved",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        await session.commit()
    stranger = await login(app_client(), f"unserved-{tag}@alkera.dev", "pw-1234567890")
    try:
        theirs = await stranger.get(f"{BASE}/drives")
        assert theirs.status_code == 200, theirs.text
        foreign_drive = str(theirs.json()["id"])
        foreign_root = str(theirs.json()["rootId"])
    finally:
        await stranger.aclose()
    assert foreign_drive != str(members_chat.drive)

    probe = await box.client.get(f"{BASE}/drives/{foreign_drive}/items/{foreign_root}")
    assert probe.status_code == 404, probe.text
    assert refusal(probe) == OPAQUE
    missing = await box.client.get(f"{BASE}/drives/{uuid.uuid4()}/items/{members_chat.node.id}")
    assert missing.status_code == 404, missing.text
    assert refusal(missing) == OPAQUE
    assert await _decisions(foreign_org.id, uuid.UUID(foreign_root)) == [], (
        "an unserved drive is refused before any decision names it"
    )
    still = await box.client.get(members_chat.item)
    assert still.status_code == 200, still.text


# =========================================================================== #
# what is under the folder the box holds, addressed from the folder
# =========================================================================== #


async def test_a_box_is_told_its_chat_folders_bare_name_and_addresses_below_it_from_the_folder(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    """The box reads nothing above the chat folder, so the drive spells the
    folder's path as its bare name — an absolute lookup built from it names
    nothing. What is under the folder is reached from the folder, by the
    step down, and decided on the target exactly as the id form: the member's
    view of the same file is the same node."""
    await _bind(members_chat.chat_id, box.machine_id)
    folder_name = bytes(members_chat.node.name).decode()
    read = await box.client.get(members_chat.item)
    assert read.status_code == 200, read.text
    assert read.json()["pathBytes"] == folder_name, "cut to the bare name"
    owners = await members_chat.member.get(members_chat.item)
    assert owners.json()["pathBytes"].startswith("/home/"), "the owner reads the whole chain"

    holder = MockHolder(box.client, members_chat.drive, members_chat.node.id, machine="typed-name")
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    # The chat's working directory, made with the chat: reached from the
    # folder by its step, the only way the box can spell it.
    scratch = await box.client.get(f"{members_chat.item}:/scratch")
    assert scratch.status_code == 200, scratch.text
    finished = await _upload(
        box.client,
        uuid.UUID(scratch.json()["id"]),
        "answer.txt",
        b"pong",
        _idem,
        fence=holder.fence,
    )
    assert finished.status_code == 202, finished.text

    absolute = await box.client.get(
        f"{BASE}/drives/{members_chat.drive}/root:/{folder_name}/scratch/answer.txt"
    )
    assert absolute.status_code == 404, "the bare name is not a path from the root"
    below = await box.client.get(f"{members_chat.item}:/scratch/answer.txt")
    assert below.status_code == 200, below.text
    theirs = await members_chat.member.get(
        f"{BASE}/drives/{members_chat.drive}/root:/{owners.json()['pathBytes'].lstrip('/')}"
        "/scratch/answer.txt"
    )
    assert theirs.status_code == 200, theirs.text
    assert below.json()["id"] == theirs.json()["id"]
    assert below.json()["name"] == "answer.txt"
    itself = await box.client.get(f"{members_chat.item}:/")
    assert itself.status_code == 200 and itself.json()["id"] == str(members_chat.node.id)
    rows = await _decisions(files_org.org.org_id, uuid.UUID(below.json()["id"]))
    assert ("read", "allow", "machine_holds_chat") in _effects(rows)


@pytest.mark.parametrize(
    "step",
    [
        pytest.param("scratch/nope.txt", id="a-step-that-names-nothing"),
        # A literal ``..`` never reaches the server (a client collapses it
        # before sending); the encoded spelling does, and is no node's name.
        pytest.param("%2E%2E", id="a-step-that-climbs"),
        pytest.param("%2E%2E/%2E%2E", id="a-step-that-climbs-twice"),
    ],
)
async def test_a_box_learns_nothing_from_a_step_below_its_folder_that_leads_nowhere(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture, step: str
) -> None:
    await _bind(members_chat.chat_id, box.machine_id)
    home = members_chat.node.parent_id
    assert home is not None
    answered = await box.client.get(f"{members_chat.item}:/{step}")
    assert answered.status_code == 404, answered.text
    assert refusal(answered) == OPAQUE
    assert await _decisions(files_org.org.org_id, home) == [], "nothing above was decided on"


async def test_a_box_cannot_anchor_below_a_folder_it_does_not_hold(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """The anchored form is a read like any other: a step below the member's
    home — the folder the chat is filed in — is refused on the target the way
    the id form refuses the home itself, with the same reason on record."""
    await _bind(members_chat.chat_id, box.machine_id)
    home = members_chat.node.parent_id
    assert home is not None
    folder_name = bytes(members_chat.node.name).decode()
    answered = await box.client.get(
        f"{BASE}/drives/{members_chat.drive}/items/{home}:/{folder_name}"
    )
    # The step lands on the chat folder itself, which the box holds: allowed.
    assert answered.status_code == 200, answered.text
    assert answered.json()["id"] == str(members_chat.node.id)
    sibling = await box.client.get(f"{BASE}/drives/{members_chat.drive}/items/{home}:/")
    assert sibling.status_code == 404, sibling.text
    assert refusal(sibling) == OPAQUE
    rows = await _decisions(files_org.org.org_id, home)
    assert _effects(rows) == {("read", "deny", "machine_outside_chat")}


# =========================================================================== #
# what a person may drop into a running chat
# =========================================================================== #


async def test_a_member_drops_a_folder_into_the_working_directory_of_a_chat_a_box_holds(
    box: Box, members_chat: MemberChat, real_session: AsyncSession
) -> None:
    """The box holds the chat's lease, which admits other people's writes into
    the working directory and nowhere else. The composer's own first act on a
    paste — making ``uploads/`` under ``scratch/`` — is such a write: a
    member with no fence is admitted there (and the box sees the folder on its
    next drain), and is still refused a folder beside the working directory,
    which is the mount the box stands on."""
    await _bind(members_chat.chat_id, box.machine_id)
    holder = MockHolder(box.client, members_chat.drive, members_chat.node.id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    scratch = await box.client.get(f"{members_chat.item}:/scratch")
    assert scratch.status_code == 200, scratch.text

    dropped = await members_chat.member.post(
        f"{BASE}/drives/{members_chat.drive}/items/{scratch.json()['id']}/children",
        json={"name": "uploads", "kind": "folder"},
        headers=_idem(),
    )
    assert dropped.status_code == 201, dropped.text
    seen = await box.client.get(f"{members_chat.item}:/scratch/uploads")
    assert seen.status_code == 200 and seen.json()["id"] == dropped.json()["id"]

    beside = await members_chat.member.post(
        f"{members_chat.item}/children",
        json={"name": "notes", "kind": "folder"},
        headers=_idem(),
    )
    assert beside.status_code == 409, beside.text
    assert beside.json()["code"] == "files.leased"


# =========================================================================== #
# the operation behind the box's own upload
# =========================================================================== #


async def _open_and_send(
    client: AsyncClient, parent_id: uuid.UUID, name: str, payload: bytes, fence: dict[str, str]
) -> tuple[str, str]:
    """Open an upload and send its one part, stopping short of the commit.
    Returns the session id and the part checksum the commit needs."""
    opened = await client.post(
        f"{BASE}/uploads",
        json={"declaredSize": len(payload), "name": name, "parentId": str(parent_id)},
        headers={**_idem(), **fence},
    )
    assert opened.status_code == 201, opened.text
    upload_id = str(opened.json()["uploadId"])
    checksum = blake3(payload).digest().hex()
    part = await client.put(
        f"{BASE}/uploads/{upload_id}/parts/1",
        content=payload,
        headers={**_idem(), "X-Part-Checksum": checksum, **fence},
    )
    assert part.status_code == 200, part.text
    return upload_id, checksum


async def _commit(
    client: AsyncClient, upload_id: str, checksum: str, size: int, fence: dict[str, str]
) -> str:
    finished = await client.post(
        f"{BASE}/uploads/{upload_id}/complete",
        json={"parts": [{"partNo": 1, "size": size, "checksum": checksum}]},
        headers={**_idem(), **fence},
    )
    assert finished.status_code == 202, finished.text
    return str(finished.json()["id"])


async def test_a_box_follows_its_own_upload_from_the_moment_it_is_queued(
    box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit is queued and lands later; the box polls the operation in
    between. Nothing has been produced yet, so the read is decided on the
    folder the upload was opened against — the box's own chat folder — and
    answers; a stand-in the box may not read (the drive root) refused the box
    its own upload until the commit landed, and the box gave the whole push up
    as a failure."""
    from alkera_core.config import settings

    await _bind(members_chat.chat_id, box.machine_id)
    holder = MockHolder(box.client, members_chat.drive, members_chat.node.id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    monkeypatch.setattr(settings, "files_inline_operations", False)  # a worker commits it later
    upload_id, checksum = await _open_and_send(
        box.client, members_chat.node.id, "queued.txt", b"still queued", holder.fence
    )
    op_id = await _commit(box.client, upload_id, checksum, len(b"still queued"), holder.fence)

    polled = await box.client.get(f"{BASE}/drives/{members_chat.drive}/operations/{op_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["state"] == "queued"
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert ("read", "allow", "machine_holds_chat") in _effects(rows), "decided on the chat folder"
    assert all(_chain(row) == [("machine", box.machine_id)] for row in rows)


async def test_a_box_follows_its_own_upload_once_it_has_landed(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture, real_session: AsyncSession
) -> None:
    await _bind(members_chat.chat_id, box.machine_id)
    holder = MockHolder(box.client, members_chat.drive, members_chat.node.id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    upload_id, checksum = await _open_and_send(
        box.client, members_chat.node.id, "landed.txt", b"landed", holder.fence
    )
    op_id = await _commit(box.client, upload_id, checksum, len(b"landed"), holder.fence)

    polled = await box.client.get(f"{BASE}/drives/{members_chat.drive}/operations/{op_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["state"] == "done"
    landed = uuid.UUID(polled.json()["resultNodeId"])
    rows = await _decisions(files_org.org.org_id, landed)
    assert _effects(rows) == {("read", "allow", "machine_holds_chat")}, "decided on the file"


async def test_a_box_is_told_nothing_of_an_upload_into_a_folder_it_does_not_hold(
    box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member uploads into their own home; its commit is queued. The box,
    on the same drive, is decided on that home — a folder outside its chats —
    and learns nothing, on record as the boundary."""
    from alkera_core.config import settings

    await _bind(members_chat.chat_id, box.machine_id)
    home = members_chat.node.parent_id
    assert home is not None
    monkeypatch.setattr(settings, "files_inline_operations", False)
    upload_id, checksum = await _open_and_send(
        members_chat.member, home, "private.txt", b"theirs", fence={}
    )
    op_id = await _commit(members_chat.member, upload_id, checksum, len(b"theirs"), fence={})

    polled = await box.client.get(f"{BASE}/drives/{members_chat.drive}/operations/{op_id}")
    assert polled.status_code == 404, polled.text
    assert refusal(polled) == OPAQUE
    # The member's own write on the home is on record beside it; the box's
    # rows are the refusal alone.
    rows = await _decisions(files_org.org.org_id, home)
    boxs = [row for row in rows if _chain(row) == [("machine", box.machine_id)]]
    assert _effects(boxs) == {("read", "deny", "machine_outside_chat")}
    theirs = await members_chat.member.get(f"{BASE}/drives/{members_chat.drive}/operations/{op_id}")
    assert theirs.status_code == 200, "the member follows their own upload as before"


# =========================================================================== #
# the drive of a chat the box names
# =========================================================================== #


def _drives(chat_id: str) -> str:
    return f"{BASE}/drives?chatId={chat_id}"


async def test_a_box_is_answered_the_drive_of_a_chat_it_holds_and_no_home(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """A box has no drive of its own, so it names the chat: the answer is that
    chat's drive — the served org's — with no home, decided on the chat's
    folder as the machine the chat is bound to and on record as such."""
    await _bind(members_chat.chat_id, box.machine_id)
    answered = await box.client.get(_drives(members_chat.chat_id))
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["id"] == str(members_chat.drive)
    assert body["orgId"] == str(files_org.org.org_id)
    assert body["homeId"] is None, "a box has no home folder"
    assert uuid.UUID(body["rootId"])
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert _effects(rows) == {("read", "allow", "machine_holds_chat")}
    assert all(_chain(row) == [("machine", box.machine_id)] for row in rows)


async def test_a_box_asking_for_the_drive_of_another_boxs_chat_is_told_nothing_is_there(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    await _bind(members_chat.chat_id, str(uuid.uuid4()))
    answered = await box.client.get(_drives(members_chat.chat_id))
    assert answered.status_code == 404, answered.text
    assert refusal(answered) == OPAQUE
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert _effects(rows) == {("read", "deny", "machine_mismatch")}
    assert all(_chain(row) == [("machine", box.machine_id)] for row in rows)


async def test_a_box_asking_for_the_drive_of_a_chat_no_box_holds_is_told_nothing_is_there(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    await _bind(members_chat.chat_id, None)
    answered = await box.client.get(_drives(members_chat.chat_id))
    assert answered.status_code == 404, answered.text
    assert refusal(answered) == OPAQUE
    rows = await _decisions(files_org.org.org_id, members_chat.node.id)
    assert _effects(rows) == {("read", "deny", "machine_does_not_run")}


@pytest.mark.parametrize(
    "named",
    [
        pytest.param(lambda chat: str(uuid.uuid4()), id="a-chat-that-does-not-exist"),
        pytest.param(lambda chat: "not-an-id", id="a-malformed-id"),
        pytest.param(lambda chat: "", id="an-empty-id"),
    ],
)
async def test_a_box_naming_no_chat_the_drive_holds_is_told_nothing_is_there(
    box: Box,
    members_chat: MemberChat,
    files_org: FilesOrgFixture,
    named: Callable[[MemberChat], str],
) -> None:
    """No folder to decide on, so nothing is decided and nothing recorded: the
    refusal is the same opaque not-found the bare drive read gets."""
    await _bind(members_chat.chat_id, box.machine_id)
    answered = await box.client.get(_drives(named(members_chat)))
    assert answered.status_code == 404, answered.text
    assert refusal(answered) == OPAQUE
    assert await _decisions(files_org.org.org_id, members_chat.node.id) == []


async def test_a_box_asking_for_the_drive_of_a_deleted_chat_is_told_nothing_is_there(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """Deleting the chat trashes its folder; a trashed folder is no chat's
    folder any more, and the box that ran the chat learns nothing from it."""
    await _bind(members_chat.chat_id, box.machine_id)
    deleted = await members_chat.member.delete(f"/api/v1/chats/{members_chat.chat_id}")
    assert deleted.status_code in (200, 204), deleted.text
    answered = await box.client.get(_drives(members_chat.chat_id))
    assert answered.status_code == 404, answered.text
    assert refusal(answered) == OPAQUE


async def test_a_box_asking_for_the_drive_of_a_chat_in_an_org_it_does_not_serve_leaves_no_trace(
    box: Box, members_chat: MemberChat
) -> None:
    """A chat in an org the credential does not serve is refused before its
    folder is read, so no decision names that org's node under the box."""
    await _bind(members_chat.chat_id, box.machine_id)
    tag = secrets.token_hex(4)
    async with AsyncSessionLocal() as session:
        foreign_org, foreign_admin = await team_service.create_org_with_admin(
            session,
            org_name=f"Unserved Org {tag}",
            admin_email=f"unserved-{tag}@alkera.dev",
            admin_first_name="Unserved",
            admin_last_name="Admin",
            admin_password="pw-1234567890",
        )
        foreign_admin.email_verified_at = datetime.now(UTC)
        await session.commit()
    stranger = await login(app_client(), f"unserved-{tag}@alkera.dev", "pw-1234567890")
    try:
        made = await stranger.post(
            "/api/v1/chats", json={"title": "Theirs", "clientId": secrets.token_hex(8)}
        )
        assert made.status_code == 201, made.text
        theirs = made.json()
    finally:
        await stranger.aclose()
    assert theirs["files_node_id"], "the foreign chat has a folder to be refused on"
    await _bind(theirs["id"], box.machine_id)  # even bound to this very machine
    answered = await box.client.get(_drives(theirs["id"]))
    assert answered.status_code == 404, answered.text
    assert refusal(answered) == OPAQUE
    assert await _decisions(foreign_org.id, uuid.UUID(theirs["files_node_id"])) == []


async def test_a_person_naming_a_chat_is_still_answered_their_own_drive_and_home(
    box: Box, members_chat: MemberChat, files_org: FilesOrgFixture
) -> None:
    """The chat parameter is a box's: a person's drive is their credential's,
    home and all, whatever chat they name — a colleague's, a stranger's, or
    nothing that exists."""
    await _bind(members_chat.chat_id, box.machine_id)
    admin = await login(app_client(), files_org.org.admin_email, files_org.org.admin_password)
    try:
        bare = await admin.get(f"{BASE}/drives")
        assert bare.status_code == 200, bare.text
        for named in (members_chat.chat_id, str(uuid.uuid4()), "not-an-id"):
            with_chat = await admin.get(_drives(named))
            assert with_chat.status_code == 200, with_chat.text
            assert with_chat.json() == bare.json()
        assert bare.json()["homeId"] is not None
    finally:
        await admin.aclose()


@pytest.mark.usefixtures("content_on")
async def test_a_revoked_credential_is_refused_at_the_files_door_and_at_the_origin(
    box: Box, members_chat: MemberChat, real_session: AsyncSession
) -> None:
    """A revoke reaches every door: the item route answers the credential
    refusal, and a content URL minted a moment before is not served after —
    the origin re-asks the credential's standing before a byte leaves."""
    await _bind(members_chat.chat_id, box.machine_id)
    holder = MockHolder(box.client, members_chat.drive, members_chat.node.id)
    assert (await holder.take(real_session, _idem, purpose="chat")).status_code == 200
    finished = await _upload(
        box.client, members_chat.node.id, "before.txt", b"minted before", _idem, fence=holder.fence
    )
    assert finished.status_code == 202, finished.text
    listed = await box.client.get(f"{members_chat.item}/children")
    (file_row,) = [row for row in listed.json()["value"] if row["name"] == "before.txt"]
    minted = await box.client.get(
        f"{BASE}/drives/{members_chat.drive}/items/{file_row['id']}/content",
        headers=holder.fence,
        follow_redirects=False,
    )
    assert minted.status_code == 302, minted.text

    credential = await real_session.get(MachineCredential, box.credential_id)
    assert credential is not None
    credential.revoked_at = datetime.now(UTC)
    await real_session.commit()

    refused = await box.client.get(members_chat.item)
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"]["code"] == REFUSED
    served = await _redeem(box.client, str(minted.headers["location"]), holder.fence)
    assert served.status_code == 404, served.text
