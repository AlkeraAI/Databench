"""LIVE web-tools tests — hit the REAL search engines + a real public site
(marked ``live``, opt-in via ``uv run pytest -m live``). No credential needed,
but the network is. A web tool wraps a transport failure in its own tool error,
which keeps the network's refusal, so an offline run skips rather than reds.

This is the wire proof the mocked unit tier can't give: ddgs' engine responses
still parse into ``{title, url, snippet}`` rows, and a primp browser-fingerprint
GET is accepted by an ordinary site and yields readable markdown.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers.connectors import live_reader, tool_context
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_cli.plugins.plugin_base.web_tools import register_web_tools

pytestmark = pytest.mark.live

_BROKER = object()  # a fetch is an egress effect, and the gate needs an approver present


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    reg = tool_context(tmp_path).registry
    register_web_tools(reg)
    return reg


async def test_live_search_returns_parsed_results(registry: ToolRegistry, tmp_path: Path) -> None:
    read = live_reader(registry, tmp_path, None)
    out = await read("web.search", {"query": "python programming language", "max_results": 5})
    results = out["results"]
    assert results, "a common query must yield results"
    assert all(r["url"].startswith("http") for r in results)
    assert any(r["title"] for r in results)


async def test_live_fetch_renders_readable_markdown(registry: ToolRegistry, tmp_path: Path) -> None:
    args = {"url": "https://example.com/", "max_chars": 5000}
    read = live_reader(registry, tmp_path, None, broker=_BROKER, permission_mode="bypass")
    out = await read("web.fetch", args)
    assert out["status"] == 200
    # example.com's canonical marker text, surviving the markdown extraction.
    assert "Example Domain" in (out["title"] + out["content"])
