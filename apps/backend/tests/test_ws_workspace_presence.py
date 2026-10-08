"""Who is in a workspace, over the socket: ``doc:workspace:<id>`` is a
presence-only channel decided by the workspace's own READ answer.

A real server, real ``websockets`` clients and real Postgres, as in the
gateway suite. The owner and a colleague the workspace's folder was shared
with both hold the channel and see each other join and leave; a colleague it
was never shared with, and a member of another org, are refused with the same
``not_found`` an id nobody uses gets; and no document rides the channel.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.models import User
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, make_member
from tests.test_ws_gateway import connect, logged_in

pytestmark = [pytest.mark.compute_rows, pytest.mark.xdist_group("ws_gateway")]


@pytest.fixture(autouse=True)
def _project_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """Project workspaces are made only where a workspace may hold several chats."""
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)


async def _workspace(client: AsyncClient, title: str = "Q4 forecast") -> dict[str, Any]:
    resp = await client.post("/api/v1/workspaces", json={"title": title})
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    assert body["files_node_id"], "the test needs the workspace's folder to share"
    return body


async def _etag(node_id: str) -> str:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, UUID(node_id))
        assert node is not None
        return str(node.etag)


async def _share(client: AsyncClient, workspace: dict[str, Any], member: User) -> str:
    """Share the workspace's folder the way the share dialog does; the share's id."""
    etag = await _etag(workspace["files_node_id"])
    granted = await client.post(
        f"/api/v1/files/drives/{workspace['files_drive_id']}/items/"
        f"{workspace['files_node_id']}/permissions",
        json={"principal": {"kind": "user", "id": str(member.id)}, "role": ROLE_READER},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(etag)},
    )
    assert granted.status_code == 201, granted.text
    share_id: str = granted.json()["id"]
    return share_id


async def _revoke(client: AsyncClient, workspace: dict[str, Any], share_id: str) -> None:
    """Take the share back the way the share dialog does."""
    revoked = await client.delete(
        f"/api/v1/files/drives/{workspace['files_drive_id']}/items/"
        f"{workspace['files_node_id']}/permissions/{share_id}",
        headers={
            "Idempotency-Key": uuid.uuid4().hex,
            "If-Match": await _etag(workspace["files_node_id"]),
        },
    )
    assert revoked.status_code == 204, revoked.text


async def _member(real_session: AsyncSession, org_id: UUID) -> tuple[User, AsyncClient]:
    member, password = await make_member(real_session, org_id=org_id, verified=True)
    assert password is not None
    return member, await logged_in(member.email, password)


@pytest.mark.usefixtures("files_on")
async def test_the_owner_and_a_colleague_it_was_shared_with_see_each_other_in_the_workspace(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(owner_client)
    member, member_client = await _member(real_session, org_admin.org_id)
    await _share(owner_client, workspace, member)
    channel = f"doc:workspace:{workspace['id']}"
    async with (
        connect(uvicorn_server, owner_client) as owner,
        connect(uvicorn_server, member_client) as colleague,
    ):
        # Presence only: nobody is told they may write to it.
        assert await owner.subscribe(channel) is False
        assert await colleague.subscribe(channel) is False
        await owner.send({"t": "presence.join", "channel": channel})
        joined = await colleague.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")
        assert joined["channel"] == channel
        assert joined["peers"][0]["peer_id"] == owner.peer_id
        assert joined["peers"][0]["user_id"] == str(org_admin.admin_id)
        # Someone arriving later is handed the roster with the owner on it.
        async with connect(uvicorn_server, member_client) as late:
            await late.send({"t": "subscribe", "channel": channel})
            assert (await late.recv_until(lambda f: f["t"] == "subscribed"))["channel"] == channel
            roster = await late.recv_until(lambda f: f["t"] == "presence")
            assert roster["event"] == "roster"
            assert [p["peer_id"] for p in roster["peers"]] == [owner.peer_id]
        await owner.send({"t": "presence.leave", "channel": channel})
        left = await colleague.recv_until(lambda f: f["t"] == "presence" and f["event"] == "leave")
        assert left["peers"][0]["peer_id"] == owner.peer_id
    await owner_client.aclose()
    await member_client.aclose()


@pytest.mark.usefixtures("files_on")
async def test_revoking_the_share_ends_the_colleagues_presence_in_the_workspace_at_once(
    uvicorn_server: str, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A colleague whose share is taken back leaves the workspace's roster the
    moment it is revoked, on the open socket, with no reconnect: the owner is
    told they left, and the colleague's subscription is ended with the answer
    a fresh subscribe would now get."""
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(owner_client)
    member, member_client = await _member(real_session, org_admin.org_id)
    share_id = await _share(owner_client, workspace, member)
    channel = f"doc:workspace:{workspace['id']}"
    async with (
        connect(uvicorn_server, owner_client) as owner,
        connect(uvicorn_server, member_client) as colleague,
    ):
        await owner.subscribe(channel)
        await colleague.subscribe(channel)
        await colleague.send({"t": "presence.join", "channel": channel})
        await owner.recv_until(lambda f: f["t"] == "presence" and f["event"] == "join")

        await _revoke(owner_client, workspace, share_id)

        left = await owner.recv_until(lambda f: f["t"] == "presence" and f["event"] == "leave")
        assert left["channel"] == channel
        assert left["peers"][0]["peer_id"] == colleague.peer_id
        ended = await colleague.recv_until(
            lambda f: f["t"] == "error" and f.get("channel") == channel
        )
        assert ended["code"] == "not_found"
        # And it stays ended: a fresh subscribe is refused the same way.
        assert (await colleague.subscribe_error(channel))["code"] == "not_found"
    await owner_client.aclose()
    await member_client.aclose()


