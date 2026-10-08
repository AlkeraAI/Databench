"""A notebook tool result on a huge notebook reaches the model inline.

The open core bounds every notebook result to its own budget; this pins the
other half: what the harness actually sends (the dispatch result encoded by
the one wire encoder, with the notebook guide the first result of a
conversation carries) stays under the harness's inline cap, so it is never
spilled to a blob with a two-kilobyte preview.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_cli.notebooks.session import WorkspaceNotebookService
from alkera_cli.notebooks.tools import register_notebook_tools
from alkera_cli.plugins.plugin_base.delivery import RESULT_INLINE_BYTE_CAP
from alkera_cli.plugins.plugin_base.permissions import DecisionSink, PermissionsConfig
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_cli.plugins.plugin_base.wire import model_facing_text
from alkera_core.project import ProjectDirectory
from alkera_notebook.sim.oracle import Cell
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.tools import NOTEBOOK_GUIDE, ActorRef, NotebookHost

PATH = "huge.alknb.py"
CELLS = 5_000


def _source(i: int) -> str:
    if i % 500 == 7:
        return "\n".join(f"v{i}_{j} = {j}  # a long cell" for j in range(2_000))
    return f"# step {i}\nx{i} = {i}" if i % 100 == 0 else f"# step {i}\nx{i} = x{i - 1} + 1"


class _Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.project = ProjectDirectory(tmp_path / ".alkera")
        self.registry = ToolRegistry(
            self.project.blobs(), decision_sink=DecisionSink(self.project.path)
        )
        register_notebook_tools(self.registry)
        self.workspace: ReferenceWorkspace | None = None

        def factory(root: Path, actor: ActorRef) -> NotebookHost:
            if self.workspace is None:
                self.workspace = ReferenceWorkspace(root=str(root))
            return self.workspace.host(actor)

        self.service = WorkspaceNotebookService(factory, lambda *_: None)
        self.registry.notebooks = self.service
        self.sandbox = tmp_path / "workspace"
        self.sandbox.mkdir()

    async def call(self, tool: str, args: dict[str, Any], session_id: str) -> dict[str, Any]:
        return dict(
            await self.registry.dispatch(
                tool,
                {"path": PATH, **args},
                session_id=session_id,
                permissions=PermissionsConfig(),
                permission_mode="bypass",
                broker=object(),
                decision_sink=DecisionSink(self.project.path),
                alkera_dir=self.project.path,
                sandbox_dir=self.sandbox,
            )
        )

    async def huge(self) -> None:
        await self.call("notebook.create", {}, session_id="setup")
        assert self.workspace is not None
        nb = self.workspace.notebooks[PATH]
        doc = nb.doc.copy()
        for i in range(1, CELLS):
            cid = f"{i:010d}".translate(str.maketrans("01", "ab"))
            doc.cells[cid] = Cell(id=cid, kind="python", name=f"c{i}", source=_source(i))
            doc.order.append(cid)
        nb.doc = doc


@pytest.mark.parametrize(
    ("tool", "args", "field"),
    [
        pytest.param("notebook.read", {}, "cells", id="read"),
        pytest.param("notebook.read", {"cells": ["c507", "c1007"]}, "cells", id="read_sources"),
        pytest.param("notebook.graph", {}, "summary", id="graph"),
        pytest.param("notebook.graph", {"summary": False}, "edges", id="graph_edges"),
        pytest.param(
            "notebook.run", {"target": {"kind": "all"}, "confirm_expensive": True}, "plan", id="run"
        ),
    ],
)
async def test_the_first_result_on_a_huge_notebook_is_delivered_inline_with_the_guide(
    tmp_path: Path, tool: str, args: dict[str, Any], field: str
) -> None:
    rig = _Rig(tmp_path)
    await rig.huge()
    out = await rig.call(tool, args, session_id="chat")
    assert "error" not in out, out.get("error")
    assert out["notebook_guide"] == NOTEBOOK_GUIDE.model_dump()
    assert field in out
    assert len(model_facing_text(out).encode()) <= RESULT_INLINE_BYTE_CAP
