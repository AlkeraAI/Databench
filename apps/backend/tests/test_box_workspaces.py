"""What a box that runs a workspace's chats in one sandbox reads and says.

Through the real routes, both sides of the compatibility line:

* a chat in a workspace that owns a folder reads the folder a box leases and
  the shared tree it roots at, and a chat in a workspace of one reads
  ``adopted`` with neither, so a box serves it exactly as before;
* a box that says it can run workspaces records it on its heartbeat, and a
  box that says nothing leaves the row as it was;
* the box's report on a workspace's sandbox lands on the workspace and is
  what the workspace reads back, while a report without it (a box on the
  previous build) leaves the workspace derived from its chats;
* a second chat in a workspace is placed on the box its sibling is on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import WorkspaceObject
from alkera_core.models.compute import ComputeAllocation
from alkera_core.schemas.objects import WorkspaceSpec
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, mint_cli_token

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("files_on"),
    pytest.mark.compute_rows,
]

CHATS = "/api/v1/chats"
WORKSPACES = "/api/v1/workspaces"


@pytest.fixture
def multi_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    yield


@pytest.fixture(autouse=True)
def _ready_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box that beat once stays ``ready`` for the length of a case."""
    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)


def _browser() -> AsyncClient:
    return app_client(base_url="http://testserver")


_device_tokens: dict[UUID, str] = {}


async def _daemon_headers(org: OrgWithAdmin, agent_id: str) -> dict[str, str]:
    """The box's one device token plus its agent assertion: a machine is
    verified only on the credential that registered it, so the token is
    minted once per operator, the way ``alkera login`` leaves one."""
    token = _device_tokens.get(org.admin_id)
    if token is None:
        token = await mint_cli_token(
            user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
        )
        _device_tokens[org.admin_id] = token
    return {"Authorization": f"Bearer {token}", **agent_headers(agent_id)}


async def _box(real_session: AsyncSession, org: OrgWithAdmin, pod: str) -> str:
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org.org_id, machine_type_id=machine_type.id)
    async with _browser() as daemon:
        registered = await daemon.post(
            "/api/v1/machines/register",
            json={
                "provider": "runpod",
                "provider_pod_id": pod,
                "name": "workspace-box",
                "machine_type_code": machine_type.provider_type_id,
            },
            headers=await _daemon_headers(org, "registering"),
        )
    assert registered.status_code in (200, 201), registered.text
    return str(registered.json()["id"])


async def _beat(org: OrgWithAdmin, machine_id: str, body: dict[str, Any] | None = None) -> None:
    async with _browser() as daemon:
        beat = await daemon.post(
            f"/api/v1/machines/{machine_id}/heartbeat",
            json=body,
            headers=await _daemon_headers(org, "registering"),
        )
    assert beat.status_code == 204, beat.text


async def _capabilities(machine_id: str) -> list[str] | None:
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, UUID(machine_id))
        assert alloc is not None
        return alloc.capabilities_json


async def _workspace_spec(workspace_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, UUID(workspace_id))
        assert row is not None
        return dict(row.spec or {})


async def _register(org: OrgWithAdmin, pod: str, code: str, instance: str | None) -> str:
    async with _browser() as daemon:
        registered = await daemon.post(
            "/api/v1/machines/register",
            json={
                "provider": "runpod",
                "provider_pod_id": pod,
                "name": "workspace-box",
                "machine_type_code": code,
                **({"daemon_instance_id": instance} if instance else {}),
            },
            headers=await _daemon_headers(org, "registering"),
        )
    assert registered.status_code in (200, 201), registered.text
    return str(registered.json()["id"])


@pytest.mark.parametrize(
    ("rolled_back_instance", "id_"),
    [
        pytest.param("old-build-process", "a-new-process", id="a-new-process"),
        pytest.param(None, "an-older-daemon-naming-none", id="an-older-daemon-naming-none"),
    ],
)
async def test_capabilities_do_not_survive_a_box_rolled_back_to_an_older_build(
    real_session: AsyncSession, org_admin: OrgWithAdmin, rolled_back_instance: str | None, id_: str
) -> None:
    """A box says on every beat what its build can do. Rolled back to a build
    that can do less, it registers as a new process and beats without the
    list: what the newer build said must not outlive it, or placement binds
    it shared-workspace chats it would serve without their files. The same
    process registering again (a reconnect) keeps what it said."""
    pod = f"pod-caps-{id_}"
    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    code = machine_type.provider_type_id
    machine_id = await _register(org_admin, pod, code, "new-build-process")
    assert await _capabilities(machine_id) is None

    await _beat(
        org_admin,
        machine_id,
        {"daemon_instance_id": "new-build-process", "capabilities": ["workspaces", "workspaces"]},
    )
    assert await _capabilities(machine_id) == ["workspaces"]

    # A reconnect of the same process keeps what it said.
    await _register(org_admin, pod, code, "new-build-process")
    assert await _capabilities(machine_id) == ["workspaces"]

    # Rolled back: the older process registers, and nothing it has not said stands.
    await _register(org_admin, pod, code, rolled_back_instance)
    assert await _capabilities(machine_id) is None
    await _beat(
        org_admin,
        machine_id,
        {"daemon_instance_id": rolled_back_instance}
        if rolled_back_instance
        else {"chats_served": 0},
    )
    assert await _capabilities(machine_id) is None


