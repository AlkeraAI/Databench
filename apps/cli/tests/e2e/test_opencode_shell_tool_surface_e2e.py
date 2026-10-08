"""E2E proof: the model is offered exactly ONE shell tool, and it is ours.

Drives a REAL opencode subprocess and inspects the `tools` array actually sent to
the model, so this asserts what the model SEES rather than what we intended.

The bug this pins: the old approach denied the native shell via `ALKERA_PERMISSION`
and left it advertised, so the model was offered both a native `bash` and our
`alkera_bash`, and every attempt at the `bash` it was pretrained on burned a turn
on a `DeniedError` whose text is a raw ruleset dump that never names the
replacement. A deny cannot be the gate here: the subtraction that a bare
`pattern:"*"` deny gets (`Permission.disabled` → `resolveTools`) does not apply to
the pattern-scoped rules the shell needs, and even a bare one only holds while our
ruleset is the last word on the tool. The `tools()` filter is what removes it.

To confirm this is not coverage theater: unset `ALKERA_PARENT_SHELL` in
`opencode_http.py::_build_env` and both assertions below fail — the native
ShellTool reappears in the advertised set and our tool reverts to `alkera_bash`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import FOLLOWUP_KEY, text_chunks, tool_call_chunks

pytestmark = [
    pytest.mark.opencode_e2e,
    pytest.mark.skipif(os.name != "posix", reason="the parent-hosted shell is POSIX-only"),
]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}


def _advertised_tools(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The tools offered to the model in one request, by name.

    OpenAI wire shape is ``{"type": "function", "function": {"name", "parameters"}}``;
    fall back to a flat shape so a provider-transform tweak doesn't silently make
    this scan find nothing (which would turn every assertion below vacuous)."""
    tools: dict[str, dict[str, Any]] = {}
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if isinstance(name, str):
            tools[name] = fn
    return tools


async def _wait_for_tool_surface(
    server: Any, *, budget_seconds: float = 60.0
) -> dict[str, dict[str, Any]]:
    """Poll until a recorded request carries a tool array, and return those tools.

    Polls rather than reading `server.requests` once: `send_prompt` returns before
    the model request necessarily lands, so a single read races the transport.
    Scans all requests because opencode also fires auxiliary calls (e.g. title
    generation) that carry no tools at all."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        for request in server.requests:
            tools = _advertised_tools(request)
            if tools:
                return tools
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no model request carried a tool array within {budget_seconds}s "
        f"({len(server.requests)} requests recorded)"
    )


async def test_exactly_one_shell_tool_is_offered_and_it_is_ours(tmp_path: Path) -> None:
    """The model sees a bare `bash` and no `alkera_bash` — one shell surface, under
    the name it was pretrained on."""
    script = {
        "*": tool_call_chunks("bash", {"command": "echo hi", "description": "echo"}),
        FOLLOWUP_KEY: text_chunks("done"),
    }
    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, server):
        session = await runtime.open_chat(sid)
        await session.send_prompt("say hi", model=_MODEL)
        names = set(await _wait_for_tool_surface(server))

    # Our tool is advertised under the bare native name...
    assert "bash" in names, f"the model was offered no `bash` tool; saw {sorted(names)}"
    # ...and NOT additionally under the loopback-MCP composite, which would mean the
    # bare-name remap in vendor `mcp/index.ts` did not fire.
    assert "alkera_bash" not in names, (
        f"the shell is still offered as `alkera_bash`; saw {sorted(names)}"
    )
    # Exactly one shell surface — a second one would be the native ShellTool that
    # `tool/registry.ts` is supposed to drop.
    shell_like = [n for n in names if "bash" in n.lower() or "shell" in n.lower()]
    assert shell_like == ["bash"], f"expected one shell tool, saw {sorted(shell_like)}"


async def test_the_offered_bash_is_the_parent_hosted_tool(tmp_path: Path) -> None:
    """Name equality alone would pass against opencode's native shell too, so prove
    the `bash` the model calls is OURS — via `background`, a parameter only the
    parent-hosted tool declares."""
    script = {
        "*": tool_call_chunks("bash", {"command": "echo hi", "description": "echo"}),
        FOLLOWUP_KEY: text_chunks("done"),
    }
    async with opencode_e2e_runtime(tmp_path, mock_script=script) as (runtime, sid, server):
        session = await runtime.open_chat(sid)
        await session.send_prompt("say hi", model=_MODEL)
        tools = await _wait_for_tool_surface(server)

    assert "bash" in tools, f"no `bash` tool was advertised; saw {sorted(tools)}"
    properties = (tools["bash"].get("parameters") or {}).get("properties") or {}
    assert "background" in properties, (
        "the advertised `bash` has no `background` parameter — it is opencode's "
        f"native shell, not ours. Params: {sorted(properties)}"
    )
