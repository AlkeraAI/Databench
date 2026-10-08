"""A Files change reaches an open event stream, and reaches it thin.

The delta feed is how a client catches up; this is how it learns there is
something to catch up on. Both Files announcements — ``file_node.changed`` and
``file_operation.changed`` — are written to the outbox inside the mutation's own
transaction, so the whole path here is real: commit → ``NOTIFY`` → the runtime's
listener → the hub → one SSE frame. What the frame may carry is the other half
of the contract: ids and a version, never a name, a path or a payload, because
the stream fans out to every session in the org and the feed itself is the
authorized read.

Each case here waits for its frame rather than for a clock: the stream is read
as the server writes it (``tests._sse_reader``), so the wait ends when the
announcement arrives however long the several hops took, and "nothing else
arrived" is asserted over a quiet moment with the stream still open. A window
short enough to collect a finished body was measuring the host, not the path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture
from alkera_core.authz.principal import ActingContext
from alkera_core.files.history import (
    emit_lease_changed,
    emit_node_changed,
    emit_operation_changed,
)
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.models.files.tree import FileNode
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text
from tests._sse_reader import EventStream, open_event_stream
from tests.conftest import login

pytestmark = pytest.mark.asyncio


def _ctx(files_org: FilesOrgFixture) -> ActingContext:
    return ActingContext.for_user(
        user_id=files_org.org.admin_id,
        org_id=files_org.org.org_id,
        email=files_org.org.admin_email,
    )


async def _nodes_before(fx: FilesFixtures, drive_id: uuid.UUID) -> set[str]:
    """Every node the drive held before the stream opened."""
    rows = await fx._session.execute(select(FileNode.id).where(FileNode.drive_id == drive_id))
    return {str(node_id) for node_id in rows.scalars()}


def _the_frame(
    stream: EventStream,
    *,
    drive_id: uuid.UUID,
    event: str,
    entity_id: uuid.UUID,
    version: int,
    nodes_before: set[str],
) -> dict[str, Any]:
    """The one frame this case's emit put on the stream, told apart by what it
    names and never by when it arrived.

    Building the drive announced each folder of its skeleton, committed before
    the stream opened; the listener can still be carrying those to the hub when
    the stream subscribes, so one may land before the frame under test or after
    it. Such an echo names a node that already existed and is let through;
    every other frame of this drive fails the case. Frames of other drives are
    other cases' in the same org.
    """
    frames = [
        (f["event"], json.loads(f["data"]))
        for f in stream.events
        if json.loads(f["data"]).get("drive_id") == str(drive_id)
    ]
    under_test = [
        data
        for name, data in frames
        if name == event and data["entity_id"] == str(entity_id) and data["version"] == version
    ]
    echoes = [(name, data) for name, data in frames if data not in under_test]
    assert len(under_test) == 1, stream.events
    assert all(
        name == "file_node.changed" and data["entity_id"] in nodes_before for name, data in echoes
    ), stream.events
    return under_test[0]


async def test_a_file_node_change_reaches_an_open_stream_as_a_thin_frame(
    files_on: None,
    client: AsyncClient,
    realtime_app: FastAPI,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The frame names the node and its version and nothing else — no name, no
    parent, no drive — because the stream is a nudge, not an authorized read."""
    drive = await fx.drive()
    node = await fx.node(b"quarterly-plan.txt")
    drive_id, node_id = drive.id, node.id
    nodes_before = await _nodes_before(fx, drive_id)
    await login(client, files_org.org.admin_email, files_org.org.admin_password)

    async with open_event_stream(realtime_app, client) as stream:
        await stream.opened()
        async with fx.repo.transaction():
            await emit_node_changed(
                fx.repo,
                _ctx(files_org),
                node_id=NodeId(node_id),
                drive_id=DriveId(drive_id),
                version=7,
            )
        await fx._session.commit()
        await stream.wait_for(
            lambda s: any(json.loads(f["data"]).get("version") == 7 for f in s.events),
            what="the version-7 node frame",
        )
        await stream.quiet()

    data = _the_frame(
        stream,
        drive_id=drive_id,
        event="file_node.changed",
        entity_id=node_id,
        version=7,
        nodes_before=nodes_before,
    )
    assert data == {
        "type": "file_node.changed",
        "entity": "file_node",
        "entity_id": str(node_id),
        "version": 7,
        "org_id": str(files_org.org.org_id),
        # The drive is an id the reader holds already; the folder and the
        # reason are absent because this caller passed neither, and an absent
        # one is left off the wire rather than sent as a null.
        "drive_id": str(drive_id),
    }
    assert "quarterly-plan" not in stream.text


