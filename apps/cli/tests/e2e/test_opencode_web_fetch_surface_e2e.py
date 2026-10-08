"""E2E proof: the model is offered exactly ONE fetch tool, and which one depends
on whether we mount our own.

Drives a REAL opencode subprocess and inspects the `tools` array actually sent to
the model, so this asserts what the model SEES rather than what we intended.

The bug this pins: opencode's advertised tool set (vendor `tool/registry.ts`
`tools()`) has no permission filter, so the native `webfetch` is offered beside
our `web_fetch` whenever the org's web toggle is on. Ours has the blob spill, the
per-hop private-IP check and the decision log; the vendor one is classified EGRESS
and is therefore refused outright in `read_only`/`plan` — the stance a cloud chat
starts in. The model was being handed a fetch tool that could only ever fail.

The mirror case matters just as much: with the mount OFF the vendor tool must
stay, or a deployment with the toggle off would have no fetch tool at all.

To confirm this is not coverage theater: unset `ALKERA_PARENT_WEB_FETCH` in
`opencode_http.py::_build_env` and the first test fails — the native `webfetch`
reappears in the advertised set beside `web_fetch`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from _helpers.opencode_runner import opencode_e2e_runtime
from _mocks.mock_openai_server import text_chunks

pytestmark = pytest.mark.opencode_e2e

_MODEL = {"provider_id": "mock", "model_id": "mock-model"}
_SCRIPT = {"*": text_chunks("done")}

#: Every name a tool that fetches a URL off the open internet is advertised under:
#: ours (the `web` loopback mount) and the vendor's (opencode's own builtin).
_WEB_FETCH_SPELLINGS = {"web_fetch", "webfetch"}


def _advertised_tools(request: dict[str, Any]) -> set[str]:
    """The names of the tools offered to the model in one request.

    OpenAI wire shape is ``{"type": "function", "function": {"name", ...}}``; fall
    back to a flat shape so a provider-transform tweak doesn't silently make this
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


async def _wait_for_tool_surface(server: Any, *, budget_seconds: float = 90.0) -> set[str]:
    """Poll until a recorded request carries a tool array, and return those names.

    Polls rather than reading `server.requests` once: `send_prompt` returns before
    the model request necessarily lands. Scans all requests because opencode also
    fires auxiliary calls (e.g. title generation) that carry no tools at all."""
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


async def _surface(tmp_path: Path, *, web_search_enabled: bool) -> set[str]:
    async with opencode_e2e_runtime(
        tmp_path, mock_script=_SCRIPT, web_search_enabled=web_search_enabled
    ) as (runtime, sid, server):
        session = await runtime.open_chat(sid)
        await session.send_prompt("hello", model=_MODEL)
        return await _wait_for_tool_surface(server)


async def test_our_web_tools_replace_the_vendor_fetch(tmp_path: Path) -> None:
    """Toggle on: the model sees `web_fetch` (ours) and no `webfetch` (the vendor's)."""
    names = await _surface(tmp_path, web_search_enabled=True)

    assert "web_fetch" in names, f"the model was offered no `web_fetch`; saw {sorted(names)}"
    assert "web_search" in names, f"the model was offered no `web_search`; saw {sorted(names)}"
    assert "webfetch" not in names, (
        "the vendor fetch tool is still advertised beside ours — the model has two "
        f"fetch tools and one of them is always refused. Saw {sorted(names)}"
    )
    # Exactly one WEB fetch surface, whichever way it is spelled. Scoped to the
    # known spellings on purpose: `alkera_fetch_result` also carries "fetch" in
    # its name, but it reads back a spilled tool result from our own blob store
    # and has nothing to do with the open internet.
    assert names & _WEB_FETCH_SPELLINGS == {"web_fetch"}, (
        f"expected one web fetch tool, saw {sorted(names & _WEB_FETCH_SPELLINGS)}"
    )


async def test_without_our_web_tools_the_vendor_fetch_stays(tmp_path: Path) -> None:
    """Toggle off: dropping the vendor tool unconditionally would leave the agent
    with no fetch at all, so it must still be advertised."""
    names = await _surface(tmp_path, web_search_enabled=False)

    assert "webfetch" in names, (
        "the vendor fetch tool was dropped with no replacement mounted — this "
        f"deployment has no fetch tool at all. Saw {sorted(names)}"
    )
    assert "web_fetch" not in names, (
        f"`web_fetch` is advertised with the org toggle off; saw {sorted(names)}"
    )
