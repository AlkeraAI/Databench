"""A trash over a folder a machine holds waits for what the machine holds.

A trash ends the leases inside what it trashes, and every write their machines
send afterwards is fenced, so whatever a box wrote and had not pushed yet used
to go nowhere: the trash took the drive's copy, and a restore brought back a
tree missing the box's last work. The trash now asks each machine holding a
folder at or under it to push first, over the real machine channel, and lands
once each says it has (or the bound passes).

Everything here is the real thing: a registered box holding
``/Shared/team/project`` live through the holder's routes, its socket holding
``machine:<its id>`` on a served app, the trash and the restore through their
routes. The box's side of the socket is driven by the test: it pushes through
the fenced content route exactly as the box's checkpoint push does.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio
from _promote_world import FRAME_BACKSTOP, World, reported
from alkera_core.authz import agent_headers
from alkera_core.config import settings
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import app_client
from tests.files._boxes import registered_box
from tests.files._live_holder import PREFIX, MockHolder

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("files_on"),
    # A served app and a real socket per case, in series on one worker, as the
    # content promotion cases run.
    pytest.mark.xdist_group("content_promote"),
]

AGREED = b"what the drive and the box last agreed\n"
UNSENT = b"what the agent wrote and the box had not pushed\n"


@pytest_asyncio.fixture
async def world(
    uvicorn_server: str,
    files_client: AsyncClient,
    fx: Any,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
    files_org: Any,
) -> tuple[World, uuid.UUID]:
    """``/Shared/team/project`` held live by the box, with ``notes.md`` on the
    drive at the bytes the two agreed; answers the world and ``team``."""
    drive = await fx.drive()
    drive.quota_bytes = settings.files_quota_default_bytes
    drive.quota_nodes = settings.files_quota_default_nodes
    await real_session.commit()
    team = await fx.node(b"team", kind="folder", parent=await fx.shared())
    project = await fx.node(b"project", kind="folder", parent=team)
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    holder = MockHolder(files_client, drive.id, project.id, machine=machine_id)
    taken = await holder.take(real_session, idem, purpose="mount", live=True)
    assert taken.status_code == 200, taken.text
    built = World(
        drive_id=drive.id,
        project_id=project.id,
        holder=holder,
        machine_id=machine_id,
        box_http=app_client(
            headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)}
        ),
        session=real_session,
        idem=idem,
    )
    await built.report(reported("notes.md", AGREED))
    await built.land(await built.node_at(b"notes.md"), AGREED)
    return built, team.id


async def _trash(client: AsyncClient, world: World, node_id: uuid.UUID) -> Any:
    etag = await world.session.scalar(
        text("SELECT etag FROM file_nodes WHERE id = :node"), {"node": node_id}
    )
    await world.session.commit()
    return await client.delete(
        f"{PREFIX}/drives/{world.drive_id}/items/{node_id}",
        headers={**world.idem(), "If-Match": str(etag)},
    )


async def _restore(client: AsyncClient, world: World, node_id: uuid.UUID) -> None:
    op_id = await world.session.scalar(
        text("SELECT trash_op_id FROM file_nodes WHERE id = :node"), {"node": node_id}
    )
    await world.session.commit()
    assert op_id is not None, "the folder was not trashed"
    restored = await client.post(
        f"{PREFIX}/drives/{world.drive_id}/trash/{op_id}/restore",
        json={},
        headers={**world.idem(), "If-Match": "0"},
    )
    assert restored.status_code in (200, 202), restored.text


async def _notes_size(world: World) -> int:
    size = await world.session.scalar(
        text(
            "SELECT v.size_bytes FROM file_nodes n JOIN file_versions v "
            "ON v.id = n.head_version_id WHERE n.drive_id = :drive AND n.name = :name "
            "AND n.trashed_at IS NULL"
        ),
        {"drive": world.drive_id, "name": b"notes.md"},
    )
    await world.session.commit()
    return int(size)


async def test_a_trash_above_a_held_folder_waits_for_the_box_and_a_restore_has_its_edit(
    uvicorn_server: str, world: tuple[World, uuid.UUID], files_client: AsyncClient
) -> None:
    """The box holds an edit to ``notes.md`` it has not pushed when a person
    trashes the folder above the one it holds. The trash asks the box to
    flush, the box pushes the edit and says so, and only then does the trash
    land. Restored, the folder holds the box's edit."""
    held, team = world
    notes = await held.node_at(b"notes.md")
    async with held.box(uvicorn_server) as box:
        trashing = asyncio.create_task(_trash(files_client, held, team))
        request = await box.request()
        assert (request["kind"], request["lease_node_id"], request["epoch"]) == (
            "flush",
            str(held.project_id),
            held.holder.epoch,
        )
        await box.ack(request, "accepted")
        await asyncio.sleep(0.2)
        assert not trashing.done(), "the trash landed before the box had pushed"
        await held.land(notes, UNSENT)
        await box.ack(request, "flushed")
        trashed = await asyncio.wait_for(trashing, FRAME_BACKSTOP)
    assert trashed.status_code == 200, trashed.text

    await _restore(files_client, held, team)
    assert await _notes_size(held) == len(UNSENT)


async def test_a_box_that_never_answers_delays_the_trash_by_the_bound_and_no_more(
    uvicorn_server: str,
    world: tuple[World, uuid.UUID],
    files_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trash is never refused for a machine: one that does not answer
    costs the ack window, and the folder goes to the trash as it was."""
    monkeypatch.setattr(settings, "files_promote_ack_seconds", 0.5)
    held, team = world
    async with held.box(uvicorn_server) as box:
        trashing = asyncio.create_task(_trash(files_client, held, team))
        assert (await box.request())["kind"] == "flush"
        trashed = await asyncio.wait_for(trashing, FRAME_BACKSTOP)
    assert trashed.status_code == 200, trashed.text
    await _restore(files_client, held, team)
    assert await _notes_size(held) == len(AGREED)


async def test_a_trash_of_a_folder_no_machine_holds_asks_nobody(
    uvicorn_server: str,
    world: tuple[World, uuid.UUID],
    files_client: AsyncClient,
    fx: Any,
) -> None:
    held, _team = world
    elsewhere = await fx.node(b"elsewhere", kind="folder", parent=await fx.shared())
    async with held.box(uvicorn_server) as box:
        trashed = await _trash(files_client, held, elsewhere.id)
        await box.nothing_asked(0.5)
    assert trashed.status_code == 200, trashed.text
