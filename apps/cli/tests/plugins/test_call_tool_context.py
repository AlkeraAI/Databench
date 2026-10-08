"""``call_tool`` carries the WHOLE session context into the tool it dispatches.

Two reviews confirmed the indirection dropped ``tool_scope`` (so a scoped agent
could reach any tool by name — ``ToolContext`` had no such field) and ``abort``
(so Stop never reached ``call_integration_sdk``, which is reachable no other
way). The fix is one spelling, ``ToolContext.dispatch_kwargs``, and the last
case pins that spelling against ``ToolRegistry.dispatch``'s own signature so the
next handle added to dispatch cannot be dropped by the indirection either.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, ClassVar

from alkera_cli.plugins.plugin_base import Effect, ToolRegistry
from alkera_cli.plugins.plugin_base.bash_exec import ExecResult, run_with_abort
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolSpec
from alkera_core.project.directory import ProjectDirectory
from pydantic import BaseModel


class _NoInput(BaseModel):
    pass


class _Seen(BaseModel):
    seen: dict[str, Any]


class _SlowTool(Tool[_NoInput, _Seen]):
    """A long-tail (``hot=False``) tool that runs until told to stop — the shape
    of ``call_integration_sdk``: reachable only through ``call_tool``."""

    spec: ClassVar[ToolSpec] = ToolSpec(name="probe.slow", hot=False, effect_hint=Effect.READ)
    Input: ClassVar[type[BaseModel]] = _NoInput
    Output: ClassVar[type[BaseModel]] = _Seen

    async def run(self, args: _NoInput, ctx: ToolContext) -> _Seen:
        async def _forever() -> ExecResult:
            await asyncio.sleep(30)
            return ExecResult(output="done", exit_code=0)

        await run_with_abort(_forever, ctx.abort)
        return _Seen(seen={"finished": True})


class _WriteTool(Tool[_NoInput, _Seen]):
    spec: ClassVar[ToolSpec] = ToolSpec(name="probe.write", hot=False, effect_hint=Effect.WRITE)
    Input: ClassVar[type[BaseModel]] = _NoInput
    Output: ClassVar[type[BaseModel]] = _Seen

    async def run(self, args: _NoInput, ctx: ToolContext) -> _Seen:
        return _Seen(seen={"ran": True})


class _EchoContextTool(Tool[_NoInput, _Seen]):
    """Reports which handles reached it, by identity."""

    spec: ClassVar[ToolSpec] = ToolSpec(name="probe.echo", hot=False, effect_hint=Effect.READ)
    Input: ClassVar[type[BaseModel]] = _NoInput
    Output: ClassVar[type[BaseModel]] = _Seen

    async def run(self, args: _NoInput, ctx: ToolContext) -> _Seen:
        return _Seen(seen={name: id(value) for name, value in ctx.dispatch_kwargs().items()})


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_meta_tools(registry)
    registry.register(_SlowTool)
    registry.register(_WriteTool)
    registry.register(_EchoContextTool)
    return registry


async def test_a_read_only_scope_cannot_reach_a_write_tool_through_call_tool(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch(
        "call_tool", {"name": "probe.write", "args": {}}, tool_scope="read_only"
    )
    assert "not available to this agent" in out["error"]


async def test_a_name_list_scope_cannot_reach_an_unlisted_tool_through_call_tool(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch(
        "call_tool",
        {"name": "probe.write", "args": {}},
        tool_scope=["call_tool", "search_tools", "probe.echo"],
    )
    assert "not available to this agent" in out["error"]
    # And a listed tool still runs through the same indirection.
    ok = await registry.dispatch(
        "call_tool",
        {"name": "probe.echo", "args": {}},
        tool_scope=["call_tool", "search_tools", "probe.echo"],
    )
    assert "error" not in ok


async def test_an_unscoped_session_reaches_the_write_tool_through_call_tool(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("call_tool", {"name": "probe.write", "args": {}})
    # The write pre-gate may still refuse it (no broker bound here); what the
    # scope check must NOT have said is that the tool is unavailable.
    assert "not available to this agent" not in out.get("error", "")


async def test_stop_cancels_a_long_tool_reached_through_call_tool(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    abort = asyncio.Event()
    task = asyncio.ensure_future(
        registry.dispatch("call_tool", {"name": "probe.slow", "args": {}}, abort=abort)
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    abort.set()
    out = await asyncio.wait_for(task, timeout=2)
    assert "command cancelled" in out["error"]


async def test_every_dispatch_handle_reaches_the_nested_tool(tmp_path: Path) -> None:
    """Pass a distinct sentinel for every keyword ``dispatch`` takes and assert
    the SAME object arrives inside the tool ``call_tool`` dispatched."""
    registry = _registry(tmp_path)
    parameters = inspect.signature(ToolRegistry.dispatch).parameters
    handles = [
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    sentinels: dict[str, Any] = {}
    for name in handles:
        if name == "permission_mode":
            sentinels[name] = "auto"
        elif name == "session_id":
            sentinels[name] = "sid-probe"
        elif name == "owner_session_id":
            # A chat id: the context names blobs as this chat, so it must be text.
            sentinels[name] = "owner-probe"
        elif name == "task_goal":
            sentinels[name] = "the goal"
        elif name == "tool_scope":
            sentinels[name] = ["call_tool", "probe.echo"]
        else:
            sentinels[name] = object()
    out = await registry.dispatch("call_tool", {"name": "probe.echo", "args": {}}, **sentinels)
    seen = out["result"]["seen"]
    # Strings and lists are compared by identity too: the nested context must
    # hold the very objects handed to the outer dispatch, not copies or defaults.
    for name, value in sentinels.items():
        assert name in seen, f"{name} is not forwarded by call_tool"
        assert seen[name] == id(value), f"{name} did not reach the nested tool"


def test_dispatch_kwargs_spells_every_keyword_of_dispatch() -> None:
    """The one spelling stays complete: a keyword added to ``dispatch`` without
    a field on the context is caught here, not in production."""
    parameters = inspect.signature(ToolRegistry.dispatch).parameters
    keywords = {
        name
        for name, parameter in parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    fields = set(ToolContext.__dataclass_fields__)
    assert keywords <= fields, sorted(keywords - fields)
    registry = ToolRegistry.__new__(ToolRegistry)
    context = ToolContext(registry=registry, blobs=None)  # type: ignore[arg-type]
    assert set(context.dispatch_kwargs()) == keywords


async def test_a_nested_tool_error_is_still_this_calls_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("call_tool", {"name": "no.such.tool", "args": {}})
    assert out["error"].startswith("unknown tool")
    assert isinstance(ToolError("x"), Exception)