@pytest.mark.usefixtures("files_on")
async def test_a_workspace_nobody_shared_is_the_same_not_found_as_one_that_does_not_exist(
    uvicorn_server: str,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_support: OrgWithAdmin,
) -> None:
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(owner_client)
    _, colleague_client = await _member(real_session, org_admin.org_id)
    stranger_client = await logged_in(platform_support.admin_email, platform_support.admin_password)
    channel = f"doc:workspace:{workspace['id']}"
    nobody = f"doc:workspace:{uuid.uuid4()}"
    async with connect(uvicorn_server, colleague_client) as colleague:
        refused = await colleague.subscribe_error(channel)
        unknown = await colleague.subscribe_error(nobody)
    async with connect(uvicorn_server, stranger_client) as stranger:
        foreign = await stranger.subscribe_error(channel)
    for frame in (refused, unknown, foreign):
        assert frame["code"] == "not_found"
    # The answer is word for word the same: the subscribe is no oracle for
    # whether a workspace exists.
    same = {k: v for k, v in refused.items() if k != "channel"}
    assert same == {k: v for k, v in unknown.items() if k != "channel"}
    assert same == {k: v for k, v in foreign.items() if k != "channel"}
    for c in (owner_client, colleague_client, stranger_client):
        await c.aclose()


@pytest.mark.usefixtures("files_on")
async def test_no_document_rides_a_workspace_channel(
    uvicorn_server: str, org_admin: OrgWithAdmin
) -> None:
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    workspace = await _workspace(owner_client)
    channel = f"doc:workspace:{workspace['id']}"
    async with connect(uvicorn_server, owner_client) as owner:
        await owner.subscribe(channel)
        await owner.send(owner.envelope(channel, "hello", epoch=0, payload={}))
        answer = await owner.recv_until(lambda f: f["t"] in ("error", "doc"))
        # Refused in band, never answered with a snapshot.
        assert answer["t"] == "error" or answer["envelope"]["kind"] == "error", answer
        await owner.send({"t": "ping"})
        assert (await owner.recv_until(lambda f: f["t"] == "pong"))["t"] == "pong"
    await owner_client.aclose()


@pytest.mark.parametrize(
    "channel",
    [
        pytest.param("doc:workspace:not-a-uuid", id="not-an-id"),
        pytest.param("doc:workspace:", id="empty"),
    ],
)
async def test_a_workspace_channel_that_cannot_name_a_workspace_is_refused(
    uvicorn_server: str, org_admin: OrgWithAdmin, channel: str
) -> None:
    owner_client = await logged_in(org_admin.admin_email, org_admin.admin_password)
    async with connect(uvicorn_server, owner_client) as owner:
        frame = await owner.subscribe_error(channel)
        assert frame["code"] in ("bad_channel", "not_found")
    await owner_client.aclose()


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("AAAAAAAA-0000-4000-8000-000000000001", id="upper-case"),
        pytest.param("Aaaaaaaa-0000-4000-8000-000000000001", id="mixed-case"),
        pytest.param("aaaaaaa-a0000-4000-8000-000000000001", id="misplaced-hyphens"),
    ],
)
def test_a_workspace_id_in_another_spelling_is_refused_not_respelled(spelling: str) -> None:
    """A channel is a presence key: respelling an id would let two spellings of
    one workspace name the same room under different keys. Refused instead,
    like every other document type's id is matched only as it is spelled."""
    from backend.services.realtime.channels import ChannelError, parse_channel

    with pytest.raises(ChannelError) as refused:
        parse_channel(f"doc:workspace:{spelling}")
    assert refused.value.code == "bad_channel"
    assert "workspace id" in refused.value.message


def test_a_workspace_id_in_its_one_spelling_parses_as_it_is() -> None:
    from backend.services.realtime.channels import WorkspaceChannel, parse_channel

    raw = "aaaaaaaa-0000-4000-8000-000000000001"
    channel = parse_channel(f"doc:workspace:{raw}")
    assert isinstance(channel, WorkspaceChannel)
    assert channel.key == f"doc:workspace:{raw}"
