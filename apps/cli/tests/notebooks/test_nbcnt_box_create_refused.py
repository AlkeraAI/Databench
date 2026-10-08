"""A notebook the agent creates on the box is whole or nothing.

The box writes a new notebook's file into the folder it holds (the live sync
takes it to the drive), then inserts its cells as the agent's batch. When the
backend refuses that batch, the file written for it is taken back off the
disk and off the drive: a refused create leaves no empty notebook behind.

The drive here is the folder on disk as the live sync mirrors it (a path has
a node exactly while its file exists), and the backend's notebook routes are
an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.notebooks.box import BoxFolder, BoxStores
from alkera_cli.notebooks.engine_host import WorkspaceTenancy
from alkera_cli.notebooks.store_loro import DocSignal, StoreError, agent_actor_id
from alkera_notebook.document.ops import InsertCell
from alkera_notebook.engine.errors import ForbiddenError
from alkera_notebook.engine.models import Actor

pytestmark = pytest.mark.asyncio

PATH = "new-notebook.alknb.py"
CHAT = str(uuid.uuid4())
AGENT = Actor(
    kind="agent", id=agent_actor_id(CHAT), display_name="Agent", can_edit=True, can_run=True
)
CELLS = [
    InsertCell(op="insert", kind="markdown", source="# Revenue"),
    InsertCell(op="insert", source="x = 1", name="x"),
]


class Backend:
    """The notebook routes: the ops route answers ``ops_status``."""

    def __init__(self, ops_status: int, ops_body: dict[str, Any]) -> None:
        self.ops_status = ops_status
        self.ops_body = ops_body
        self.batches: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/ops"):
            self.batches.append(json.loads(request.content))
            return httpx.Response(self.ops_status, json=self.ops_body)
        return httpx.Response(200, json={"token": "v1", "cells": [], "settings": {}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://backend", transport=httpx.MockTransport(self.handler)
        )


class Channel:
    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        yield DocSignal(kind="ready")


def _store(tmp_path: Path, backend: Backend) -> Any:
    nodes: dict[str, str] = {}

    def node_at(relative: str) -> str | None:
        """The drive as the live sync mirrors the folder: a node while the
        file is there."""
        if not (tmp_path / relative).exists():
            nodes.pop(relative, None)
            return None
        return nodes.setdefault(relative, str(uuid.uuid4()))

    folder = BoxFolder(
        key="ws:main",
        root=tmp_path,
        drive_id="d1",
        lease_node_id="lease-1",
        step="",
        headers={},
        node_at=node_at,
    )

    class Folders:
        def by_key(self, key: str) -> BoxFolder | None:
            return folder

        def by_lease(self, lease_node_id: str) -> BoxFolder | None:
            return folder

    stores = BoxStores(
        http=backend.client(), folders=Folders(), signals=Channel(), visible_within=2.0
    )
    return stores.store_for(WorkspaceTenancy("ws:main", tmp_path, tmp_path / "envs")), node_at


@pytest.mark.parametrize(
    ("status", "body", "raised"),
    [
        pytest.param(
            403,
            {
                "code": "notebook.agent_chat_refused",
                "message": "the chat is not in the notebook's workspace",
            },
            ForbiddenError,
            id="the-agent-s-chat-refused",
        ),
        pytest.param(
            503,
            {"code": "crdt_unsupported", "message": "live editing is off"},
            StoreError,
            id="live-editing-off",
        ),
    ],
)
async def test_a_refused_create_leaves_nothing_behind(
    tmp_path: Path, status: int, body: dict[str, Any], raised: type[Exception]
) -> None:
    backend = Backend(status, body)
    store, node_at = _store(tmp_path, backend)

    with pytest.raises(raised):
        await store.create(PATH, CELLS, {}, AGENT)

    assert [b.get("agent_chat_id") for b in backend.batches] == [CHAT]
    assert not (tmp_path / PATH).exists()
    assert node_at(PATH) is None
    # The path is free again: a later create there is not refused as taken.
    backend.ops_status, backend.ops_body = 200, _accepted()
    await store.create(PATH, CELLS, {}, AGENT)
    assert (tmp_path / PATH).exists()


def _accepted() -> dict[str, Any]:
    return {"token": "v2", "repeat": False, "cells": [], "created": [], "notices": [], "graph": {}}


async def test_an_accepted_create_keeps_its_file(tmp_path: Path) -> None:
    backend = Backend(200, _accepted())
    store, node_at = _store(tmp_path, backend)

    await store.create(PATH, CELLS, {}, AGENT)

    assert (tmp_path / PATH).read_text(encoding="utf-8").startswith("# >>> alkera")
    assert node_at(PATH) is not None


class LandingBackend(Backend):
    """The drive has the new notebook's row before its bytes: the first
    ``landing`` batches are answered that the bytes are still landing (what
    the backend says instead of starting the document from no text), the
    next is taken."""

    def __init__(self, landing: int) -> None:
        super().__init__(200, _accepted())
        self.landing = landing

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/ops") and self.landing:
            self.landing -= 1
            self.batches.append(json.loads(request.content))
            return httpx.Response(
                503,
                json={
                    "code": "content_landing",
                    "message": "the file's content has not reached the drive yet",
                },
                headers={"Retry-After": "1"},
            )
        return super().handler(request)


async def test_a_create_whose_bytes_are_still_landing_waits_for_them(tmp_path: Path) -> None:
    backend = LandingBackend(landing=1)
    store, node_at = _store(tmp_path, backend)

    await store.create(PATH, CELLS, {}, AGENT)

    # The same batch was sent again once the bytes were on the drive, and the
    # notebook is kept, never taken back as a refused create.
    assert len(backend.batches) == 2
    assert backend.batches[0] == backend.batches[1]
    assert (tmp_path / PATH).read_text(encoding="utf-8").startswith("# >>> alkera")
    assert node_at(PATH) is not None
