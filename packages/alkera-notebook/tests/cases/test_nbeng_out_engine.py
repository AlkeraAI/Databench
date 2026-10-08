"""Outputs through a real engine: snapshots beside the notebook, reattachment,
provenance and the ``outdated`` mark."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alkera_notebook.document.ops import DeleteCell, ReplaceCell
from alkera_notebook.engine import AllTarget, SettingsChange
from alkera_notebook.envs.models import EnvDescriptor
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.outputs import snapshot_path
from nbeng_harness import ANN, engine_for, notebook, statuses, until


async def run_all(client: object) -> None:
    await (await client.run(AllTarget())).wait(30)  # type: ignore[attr-defined]


def snap(root: Path, name: str = "nb.alknb.py") -> dict:  # type: ignore[type-arg]
    return json.loads(snapshot_path(root / name).read_text())


async def flushed(engine: object) -> None:
    for s in engine.sessions:  # type: ignore[attr-defined]
        await s.snapshots.flush()


async def test_out_snapshot_written_and_reloaded_by_a_new_engine(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 6", "y = x * 7\ny"])
        await run_all(ann)
        await flushed(engine)
    data = snap(tmp_path / "ws")
    assert [c["id"] for c in data["cells"]] == [a, b]
    assert all(c["code_hash"] for c in data["cells"])  # every cell, even without output
    assert data["alkera"]["schema_version"] == "1.1.0"
    async with engine_for(tmp_path) as again:
        client = (await again.open("nb.alknb.py")).attach(ANN)
        view = await client.read()
        cell_b = view.cells[1]
        assert cell_b.output is not None and cell_b.output.text.strip() == "42"
        assert cell_b.output_origin == "saved" and cell_b.status == "not_run"
        assert (await client.kernel("status")).state == "absent"


async def test_out_stock_marimo_snapshot_reattached_as_origin_unknown(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, _ann, (a,) = await notebook(engine, ["answer = 42\nanswer"])
    path = snapshot_path(tmp_path / "ws" / "nb.alknb.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    code_hash = hashlib.md5(b"answer = 42\nanswer", usedforsecurity=False).hexdigest()
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "metadata": {"marimo_version": "0.25.1", "script_metadata_hash": None},
                "cells": [
                    {
                        "id": "Hbol",
                        "code_hash": code_hash,
                        "outputs": [{"type": "data", "data": {"text/plain": "42"}}],
                        "console": [],
                    }
                ],
            }
        )
    )
    async with engine_for(tmp_path) as again:
        client = (await again.open("nb.alknb.py")).attach(ANN)
        cell = (await client.read()).cells[0]
        assert cell.id == a
        assert cell.output_origin == "unknown"
        assert cell.output is not None and cell.output.text.strip() == "42"
        assert not cell.output_outdated


async def test_out_large_value_stored_as_a_blob_by_hash(tmp_path: Path) -> None:
    code = (
        "class Big:\n"
        "    def _repr_mimebundle_(self, **kwargs):\n"
        "        return {'text/html': '<p>' + 'x' * 300000 + '</p>', 'text/plain': 'big'}\n"
        "Big()"
    )
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, [code])
        await run_all(ann)
        await flushed(engine)
    root = tmp_path / "ws"
    html = snap(root)["cells"][0]["outputs"][0]["data"]["text/html"]
    ref = html["application/vnd.alkera.ref+json"]
    blob = snapshot_path(root / "nb.alknb.py").parent / "nb.alknb.py.d" / f"{ref['sha256']}.html"
    assert blob.is_file() and hashlib.sha256(blob.read_bytes()).hexdigest() == ref["sha256"]
    async with engine_for(tmp_path) as again:
        client = (await again.open("nb.alknb.py")).attach(ANN)
        detail = await client.output(a)
        assert detail.text.strip() == "big"


async def test_out_outdated_after_upstream_edit_not_after_unrelated_edit(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, _b, c) = await notebook(engine, ["x = 1", "y = x + 1\ny", "z = 3\nz"])
        await run_all(ann)
        view = await ann.read()
        assert [cell.output_outdated for cell in view.cells] == [False, False, False]
        await ann.apply([ReplaceCell(cell_id=c, source="z = 4\nz")], None)
        view = await ann.read()
        assert [cell.output_outdated for cell in view.cells] == [False, False, True]
        await ann.apply([ReplaceCell(cell_id=a, source="x = 2")], None)
        view = await ann.read()
        # b's output came from x = 1: its lineage no longer matches.
        assert view.cells[1].output_outdated


class SwitchableFingerprint(StaticEnvRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.lock = "lock-1"

    def fingerprint(self, env: EnvDescriptor) -> str:
        return hashlib.sha256(self.lock.encode()).hexdigest()


async def test_out_outdated_after_environment_lock_change(tmp_path: Path) -> None:
    envs = SwitchableFingerprint()
    async with engine_for(tmp_path, envs=envs) as engine:
        _, ann, (_a,) = await notebook(engine, ["x = 1\nx"])
        await run_all(ann)
        assert not (await ann.read()).cells[0].output_outdated
        envs.lock = "lock-2"
        assert (await ann.read()).cells[0].output_outdated


async def test_out_outputs_of_deleted_cells_dropped(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 1\nx", "y = 2\ny"])
        await run_all(ann)
        await flushed(engine)
        assert [c["id"] for c in snap(tmp_path / "ws")["cells"]] == [a, b]
        await ann.apply([DeleteCell(cell_id=b)], None)
        await flushed(engine)
        assert [c["id"] for c in snap(tmp_path / "ws")["cells"]] == [a]


async def test_out_gitignore_toggles_with_outputs_in_git(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, ["x = 1\nx"])
        await run_all(ann)
        await flushed(engine)
        ignore = snapshot_path(tmp_path / "ws" / "nb.alknb.py").parent / ".gitignore"
        assert "/nb.alknb.py.json" in ignore.read_text().splitlines()
        await ann.settings(SettingsChange(outputs_in_git=True))
        await until(lambda: not ignore.exists() or "/nb.alknb.py.json" not in ignore.read_text())
        assert "outputs_in_git = true" in (tmp_path / "ws" / "nb.alknb.py").read_text()
        await ann.settings(SettingsChange(outputs_in_git=False))
        assert "/nb.alknb.py.json" in ignore.read_text().splitlines()
        assert (await statuses(ann))[a] == "fresh"


async def test_out_printed_text_stays_above_the_value_live_and_after_a_reload(
    tmp_path: Path,
) -> None:
    """What a cell printed before its value shows above the value in the
    live view; a reload (the view a joining client reads, and the saved
    snapshot a new engine reattaches) keeps that order."""
    from alkera_notebook.events.models import AnyEvent, CellOutputEvent, CellStreamEvent

    code = "print('first')\n'the value'"
    async with engine_for(tmp_path) as engine:
        session, ann, _ = await notebook(engine, [code])
        events: list[AnyEvent] = []
        session.runtime.hub.listen(events.append)
        await run_all(ann)
        live = [type(e) for e in events if isinstance(e, (CellStreamEvent, CellOutputEvent))]
        assert live[0] is CellStreamEvent and live[-1] is CellOutputEvent
        (cell,) = (await ann.read()).cells
        assert [o.type for o in cell.outputs] == ["stream", "display"]
        assert cell.outputs[0].text == "first\n"  # type: ignore[union-attr]
        await flushed(engine)
    async with engine_for(tmp_path) as again:
        client = (await again.open("nb.alknb.py")).attach(ANN)
        (cell,) = (await client.read()).cells
        assert cell.output_origin == "saved"
        assert [o.type for o in cell.outputs] == ["stream", "display"]
