"""E2E proof: with delegation off, the model is offered no way to spawn an agent.

Drives a REAL opencode subprocess and inspects the `tools` array actually sent to
the model, so this asserts what the model SEES rather than what we intended.

The gap this pins: withholding Alkera's own `spawn_agent` / `list_agent_types`
leaves opencode's own spawner (`task`) in place. Both cases below lift the
`"task": "deny"` permission rule, because a bare deny already subtracts the tool
(`Permission.disabled` → request.ts `resolveTools`) and would make an assertion
about the registry filter vacuous — with the rule gone, the ONLY thing that can
remove `task` is the `ALKERA_SUBAGENTS_ENABLED` filter in vendor
`tool/registry.ts`, which is what the off-case proves.

To confirm this is not coverage theater: drop that filter from the vendored
registry and the off-case fails — `task` comes back into the advertised set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks
from alkera_cli.harness.adapters import opencode_http

pytestmark = [pytest.mark.opencode_e2e]

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}


@pytest.fixture(autouse=True)
def _permission_allows_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the call-time deny for the duration, so the advertised set is decided
    by the registry filter alone and neither case can pass for the wrong reason."""
    monkeypatch.delitem(opencode_http._OPENCODE_PERMISSION_ASK, "task", raising=True)


def _advertised_tools(request: dict[str, Any]) -> set[str]:
    """The names of the tools offered to the model in one request.

    OpenAI wire shape is ``{"type": "function", "function": {"name", ...}}``; fall
    back to a flat shape so a provider-transform tweak can't silently make this
    scan find nothing (which would turn every assertion below vacuous)."""
    names: set[str] = set()
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


async def _wait_for_tool_surface(server: Any, *, budget_seconds: float = 60.0) -> set[str]:
    """Poll until a recorded request carries a tool array, and return those names.

    `send_prompt` returns before the model request necessarily lands, so a single
    read races the transport; opencode also fires auxiliary calls (title
    generation) that carry no tools at all."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    while loop.time() < deadline:
        for request in server.requests:
            names = _advertised_tools(request)
            if names:
                return names
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"no model request carried a tool array within {budget_seconds}s "
        f"({len(server.requests)} requests recorded)"
    )


async def _offered_tools(tmp_path: Path, *, subagents: bool) -> set[str]:
    script = {"*": text_chunks("done")}
    async with opencode_e2e_runtime(tmp_path, mock_script=script, subagents_enabled=subagents) as (
        runtime,
        sid,
        server,
    ):
        session = await runtime.open_chat(sid)
        await session.send_prompt("say hi", model=_MODEL)
        return await _wait_for_tool_surface(server)


async def test_delegation_off_offers_no_agent_spawner(tmp_path: Path) -> None:
    """A deployment with subagents off serves no spawner, vendor one included."""
    names = await _offered_tools(tmp_path, subagents=False)
    assert "task" not in names, f"opencode's own spawner is still advertised; saw {sorted(names)}"
    # The session is otherwise intact — an empty/garbled tool array would satisfy
    # the assertion above for the wrong reason.
    assert "read" in names, f"the tool surface looks broken, not filtered: {sorted(names)}"


async def test_delegation_on_still_offers_the_spawner(tmp_path: Path) -> None:
    """The filter is conditional: nothing is withheld from a deployment that
    serves delegation, so the off-case above cannot be passing unconditionally."""
    names = await _offered_tools(tmp_path, subagents=True)
    assert "task" in names, f"the task tool was dropped with delegation ON; saw {sorted(names)}"