async def test_a_beat_that_names_no_capabilities_clears_them(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id = await _box(real_session, org_admin, "pod-caps-beat")
    await _beat(org_admin, machine_id, {"capabilities": ["workspaces"]})
    assert await _capabilities(machine_id) == ["workspaces"]

    await _beat(org_admin, machine_id, {"chats_served": 0})
    assert await _capabilities(machine_id) is None

    await _beat(org_admin, machine_id, {"capabilities": []})
    assert await _capabilities(machine_id) == []


async def test_a_chat_in_a_workspace_of_one_reads_adopted_and_names_no_workspace_folder(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    created = await client.post(CHATS, json={"title": "On its own"})
    assert created.status_code == 201, created.text
    chat = (await client.get(f"{CHATS}/{created.json()['id']}")).json()

    assert chat["workspace_layout"] == "adopted"
    assert chat["workspace_node_id"] is None
    assert chat["workspace_files_node_id"] is None
    # The folder a box takes is the chat's own, as it always was.
    assert chat["files_node_id"] is not None


@pytest.mark.usefixtures("multi_chat")
async def test_chats_of_one_workspace_read_its_folder_and_its_shared_tree(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = (await client.post(WORKSPACES, json={"title": "Pricing"})).json()
    one = (await client.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})).json()
    two = (await client.post(CHATS, json={"title": "Two", "workspace_id": workspace["id"]})).json()

    listed = {item["id"]: item for item in (await client.get(CHATS)).json()["items"]}
    for chat in (one, two):
        for read in (chat, (await client.get(f"{CHATS}/{chat['id']}")).json(), listed[chat["id"]]):
            assert read["workspace_layout"] == "native"
            assert read["workspace_node_id"] == workspace["files_node_id"]
            assert read["workspace_files_node_id"] == workspace["working_node_id"]
            # Each chat keeps its own records folder, under the workspace.
            assert read["files_node_id"] not in (None, workspace["files_node_id"])
    assert one["files_node_id"] != two["files_node_id"]


@pytest.mark.usefixtures("multi_chat")
async def test_the_boxs_report_on_a_sandbox_is_what_the_workspace_reads(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine_id = await _box(real_session, org_admin, "pod-report-1")
    await _beat(org_admin, machine_id, {"capabilities": ["workspaces"]})
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        workspace = (await browser.post(WORKSPACES, json={"title": "Report"})).json()
        one = (
            await browser.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})
        ).json()
        two = (
            await browser.post(CHATS, json={"title": "Two", "workspace_id": workspace["id"]})
        ).json()
        assert one["machine_id"] == two["machine_id"] == machine_id

        async with _browser() as daemon:
            headers = await _daemon_headers(org_admin, machine_id)
            # The old box's report: publishing, nothing about a workspace.
            plain = await daemon.put(
                f"{CHATS}/{one['id']}/publisher-state",
                json={"state": "publishing"},
                headers=headers,
            )
            assert plain.status_code == 200, plain.text
            spec = await _workspace_spec(workspace["id"])
            assert spec.get("binding_authority", "chat") == "chat"
            assert spec.get("sandbox_state") is None

            # Chat one's agent server stops while the sandbox stays up for two.
            asleep = await daemon.put(
                f"{CHATS}/{one['id']}/publisher-state",
                json={
                    "state": "asleep",
                    "ending": "idle",
                    "workspace_sandbox": "awake",
                    "workspace_memory_mb": 512,
                },
                headers=headers,
            )
            assert asleep.status_code == 200, asleep.text

        read = (await browser.get(f"{WORKSPACES}/{workspace['id']}")).json()
        assert read["sandbox_state"] == "awake"
        assert read["sandbox_memory_used_mb"] == 512
        assert read["mirror_state"] == "awake"
        assert read["machine_id"] == machine_id
        assert (await browser.get(f"{CHATS}/{one['id']}")).status_code == 200
        spec = await _workspace_spec(workspace["id"])
        assert spec["binding_authority"] == "workspace"
        # The writer stamps the version it writes: the current one.
        assert spec["schema_version"] == WorkspaceSpec.SCHEMA_VERSION

        async with _browser() as daemon:
            down = await daemon.put(
                f"{CHATS}/{two['id']}/publisher-state",
                json={"state": "asleep", "ending": "idle", "workspace_sandbox": "asleep"},
                headers=await _daemon_headers(org_admin, machine_id),
            )
            assert down.status_code == 200, down.text
        read = (await browser.get(f"{WORKSPACES}/{workspace['id']}")).json()
        assert read["sandbox_state"] == "asleep"
        assert read["mirror_state"] == "asleep"


