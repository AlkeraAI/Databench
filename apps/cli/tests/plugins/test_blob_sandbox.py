"""The per-chat sandbox dir (`chats/<sid>/sandbox/`) + its lifecycle.

The sandbox is where blob.materialize / plan mode write files. It lives INSIDE
the chat dir so deleting the chat reclaims it, and the ChatStore accessor + the
ToolContext threading are the two seams the write tools depend on.
"""

from __future__ import annotations

from pathlib import Path

from alkera_core.project.directory import SANDBOX_SUBDIR, ProjectDirectory


def test_chat_sandbox_dir_is_under_the_chat(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    sandbox = store.chat_sandbox_dir("sess-1")
    assert sandbox == project.chats_path / "sess-1" / SANDBOX_SUBDIR


def test_chat_sandbox_dir_is_not_created_eagerly(tmp_path: Path) -> None:
    # A read-only session must never materialize the dir just by asking for its path.
    project = ProjectDirectory(tmp_path / ".alkera")
    sandbox = project.chats().chat_sandbox_dir("sess-1")
    assert not sandbox.exists()


def test_delete_reclaims_the_sandbox(tmp_path: Path) -> None:
    project = ProjectDirectory(tmp_path / ".alkera")
    store = project.chats()
    sid = "sess-1"
    (project.chats_path / sid).mkdir(parents=True)
    sandbox = store.chat_sandbox_dir(sid)
    sandbox.mkdir(parents=True)
    (sandbox / "result.parquet").write_bytes(b"some bytes")
    assert sandbox.exists()

    store.delete(sid)

    assert not (project.chats_path / sid).exists()
    assert not sandbox.exists()


def test_sandbox_dir_threads_through_dispatch(tmp_path: Path) -> None:
    # The ToolContext seam: dispatch → build_context → ctx.sandbox_dir. Pin it with
    # a tiny probe tool so the whole feature doesn't silently lose the path.
    import asyncio
    from typing import ClassVar

    from alkera_cli.contracts.tool_types import Effect
    from alkera_cli.plugins.plugin_base.tool import (
        Tool,
        ToolContext,
        ToolRegistry,
        ToolSpec,
    )
    from pydantic import BaseModel

    class _ProbeIn(BaseModel):
        pass

    class _ProbeOut(BaseModel):
        sandbox: str

    class _ProbeTool(Tool[_ProbeIn, _ProbeOut]):
        spec: ClassVar[ToolSpec] = ToolSpec(name="probe.sandbox", effect_hint=Effect.READ)
        Input: ClassVar[type[BaseModel]] = _ProbeIn
        Output: ClassVar[type[BaseModel]] = _ProbeOut

        async def run(self, args: _ProbeIn, ctx: ToolContext) -> _ProbeOut:
            return _ProbeOut(sandbox=str(ctx.sandbox_dir))

    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    registry.register(_ProbeTool)
    sandbox = tmp_path / ".alkera" / "chats" / "s1" / "sandbox"
    out = asyncio.run(registry.dispatch("probe.sandbox", {}, sandbox_dir=sandbox))
    assert out["sandbox"] == str(sandbox)
