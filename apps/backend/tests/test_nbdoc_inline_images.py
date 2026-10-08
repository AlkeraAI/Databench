"""The output blob route answers a raster image the notebook carries inline.

An image under the blob threshold has no file of its own beside the notebook,
yet a chat card names it by the same hash a stored one has. Pinned here: the
route finds such an image among the outputs the cells show now (the kernel's
snapshot with the events after it folded in) or in the saved snapshot, serves
its bytes as an image, says an output no cell holds any more is gone, serves
no markup, and answers nobody the notebook and its snapshot would not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core.events import HubEvent
from alkera_core.events.types import EventType
from alkera_core.files import NodeId
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
from alkera_core.files.clock import SystemClock
from alkera_core.files.content import ContentService
from alkera_core.files.namespace import Namespace
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.realtime.machine import NotebookRequestOp
from backend.authz import decide_on_record
from backend.services.files.context import build_files_context
from backend.services.notebooks import app as notebook_app
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from backend.services.notebooks.transport import KernelHost
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.file_world import FileWorld, acting, file_world, node_of

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

NAME = "geo.alknb.py"
CELL = "cccccccccc"
NOTEBOOK = b"import marimo\napp = marimo.App()\n"

# Two distinct PNG payloads (the route never decodes them as images).
CHART_PNG = b"\x89PNG\r\n\x1a\n" + b"chart-one" * 8
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"chart-two" * 8
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _display(output_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
    return {"type": "display", "output_id": output_id, "data": bundle}


class _Silent:
    """A transport no test here reaches: the blob route sends the box nothing."""

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Any,
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        raise AssertionError(f"the blob route asked the box for {op}")


@dataclass
class Rig:
    fw: FileWorld
    feed: NotebookFeed
    org_id: uuid.UUID
    drive_id: uuid.UUID

    def url(self, sha256: str) -> str:
        return f"/api/v1/notebooks/{self.drive_id}/{self.fw.node_id}/blobs/{sha256}"

    def hear(self, events: list[dict[str, Any]], kernel_id: str = "k-test") -> None:
        channel = f"nb:{self.fw.node_id}"
        self.feed.observe(
            HubEvent(
                lane="durable",
                org_id=self.org_id,
                type=EventType.NOTEBOOK_EVENT.value,
                entity="notebook",
                entity_id=channel,
                version=0,
                visibility="org",
                payload={"kernel_id": kernel_id, "state": None, "events": events},
                channel=channel,
            )
        )

    def hear_snapshot(self, outputs: list[dict[str, Any]], seq: int = 1) -> None:
        self.hear(
            [
                {
                    "seq": seq,
                    "type": "snapshot",
                    "kernel_id": "k-test",
                    "view": {"cells": [{"id": CELL, "outputs": outputs}]},
                }
            ]
        )


@pytest.fixture
async def rig(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Rig]:
    from tests.conftest import fastapi_app as app

    fw = await file_world(real_session, org_admin, content=NOTEBOOK, name=NAME)
    node = await node_of(real_session, fw.node_id)
    feed = NotebookFeed()
    service = NotebookService(
        transport=_Silent(), feed=feed, carets=CaretBoard(), decide=decide_on_record
    )
    setattr(app.state, notebook_app.STATE_ATTR, service)
    try:
        yield Rig(fw, feed, uuid.UUID(str(node.org_team_id)), uuid.UUID(str(node.drive_id)))
    finally:
        await service.close()
        setattr(app.state, notebook_app.STATE_ATTR, None)


async def _save_snapshot(
    db: AsyncSession, fw: FileWorld, outputs: list[dict[str, Any]]
) -> uuid.UUID:
    """The saved snapshot ``__marimo__/session/<name>.json`` beside the
    notebook, as its owner's box writes it."""
    data = json.dumps(
        {
            "version": "1",
            "metadata": {"marimo_version": "0.25.1"},
            "alkera": {"schema_version": "1.1.0"},
            "cells": [{"id": CELL, "code_hash": "h", "outputs": outputs, "console": []}],
        }
    ).encode()
    notebook = await node_of(db, fw.node_id)
    context = await build_files_context(db, acting(fw.world.owner))
    namespace = Namespace(context.repo, context.ctx, SystemClock())
    async with context.repo.transaction():
        parent = NodeId(uuid.UUID(str(notebook.parent_id)))
        for folder in (b"__marimo__", b"session"):
            made = await namespace.create(notebook.drive_id, parent, "folder", folder)
            parent = NodeId(uuid.UUID(str(made.id)))
        file = await namespace.create(notebook.drive_id, parent, "file", f"{NAME}.json".encode())
    await db.commit()

    async def body() -> AsyncIterator[bytes]:
        yield data

    service = ContentService(context.repo, context.ctx, context.clock, context.store)
    await service.put_version(
        NodeId(uuid.UUID(str(file.id))), body(), size_declared=len(data), if_match=file.etag
    )
    await db.commit()
    return uuid.UUID(str(file.id))


