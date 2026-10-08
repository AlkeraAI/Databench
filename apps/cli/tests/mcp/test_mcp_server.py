"""The parent-hosted loopback MCP server (Option A) — the single transport both
harness backends connect to. These drive a REAL MCP client over loopback HTTP to
prove: the fixed hot set is served, ``search_tools``→``call_tool`` runs a tool,
the bearer token is enforced, and — the decisive property — a write gates through
the session's broker + live permission mode (Layer A) because dispatch runs in
THIS process. The connector chokepoint (Layer B) is covered in test_snowflake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import duckdb
import pytest
from alkera_cli.harness.mcp_server import (
    AlkeraToolServer,
    SessionToolBinding,
    _DropIncompleteResponse,
    _quiet_transport_logs,
)
from alkera_cli.plugins.generic_sql import GenericSqlPlugin
from alkera_cli.plugins.generic_sql.plugin import GenericSqlCapabilities
from alkera_cli.plugins.plugin_base import Environment, ToolRegistry
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.permissions import DecisionSink
from alkera_cli.plugins.plugin_base.permissions.refusal_words import NOT_GRANTED
from alkera_cli.plugins.plugin_base.sql_tools import register_sql_tools
from alkera_cli.plugins.plugin_base.task_tools import register_task_tools
from alkera_core.project.directory import ProjectDirectory
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class _FakeBroker:
    """Returns a fixed option; records what it was asked to resolve."""

    def __init__(self, option: str) -> None:
        self.option = option
        self.requests: list[Any] = []

    async def resolve(self, request: Any) -> str:
        self.requests.append(request)
        return self.option


def _registry(tmp_path: Path) -> ToolRegistry:
    dbfile = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(dbfile))
    con.execute("CREATE TABLE t (id INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    con.close()
    built, _credential = GenericSqlPlugin().build_connection(
        "duck", "", {"url": f"duckdb:///{dbfile}"}
    )
    conn = built.model_copy(update={"environment": Environment.LOCAL})
    project = ProjectDirectory(tmp_path / ".alkera")
    # Every real session hands the server a decisions log (runtime builds a per-chat
    # one). Without a sink the gate refuses before any of these tests reach what they
    # are pinning.
    registry = ToolRegistry(project.blobs(), decision_sink=DecisionSink(project.path))
    registry.register_connection(conn, capabilities=GenericSqlCapabilities().capabilities(conn))
    register_meta_tools(registry)
    register_sql_tools(registry)
    return registry


def _file_size(path: str) -> int:
    """Bytes on disk at ``path`` (sync, so an async test doesn't block the loop)."""
    return Path(path).stat().st_size


async def _server(binding: SessionToolBinding) -> AlkeraToolServer:
    server = AlkeraToolServer(binding)
    await server.start()
    return server


async def test_lists_hot_set_and_runs_search_then_call(tmp_path: Path) -> None:
    binding = SessionToolBinding(registry=_registry(tmp_path), session_id="s1")
    server = await _server(binding)
    try:
        async with streamablehttp_client(server.url, headers=server.auth_headers()) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = await sess.list_tools()
                # The meta-tools (incl. list_plugins) + sql.connections (hot, because
                # this registry has a SQL connection — the agent must discover handles
                # without a search).
                assert sorted(t.name for t in tools.tools) == [
                    "call_tool",
                    "list_plugins",
                    "search_tools",
                    "sql.connections",
                ]

                found = json.loads(
                    (await sess.call_tool("search_tools", {"query": "query the duck db"}))
                    .content[0]
                    .text
                )
                assert "sql.query" in [c["name"] for c in found["tools"]]

                out = json.loads(
                    (
                        await sess.call_tool(
                            "call_tool",
                            {
                                "name": "sql.query",
                                "args": {
                                    "mode": "sql",
                                    "connection": "duck",
                                    "sql": "SELECT id FROM t",
                                },
                            },
                        )
                    )
                    .content[0]
                    .text
                )
                assert out["result"]["preview_rows"] == [[42]]
    finally:
        await server.stop()


async def test_bearer_auth_enforced_at_http_layer(tmp_path: Path) -> None:
    """C8: the loopback bearer token is the whole security boundary (any local
    process could otherwise reach the ephemeral port). Pin it at the HTTP layer,
    not just via the MCP client: a MISSING token and a WRONG token both get a hard
    401 with the ``unauthorized`` body, and the CORRECT token clears the auth gate
    (the manager answers, but never with the auth 401)."""
    import httpx

    binding = SessionToolBinding(registry=_registry(tmp_path), session_id="s1")
    server = await _server(binding)
    mcp_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    try:
        # NO follow_redirects: the bare `/mcp` must be auth-gated DIRECTLY (401), not
        # 307-redirect to `/mcp/` before the auth check runs (C8 — redirect_slashes
        # is off). A 307 here would be a pre-auth oracle and fail this assertion.
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            # No Authorization header at all → 401 (auth is checked before routing).
            no_auth = await client.post(server.url, json=body, headers=mcp_headers)
            assert no_auth.status_code == 401
            assert no_auth.json()["error"] == "unauthorized"

            # A non-matching token → 401 (constant-time compare, so any mismatch).
            wrong = await client.post(
                server.url, json=body, headers={**mcp_headers, "Authorization": "Bearer WRONG"}
            )
            assert wrong.status_code == 401

            # The correct token passes the gate — whatever the manager does next,
            # it is NOT the auth 401 (proving auth, not request shape, was the diff).
            ok = await client.post(
                server.url, json=body, headers={**mcp_headers, **server.auth_headers()}
            )
            assert ok.status_code != 401
    finally:
        await server.stop()


async def test_wrong_bearer_token_is_rejected(tmp_path: Path) -> None:
    binding = SessionToolBinding(registry=_registry(tmp_path), session_id="s1")
    server = await _server(binding)
    try:
        with pytest.raises(BaseException):  # noqa: B017 - transport raises a group/HTTP error
            async with streamablehttp_client(
                server.url, headers={"Authorization": "Bearer WRONG"}
            ) as (r, w, _):
                async with ClientSession(r, w) as sess:
                    await asyncio.wait_for(sess.initialize(), timeout=8)
    finally:
        await server.stop()


async def _run_write(binding: SessionToolBinding) -> Any:
    """Drive a write through call_tool over the real loopback transport; return the raw
    MCP ``CallToolResult`` so a caller can assert BOTH ``isError`` and the payload text."""
    server = await _server(binding)
    try:
        async with streamablehttp_client(server.url, headers=server.auth_headers()) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                return await sess.call_tool(
                    "call_tool",
                    {
                        "name": "sql.query",
                        "args": {
                            "mode": "sql",
                            "connection": "duck",
                            "sql": "CREATE TABLE made AS SELECT 1 AS n",
                        },
                    },
                )
    finally:
        await server.stop()


async def test_write_gates_through_session_broker_approve(tmp_path: Path) -> None:
    # Dispatch runs in-parent, so the session broker is reachable — a write the
    # human approves runs (this is what the OpenCode child could NOT do before).
    binding = SessionToolBinding(
        registry=_registry(tmp_path),
        broker=_FakeBroker("allow_once"),
        session_id="s1",
        permission_mode="default",
    )
    res = await _run_write(binding)
    assert res.isError is not True  # a successful write is NOT flagged an error
    payload = json.loads(res.content[0].text)
    assert "error" not in payload.get("result", {}), payload


async def test_write_gates_through_session_broker_reject(tmp_path: Path) -> None:
    binding = SessionToolBinding(
        registry=_registry(tmp_path),
        broker=_FakeBroker("reject_once"),
        session_id="s1",
        permission_mode="default",
    )
    res = await _run_write(binding)
    # A denied write is surfaced as a proper MCP tool ERROR (isError), not a
    # "successful" result the model has to notice contains an error string.
    assert res.isError is True
    payload = json.loads(res.content[0].text)
    assert payload["error"] == NOT_GRANTED


async def test_live_permission_mode_read_only_refuses_write(tmp_path: Path) -> None:
    # read_only rejects ALL mutation regardless of the broker — proves the binding's
    # live mode (mutated by ChatSession.set_permission_mode) reaches the gate.
    binding = SessionToolBinding(
        registry=_registry(tmp_path),
        broker=_FakeBroker("allow_once"),  # would approve IF asked
        session_id="s1",
        permission_mode="read_only",
    )
    res = await _run_write(binding)
    assert res.isError is True
    payload = json.loads(res.content[0].text)
    assert "permission denied" in payload["error"]


# --- transport-log suppression -------------------------------------------------
# The stateless loopback server closes the agent's streaming MCP request each turn,
# so uvicorn logs a benign ERROR ~1/sec per open chat. _quiet_transport_logs drops
# exactly that line while leaving every other record (incl. real errors) intact.


def _record(name: str, msg: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.ERROR, __file__, 0, msg, (), None)


def test_drop_filter_removes_only_the_incomplete_response_line() -> None:
    f = _DropIncompleteResponse()
    dropped = _record("uvicorn.error", "ASGI callable returned without completing response.")
    kept = _record("uvicorn.error", "Connection reset by peer")
    assert f.filter(dropped) is False
    assert f.filter(kept) is True


def test_drop_filter_matches_with_substituted_args() -> None:
    # uvicorn logs this with %-style args, so the filter must match the FORMATTED
    # message (getMessage()), not the raw template.
    rec = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        0,
        "ASGI callable returned without completing %s.",
        ("response",),
        None,
    )
    assert _DropIncompleteResponse().filter(rec) is False


