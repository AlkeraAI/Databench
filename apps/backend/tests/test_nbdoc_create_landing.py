"""A notebook the box has just written is edited once its bytes land.

The box creates a notebook by writing its file into the folder it holds; its
live sync reports the row first (a tree report, bytes or no bytes) and
uploads the bytes after. The agent's first batch on the new notebook can
reach the backend in between. The live document must not start from the
empty text the drive holds for that moment (no notebook reads from it, so it
was refused as "cannot be edited live" and the create was taken back): the
backend answers that the bytes are still landing, with a wait, and the same
batch is taken once they are on the drive.

Real Postgres, real Files, the real CRDT lane and its sandbox workers, the
real routes; the box is a registered machine holding the chat folder's lease.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alkera_core.models.files.tree import FileNode
from alkera_notebook import format as notebook_format
from alkera_notebook.format.ir import NotebookIR
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin
from tests.test_nbdoc_box_agent import Live, _box_for_chat, _live
from tests.test_nbdoc_routes import _idem

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

NEW_NAME = "fresh.alknb.py"
MTIME_NS = 1_791_319_087_398_463_436


@pytest.fixture
async def live(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Live]:
    async for found in _live(real_session, org_admin, sessions=False):
        yield found


def _empty_notebook() -> bytes:
    """The text the box writes for a new notebook before its cells go in."""
    return notebook_format.write(
        NotebookIR(
            format="1.0",
            header_text="",
            settings={},
            unknown_settings="",
            app_config={},
            generated_with="",
            cells=(),
            violations=(),
            read_only_reason=None,
        )
    ).encode()


def _insert(chat_id: str) -> dict[str, Any]:
    return {
        "ops": [{"op": "insert", "kind": "python", "source": "x = 1", "name": "x"}],
        "agent_chat_id": chat_id,
    }


async def _new_path(db: AsyncSession, live: Live, lease_node_id: uuid.UUID) -> str:
    """A path beside the chat's notebook, as the holder spells it: from the
    leased folder down."""
    names = [NEW_NAME]
    node = await db.get(FileNode, live.fw.node_id)
    assert node is not None
    parent_id = node.parent_id
    while parent_id is not None and parent_id != lease_node_id:
        parent = await db.get(FileNode, parent_id)
        assert parent is not None
        names.append(parent.name_display)
        parent_id = parent.parent_id
    assert parent_id == lease_node_id
    await db.commit()
    return "/".join(reversed(names))


async def _new_node(db: AsyncSession, drive_id: uuid.UUID) -> uuid.UUID:
    found = (
        await db.execute(
            select(FileNode.id).where(
                FileNode.drive_id == drive_id,
                FileNode.name_display == NEW_NAME,
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalar_one()
    await db.commit()
    return uuid.UUID(str(found))


async def test_the_first_batch_on_a_notebook_whose_bytes_are_landing_waits_for_them(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    box = await _box_for_chat(real_session, client, live)
    new_path = await _new_path(real_session, live, box.holder.node_id)
    text = _empty_notebook()
    minted = await box.holder.tree(
        [
            {
                "op": "upsert",
                "path": new_path,
                "kind": "file",
                "size": len(text),
                "mtime_ns": MTIME_NS,
            }
        ]
    )
    assert minted.status_code == 200, minted.text
    node_id = await _new_node(real_session, live.drive_id)
    url = f"/api/v1/notebooks/{live.drive_id}/{node_id}/ops"

    early = await box.client.post(url, json=_insert(live.chat_id), headers=box.fence)
    assert early.status_code == 503, early.text
    assert early.json()["code"] == "content_landing"
    assert int(early.headers["Retry-After"]) >= 1

    pushed = await box.holder.push(real_session, _idem, node_id, text)
    assert pushed.status_code in (200, 201), pushed.text

    landed = await box.client.post(url, json=_insert(live.chat_id), headers=box.fence)
    assert landed.status_code == 200, landed.text
    view = await box.client.get(f"/api/v1/notebooks/{live.drive_id}/{node_id}", headers=box.fence)
    assert view.status_code == 200, view.text
    assert [c["source"] for c in view.json()["cells"] if c["kind"] == "python"] == ["x = 1"]


async def test_a_notebook_file_that_reads_as_no_notebook_names_why_it_is_not_editable(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """A file whose bytes are on the drive and are no notebook is refused as
    not editable, and the answer says why, not only that."""
    box = await _box_for_chat(real_session, client, live)
    new_path = await _new_path(real_session, live, box.holder.node_id)
    text = b"print('not a notebook')\n"
    minted = await box.holder.tree(
        [{"op": "upsert", "path": new_path, "kind": "file", "size": len(text), "mtime_ns": 1}]
    )
    assert minted.status_code == 200, minted.text
    node_id = await _new_node(real_session, live.drive_id)
    pushed = await box.holder.push(real_session, _idem, node_id, text)
    assert pushed.status_code in (200, 201), pushed.text

    refused = await box.client.post(
        f"/api/v1/notebooks/{live.drive_id}/{node_id}/ops",
        json=_insert(live.chat_id),
        headers=box.fence,
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert (body["code"], body["message"]) == (
        "not_editable",
        "this file cannot be edited live (not_a_notebook)",
    )
