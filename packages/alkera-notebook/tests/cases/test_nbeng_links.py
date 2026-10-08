"""A link placed in the workspace tree is never followed by the engine.

Anyone who can edit a notebook can run a cell, and a cell can make links in
the tree. On a box the engine runs as the org's worker, a uid that owns far
more than the tree (other workspaces, members' private chat folders, its
credential). Each case plants a link where the engine reads or writes a
notebook's files, pointing at a folder outside the tree that holds one file,
and checks that the run still completes, the outside folder is exactly as it
was, and (for the outputs) that the refused snapshot was logged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

import pytest
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import default_format
from alkera_notebook.document.ops import InsertCell, ReplaceCell
from alkera_notebook.engine import AllTarget
from nbeng_harness import ANN, engine_for, notebook

needs_posix_links = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symbolic links")

pytestmark = needs_posix_links

SENTINEL = "sentinel.txt"
SENTINEL_TEXT = "another member's file\n"

# A value above the blob threshold, so the snapshot writes a blob too.
BIG = (
    "class Big:\n"
    "    def _repr_mimebundle_(self, **kwargs):\n"
    "        return {'text/html': '<p>' + 'x' * 300000 + '</p>', 'text/plain': 'big'}\n"
    "Big()"
)


def _victim(tmp_path: Path) -> Path:
    """A folder outside the workspace tree holding one regular file."""
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / SENTINEL).write_text(SENTINEL_TEXT, encoding="utf-8")
    return victim


def _untouched(victim: Path) -> bool:
    entries = sorted(p.name for p in victim.rglob("*"))
    return entries == [SENTINEL] and (victim / SENTINEL).read_text(encoding="utf-8") == (
        SENTINEL_TEXT
    )


def _plant(ws: Path, link: str, victim: Path) -> None:
    where = ws / link
    where.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(victim, where, target_is_directory=True)


@pytest.mark.parametrize(
    ("link", "code"),
    [
        pytest.param("__marimo__", BIG, id="marimo-folder"),
        pytest.param("__marimo__/session", BIG, id="session-folder"),
        pytest.param("__marimo__/session/nb.alknb.py.d", "x = 1\nx", id="blob-folder-no-blobs"),
        pytest.param("__marimo__/session/nb.alknb.py.d", BIG, id="blob-folder-with-a-blob"),
    ],
)
async def test_a_linked_outputs_folder_is_refused_and_the_run_completes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, link: str, code: str
) -> None:
    victim = _victim(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _plant(ws, link, victim)
    caplog.set_level(logging.WARNING, logger="alkera_notebook.outputs.snapshot")
    async with engine_for(tmp_path) as engine:
        _, client, (cid,) = await notebook(engine, [code])
        record = await (await client.run(AllTarget())).wait(30)
        for session in engine.sessions:
            await session.snapshots.flush()
        assert record.status == "ok"
        view = await client.read()
        assert view.cells[0].id == cid and view.cells[0].status == "fresh"
    assert _untouched(victim)
    assert any("outputs not saved" in r.getMessage() for r in caplog.records)


async def test_a_notebook_folder_swapped_for_a_link_is_not_written_through(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The notebook's folder is a real folder when it is opened, then is
    replaced by a link: neither the next edit nor the next snapshot lands in
    the link's target. (The store refused the edit before this change too;
    the snapshot went through the link.)"""
    victim = _victim(tmp_path)
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    caplog.set_level(logging.WARNING, logger="alkera_notebook.outputs.snapshot")
    async with engine_for(tmp_path) as engine:
        _, client, (cid,) = await notebook(engine, ["x = 1\nx"], path="sub/nb.alknb.py")
        record = await (await client.run(AllTarget())).wait(30)
        assert record.status == "ok"
        (ws / "sub").rename(ws / "sub-moved")
        _plant(ws, "sub", victim)
        for session in engine.sessions:
            await session.snapshots.flush()
        assert _untouched(victim)
        assert any("outputs not saved" in r.getMessage() for r in caplog.records)
        with pytest.raises((OSError, ValueError)):
            await client.apply([ReplaceCell(cell_id=cid, source="x = 2\nx")], None)
    assert _untouched(victim)


async def test_a_notebook_file_swapped_for_a_link_is_not_read_through(tmp_path: Path) -> None:
    """A notebook file replaced by a link to someone else's file: reloading
    it neither reads that file into the notebook nor writes to it. (The store
    refused a link out of its root before this change as well, by resolving
    it first; this pins that it still does, now without resolving.)"""
    victim = _victim(tmp_path)
    secret = victim / SENTINEL
    secret.write_text("SECRET = 'not yours'\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    store = FileDocumentStore(ws, fmt=default_format())
    await store.create("nb.alknb.py", [InsertCell(source="x = 1")], {}, ANN)
    (ws / "nb.alknb.py").unlink()
    os.symlink(secret, ws / "nb.alknb.py")
    with pytest.raises((OSError, ValueError)):
        await store.reload("nb.alknb.py")
    with contextlib.suppress(OSError, ValueError):
        loaded = await store.load("nb.alknb.py")
        assert all("SECRET" not in c.code for c in loaded.document.live_cells())
    with pytest.raises((OSError, ValueError)):
        await store.apply("nb.alknb.py", [InsertCell(source="y = 2")], None, ANN, None)
    assert secret.read_text(encoding="utf-8") == "SECRET = 'not yours'\n"
    assert sorted(p.name for p in victim.iterdir()) == [SENTINEL]


def test_a_linked_build_records_folder_is_not_written_through(tmp_path: Path) -> None:
    """The env root is bound into the kernel sandbox: a link where the build
    records go must not carry the engine's record (or its mode) elsewhere."""
    from alkera_notebook.envs.state import BuildRecord, BuildRecords

    victim = _victim(tmp_path)
    env_root = tmp_path / "envs"
    env_root.mkdir()
    _plant(env_root, "records", victim)
    records = BuildRecords(env_root)
    with pytest.raises(OSError):
        records.put("default:x", BuildRecord(log="built"))
    assert records.get("default:x") == BuildRecord()
    assert _untouched(victim)


def test_a_linked_widget_asset_folder_is_not_written_through(tmp_path: Path) -> None:
    import hashlib

    from alkera_notebook.widgets.assets import FileBlobStore

    victim = _victim(tmp_path)
    root = tmp_path / "assets"
    root.mkdir()
    _plant(root, hashlib.sha256(b"scope").hexdigest()[:32], victim)
    with pytest.raises(OSError):
        FileBlobStore(root).put("scope", b"widget bytes")
    assert _untouched(victim)