def test_quiet_transport_logs_is_idempotent_and_installs_one_filter() -> None:
    import alkera_cli.harness.mcp_server as mod

    uvicorn_error = logging.getLogger("uvicorn.error")
    streamable = logging.getLogger("mcp.server.streamable_http")
    before_filters = list(uvicorn_error.filters)
    before_level = streamable.level
    before_flag = mod._transport_logs_quieted
    try:
        mod._transport_logs_quieted = False
        uvicorn_error.filters = [
            g for g in uvicorn_error.filters if not isinstance(g, _DropIncompleteResponse)
        ]

        _quiet_transport_logs()
        _quiet_transport_logs()  # second call is a no-op — no duplicate filter

        installed = [g for g in uvicorn_error.filters if isinstance(g, _DropIncompleteResponse)]
        assert len(installed) == 1
        assert streamable.level == logging.WARNING
        # the benign line is now suppressed on the real logger
        assert (
            installed[0].filter(
                _record("uvicorn.error", "ASGI callable returned without completing response.")
            )
            is False
        )
    finally:
        uvicorn_error.filters = before_filters
        streamable.setLevel(before_level)
        mod._transport_logs_quieted = before_flag


async def _spawn(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - never invoked
    raise AssertionError("spawn must not be called here")


@pytest.mark.parametrize(
    ("is_root", "present"),
    [
        pytest.param(True, True, id="root-sees-manage_tasks"),
        pytest.param(False, False, id="subagent-cannot-see-manage_tasks"),
    ],
)
async def test_task_tool_withheld_from_subagent_through_the_real_server(
    tmp_path: Path, is_root: bool, present: bool
) -> None:
    """The hard requirement, proven through the ACTUAL loopback server: a root
    binding (spawn set) advertises manage_tasks; a subagent binding (spawn None)
    does NOT — exercising _list's `is_root = binding.spawn is not None` →
    allow_task_tools wiring, not just the lower layers in isolation."""
    registry = ToolRegistry(ProjectDirectory(tmp_path / ".alkera").blobs())
    register_meta_tools(registry)
    register_task_tools(registry)
    binding = SessionToolBinding(
        registry=registry, session_id="s1", spawn=(_spawn if is_root else None)
    )
    server = await _server(binding)
    try:
        async with streamablehttp_client(server.url, headers=server.auth_headers()) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                names = {t.name for t in (await sess.list_tools()).tools}
                assert ("manage_tasks" in names) is present
    finally:
        await server.stop()


async def test_web_mount_serves_bare_names_and_dispatches(tmp_path: Path) -> None:
    """The dedicated `/mcp-web` mount: advertises the web tools under their BARE
    names (`search`/`fetch` — so the backend composes model-facing `web_search`/
    `web_fetch`, never `alkera_…`), maps a call back to the registry name, and the
    main `/mcp` mount does NOT double-advertise them."""
    from unittest.mock import patch

    from alkera_cli.plugins.plugin_base.web_tools import register_web_tools

    registry = _registry(tmp_path)
    register_web_tools(registry)
    binding = SessionToolBinding(registry=registry, session_id="s1")
    server = await _server(binding)
    try:
        # The web mount lists exactly the bare-named web tools…
        async with streamablehttp_client(server.web_url, headers=server.auth_headers()) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = await sess.list_tools()
                assert {t.name for t in tools.tools} == {"search", "fetch"}

                # …and a bare-named call dispatches the real registry tool.
                with patch("ddgs.DDGS") as ddgs_cls:
                    ddgs_cls.return_value.text.return_value = [
                        {"title": "T", "href": "https://t.example", "body": "s"}
                    ]
                    result = await sess.call_tool("search", {"query": "q"})
                payload = json.loads(result.content[0].text)
                assert payload["results"][0]["url"] == "https://t.example"

        # The main mount must not also advertise them (no alkera_web_… composite).
        async with streamablehttp_client(server.url, headers=server.auth_headers()) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = await sess.list_tools()
                names = {t.name for t in tools.tools}
                assert not {n for n in names if n.startswith("web.") or n in ("search", "fetch")}
    finally:
        await server.stop()


async def test_web_mount_requires_the_bearer_token(tmp_path: Path) -> None:
    # The auth gate runs BEFORE routing — the new mount must be behind it too.
    import httpx

    registry = _registry(tmp_path)
    binding = SessionToolBinding(registry=registry, session_id="s1")
    server = await _server(binding)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(server.web_url, json={})
        assert resp.status_code == 401
    finally:
        await server.stop()


async def test_a_serve_that_ends_before_it_binds_fails_the_start_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start waits for the server to say it has bound. A serve that raised
    before then never will, and the wait must end with that failure rather
    than poll a dead task for the rest of the process."""
    import uvicorn

    async def serve(self: Any, sockets: Any = None) -> None:
        raise RuntimeError("no port for you")

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    server = AlkeraToolServer(SessionToolBinding(registry=_registry(tmp_path), session_id="s1"))
    async with asyncio.timeout(5.0):
        with pytest.raises(RuntimeError, match="no port for you"):
            await server.start()
        await server.stop()  # nothing to stop; a no-op, not a second failure


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only bash tool")
async def test_a_host_spilled_result_declares_where_the_rest_went(tmp_path: Path) -> None:
    """A result the tool already shortened says so, and names its own file.

    The declaration is what stops a backend shortening that tail a second time
    into a file of its own and stamping THAT path on the tool call — which would
    leave the transcript row pointing at a copy of the preview while the output it
    promises sits in a file the row never names. It rides on the MCP result's
    ``_meta``, so it survives the wire rather than being re-derived from prose.
    """
    from alkera_cli.plugins.plugin_base.bash_ids import MAX_BYTES
    from alkera_cli.plugins.plugin_base.bash_tool import register_bash_tools
    from alkera_cli.plugins.plugin_base.wire import HOST_SPILL_META_KEY

    registry = _registry(tmp_path)
    register_bash_tools(registry)
    binding = SessionToolBinding(
        registry=registry,
        session_id="s1",
        alkera_dir=str(tmp_path / ".alkera"),
        # The shell gate is exhausted in test_bash_gate; here it just has to pass.
        broker=_FakeBroker("allow_once"),
    )
    server = await _server(binding)
    line = "x" * 79
    lines = (MAX_BYTES * 3) // 80
    written = (len(line) + 1) * lines
    try:
        async with streamablehttp_client(server.url, headers=server.auth_headers()) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()

                big = await sess.call_tool(
                    "call_tool",
                    {
                        "name": "bash",
                        "args": {
                            "command": f"yes '{line}' | head -n {lines}",
                            "description": "dump a lot",
                        },
                    },
                )
                assert big.meta is not None, big.content[0].text[:400]
                pointer = big.meta[HOST_SPILL_META_KEY]["outputPath"]
                # The file the result names holds every byte the command wrote,
                # not the tail the model was handed.
                assert await asyncio.to_thread(_file_size, pointer) == written
                assert len(big.content[0].text.encode()) < written // 2

                # A result that fit makes no such claim, so a backend stays free
                # to bound it.
                small = await sess.call_tool(
                    "call_tool",
                    {"name": "bash", "args": {"command": "echo hi", "description": "say hi"}},
                )
                assert small.meta is None or HOST_SPILL_META_KEY not in small.meta
    finally:
        await server.stop()
