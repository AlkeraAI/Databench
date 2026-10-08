"""The stored-notebook route: a notebook read from the drive as cells with the
outputs saved beside it, for a preview of the file.

Pinned here: it answers exactly who the file's own bytes answer (every rung,
downloads off included), it reads the drive and nothing live (no notebook
service is installed, so a reach for the live document or the kernel fails the
test), and the saved snapshot is found beside the notebook and attached.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from alkera_core.files import NodeId
from alkera_core.files.authz.decider import NO_DOWNLOAD_BIT
from alkera_core.files.clock import SystemClock
from alkera_core.files.content import ContentService
from alkera_core.files.namespace import Namespace
from alkera_core.models.files.tree import FileNode
from alkera_notebook.format import read
from alkera_notebook.outputs import code_hash
from backend.services.files.context import build_files_context
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.file_world import FileWorld, acting, file_world, node_of

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

NAME = "sales.alknb.py"
SETUP = "ssssssssss"
MD = "aaaaaaaaaa"
PY = "bbbbbbbbbb"
TABLE_MIME = "application/vnd.alkera.table+json"
TABLE = {"columns": [{"name": "region"}], "rows": [["west"]]}
NOTEBOOK = "\n".join(
    [
        "# >>> alkera",
        '# format = "1.0"',
        "# <<< alkera",
        "",
        "import marimo",
        "",
        '__generated_with = "0.25.1"',
        "app = marimo.App()",
        "",
        f'with app.setup(alkera_id="{SETUP}"):',
        "    import alkera",
        "",
        "",
        f'@app.cell(alkera_id="{MD}")',
        "def _():",
        "    alkera.md(",
        '        r"""',
        "        # Sales",
        '        """',
        "    )",
        "    return",
        "",
        "",
        f'@app.cell(alkera_id="{PY}")',
        "def _():",
        "    x = 1",
        "    x",
        "    return",
        "",
        "",
        'if __name__ == "__main__":',
        "    app.run()",
        "",
    ]
)


def _snapshot() -> bytes:
    code = next(cell.code for cell in read(NOTEBOOK).cells if cell.id == PY)
    return json.dumps(
        {
            "version": "1",
            "metadata": {"marimo_version": "0.25.1"},
            "alkera": {"schema_version": "1.1.0"},
            "cells": [
                {
                    "id": PY,
                    "code_hash": code_hash(code),
                    "outputs": [{"type": "data", "data": {TABLE_MIME: TABLE}}],
                    "console": [],
                }
            ],
        }
    ).encode()


async def _put_beside(
    db: AsyncSession, fw: FileWorld, folders: list[bytes], name: bytes, data: bytes
) -> uuid.UUID:
    """A file in ``folders`` under the notebook's own folder, as its owner
    writes it."""
    notebook = await node_of(db, fw.node_id)
    context = await build_files_context(db, acting(fw.world.owner))
    namespace = Namespace(context.repo, context.ctx, SystemClock())
    async with context.repo.transaction():
        parent = NodeId(uuid.UUID(str(notebook.parent_id)))
        for folder in folders:
            made = await namespace.create(notebook.drive_id, parent, "folder", folder)
            parent = NodeId(uuid.UUID(str(made.id)))
        file = await namespace.create(notebook.drive_id, parent, "file", name)
    await db.commit()

    async def body() -> AsyncIterator[bytes]:
        yield data

    service = ContentService(context.repo, context.ctx, context.clock, context.store)
    await service.put_version(
        NodeId(uuid.UUID(str(file.id))), body(), size_declared=len(data), if_match=file.etag
    )
    await db.commit()
    return uuid.UUID(str(file.id))


@pytest.fixture
async def fw(real_session: AsyncSession, org_admin: OrgWithAdmin) -> FileWorld:
    return await file_world(real_session, org_admin, content=NOTEBOOK.encode(), name=NAME)


def _urls(fw: FileWorld, drive_id: uuid.UUID) -> tuple[str, str]:
    stored = f"/api/v1/notebooks/{drive_id}/{fw.node_id}/stored"
    content = f"/api/v1/files/drives/{drive_id}/items/{fw.node_id}/content"
    return stored, content


async def _drive(db: AsyncSession, fw: FileWorld) -> uuid.UUID:
    return uuid.UUID(str((await node_of(db, fw.node_id)).drive_id))