async def test_a_report_about_a_workspace_of_one_writes_nothing_to_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A workspace of one is its chat: the chat's own spec says everything,
    and a second copy on the workspace is what drifts."""
    machine_id = await _box(real_session, org_admin, "pod-report-2")
    await _beat(org_admin, machine_id)
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        chat = (await browser.post(CHATS, json={"title": "Alone"})).json()
    async with _browser() as daemon:
        said = await daemon.put(
            f"{CHATS}/{chat['id']}/publisher-state",
            json={"state": "publishing", "workspace_sandbox": "awake"},
            headers=await _daemon_headers(org_admin, machine_id),
        )
    assert said.status_code == 200, said.text
    spec = await _workspace_spec(chat["workspace_id"])
    assert spec.get("binding_authority", "chat") == "chat"
    assert spec.get("sandbox_state") is None


@pytest.mark.usefixtures("multi_chat")
async def test_a_person_cannot_report_a_workspaces_sandbox(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = (await client.post(WORKSPACES, json={"title": "Not yours to say"})).json()
    chat = (await client.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})).json()
    refused = await client.put(
        f"{CHATS}/{chat['id']}/publisher-state",
        json={"state": "publishing", "workspace_sandbox": "awake"},
    )
    assert refused.status_code in (403, 404), refused.text
    assert (await _workspace_spec(workspace["id"])).get("sandbox_state") is None


@pytest.mark.usefixtures("multi_chat")
async def test_a_shared_workspace_chat_is_refused_on_a_box_that_cannot_run_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The org's only box is on the previous build: a chat in a shared
    workspace would run there without the workspace's files, so it is refused
    with a reason, and served once the box says it can."""
    machine_id = await _box(real_session, org_admin, "pod-old-build")
    await _beat(org_admin, machine_id)
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        workspace = (await browser.post(WORKSPACES, json={"title": "Waiting"})).json()

        refused = await browser.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "workspace_box_unsupported"

        await _beat(org_admin, machine_id, {"capabilities": ["workspaces"]})
        placed = await browser.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})
        assert placed.status_code == 201, placed.text
        assert placed.json()["machine_id"] == machine_id