async def test_a_file_operation_change_reaches_an_open_stream_as_a_thin_frame(
    files_on: None,
    client: AsyncClient,
    realtime_app: FastAPI,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The other Files announcement travels the same path: a long-running move
    or copy tells the portal it advanced without shipping any of its rows."""
    drive = await fx.drive()
    drive_id = drive.id
    op_id = uuid.uuid4()
    nodes_before = await _nodes_before(fx, drive_id)
    await login(client, files_org.org.admin_email, files_org.org.admin_password)

    async with open_event_stream(realtime_app, client) as stream:
        await stream.opened()
        async with fx.repo.transaction():
            await emit_operation_changed(
                fx.repo,
                _ctx(files_org),
                op_id=OperationId(op_id),
                drive_id=DriveId(drive_id),
                version=3,
            )
        await fx._session.commit()
        await stream.wait_for_event("file_operation.changed")
        await stream.quiet()

    assert _the_frame(
        stream,
        drive_id=drive_id,
        event="file_operation.changed",
        entity_id=op_id,
        version=3,
        nodes_before=nodes_before,
    ) == {
        "type": "file_operation.changed",
        "entity": "file_operation",
        "entity_id": str(op_id),
        "version": 3,
        "org_id": str(files_org.org.org_id),
        "drive_id": str(drive_id),
    }


async def test_a_lease_change_reaches_an_open_stream_as_a_thin_frame(
    files_on: None,
    client: AsyncClient,
    realtime_app: FastAPI,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The third Files announcement: what a leased folder is doing right now.

    One frame for the whole mount rather than one per file in flight, carrying
    the leased node and the sequence — the client reads the plane back through
    the authorized listing, so the frame only has to say "you are behind".
    """
    drive = await fx.drive()
    node = await fx.node(b"team", kind="folder")
    drive_id, node_id = drive.id, node.id
    nodes_before = await _nodes_before(fx, drive_id)
    await login(client, files_org.org.admin_email, files_org.org.admin_password)

    async with open_event_stream(realtime_app, client) as stream:
        await stream.opened()
        async with fx.repo.transaction():
            await emit_lease_changed(
                fx.repo,
                _ctx(files_org),
                lease_node_id=NodeId(node_id),
                drive_id=DriveId(drive_id),
                live_seq=4,
            )
        await fx._session.commit()
        await stream.wait_for_event("file_lease.changed")
        await stream.quiet()

    assert _the_frame(
        stream,
        drive_id=drive_id,
        event="file_lease.changed",
        entity_id=node_id,
        version=4,
        nodes_before=nodes_before,
    ) == {
        "type": "file_lease.changed",
        "entity": "file_lease",
        "entity_id": str(node_id),
        "version": 4,
        "org_id": str(files_org.org.org_id),
        "drive_id": str(drive_id),
        # Spelled beside the entity id it repeats because a subscriber matches
        # on it by name: a surface watching ONE mount reads this key rather
        # than having to know that a lease frame's entity is its leased node.
        "lease_node_id": str(node_id),
    }
    assert "team" not in stream.text


@pytest.mark.parametrize(
    ("parent", "reason"),
    [
        pytest.param(True, "live_saved", id="a-live-save-in-a-folder"),
        pytest.param(False, None, id="a-caller-that-holds-neither"),
    ],
)
async def test_the_node_announcement_carries_ids_and_an_enum_and_nothing_else(
    files_on: None,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    parent: bool,
    reason: str | None,
) -> None:
    """The outbox row behind the frame: exactly five keys, every one an id, a
    number or a fixed word.

    The payload outlives the node it describes by the outbox's retention
    window, so a name or a path here would put the outbox on the erasure path.
    ``parent_id`` earns its place by being an id the client already holds, and
    ``reason`` by being one of two fixed words — a client uses them to refresh
    one listing instead of all of them, never to render as text.
    """
    drive = await fx.drive()
    folder = await fx.node(b"reports", kind="folder")
    node = await fx.node(b"q3.html", parent=folder)

    async with fx.repo.transaction():
        await emit_node_changed(
            fx.repo,
            _ctx(files_org),
            node_id=NodeId(node.id),
            drive_id=DriveId(drive.id),
            version=2,
            parent_id=NodeId(folder.id) if parent else None,
            reason=reason,  # type: ignore[arg-type]  # parametrized over the two members
        )
    await fx._session.commit()

    payload = (
        await fx._session.execute(
            text(
                "SELECT payload FROM event_outbox WHERE type = 'file_node.changed' "
                "AND org_id = :org AND entity_id = :node"
            ),
            {"org": files_org.org.org_id, "node": str(node.id)},
        )
    ).scalar_one()
    assert payload == {
        "node_id": str(node.id),
        "drive_id": str(drive.id),
        "version": 2,
        "parent_id": str(folder.id) if parent else None,
        "reason": reason,
    }
    assert "reports" not in json.dumps(payload)
