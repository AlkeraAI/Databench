"""Kernel lifecycle beyond runs: a deleted notebook, idle stop, rights."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.document.ops import ReplaceCell
from alkera_notebook.engine import (
    AllTarget,
    ForbiddenError,
    InspectQuery,
    WidgetAction,
)
from alkera_notebook.kernels.launch_local import group_pids
from nbeng_harness import VIEWER, engine_for, notebook, run_cells, until


async def test_lifecycle_deleted_notebook_stops_its_kernel(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        kernel = session.runtime.kernel
        assert kernel is not None
        (tmp_path / "ws" / "nb.alknb.py").unlink()
        await session.reload_from_store()
        assert (await ann.kernel("status")).state == "stopped"
        await until(lambda: group_pids(kernel.launched.pgid) == [], timeout_s=5)
        assert any(n.kind == "file_deleted" for n in (await ann.read()).notices)


async def test_lifecycle_idle_stop_only_when_configured(tmp_path: Path) -> None:
    async with engine_for(tmp_path, idle_s=0.3) as engine:
        _, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        await until(lambda: _state(ann, "stopped"), timeout_s=5)
    async with engine_for(tmp_path, name="other") as engine:
        _session, ann, (a,) = await notebook(engine, ["x = 1"])
        await run_cells(ann, a)
        await _sleep(0.6)
        assert (await ann.kernel("status")).state == "idle"


async def _state(client: object, state: str) -> bool:
    return (await client.kernel("status")).state == state  # type: ignore[attr-defined]


async def _sleep(s: float) -> None:
    import asyncio

    await asyncio.sleep(s)


@pytest.mark.parametrize(
    "action",
    [
        pytest.param("apply", id="lifecycle.viewer_cannot_edit"),
        pytest.param("run", id="lifecycle.viewer_cannot_run"),
        pytest.param("interrupt", id="lifecycle.viewer_cannot_interrupt"),
        pytest.param("comm", id="lifecycle.viewer_cannot_send_widget_messages"),
        pytest.param("inspect_value", id="lifecycle.viewer_cannot_inspect_values"),
        pytest.param("widget_set", id="lifecycle.viewer_cannot_set_widgets"),
    ],
)
async def test_lifecycle_rights_are_checked_per_call(tmp_path: Path, action: str) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(
            engine, ["import alkera.ui as ui\ns = ui.slider(0, 10, value=1)\nv = 2"]
        )
        await (await ann.run(AllTarget())).wait(30)
        mid = (await ann.widget(WidgetAction(action="list"))).widgets[0].model_id
        viewer = session.attach(VIEWER)
        with pytest.raises(ForbiddenError):
            if action == "apply":
                await viewer.apply([ReplaceCell(cell_id=a, source="x = 2")], None)
            elif action == "run":
                await viewer.run(AllTarget())
            elif action == "interrupt":
                await viewer.kernel("interrupt")
            elif action == "comm":
                await viewer.comm_send(
                    mid, "msg-1", {"data": {"method": "update", "state": {"value": 3}}}, []
                )
            elif action == "inspect_value":
                await viewer.inspect(InspectQuery(what="value", name="v"))
            else:
                await viewer.widget(WidgetAction(action="set", model_id=mid, state={"value": 3}))
        # Reading is never gated.
        assert (await viewer.read()).cells[0].id == a
        assert (await viewer.widget(WidgetAction(action="list"))).widgets[0].value == 1
        assert [
            v.name for v in (await viewer.inspect(InspectQuery(what="variables"))).variables or []
        ] == ["s", "ui", "v"]