async def test_the_notebook_comes_back_as_cells_with_its_saved_table(
    real_session: AsyncSession, fw: FileWorld, client: AsyncClient
) -> None:
    await _put_beside(
        real_session, fw, [b"__marimo__", b"session"], f"{NAME}.json".encode(), _snapshot()
    )
    stored, _ = _urls(fw, await _drive(real_session, fw))
    await login(client, fw.world.reader.user.email, fw.world.reader.password)
    answer = await client.get(stored)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [(c["id"], c["kind"], c["source"]) for c in body["cells"]] == [
        (SETUP, "setup", "import alkera"),
        (MD, "markdown", "# Sales"),
        (PY, "python", "x = 1\nx"),
    ]
    py = body["cells"][2]
    assert py["output_origin"] == "saved"
    assert [o["data"][TABLE_MIME] for o in py["outputs"]] == [TABLE]
    assert body["cells"][1]["outputs"] == []


async def test_a_snapshot_for_another_notebook_is_not_attached(
    real_session: AsyncSession, fw: FileWorld, client: AsyncClient
) -> None:
    await _put_beside(
        real_session, fw, [b"__marimo__", b"session"], b"other.alknb.py.json", _snapshot()
    )
    stored, _ = _urls(fw, await _drive(real_session, fw))
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    answer = await client.get(stored)
    assert answer.status_code == 200, answer.text
    assert all(cell["outputs"] == [] for cell in answer.json()["cells"])


async def test_a_notebook_with_no_saved_session_has_cells_and_no_outputs(
    real_session: AsyncSession, fw: FileWorld, client: AsyncClient
) -> None:
    stored, _ = _urls(fw, await _drive(real_session, fw))
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    answer = await client.get(stored)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [c["id"] for c in body["cells"]] == [SETUP, MD, PY]
    assert all(c["outputs"] == [] for c in body["cells"]) and body["notices"] == []


@pytest.mark.parametrize("who", ["owner", "writer", "commenter", "reader", "stranger"])
@pytest.mark.parametrize("downloads_off", [False, True], ids=["downloads-on", "downloads-off"])
async def test_the_route_answers_exactly_who_the_file_s_own_bytes_answer(
    real_session: AsyncSession, fw: FileWorld, client: AsyncClient, who: str, downloads_off: bool
) -> None:
    if downloads_off:
        await real_session.execute(
            update(FileNode)
            .where(FileNode.id == fw.node_id)
            .values(flags=FileNode.flags.op("|")(NO_DOWNLOAD_BIT))
        )
        await real_session.commit()
    stored, content = _urls(fw, await _drive(real_session, fw))
    person = getattr(fw.world, who)
    await login(client, person.user.email, person.password)
    bytes_answer = await client.get(content)
    stored_answer = await client.get(stored)
    # The content route hands a reader on with a redirect to the bytes.
    allowed = bytes_answer.status_code in (200, 302, 307)
    assert (stored_answer.status_code == 200) is allowed, (
        bytes_answer.status_code,
        stored_answer.text,
    )
    if not allowed:
        assert stored_answer.status_code == bytes_answer.status_code


async def test_a_snapshot_the_reader_may_not_take_leaves_the_outputs_out(
    real_session: AsyncSession, fw: FileWorld, client: AsyncClient
) -> None:
    snapshot = await _put_beside(
        real_session, fw, [b"__marimo__", b"session"], f"{NAME}.json".encode(), _snapshot()
    )
    await real_session.execute(
        update(FileNode)
        .where(FileNode.id == snapshot)
        .values(flags=FileNode.flags.op("|")(NO_DOWNLOAD_BIT))
    )
    await real_session.commit()
    stored, _ = _urls(fw, await _drive(real_session, fw))
    await login(client, fw.world.reader.user.email, fw.world.reader.password)
    answer = await client.get(stored)
    assert answer.status_code == 200, answer.text
    assert all(cell["outputs"] == [] for cell in answer.json()["cells"])


async def test_a_file_that_is_not_text_is_a_structured_refusal(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    binary = await file_world(real_session, org_admin, content=b"\x00\x01\x02" * 50, name=NAME)
    stored, _ = _urls(binary, await _drive(real_session, binary))
    await login(client, binary.world.owner.user.email, binary.world.owner.password)
    answer = await client.get(stored)
    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == "notebook_binary"