async def _as(client: AsyncClient, rig: Rig, who: str) -> None:
    person = getattr(rig.fw.world, who)
    await login(client, person.user.email, person.password)


async def test_an_image_a_cell_shows_now_is_served_as_its_bytes(
    rig: Rig, client: AsyncClient
) -> None:
    rig.hear_snapshot([_display("o1", {"image/png": _b64(CHART_PNG), "text/plain": "<Figure>"})])
    await _as(client, rig, "reader")

    answer = await client.get(rig.url(_sha(CHART_PNG)))

    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "image/png"
    assert answer.content == CHART_PNG
    assert answer.headers["cache-control"] == "private, max-age=86400, immutable"


async def test_an_image_the_cell_showed_after_the_snapshot_is_found_and_the_old_one_is_gone(
    rig: Rig, client: AsyncClient
) -> None:
    """The cell ran again after the kernel's snapshot: its new image is in an
    event, and the image the snapshot held is no longer shown by any cell."""
    rig.hear_snapshot([_display("o1", {"image/png": _b64(CHART_PNG)})])
    rig.hear(
        [
            {
                "seq": 2,
                "type": "cell.output",
                "cell_id": CELL,
                "run_id": "r2",
                "mode": "replace",
                "output": _display("o2", {"image/png": _b64(OTHER_PNG)}),
            }
        ]
    )
    await _as(client, rig, "reader")

    fresh = await client.get(rig.url(_sha(OTHER_PNG)))
    stale = await client.get(rig.url(_sha(CHART_PNG)))

    assert fresh.status_code == 200 and fresh.content == OTHER_PNG
    assert stale.status_code == 404


async def test_cleared_outputs_are_gone(rig: Rig, client: AsyncClient) -> None:
    rig.hear_snapshot([_display("o1", {"image/png": _b64(CHART_PNG)})])
    rig.hear([{"seq": 2, "type": "cell.outputs_cleared", "cell_ids": [CELL]}])
    await _as(client, rig, "owner")

    assert (await client.get(rig.url(_sha(CHART_PNG)))).status_code == 404


async def test_an_image_only_the_saved_snapshot_holds_is_served(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """No replica heard the kernel (a restart, another replica): the saved
    snapshot beside the notebook still names the image."""
    await _save_snapshot(
        real_session, rig.fw, [{"type": "data", "data": {"image/png": _b64(CHART_PNG)}}]
    )
    await _as(client, rig, "reader")

    answer = await client.get(rig.url(_sha(CHART_PNG)))

    assert answer.status_code == 200, answer.text
    assert answer.content == CHART_PNG


async def test_an_image_no_cell_holds_is_a_404(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    rig.hear_snapshot([_display("o1", {"image/png": _b64(CHART_PNG)})])
    await _save_snapshot(
        real_session, rig.fw, [{"type": "data", "data": {"image/png": _b64(CHART_PNG)}}]
    )
    await _as(client, rig, "owner")

    assert (await client.get(rig.url(_sha(OTHER_PNG)))).status_code == 404


async def test_markup_is_never_served_from_an_inline_output(rig: Rig, client: AsyncClient) -> None:
    """An SVG is markup a page could run: only rasters come back as bytes."""
    rig.hear_snapshot([_display("o1", {"image/svg+xml": SVG.decode()})])
    await _as(client, rig, "owner")

    assert (await client.get(rig.url(_sha(SVG)))).status_code == 404


async def test_a_stranger_gets_the_opaque_404_and_no_bytes(rig: Rig, client: AsyncClient) -> None:
    rig.hear_snapshot([_display("o1", {"image/png": _b64(CHART_PNG)})])
    await _as(client, rig, "stranger")

    answer = await client.get(rig.url(_sha(CHART_PNG)))

    assert answer.status_code == 404
    assert CHART_PNG not in answer.content


async def test_a_saved_snapshot_the_reader_may_not_export_serves_nothing(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """The saved snapshot is read under the decision the stored notebook's
    preview takes: a snapshot with downloads off is not read for anyone it
    would not be exported to."""
    snapshot = await _save_snapshot(
        real_session, rig.fw, [{"type": "data", "data": {"image/png": _b64(CHART_PNG)}}]
    )
    await real_session.execute(
        update(FileNode)
        .where(FileNode.id == snapshot)
        .values(flags=FileNode.flags.op("|")(NO_DOWNLOAD_BIT))
    )
    await real_session.commit()
    await _as(client, rig, "reader")

    assert (await client.get(rig.url(_sha(CHART_PNG)))).status_code == 404