@pytest.mark.usefixtures("multi_chat")
async def test_a_box_hears_a_drop_into_the_shared_tree_of_a_workspace_it_serves(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A drop into a workspace's ``files/`` names the workspace's folder as its
    lease root; the box serving a chat of that workspace must hear it, or the
    file waits on disk until a turn starts. A box serving no chat of it does
    not hear it."""
    from alkera_core.authz import ActingContext as Ctx
    from alkera_core.events import EventType, HubEvent
    from backend.services.realtime.filters import load_machine_scope, machine_visible_to

    machine_id = await _box(real_session, org_admin, "pod-scope-1")
    await _beat(org_admin, machine_id, {"capabilities": ["workspaces"]})
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        workspace = (await browser.post(WORKSPACES, json={"title": "Heard"})).json()
        chat = (
            await browser.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})
        ).json()
    assert chat["machine_id"] == machine_id

    def drop(org: UUID) -> HubEvent:
        return HubEvent(
            lane="durable",
            org_id=org,
            type=EventType.FILE_LEASE_CHANGED.value,
            entity="file_lease",
            entity_id=workspace["files_node_id"],
            version=1,
            visibility="org",
            payload={"lease_node_id": workspace["files_node_id"], "reason": "inbound"},
            id=1,
            channel=None,
        )

    def box(machine: UUID) -> Ctx:
        return Ctx.for_machine(
            machine_id=machine,
            credential_id=machine,
            org_id=org_admin.org_id,
            label="box",
            served_org_ids=frozenset({org_admin.org_id}),
        )

    async with AsyncSessionLocal() as db:
        serving = await load_machine_scope(db, box(UUID(machine_id)))
        stranger = await load_machine_scope(db, box(UUID(int=7)))
    assert workspace["files_node_id"] in serving.node_ids
    assert machine_visible_to(drop(org_admin.org_id), scope=serving) is True
    assert machine_visible_to(drop(org_admin.org_id), scope=stranger) is False


@pytest.mark.usefixtures("multi_chat")
async def test_deleting_a_workspace_rings_each_chats_doorbell_with_the_deletion(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box that served a chat cannot tell a deleted chat from one it lost
    its sight of: both read not-found. The doorbell that ends each chat of a
    deleted workspace says ``reason: deleted`` on the frame the box receives,
    so the box leaves nothing of the chat or the shared tree on its disk. An
    ordinary edit's doorbell says no such thing."""
    from alkera_core.events import CHAT_DELETED_REASON
    from alkera_core.events.hub import HubEvent
    from alkera_core.models import EventOutbox
    from backend.services.realtime.sse import event_frame
    from sqlalchemy import select

    await login(client, org_admin.admin_email, org_admin.admin_password)
    workspace = (await client.post(WORKSPACES, json={"title": "Doomed"})).json()
    chat = (await client.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})).json()
    edited = await client.put(f"{CHATS}/{chat['id']}/permission-mode", json={"mode": "plan"})
    assert edited.status_code == 200, edited.text

    assert (await client.delete(f"{WORKSPACES}/{workspace['id']}")).status_code == 204

    rows = (
        (
            await real_session.execute(
                select(EventOutbox)
                .where(EventOutbox.entity_id == chat["id"], EventOutbox.type == "chat.updated")
                .order_by(EventOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    reasons = [row.payload.get("reason") for row in rows]
    assert reasons[-1] == CHAT_DELETED_REASON
    assert CHAT_DELETED_REASON not in reasons[:-1], "only the deletion says it"
    last = rows[-1]
    frame = event_frame(
        HubEvent(
            lane="durable",
            org_id=last.org_id,
            type=last.type,
            entity=last.entity,
            entity_id=last.entity_id,
            version=last.version,
            visibility="org",
            payload=last.payload,
            id=last.id,
            channel=None,
        )
    )
    assert f'"reason":"{CHAT_DELETED_REASON}"' in frame


@pytest.mark.usefixtures("multi_chat")
async def test_a_second_chat_starts_while_a_sibling_is_awake_on_the_box(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The box holds the workspace's folder and its first chat's records while
    that chat answers. A person starting a second chat there has the server
    create the new chat's record folder under ``.chats/``: the workspace lease
    covers its folder and shared tree, not a sibling-to-be's records, so the
    chat starts at once, bound to the same box. A write into the awake
    chat's own records stays refused."""
    import uuid as uuid_module

    from tests.files._files_kit import node_etag

    machine_id = await _box(real_session, org_admin, "pod-second-chat")
    await _beat(org_admin, machine_id, {"capabilities": ["workspaces"]})
    async with _browser() as browser:
        await login(browser, org_admin.admin_email, org_admin.admin_password)
        workspace = (await browser.post(WORKSPACES, json={"title": "Busy"})).json()
        first = (
            await browser.post(CHATS, json={"title": "One", "workspace_id": workspace["id"]})
        ).json()
        drive = workspace["files_drive_id"]

        async with _browser() as daemon:
            daemon.headers.update(await _daemon_headers(org_admin, machine_id))
            for node, purpose, instance in (
                (workspace["files_node_id"], "workspace", f"{machine_id}:ws:{workspace['id']}"),
                (first["files_node_id"], "chat", f"{machine_id}:{first['id']}"),
            ):
                taken = await daemon.post(
                    f"/api/v1/files/drives/{drive}/items/{node}/lease",
                    json={"instanceId": instance, "machineId": machine_id, "purpose": purpose},
                    headers={
                        "Idempotency-Key": uuid_module.uuid4().hex,
                        "If-Match": await node_etag(real_session, UUID(node)),
                    },
                )
                assert taken.status_code == 200, taken.text

        second = await browser.post(CHATS, json={"title": "Two", "workspace_id": workspace["id"]})
        assert second.status_code == 201, second.text
        assert second.json()["machine_id"] == machine_id
        assert second.json()["workspace_node_id"] == workspace["files_node_id"]

        forged = await browser.post(
            f"/api/v1/files/drives/{drive}/items/{first['files_node_id']}/children",
            json={"name": "forged", "kind": "folder"},
            headers={"Idempotency-Key": uuid_module.uuid4().hex},
        )
        assert forged.status_code == 409, forged.text
